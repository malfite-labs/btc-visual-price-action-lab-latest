"""Strictly causal signal evaluators for Aoying S (S01/S02/COMBO) and AY (AY02/AY03/AY04).

Faithfully reproduces:
- thankucrypto-backtest/src/backtest_engine.py (Wilder ATR/ADX, Daily SMA120 >=200d warmup,
  prior-N exclusive Donchian bands, S01/S02/COMBO arbitration, fill-time SL/TP).
- ay-research/ay_signals.py & run_experiments.py (SMA ATR, Wilder ADX, >=120d daily warmup,
  AY02 compression quantile, AY03 panic reversal, AY04 range reclaim + net-R / gap checks).
"""
from __future__ import annotations

from bisect import bisect_right, insort
from collections import deque
from decimal import Decimal as D
from typing import Any, Dict, List, Optional, Tuple

from strategies.r_series.indicators import aggregate_closed_htf, compute_atr_series
from strategies.thc_ay.specs import ThcAySpec


# ---------------------------------------------------------------------------
# S-series causal Wilder indicators (exact match to backtest_engine.py)
# ---------------------------------------------------------------------------

def compute_wilder_atr(bars: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    n = len(bars)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    trs = [0.0] * n
    trs[0] = bars[0]["h"] - bars[0]["l"]
    for i in range(1, n):
        pc = bars[i - 1]["c"]
        trs[i] = max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - pc), abs(bars[i]["l"] - pc))
    atr = sum(trs[1:period + 1]) / period
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def compute_wilder_adx(bars: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    n = len(bars)
    out: List[Optional[float]] = [None] * n
    if n < 2 * period + 1:
        return out
    tr = [0.0] * n
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        pc = bars[i - 1]["c"]
        tr[i] = max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - pc), abs(bars[i]["l"] - pc))

    atr_s = sum(tr[1:period + 1])
    pdm_s = sum(pdm[1:period + 1])
    mdm_s = sum(mdm[1:period + 1])
    dx_list: List[float] = []

    for i in range(period, n):
        if i > period:
            atr_s = atr_s - (atr_s / period) + tr[i]
            pdm_s = pdm_s - (pdm_s / period) + pdm[i]
            mdm_s = mdm_s - (mdm_s / period) + mdm[i]
        if atr_s <= 0:
            dx = 0.0
        else:
            pdi = 100.0 * pdm_s / atr_s
            mdi = 100.0 * mdm_s / atr_s
            denom = pdi + mdi
            dx = 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0
        dx_list.append(dx)
        if len(dx_list) == period:
            out[i] = sum(dx_list) / period
        elif len(dx_list) > period:
            out[i] = (out[i - 1] * (period - 1) + dx) / period  # type: ignore[operator]
    return out


# ---------------------------------------------------------------------------
# AY-series causal Wilder ADX & quantile (exact match to ay_signals.py)
# ---------------------------------------------------------------------------

