"""Frozen R01-R06 V1.1.0 Strategy Specifications & Causal Signal Engine.

Preserves V1.0 strategies.py untouched. Implements the exact 6 frozen V1.1.0
paper-trading observation configurations derived verbatim from P10/P11/P12:
  - R01 v1.1.0: P10_atr2.0_R2.0_H64
  - R02 v1.1.0: C7_R02_PlanD_vol12_wide
  - R03 v1.1.0: P10_atr2.0_R3.0_H64
  - R04 v1.1.0: C6_R04_PlanCD_combo  [PRIMARY_FORWARD_CANDIDATE]
  - R05 v1.1.0: C1_R05_PlanC_align_base
  - R06 v1.1.0: C8_R06_PlanE_rel_wide
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from strategies.r_series.indicators import (
    aggregate_closed_htf,
    classify_4h_trend_series,
    compute_atr_series,
    compute_ema_series,
)
from strategies.r_series.strategies import R_SERIES_IDS, is_bearish_confirm, is_bullish_confirm
from strategies.r_series.td_core import TdBar, compute as compute_td


@dataclass(frozen=True)
class RStrategyV11Spec:
    strategy_id: str
    name: str
    version: str
    candidate_ref: str
    observation_tier: str  # 'PRIMARY_FORWARD_CANDIDATE' or 'EXPERIMENTAL_OBSERVE'
    group: str
    signal_timeframe: str
    exec_timeframe: str
    background_timeframe: Optional[str]
    confirm_window: int
    tdst_filter: str       # 'none' or 'align_level'
    vol_mult: float        # 0.0 or 1.2
    trend_mode: str        # 'none' or 'relaxed_ema60'
    atr_period: int
    stop_atr_mult: float
    target_r: float
    max_hold_bars: int
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)


R_V11_SPECS: Dict[str, RStrategyV11Spec] = {
    "R01": RStrategyV11Spec(
        strategy_id="R01",
        name="R01 · TD9 基础反转策略 (V1.1)",
        version="1.1.0",
        candidate_ref="P10_atr_2.0_R2.0_H64",
        observation_tier="EXPERIMENTAL_OBSERVE",
        group="R系列",
        signal_timeframe="15m",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=0,
        tdst_filter="none",
        vol_mult=0.0,
        trend_mode="none",
        atr_period=14,
        stop_atr_mult=2.0,
        target_r=2.0,
        max_hold_bars=64,
        description="15m TD9 基础反转对照组（V1.1 冻结参数）：buy/sell_setup==9 次根开盘入场，2.0 ATR 止损缓冲，2.0R 止盈，最长持仓 64 根 15m K 线。",
        parameters={"candidate_ref": "P10_atr_2.0_R2.0_H64", "signal_tf": "15m", "confirm_window": 0, "tdst_filter": "none", "vol_mult": 0.0, "atr_period": 14, "stop_atr_mult": 2.0, "target_r": 2.0, "max_hold_bars": 64},
    ),
    "R02": RStrategyV11Spec(
        strategy_id="R02",
        name="R02 · 完善型 TD9 放量确认策略 (V1.1)",
        version="1.1.0",
        candidate_ref="C7_R02_PlanD_vol12_wide",
        observation_tier="EXPERIMENTAL_OBSERVE",
        group="R系列",
        signal_timeframe="15m",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=4,
        tdst_filter="none",
        vol_mult=1.2,
        trend_mode="none",
        atr_period=14,
        stop_atr_mult=1.5,
        target_r=2.0,
        max_hold_bars=64,
        description="15m 完善型 TD9 候选 + 4 根 15m 反转确认 + 确认K线因果成交量 >= 1.2×SMA20(vol) + 1.5 ATR 止损缓冲 / 2.0R 止盈 / 64 根持仓上限。",
        parameters={"candidate_ref": "C7_R02_PlanD_vol12_wide", "signal_tf": "15m", "require_perfected": True, "confirm_window": 4, "tdst_filter": "none", "vol_mult": 1.2, "atr_period": 14, "stop_atr_mult": 1.5, "target_r": 2.0, "max_hold_bars": 64},
    ),
    "R03": RStrategyV11Spec(
        strategy_id="R03",
        name="R03 · TD13 衰竭反转策略 (V1.1)",
        version="1.1.0",
        candidate_ref="P10_atr_2.0_R3.0_H64",
        observation_tier="EXPERIMENTAL_OBSERVE",
        group="R系列",
        signal_timeframe="15m",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=4,
        tdst_filter="none",
        vol_mult=0.0,
        trend_mode="none",
        atr_period=14,
        stop_atr_mult=2.0,
        target_r=3.0,
        max_hold_bars=64,
        description="15m 正式完成 TD Countdown 13（严格 ==13）候选 + 4 根 15m 反转确认 + 2.0 ATR 止损缓冲 / 3.0R 止盈 / 64 根持仓上限。",
        parameters={"candidate_ref": "P10_atr_2.0_R3.0_H64", "signal_tf": "15m", "require_exact_td13": True, "confirm_window": 4, "tdst_filter": "none", "vol_mult": 0.0, "atr_period": 14, "stop_atr_mult": 2.0, "target_r": 3.0, "max_hold_bars": 64},
    ),
    "R04": RStrategyV11Spec(
        strategy_id="R04",
        name="R04 · 双周期 TD9 结构放量策略 (V1.1 重点候选)",
        version="1.1.0",
        candidate_ref="C6_R04_PlanCD_combo",
        observation_tier="PRIMARY_FORWARD_CANDIDATE",
        group="R系列",
        signal_timeframe="1h",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=16,
        tdst_filter="align_level",
        vol_mult=1.2,
        trend_mode="none",
        atr_period=14,
        stop_atr_mult=1.5,
        target_r=2.0,
        max_hold_bars=64,
        description="【重点前瞻研究候选】1h 已收盘 TD9 候选 + 16 根 15m 反转确认 + 活跃 TDST 同向结构背书（多单需 support < close，空单需 resistance > close）+ 确认成交量 >= 1.2×SMA20 + 1.5 ATR 止损缓冲 / 2.0R 止盈 / 64 根持仓上限。",
        parameters={"candidate_ref": "C6_R04_PlanCD_combo", "signal_tf": "1h", "exec_tf": "15m", "confirm_window": 16, "tdst_filter": "align_level", "vol_mult": 1.2, "atr_period": 14, "stop_atr_mult": 1.5, "target_r": 2.0, "max_hold_bars": 64},
    ),
    "R05": RStrategyV11Spec(
        strategy_id="R05",
        name="R05 · 双周期 TD13 结构背书策略 (V1.1)",
        version="1.1.0",
        candidate_ref="C1_R05_PlanC_align_base",
        observation_tier="EXPERIMENTAL_OBSERVE",
        group="R系列",
        signal_timeframe="1h",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=16,
        tdst_filter="align_level",
        vol_mult=0.0,
        trend_mode="none",
        atr_period=14,
        stop_atr_mult=0.5,
        target_r=2.0,
        max_hold_bars=32,
        description="1h 已收盘正式完成 TD13 候选 + 16 根 15m 反转确认 + 活跃 TDST 同向结构背书 + 0.5 ATR 止损缓冲 / 2.0R 止盈 / 32 根持仓上限。",
        parameters={"candidate_ref": "C1_R05_PlanC_align_base", "signal_tf": "1h", "exec_tf": "15m", "require_exact_td13": True, "confirm_window": 16, "tdst_filter": "align_level", "vol_mult": 0.0, "atr_period": 14, "stop_atr_mult": 0.5, "target_r": 2.0, "max_hold_bars": 32},
    ),
    "R06": RStrategyV11Spec(
        strategy_id="R06",
        name="R06 · 多周期顺势衰竭放量策略 (V1.1)",
        version="1.1.0",
        candidate_ref="C8_R06_PlanE_rel_wide",
        observation_tier="EXPERIMENTAL_OBSERVE",
        group="R系列",
        signal_timeframe="1h",
        exec_timeframe="15m",
        background_timeframe="4h",
        confirm_window=16,
        tdst_filter="none",
        vol_mult=1.2,
        trend_mode="relaxed_ema60",
        atr_period=14,
        stop_atr_mult=1.5,
        target_r=2.0,
        max_hold_bars=64,
        description="4h 放松趋势识别（EMA20>EMA60 且 close>EMA60 且 |EMA20-EMA60|/close>=0.1%，空头镜像）+ 1h 顺势 TD9/TD13 + 16 根 15m 反转确认（复查 4h 趋势 + 确认量能 >= 1.2×SMA20）+ 1.5 ATR 止损缓冲 / 2.0R 止盈 / 64 根持仓上限。",
        parameters={"candidate_ref": "C8_R06_PlanE_rel_wide", "bg_tf": "4h", "signal_tf": "1h", "exec_tf": "15m", "trend_mode": "relaxed_ema60", "ema_fast": 20, "ema_slow": 60, "min_spread_ratio": 0.001, "confirm_window": 16, "tdst_filter": "none", "vol_mult": 1.2, "atr_period": 14, "stop_atr_mult": 1.5, "target_r": 2.0, "max_hold_bars": 64},
    ),
}


def compute_vol_sma20(bars: List[Dict[str, Any]]) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(bars)
    run = 0.0
    for i, b in enumerate(bars):
        run += b["v"]
        if i >= 20:
            run -= bars[i - 20]["v"]
        if i >= 19:
            out[i] = run / 20.0
    return out


def compute_relaxed_4h_trend(bars_4h: List[Dict[str, Any]], *, min_spread: float = 0.001) -> List[str]:
    closes = [b["c"] for b in bars_4h]
    ef = compute_ema_series(closes, 20)
    es = compute_ema_series(closes, 60)
    out: List[str] = []
    for i, c in enumerate(closes):
        if ef[i] is None or es[i] is None or c <= 0:
            out.append("RANGE")
            continue
        sp = abs(ef[i] - es[i]) / c
        if ef[i] > es[i] and c > es[i] and sp >= min_spread:
            out.append("UPTREND")
        elif ef[i] < es[i] and c < es[i] and sp >= min_spread:
            out.append("DOWNTREND")
        else:
            out.append("RANGE")
    return out


class MultiTFContextV11:
    """Strictly causal fixed-origin multi-timeframe context for R01-R06 V1.1.0."""

    def __init__(self, bars_15m: List[Dict[str, Any]]):
        self.bars_15m = bars_15m
        self.td_15m: List[TdBar] = compute_td(bars_15m)
        self.atr_15m: List[Optional[float]] = compute_atr_series(bars_15m, period=14)
        self.vol_sma20: List[Optional[float]] = compute_vol_sma20(bars_15m)

        self.bars_1h = aggregate_closed_htf(bars_15m, factor=4)
        self.td_1h: List[TdBar] = compute_td(self.bars_1h)
        self.h1_by_close_ms: Dict[int, int] = {b["close_ms"]: idx for idx, b in enumerate(self.bars_1h)}

        self.bars_4h = aggregate_closed_htf(bars_15m, factor=16)
        self.regime_4h_relaxed: List[str] = compute_relaxed_4h_trend(self.bars_4h, min_spread=0.001)
        self.h4_close_ms: List[int] = [b["close_ms"] for b in self.bars_4h]

        self.latest_4h_idx: List[Optional[int]] = []
        p4 = -1
        n4 = len(self.bars_4h)
        for b in bars_15m:
            cms = b["close_ms"]
            while p4 + 1 < n4 and self.h4_close_ms[p4 + 1] <= cms:
                p4 += 1
            self.latest_4h_idx.append(p4 if p4 >= 0 else None)


class RSignalEngineV11:
    """Exact frozen V1.1.0 causal signal & candidate engine for R01-R06."""

    def __init__(self, strategy_id: str):
        if strategy_id not in R_V11_SPECS:
            raise ValueError(f"UNKNOWN_R_V11_STRATEGY:{strategy_id}")
        self.sid = strategy_id
        self.spec = R_V11_SPECS[strategy_id]
        self.candidate: Optional[Dict[str, Any]] = None
        self.consumed_signal_ids: set[str] = set()
        self.last_h1_close_ms: Optional[int] = None
        self.last_rejection_reason: Optional[str] = None

    def export_state(self) -> Dict[str, Any]:
        return {
            "sid": self.sid,
            "version": self.spec.version,
            "candidate": self.candidate,
            "consumed_signal_ids": sorted(self.consumed_signal_ids)[-300:],
            "last_h1_close_ms": self.last_h1_close_ms,
            "last_rejection_reason": self.last_rejection_reason,
        }

    def load_state(self, state: Optional[Dict[str, Any]]) -> None:
        if not state:
            return
        self.candidate = state.get("candidate")
        self.consumed_signal_ids = set(state.get("consumed_signal_ids") or ())
        self.last_h1_close_ms = state.get("last_h1_close_ms")
        self.last_rejection_reason = state.get("last_rejection_reason")

    def reset_transient(self) -> None:
        self.candidate = None

    def step(
        self,
        ctx: MultiTFContextV11,
        idx_15m: int,
        *,
        has_open_position: bool = False,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        bar = ctx.bars_15m[idx_15m]
        prev_bar = ctx.bars_15m[idx_15m - 1] if idx_15m >= 1 else None
        td15 = ctx.td_15m[idx_15m]
        atr14 = ctx.atr_15m[idx_15m]
        vs20 = ctx.vol_sma20[idx_15m]

        h1_idx = ctx.h1_by_close_ms.get(bar["close_ms"])
        new_h1 = h1_idx is not None and (self.last_h1_close_ms is None or bar["close_ms"] > self.last_h1_close_ms)
        if new_h1:
            self.last_h1_close_ms = bar["close_ms"]
        h1_bar = ctx.bars_1h[h1_idx] if new_h1 and h1_idx is not None else None
        h1_td = ctx.td_1h[h1_idx] if new_h1 and h1_idx is not None else None

        h4_idx = ctx.latest_4h_idx[idx_15m]
        reg4 = ctx.regime_4h_relaxed[h4_idx] if h4_idx is not None else "RANGE"

        # 1. Opposite TD9 cancellation on signal timeframe
        if self.candidate is not None:
            cd = self.candidate["direction"]
            opp = False
            if self.spec.signal_timeframe == "15m":
                opp = (td15.sell_setup == 9) if cd == "LONG" else (td15.buy_setup == 9)
            elif new_h1 and h1_td is not None:
                opp = (h1_td.sell_setup == 9) if cd == "LONG" else (h1_td.buy_setup == 9)
            if opp:
                self.candidate = None
                self.last_rejection_reason = "CANCELED_OPPOSITE_TD9"

        # 2. Candidate confirmation / expiry (on bars AFTER signal bar)
        if self.candidate is not None and idx_15m > self.candidate["signal_15m_idx"]:
            elapsed = idx_15m - self.candidate["signal_15m_idx"]
            cd = self.candidate["direction"]
            if cd == "LONG":
                self.candidate["extreme"] = min(self.candidate["extreme"], bar["l"])
            else:
                self.candidate["extreme"] = max(self.candidate["extreme"], bar["h"])

            if elapsed > self.spec.confirm_window:
                self.candidate = None
                self.last_rejection_reason = "CANDIDATE_EXPIRED"
            else:
                trend_ok = True
                if self.sid == "R06":
                    trend_ok = (reg4 == ("UPTREND" if cd == "LONG" else "DOWNTREND"))
                if not trend_ok:
                    self.candidate = None
                    self.last_rejection_reason = "TREND_4H_LOST"
                elif prev_bar is not None:
                    brk = is_bullish_confirm(prev_bar, bar) if cd == "LONG" else is_bearish_confirm(prev_bar, bar)
                    if brk:
                        # Plan D: Causal Volume check
                        vol_ok = True
                        if self.spec.vol_mult > 0.0:
                            if vs20 is None or vs20 <= 0 or bar["v"] < self.spec.vol_mult * vs20:
                                vol_ok = False
                                self.last_rejection_reason = "VOLUME_BELOW_THRESHOLD"
                        if vol_ok:
                            # Plan C: Strict upstream TDST structural alignment
                            tdst_ok = True
                            if self.spec.tdst_filter == "align_level":
                                if cd == "LONG" and (td15.support is None or td15.support >= bar["c"]):
                                    tdst_ok = False
                                    self.last_rejection_reason = "TDST_SUPPORT_MISSING"
                                elif cd == "SHORT" and (td15.resistance is None or td15.resistance <= bar["c"]):
                                    tdst_ok = False
                                    self.last_rejection_reason = "TDST_RESISTANCE_MISSING"
                            if tdst_ok and atr14 and atr14 > 0:
                                cand = self.candidate
                                self.candidate = None
                                if cand["signal_id"] in self.consumed_signal_ids:
                                    self.last_rejection_reason = "DUPLICATE_SIGNAL"
                                    return None, "DUPLICATE_SIGNAL"
                                self.consumed_signal_ids.add(cand["signal_id"])
                                if has_open_position:
                                    self.last_rejection_reason = "HOLD_SKIP_CONFIRMED"
                                    return None, "HOLD_SKIP_CONFIRMED"
                                stop_v = (
                                    cand["extreme"] - self.spec.stop_atr_mult * atr14
                                    if cd == "LONG"
                                    else cand["extreme"] + self.spec.stop_atr_mult * atr14
                                )
                                self.last_rejection_reason = None
                                return {
                                    "strategy_id": self.sid,
                                    "version": self.spec.version,
                                    "candidate_ref": self.spec.candidate_ref,
                                    "signal_id": cand["signal_id"],
                                    "direction": cd,
                                    "signal_source": cand["signal_source"],
                                    "signal_bar_close_at": cand["signal_close_at"],
                                    "confirm_bar_close_at": bar["close_at"],
                                    "signal_cycle": bar["close_at"],
                                    "stop": str(Decimal(str(round(stop_v, 2)))),
                                    "atr14": round(atr14, 4),
                                    "regime_4h": reg4,
                                }, f"CONFIRMED_{cd}"

        # 3. Detect new exhaustion candidate event on this bar
        ev = self._detect_event(idx_15m, bar, td15, new_h1, h1_bar, h1_td, reg4)
        if ev is None:
            return None, "WAIT"
        if ev["signal_id"] in self.consumed_signal_ids:
            self.last_rejection_reason = "DUPLICATE_SIGNAL"
            return None, "DUPLICATE_SIGNAL"

        # R01 confirm_window == 0 -> immediate next-open signal
        if self.spec.confirm_window == 0:
            self.consumed_signal_ids.add(ev["signal_id"])
            if has_open_position:
                self.last_rejection_reason = "HOLD_SKIP_SIGNAL"
                return None, "HOLD_SKIP_SIGNAL"
            if not atr14 or atr14 <= 0:
                self.last_rejection_reason = "INVALID_ATR"
                return None, "INVALID_ATR"
            cd = ev["direction"]
            stop_v = ev["extreme"] - self.spec.stop_atr_mult * atr14 if cd == "LONG" else ev["extreme"] + self.spec.stop_atr_mult * atr14
            self.last_rejection_reason = None
            return {
                "strategy_id": self.sid,
                "version": self.spec.version,
                "candidate_ref": self.spec.candidate_ref,
                "signal_id": ev["signal_id"],
                "direction": cd,
                "signal_source": ev["signal_source"],
                "signal_bar_close_at": ev["signal_close_at"],
                "confirm_bar_close_at": bar["close_at"],
                "signal_cycle": bar["close_at"],
                "stop": str(Decimal(str(round(stop_v, 2)))),
                "atr14": round(atr14, 4),
                "regime_4h": reg4,
            }, f"SIGNAL_{cd}"

        # R02-R06: store candidate (same-direction overwrites unconsumed old candidate)
        self.candidate = ev
        return None, f"CANDIDATE_{ev['direction']}"

    def _detect_event(self, idx_15m, bar, td15, new_h1, h1_bar, h1_td, reg4):
        if self.sid == "R01":
            if td15.buy_setup == 9:
                return self._mk("LONG", "TD9", "15m", idx_15m, bar["close_at"], bar["l"])
            if td15.sell_setup == 9:
                return self._mk("SHORT", "TD9", "15m", idx_15m, bar["close_at"], bar["h"])
        elif self.sid == "R02":
            if td15.buy_setup == 9 and td15.buy_perfected:
                return self._mk("LONG", "TD9_PERFECTED", "15m", idx_15m, bar["close_at"], bar["l"])
            if td15.sell_setup == 9 and td15.sell_perfected:
                return self._mk("SHORT", "TD9_PERFECTED", "15m", idx_15m, bar["close_at"], bar["h"])
        elif self.sid == "R03":
            if td15.buy_countdown == 13:
                return self._mk("LONG", "TD13", "15m", idx_15m, bar["close_at"], bar["l"])
            if td15.sell_countdown == 13:
                return self._mk("SHORT", "TD13", "15m", idx_15m, bar["close_at"], bar["h"])
        elif self.sid == "R04" and new_h1 and h1_bar and h1_td:
            if h1_td.buy_setup == 9:
                return self._mk("LONG", "TD9", "1h", idx_15m, h1_bar["close_at"], h1_bar["l"])
            if h1_td.sell_setup == 9:
                return self._mk("SHORT", "TD9", "1h", idx_15m, h1_bar["close_at"], h1_bar["h"])
        elif self.sid == "R05" and new_h1 and h1_bar and h1_td:
            if h1_td.buy_countdown == 13:
                return self._mk("LONG", "TD13", "1h", idx_15m, h1_bar["close_at"], h1_bar["l"])
            if h1_td.sell_countdown == 13:
                return self._mk("SHORT", "TD13", "1h", idx_15m, h1_bar["close_at"], h1_bar["h"])
        elif self.sid == "R06" and new_h1 and h1_bar and h1_td:
            if reg4 == "UPTREND":
                if h1_td.buy_countdown == 13:
                    return self._mk("LONG", "TD13", "1h", idx_15m, h1_bar["close_at"], h1_bar["l"])
                if h1_td.buy_setup == 9:
                    return self._mk("LONG", "TD9", "1h", idx_15m, h1_bar["close_at"], h1_bar["l"])
            elif reg4 == "DOWNTREND":
                if h1_td.sell_countdown == 13:
                    return self._mk("SHORT", "TD13", "1h", idx_15m, h1_bar["close_at"], h1_bar["h"])
                if h1_td.sell_setup == 9:
                    return self._mk("SHORT", "TD9", "1h", idx_15m, h1_bar["close_at"], h1_bar["h"])
            else:
                if h1_td.buy_setup == 9 or h1_td.sell_setup == 9 or h1_td.buy_countdown == 13 or h1_td.sell_countdown == 13:
                    self.last_rejection_reason = "REGIME_4H_RANGE"
        return None

    def _mk(self, direction, src, tf, idx_15m, close_at, extreme):
        return {
            "signal_id": f"{self.sid}:{tf}:{direction}:{src}:{close_at}",
            "direction": direction,
            "signal_source": src,
            "signal_15m_idx": idx_15m,
            "signal_close_at": close_at,
            "extreme": extreme,
        }