def compute_ay_adx(bars: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    total = up_sum = down_sum = adx = 0.0
    result: List[Optional[float]] = [None] * len(bars)
    for i in range(1, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        tr = max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"]))
        up, down = cur["h"] - prev["h"], prev["l"] - cur["l"]
        plus = up if up > max(down, 0.0) else 0.0
        minus = down if down > max(up, 0.0) else 0.0
        if i <= period:
            total, up_sum, down_sum = total + tr, up_sum + plus, down_sum + minus
        else:
            total = total - total / period + tr
            up_sum = up_sum - up_sum / period + plus
            down_sum = down_sum - down_sum / period + minus
        if i < period:
            continue
        denominator = up_sum + down_sum
        dx = 100.0 * abs(up_sum - down_sum) / denominator if total and denominator else 0.0
        if i < 2 * period:
            adx += dx / period
        else:
            adx += (dx - adx) / period
        if i >= 2 * period - 1:
            result[i] = adx
    return result


def ay_quantile(sorted_values: List[float], q: float) -> float:
    idx = (len(sorted_values) - 1) * q
    left = int(idx)
    right = min(left + 1, len(sorted_values) - 1)
    return sorted_values[left] + (sorted_values[right] - sorted_values[left]) * (idx - left)


# ---------------------------------------------------------------------------
# Shared multi-timeframe evaluation context per 15m cycle
# ---------------------------------------------------------------------------

class ThcAyCycleContext:
    """Computes UTC-aligned higher timeframes and daily warmup count once per 15m close.

    Supports O(1) incremental bar extension (`extend_15m`) that is bit-for-bit equivalent to
    full-history Wilder ATR/ADX and AY ATR/ADX recomputation.
    """

    def __init__(self, bars_15m: List[Dict[str, Any]]) -> None:
        self.bars_15m: List[Dict[str, Any]] = list(bars_15m)
        self.bars_by_tf: Dict[str, List[Dict[str, Any]]] = {"15m": self.bars_15m}
        self._daily_bars: Optional[List[Dict[str, Any]]] = None
        self._daily_ends: Optional[List[int]] = None
        self._s_indicators: Dict[str, Tuple[List[Optional[float]], List[Optional[float]]]] = {}
        # Incremental Wilder ADX state per TF: (atr_s, pdm_s, mdm_s, n_computed)
        self._s_adx_state: Dict[str, Tuple[float, float, float, int]] = {}
        self._ay_atr14: Optional[List[Optional[float]]] = None
        self._ay_adx14: Optional[List[Optional[float]]] = None
        # Incremental AY ADX state: (total, up_sum, down_sum, adx, n_computed)
        self._ay_adx_state: Optional[Tuple[float, float, float, float, int]] = None

    def extend_15m(self, new_bars_15m: List[Dict[str, Any]]) -> int:
        """Append strictly newer closed 15m bars in O(k) and incrementally advance cached TFs & indicators."""
        if not new_bars_15m:
            return 0
        last_t = self.bars_15m[-1]["t"] if self.bars_15m else -1
        appended: List[Dict[str, Any]] = []
        for b in new_bars_15m:
            if b["t"] > last_t:
                appended.append(b)
                last_t = b["t"]
        if not appended:
            return 0
        old_n15 = len(self.bars_15m)
        self.bars_15m.extend(appended)

        # Incrementally extend cached HTFs (only aggregate the unclosed/new tail!)
        tf_factors = {"1h": 4, "4h": 16, "1d": 96}
        for tf, factor in tf_factors.items():
            if tf not in self.bars_by_tf:
                continue
            existing_htf = self.bars_by_tf[tf]
            if existing_htf:
                last_htf_close = existing_htf[-1]["close_ms"]
                # Find first 15m bar starting at or after last_htf_close
                start_idx = len(self.bars_15m)
                for idx in range(len(self.bars_15m) - 1, -1, -1):
                    if self.bars_15m[idx]["t"] < last_htf_close:
                        break
                    start_idx = idx
                tail_15m = self.bars_15m[start_idx:]
                new_htf = aggregate_closed_htf(tail_15m, factor)
                if new_htf:
                    old_htf_n = len(existing_htf)
                    existing_htf.extend(new_htf)
                    if tf == "1d" and self._daily_ends is not None:
                        self._daily_ends.extend(b["close_ms"] for b in new_htf)
                    if tf in self._s_indicators:
                        self._extend_s_indicators(tf, old_htf_n)
            else:
                self.bars_by_tf[tf] = aggregate_closed_htf(self.bars_15m, factor)
                if tf == "1d":
                    self._daily_bars = self.bars_by_tf[tf]
                    self._daily_ends = [b["close_ms"] for b in self._daily_bars]
                self._s_indicators.pop(tf, None)
                self._s_adx_state.pop(tf, None)

        # Incrementally extend 15m S indicators if cached
        if "15m" in self._s_indicators:
            self._extend_s_indicators("15m", old_n15)

        # Incrementally extend AY ATR14 if cached
        if self._ay_atr14 is not None:
            self._extend_ay_atr14(old_n15)

        # Incrementally extend AY ADX14 if cached
        if self._ay_adx14 is not None:
            self._extend_ay_adx14(old_n15)

        return len(appended)

    def _extend_s_indicators(self, tf: str, old_n: int, period: int = 14) -> None:
        bars = self.bars_by_tf[tf]
        n = len(bars)
        if n <= old_n:
            return
        if old_n < 2 * period + 1 or tf not in self._s_adx_state:
            self._s_indicators[tf] = self._compute_s_indicators_with_state(tf, bars, period)
            return
        atr_out, adx_out = self._s_indicators[tf]
        atr_s, pdm_s, mdm_s, _ = self._s_adx_state[tf]
        prev_atr = atr_out[old_n - 1]
        prev_adx = adx_out[old_n - 1]
        if prev_atr is None or prev_adx is None:
            self._s_indicators[tf] = self._compute_s_indicators_with_state(tf, bars, period)
            return
        for i in range(old_n, n):
            pc = bars[i - 1]["c"]
            tr = max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - pc), abs(bars[i]["l"] - pc))
            prev_atr = (prev_atr * (period - 1) + tr) / period
            atr_out.append(prev_atr)

            up = bars[i]["h"] - bars[i - 1]["h"]
            dn = bars[i - 1]["l"] - bars[i]["l"]
            pdm = up if (up > dn and up > 0) else 0.0
            mdm = dn if (dn > up and dn > 0) else 0.0
            atr_s = atr_s - (atr_s / period) + tr
            pdm_s = pdm_s - (pdm_s / period) + pdm
            mdm_s = mdm_s - (mdm_s / period) + mdm
            if atr_s <= 0:
                dx = 0.0
            else:
                pdi = 100.0 * pdm_s / atr_s
                mdi = 100.0 * mdm_s / atr_s
                denom = pdi + mdi
                dx = 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0
            prev_adx = (prev_adx * (period - 1) + dx) / period
            adx_out.append(prev_adx)
        self._s_adx_state[tf] = (atr_s, pdm_s, mdm_s, n)

    def _compute_s_indicators_with_state(
        self, tf: str, bars: List[Dict[str, Any]], period: int = 14
    ) -> Tuple[List[Optional[float]], List[Optional[float]]]:
        atr_out = compute_wilder_atr(bars, period)
        n = len(bars)
        adx_out: List[Optional[float]] = [None] * n
        if n < 2 * period + 1:
            return atr_out, adx_out
        tr = [0.0] * n
        pdm = [0.0] * n
        mdm = [0.0] * n
        for i in range(1, n):
            up = bars[i]["h"] - bars[i - 1]["h"]
            dn = bars[i - 1]["l"] - bars[i]["l"]
            pdm[i] = up if (up > dn and up > 0) else 0.0
            mdm[i] = dn if (dn > up and dn > 0) else 0.0
            pc = bars[i - 1]["c"]
            tr[i] = max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - pc), abs(bars[i]["l"] - pc))
        atr_s = sum(tr[1:period + 1])
        pdm_s = sum(pdm[1:period + 1])
        mdm_s = sum(mdm[1:period + 1])
        dx_list: List[float] = []
        for i in range(period, n):
            if i > period:
                atr_s = atr_s - (atr_s / period) + tr[i]
                pdm_s = pdm_s - (pdm_s / period) + pdm[i]
                mdm_s = mdm_s - (mdm_s / period) + mdm[i]
            if atr_s <= 0:
                dx = 0.0
            else:
                pdi = 100.0 * pdm_s / atr_s
                mdi = 100.0 * mdm_s / atr_s
                denom = pdi + mdi
                dx = 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0
            dx_list.append(dx)
            if len(dx_list) == period:
                adx_out[i] = sum(dx_list) / period
            elif len(dx_list) > period:
                adx_out[i] = (adx_out[i - 1] * (period - 1) + dx) / period  # type: ignore[operator]
        self._s_adx_state[tf] = (atr_s, pdm_s, mdm_s, n)
        return atr_out, adx_out

    def _extend_ay_atr14(self, old_n: int, period: int = 14) -> None:
        bars = self.bars_15m
        n = len(bars)
        if n <= old_n or self._ay_atr14 is None:
            return
        if old_n < period + 1:
            self._ay_atr14 = compute_atr_series(bars, period)
            return
        for i in range(old_n, n):
            s = 0.0
            for k in range(i - period + 1, i + 1):
                h, l = bars[k]["h"], bars[k]["l"]
                if k == 0:
                    s += h - l
                else:
                    pc = bars[k - 1]["c"]
                    s += max(h - l, abs(h - pc), abs(l - pc))
            self._ay_atr14.append(s / period)

    def _extend_ay_adx14(self, old_n: int, period: int = 14) -> None:
        bars = self.bars_15m
        n = len(bars)
        if n <= old_n or self._ay_adx14 is None:
            return
        if self._ay_adx_state is None or old_n < 2 * period:
            self._ay_adx14 = self._compute_ay_adx_with_state(bars, period)
            return
        total, up_sum, down_sum, adx, _ = self._ay_adx_state
        for i in range(old_n, n):
            cur, prev = bars[i], bars[i - 1]
            tr = max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"]))
            up, down = cur["h"] - prev["h"], prev["l"] - cur["l"]
            plus = up if up > max(down, 0.0) else 0.0
            minus = down if down > max(up, 0.0) else 0.0
            total = total - total / period + tr
            up_sum = up_sum - up_sum / period + plus
            down_sum = down_sum - down_sum / period + minus
            denominator = up_sum + down_sum
            dx = 100.0 * abs(up_sum - down_sum) / denominator if total and denominator else 0.0
            adx += (dx - adx) / period
            self._ay_adx14.append(adx)
        self._ay_adx_state = (total, up_sum, down_sum, adx, n)

    def _compute_ay_adx_with_state(self, bars: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
        total = up_sum = down_sum = adx = 0.0
        result: List[Optional[float]] = [None] * len(bars)
        for i in range(1, len(bars)):
            cur, prev = bars[i], bars[i - 1]
            tr = max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"]))
            up, down = cur["h"] - prev["h"], prev["l"] - cur["l"]
            plus = up if up > max(down, 0.0) else 0.0
            minus = down if down > max(up, 0.0) else 0.0
            if i <= period:
                total, up_sum, down_sum = total + tr, up_sum + plus, down_sum + minus
            else:
                total = total - total / period + tr
                up_sum = up_sum - up_sum / period + plus
                down_sum = down_sum - down_sum / period + minus
            if i < period:
                continue
            denominator = up_sum + down_sum
            dx = 100.0 * abs(up_sum - down_sum) / denominator if total and denominator else 0.0
            if i < 2 * period:
                adx += dx / period
            else:
                adx += (dx - adx) / period
            if i >= 2 * period - 1:
                result[i] = adx
        self._ay_adx_state = (total, up_sum, down_sum, adx, len(bars))
        return result

    def get_tf_bars(self, tf: str) -> List[Dict[str, Any]]:
        if tf not in self.bars_by_tf:
            factor = {"15m": 1, "1h": 4, "4h": 16, "1d": 96}[tf]
            self.bars_by_tf[tf] = aggregate_closed_htf(self.bars_15m, factor)
        return self.bars_by_tf[tf]

    def daily_bars(self) -> List[Dict[str, Any]]:
        if self._daily_bars is None:
            self._daily_bars = self.get_tf_bars("1d")
            self._daily_ends = [b["close_ms"] for b in self._daily_bars]
        return self._daily_bars

    def closed_daily_count_at(self, close_ms: int) -> int:
        if self._daily_ends is None:
            self.daily_bars()
        return bisect_right(self._daily_ends or [], close_ms)

    def s_indicators(self, tf: str) -> Tuple[List[Optional[float]], List[Optional[float]]]:
        if tf not in self._s_indicators:
            bars = self.get_tf_bars(tf)
            self._s_indicators[tf] = self._compute_s_indicators_with_state(tf, bars, 14)
        return self._s_indicators[tf]

    def ay_atr14(self) -> List[Optional[float]]:
        if self._ay_atr14 is None:
            self._ay_atr14 = compute_atr_series(self.bars_15m, 14)
        return self._ay_atr14

    def ay_adx14(self) -> List[Optional[float]]:
        if self._ay_adx14 is None:
            self._ay_adx14 = self._compute_ay_adx_with_state(self.bars_15m, 14)
        return self._ay_adx14


# ---------------------------------------------------------------------------
# Signal evaluation at latest closed bar
# ---------------------------------------------------------------------------

def evaluate_s_signal(
    spec: ThcAySpec,
    ctx: ThcAyCycleContext,
    latest_15m_close_ms: int,
) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    """Evaluate S01 / S02 / COMBO strictly on native TF close boundary.

    Returns (pending_signal_or_None, status_str, closed_native_cycle_iso_or_None).
    """
    if latest_15m_close_ms % spec.step_ms != 0:
        return None, "WAIT_NATIVE_CLOSE", None

    tf_bars = ctx.get_tf_bars(spec.timeframe)
    if not tf_bars or tf_bars[-1]["close_ms"] != latest_15m_close_ms:
        return None, "WAIT_COMPLETE_NATIVE_BAR", None

    i = len(tf_bars) - 1
    b = tf_bars[i]
    native_close_iso = b["close_at"]

    daily_cnt = ctx.closed_daily_count_at(b["close_ms"])
    if daily_cnt < spec.min_daily_warmup:
        return None, f"WARMUP_DAILY:{daily_cnt}/{spec.min_daily_warmup}", native_close_iso

    atr14, adx14 = ctx.s_indicators(spec.timeframe)
    adx_val = adx14[i]
    atr_val = atr14[i]
    if adx_val is None:
        return None, "WARMUP_ADX", native_close_iso

    p = spec.parameters
    regime = "RANGE" if adx_val < float(p.get("adx_thresh", 25.0)) else "TREND"

    def _eval_s01() -> Tuple[Optional[str], float, float]:
        if regime != "RANGE":
            return None, 0.0, 0.0
        n_win = int(p.get("range_n", 48))
        if i < n_win:
            return None, 0.0, 0.0
        upper = max(tf_bars[k]["h"] for k in range(i - n_win, i))
        lower = min(tf_bars[k]["l"] for k in range(i - n_win, i))
        if upper <= lower:
            return None, upper, lower
        c = b["c"]
        if c >= upper:
            return "SHORT", upper, lower
        if c <= lower:
            return "LONG", upper, lower
        return None, upper, lower

    def _eval_s02() -> Tuple[Optional[str], float, float]:
        n_win = int(p.get("breakout_n", 20))
        if i < n_win or atr_val is None or atr_val <= 0:
            return None, 0.0, 0.0
        prev_hh = max(tf_bars[k]["h"] for k in range(i - n_win, i))
        prev_ll = min(tf_bars[k]["l"] for k in range(i - n_win, i))
        c = b["c"]
        if c > prev_hh:
            return "LONG", prev_hh, prev_ll
        if c < prev_ll:
            return "SHORT", prev_hh, prev_ll
        return None, prev_hh, prev_ll

    side: Optional[str] = None
    sub_strat: Optional[str] = None
    upper_b = lower_b = 0.0

    if spec.family == "S01":
        side, upper_b, lower_b = _eval_s01()
        sub_strat = "S01" if side else None
    elif spec.family == "S02":
        if regime == "TREND" or p.get("s02_ignore_regime", False):
            side, upper_b, lower_b = _eval_s02()
            sub_strat = "S02" if side else None
    elif spec.family == "COMBO":
        if regime == "RANGE":
            side, upper_b, lower_b = _eval_s01()
            sub_strat = "S01" if side else None
        elif regime == "TREND":
            side, upper_b, lower_b = _eval_s02()
            sub_strat = "S02" if side else None

    if side is None or sub_strat is None:
        return None, "WAIT", native_close_iso

    sig_id = f"{spec.strategy_id}:{spec.version}:{native_close_iso}:{side}"
    pending = {
        "signal_id": sig_id,
        "signal_cycle": native_close_iso,
        "signal_close_ms": b["close_ms"],
        "direction": side,
        "sub_strategy": sub_strat,
        "signal_source": f"{spec.source_experiment}:{sub_strat}:{regime}",
        "regime": regime,
        "signal_bar_high": b["h"],
        "signal_bar_low": b["l"],
        "signal_bar_close": b["c"],
        "atr_at_signal": float(atr_val or 0.0),
        "upper_band": upper_b,
        "lower_band": lower_b,
    }
    return pending, f"SIGNAL_{sub_strat}_{side}", native_close_iso


def compute_s_sl_tp(
    sub_strategy: str,
    direction: str,
    entry_price: float,
    sig: Dict[str, Any],
    params: Dict[str, Any],
) -> Tuple[float, float]:
    """Exact fill-time SL/TP matching thankucrypto-backtest/src/backtest_engine.py."""
    atr = float(sig.get("atr_at_signal") or 0.0)
    if atr <= 0:
        atr = entry_price * 0.01
    if sub_strategy == "S01":
        pct = float(params.get("stop_pct", 0.10))
        sl = entry_price * (1.0 - pct) if direction == "LONG" else entry_price * (1.0 + pct)
        upper_band = float(sig.get("upper_band") or entry_price)
        lower_band = float(sig.get("lower_band") or entry_price)
        if direction == "LONG":
            tp = max(upper_band, entry_price * 1.005)
        else:
            tp = min(lower_band, entry_price * 0.995)
        return sl, tp

    # S02
    buf_atr = float(params.get("s02_stop_buffer_atr", 0.2))
    if direction == "LONG":
        sl = min(float(sig["signal_bar_low"]) - buf_atr * atr, entry_price * 0.995)
    else:
        sl = max(float(sig["signal_bar_high"]) + buf_atr * atr, entry_price * 1.005)
    tp_k = float(params.get("s02_tp_atr_k", 3.0))
    tp = entry_price + tp_k * atr if direction == "LONG" else entry_price - tp_k * atr
    return sl, tp


def evaluate_ay_signal(
    spec: ThcAySpec,
    ctx: ThcAyCycleContext,
) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    """Evaluate AY02 / AY03 / AY04 at the latest closed 15m bar."""
    bars = ctx.bars_15m
    if not bars:
        return None, "EMPTY_BARS", None
    i = len(bars) - 1
    b = bars[i]
    close_iso = b["close_at"]
    p = spec.parameters

    daily = ctx.daily_bars()
    n_daily = ctx.closed_daily_count_at(b["close_ms"])
    if n_daily < spec.min_daily_warmup:
        return None, f"WARMUP_DAILY:{n_daily}/{spec.min_daily_warmup}", close_iso

    atr = ctx.ay_atr14()
    if spec.family == "AY02":
        ch_n = int(p["channel_n"])
        lookback = int(p["compression_lookback"])
        sma_n = int(p["daily_sma"])
        if i < ch_n + lookback or not atr[i] or n_daily < max(120, sma_n):
            return None, "WARMUP_AY02", close_iso
        # Compute prior `lookback` widths immediately preceding bar i (exact match to ay_signals.py)
        start_w = i - lookback
        sorted_w: List[float] = []
        for k in range(start_w, i):
            u_k = max(bars[m]["h"] for m in range(k - ch_n, k))
            l_k = min(bars[m]["l"] for m in range(k - ch_n, k))
            w_k = (u_k - l_k) / bars[k - 1]["c"]
            insort(sorted_w, w_k)
        upper = max(bars[m]["h"] for m in range(i - ch_n, i))
        lower = min(bars[m]["l"] for m in range(i - ch_n, i))
        width = (upper - lower) / bars[i - 1]["c"]
        if width > ay_quantile(sorted_w, float(p["compression_quantile"])):
            return None, "WAIT", close_iso
        daily_closes = [d["c"] for d in daily[:n_daily]]
        d_sma = sum(daily_closes[-sma_n:]) / sma_n
        d_last = daily_closes[-1]
        dist = float(p["sl_atr"]) * float(atr[i])
        direction = None
        stop_px = 0.0
        if d_last > d_sma and b["c"] > upper:
            direction = "LONG"
            stop_px = b["c"] - dist
        elif d_last < d_sma and b["c"] < lower:
            direction = "SHORT"
            stop_px = b["c"] + dist
        if not direction:
            return None, "WAIT", close_iso
        sig_id = f"{spec.strategy_id}:{spec.version}:{close_iso}:{direction}"
        return {
            "signal_id": sig_id,
            "signal_cycle": close_iso,
            "signal_close_ms": b["close_ms"],
            "direction": direction,
            "sub_strategy": "AY02",
            "signal_source": "AY02:BASELINE",
            "stop": str(stop_px),
            "target": None,
            "target_r": float(p["tp_r"]),
        }, f"SIGNAL_AY02_{direction}", close_iso

    if spec.family == "AY03":
        lb = int(p["lookback"])
        if i < lb or not atr[i - lb] or not atr[i] or b["h"] == b["l"]:
            return None, "WAIT", close_iso
        prev = bars[i - 1]
        if (
            bars[i - lb]["c"] - b["c"] > float(p["shock_atr"]) * float(atr[i - lb])
            and min(b["o"], b["c"]) - b["l"] >= float(p["wick_ratio"]) * (b["h"] - b["l"])
            and b["c"] > prev["h"]
        ):
            floor = min(bars[k]["l"] for k in range(i - lb + 1, i + 1))
            stop_px = floor - float(p["sl_buffer_atr"]) * float(atr[i])
            sig_id = f"{spec.strategy_id}:{spec.version}:{close_iso}:LONG"
            return {
                "signal_id": sig_id,
                "signal_cycle": close_iso,
                "signal_close_ms": b["close_ms"],
                "direction": "LONG",
                "sub_strategy": "AY03",
                "signal_source": "AY03:BASELINE",
                "stop": str(stop_px),
                "target": None,
                "target_r": float(p["tp_r"]),
            }, "SIGNAL_AY03_LONG", close_iso
        return None, "WAIT", close_iso

    # AY04
    ch_n = int(p["channel_n"])
    adx = ctx.ay_adx14()
    if i < ch_n or adx[i] is None or not atr[i] or float(adx[i]) >= float(p["adx_max"]):
        return None, "WAIT", close_iso
    upper = max(bars[k]["h"] for k in range(i - ch_n, i))
    lower = min(bars[k]["l"] for k in range(i - ch_n, i))
    mid = (upper + lower) / 2.0
    pad = float(p["sl_buffer_atr"]) * float(atr[i])
    direction = None
    stop_px = 0.0
    if b["l"] <= lower and lower < b["c"] < mid:
        direction = "LONG"
        stop_px = lower - pad
    elif b["h"] >= upper and mid < b["c"] < upper:
        direction = "SHORT"
        stop_px = upper + pad
    if not direction:
        return None, "WAIT", close_iso
    sig_id = f"{spec.strategy_id}:{spec.version}:{close_iso}:{direction}"
    return {
        "signal_id": sig_id,
        "signal_cycle": close_iso,
        "signal_close_ms": b["close_ms"],
        "direction": direction,
        "sub_strategy": "AY04",
        "signal_source": "AY04:BASELINE",
        "stop": str(stop_px),
        "target": str(mid),
        "target_r": None,
    }, f"SIGNAL_AY04_{direction}", close_iso


def validate_ay_entry(
    direction: str,
    stop: D,
    explicit_target: Optional[D],
    target_r: Optional[float],
    reference: D,
    entry_px: D,
    fee_rate: D,
    slip_rate: D,
) -> Tuple[Optional[D], Optional[str]]:
    """Exact fill-time validation matching ay-research/run_experiments.py::opened_position."""
    side = D(1) if direction == "LONG" else D(-1)
    if stop <= 0 or min((entry_px - stop) * side, (reference - stop) * side) <= 0:
        return None, "GAP_BEYOND_STOP"
    if explicit_target is not None:
        target = explicit_target
    else:
        tr = D(str(target_r if target_r is not None else 2.0))
        target = entry_px + abs(entry_px - stop) * tr * side
    target_fill = target * (D(1) - slip_rate * side)
    stop_fill = stop * (D(1) - slip_rate * side)
    target_net = (target_fill - entry_px) * side - (entry_px + target_fill) * fee_rate
    risk_net = (entry_px - stop_fill) * side + (entry_px + stop_fill) * fee_rate
    if explicit_target is not None and min((target - entry_px) * side, (target - reference) * side) <= 0:
        return None, "GAP_BEYOND_TARGET"
    if target <= 0 or target_net <= 0:
        return None, "TARGET_CANNOT_COVER_COSTS"
    if explicit_target is not None and target_net < risk_net:
        return None, "NET_R_BELOW_ONE"
    return target, None
