"""R01-R06 independent TD Sequential strategy definitions & signal state machine.

Each strategy (R01..R06) has its own strategy_id, version ('1.0.0'), parameters,
candidate lifecycle, signal deduplication, and initial risk stop calculation.
Strictly consumes closed bars only; multi-timeframe data is aligned strictly by
closed bar timestamps (close_ms <= current_15m_close_ms).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from strategies.r_series.indicators import (
    aggregate_closed_htf,
    classify_4h_trend_series,
    compute_atr_series,
)
from strategies.r_series.td_core import TdBar, compute as compute_td

R_SERIES_IDS = ("R01", "R02", "R03", "R04", "R05", "R06")


@dataclass(frozen=True)
class RStrategySpec:
    strategy_id: str
    name: str
    version: str
    group: str
    signal_timeframe: str      # '15m' or '1h'
    exec_timeframe: str        # '15m'
    background_timeframe: Optional[str]  # None or '4h'
    confirm_window: int        # 0 for R01, 4 for R02/R03, 16 for R04/R05/R06
    atr_period: int = 14
    stop_atr_mult: float = 0.5
    target_r: float = 2.0
    max_hold_bars: int = 32
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)


R_SPECS: Dict[str, RStrategySpec] = {
    "R01": RStrategySpec(
        strategy_id="R01",
        name="R01 · TD9 基础反转策略",
        version="1.0.0",
        group="R系列",
        signal_timeframe="15m",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=0,
        description="15m 主周期基础对照组：最新已收盘 15m K 线 buy_setup==9 做多、sell_setup==9 做空，无趋势/完善型/反转确认过滤，次根 15m 开盘入场。",
        parameters={"signal_tf": "15m", "confirm_window": 0, "atr_period": 14, "stop_atr_mult": 0.5, "target_r": 2.0, "max_hold_bars": 32},
    ),
    "R02": RStrategySpec(
        strategy_id="R02",
        name="R02 · 完善型 TD9 确认策略",
        version="1.0.0",
        group="R系列",
        signal_timeframe="15m",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=4,
        description="15m 完善型 TD9（buy/sell_perfected）候选 + 最多 4 根 15m K 线价格反转确认（看涨收盘破前高且收阳；看跌收盘破前低且收阴）。",
        parameters={"signal_tf": "15m", "require_perfected": True, "confirm_window": 4, "atr_period": 14, "stop_atr_mult": 0.5, "target_r": 2.0, "max_hold_bars": 32},
    ),
    "R03": RStrategySpec(
        strategy_id="R03",
        name="R03 · TD13 衰竭反转策略",
        version="1.0.0",
        group="R系列",
        signal_timeframe="15m",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=4,
        description="15m 正式完成 TD Countdown 13（严格 ==13，排除 deferred/-12/>=14）候选 + 最多 4 根 15m K 线反转确认。",
        parameters={"signal_tf": "15m", "require_exact_td13": True, "confirm_window": 4, "atr_period": 14, "stop_atr_mult": 0.5, "target_r": 2.0, "max_hold_bars": 32},
    ),
    "R04": RStrategySpec(
        strategy_id="R04",
        name="R04 · 双周期 TD9 反转策略",
        version="1.0.0",
        group="R系列",
        signal_timeframe="1h",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=16,
        description="1h 已收盘 K 线完成底部/顶部 TD9 候选 + 最多 16 根 15m K 线价格反转确认（不要求 15m 同时出现 TD9）。",
        parameters={"signal_tf": "1h", "exec_tf": "15m", "confirm_window": 16, "atr_period": 14, "stop_atr_mult": 0.5, "target_r": 2.0, "max_hold_bars": 32},
    ),
    "R05": RStrategySpec(
        strategy_id="R05",
        name="R05 · 双周期 TD13 反转策略",
        version="1.0.0",
        group="R系列",
        signal_timeframe="1h",
        exec_timeframe="15m",
        background_timeframe=None,
        confirm_window=16,
        description="1h 已收盘 K 线正式完成底部/顶部 TD13 候选 + 最多 16 根 15m K 线价格反转确认，严禁使用未收盘 1h K 线。",
        parameters={"signal_tf": "1h", "exec_tf": "15m", "require_exact_td13": True, "confirm_window": 16, "atr_period": 14, "stop_atr_mult": 0.5, "target_r": 2.0, "max_hold_bars": 32},
    ),
    "R06": RStrategySpec(
        strategy_id="R06",
        name="R06 · 多周期顺势衰竭策略",
        version="1.0.0",
        group="R系列",
        signal_timeframe="1h",
        exec_timeframe="15m",
        background_timeframe="4h",
        confirm_window=16,
        description="4h EMA20/EMA60(>=0.3%) 趋势识别 + 1h 顺势衰竭(上涨仅接底 TD9/TD13，下跌仅接顶 TD9/TD13，震荡不开仓) + 15m 16 根窗口反转确认(确认时复查 4h 趋势)。",
        parameters={
            "bg_tf": "4h", "signal_tf": "1h", "exec_tf": "15m",
            "ema_fast": 20, "ema_slow": 60, "min_spread_ratio": 0.003,
            "confirm_window": 16, "atr_period": 14, "stop_atr_mult": 0.5, "target_r": 2.0, "max_hold_bars": 32,
        },
    ),
}


def is_bullish_confirm(prev_bar: Dict[str, Any], cur_bar: Dict[str, Any]) -> bool:
    """Bullish reversal confirmation: close > prev_high AND close > open."""
    return cur_bar["c"] > prev_bar["h"] and cur_bar["c"] > cur_bar["o"]


def is_bearish_confirm(prev_bar: Dict[str, Any], cur_bar: Dict[str, Any]) -> bool:
    """Bearish reversal confirmation: close < prev_low AND close < open."""
    return cur_bar["c"] < prev_bar["l"] and cur_bar["c"] < cur_bar["o"]


class PrecomputedMultiTFContext:
    """Precomputed strictly-causal indicators over a fixed-origin closed 15m bar list.

    Because TD Sequential state depends on the full history from fixed origin,
    computing once over the full closed-bar sequence is 100% identical to computing
    on every prefix bars[:i+1] (verified in PA-11 zero-lookahead tests), while
    allowing fast O(N) backtesting across 18+ months of 15m bars.
    """

    def __init__(self, bars_15m: List[Dict[str, Any]]):
        self.bars_15m = bars_15m
        self.td_15m: List[TdBar] = compute_td(bars_15m)
        self.atr_15m: List[Optional[float]] = compute_atr_series(bars_15m, period=14)

        self.bars_1h = aggregate_closed_htf(bars_15m, factor=4)
        self.td_1h: List[TdBar] = compute_td(self.bars_1h)
        self.h1_by_close_ms: Dict[int, int] = {b["close_ms"]: idx for idx, b in enumerate(self.bars_1h)}

        self.bars_4h = aggregate_closed_htf(bars_15m, factor=16)
        self.regime_4h: List[str] = classify_4h_trend_series(self.bars_4h, fast_period=20, slow_period=60, min_spread_ratio=0.003)
        self.h4_close_ms: List[int] = [b["close_ms"] for b in self.bars_4h]

        # For each 15m bar i, record latest available closed 4h index (strictly close_ms <= bar_15m.close_ms)
        self.latest_4h_idx: List[Optional[int]] = []
        p4 = -1
        n4 = len(self.bars_4h)
        for b in bars_15m:
            cms = b["close_ms"]
            while p4 + 1 < n4 and self.h4_close_ms[p4 + 1] <= cms:
                p4 += 1
            self.latest_4h_idx.append(p4 if p4 >= 0 else None)


class RSignalEngine:
    """Stateful bar-by-bar signal & candidate manager for one R-Series strategy.

    Enforces all Section 05 & 06 rules:
    - Closed bars only, multi-TF aligned by close_ms
    - Unique deterministic signal_id per exhaustion event
    - Single consumption per signal_id
    - Same-direction new candidate overwrites unconsumed old candidate
    - Opposite TD9 on signal_tf cancels unconsumed candidate
    - Window expiry cancels candidate without back-opening
    - R06 re-checks 4h trend at confirmation bar
    """

    def __init__(self, strategy_id: str, *, stop_atr_mult: float = 0.5):
        if strategy_id not in R_SPECS:
            raise ValueError(f"UNKNOWN_R_STRATEGY:{strategy_id}")
        self.sid = strategy_id
        self.spec = R_SPECS[strategy_id]
        self.stop_atr_mult = stop_atr_mult
        self.candidate: Optional[Dict[str, Any]] = None
        self.consumed_signal_ids: set[str] = set()
        self.last_h1_close_ms: Optional[int] = None

        # Diagnostics counters
        self.stats: Dict[str, int] = {
            "raw_candidates": 0,
            "confirmed_entries": 0,
            "canceled_opposite_td9": 0,
            "canceled_expired": 0,
            "canceled_trend_lost": 0,
            "overwritten_same_dir": 0,
            "rejected_duplicate_signal": 0,
            "rejected_invalid_atr": 0,
            "rejected_range_regime": 0,
        }

    def export_state(self) -> Dict[str, Any]:
        return {
            "sid": self.sid,
            "candidate": self.candidate,
            "consumed_signal_ids": sorted(self.consumed_signal_ids)[-200:],
            "last_h1_close_ms": self.last_h1_close_ms,
        }

    def load_state(self, state: Optional[Dict[str, Any]]) -> None:
        if not state:
            return
        self.candidate = state.get("candidate")
        self.consumed_signal_ids = set(state.get("consumed_signal_ids") or ())
        self.last_h1_close_ms = state.get("last_h1_close_ms")

    def reset_transient(self) -> None:
        """Clear unconsumed candidate on gap/unhealthy market cycle (fail-closed)."""
        self.candidate = None

    def step(
        self,
        ctx: PrecomputedMultiTFContext,
        idx_15m: int,
        *,
        has_open_position: bool = False,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """Process closed 15m bar `idx_15m`.

        Returns (pending_entry_signal_or_None, status_code).
        """
        bar = ctx.bars_15m[idx_15m]
        prev_bar = ctx.bars_15m[idx_15m - 1] if idx_15m >= 1 else None
        atr14 = ctx.atr_15m[idx_15m]
        td15 = ctx.td_15m[idx_15m]

        # Determine newly closed 1h bar aligned at this 15m close
        h1_idx = ctx.h1_by_close_ms.get(bar["close_ms"])
        new_h1 = h1_idx is not None and (self.last_h1_close_ms is None or bar["close_ms"] > self.last_h1_close_ms)
        if new_h1:
            self.last_h1_close_ms = bar["close_ms"]
        h1_bar = ctx.bars_1h[h1_idx] if new_h1 and h1_idx is not None else None
        h1_td = ctx.td_1h[h1_idx] if new_h1 and h1_idx is not None else None

        # Latest closed 4h trend regime at this 15m close
        h4_idx = ctx.latest_4h_idx[idx_15m]
        regime_4h = ctx.regime_4h[h4_idx] if h4_idx is not None else "RANGE"

        # 1. Check opposite TD9 cancellation on signal timeframe for existing unconsumed candidate
        if self.candidate is not None:
            cand_dir = self.candidate["direction"]
            opp_td9 = False
            if self.spec.signal_timeframe == "15m":
                opp_td9 = (td15.sell_setup == 9) if cand_dir == "LONG" else (td15.buy_setup == 9)
            elif new_h1 and h1_td is not None:
                opp_td9 = (h1_td.sell_setup == 9) if cand_dir == "LONG" else (h1_td.buy_setup == 9)
            if opp_td9:
                self.candidate = None
                self.stats["canceled_opposite_td9"] += 1

        # 2. Check confirmation / expiry for existing candidate (on bars AFTER signal bar)
        if self.candidate is not None and idx_15m > self.candidate["signal_15m_idx"]:
            elapsed = idx_15m - self.candidate["signal_15m_idx"]
            # Update window extreme
            if self.candidate["direction"] == "LONG":
                self.candidate["extreme"] = min(self.candidate["extreme"], bar["l"])
            else:
                self.candidate["extreme"] = max(self.candidate["extreme"], bar["h"])

            if elapsed > self.spec.confirm_window:
                self.candidate = None
                self.stats["canceled_expired"] += 1
            else:
                # For R06, re-check 4h trend validity on confirmation bar
                trend_ok = True
                if self.sid == "R06":
                    expected_regime = "UPTREND" if self.candidate["direction"] == "LONG" else "DOWNTREND"
                    if regime_4h != expected_regime:
                        trend_ok = False
                if not trend_ok:
                    self.candidate = None
                    self.stats["canceled_trend_lost"] += 1
                elif prev_bar is not None:
                    confirmed = (
                        is_bullish_confirm(prev_bar, bar)
                        if self.candidate["direction"] == "LONG"
                        else is_bearish_confirm(prev_bar, bar)
                    )
                    if confirmed:
                        cand = self.candidate
                        self.candidate = None
                        if has_open_position:
                            return None, "HOLD_SKIP_CONFIRMED"
                        if cand["signal_id"] in self.consumed_signal_ids:
                            self.stats["rejected_duplicate_signal"] += 1
                            return None, "DUPLICATE_SIGNAL_REJECTED"
                        if atr14 is None or atr14 <= 0:
                            self.stats["rejected_invalid_atr"] += 1
                            return None, "INVALID_ATR"
                        stop = (
                            cand["extreme"] - self.stop_atr_mult * atr14
                            if cand["direction"] == "LONG"
                            else cand["extreme"] + self.stop_atr_mult * atr14
                        )
                        self.consumed_signal_ids.add(cand["signal_id"])
                        self.stats["confirmed_entries"] += 1
                        return {
                            "strategy_id": self.sid,
                            "version": self.spec.version,
                            "signal_id": cand["signal_id"],
                            "direction": cand["direction"],
                            "signal_source": cand["signal_source"],
                            "signal_bar_close_at": cand["signal_close_at"],
                            "confirm_bar_close_at": bar["close_at"],
                            "signal_cycle": bar["close_at"],
                            "stop": str(Decimal(str(round(stop, 2)))),
                            "atr14": round(atr14, 4),
                            "regime_4h": regime_4h,
                        }, f"CONFIRMED_{cand['direction']}"

        # 3. Detect new candidate event on this bar
        new_event = self._detect_event(idx_15m, bar, td15, new_h1, h1_bar, h1_td, regime_4h)
        if new_event is None:
            return None, "WAIT"

        if new_event["signal_id"] in self.consumed_signal_ids:
            self.stats["rejected_duplicate_signal"] += 1
            return None, "DUPLICATE_SIGNAL_REJECTED"

        self.stats["raw_candidates"] += 1

        # R01 has confirm_window == 0 -> immediate next-open pending entry
        if self.spec.confirm_window == 0:
            if has_open_position:
                return None, "HOLD_SKIP_SIGNAL"
            if atr14 is None or atr14 <= 0:
                self.stats["rejected_invalid_atr"] += 1
                return None, "INVALID_ATR"
            stop = (
                new_event["extreme"] - self.stop_atr_mult * atr14
                if new_event["direction"] == "LONG"
                else new_event["extreme"] + self.stop_atr_mult * atr14
            )
            self.consumed_signal_ids.add(new_event["signal_id"])
            self.stats["confirmed_entries"] += 1
            return {
                "strategy_id": self.sid,
                "version": self.spec.version,
                "signal_id": new_event["signal_id"],
                "direction": new_event["direction"],
                "signal_source": new_event["signal_source"],
                "signal_bar_close_at": new_event["signal_close_at"],
                "confirm_bar_close_at": bar["close_at"],
                "signal_cycle": bar["close_at"],
                "stop": str(Decimal(str(round(stop, 2)))),
                "atr14": round(atr14, 4),
                "regime_4h": regime_4h,
            }, f"SIGNAL_{new_event['direction']}"

        # R02-R06: store candidate; same-direction new candidate overwrites unconsumed old candidate
        if self.candidate is not None and self.candidate["direction"] == new_event["direction"]:
            self.stats["overwritten_same_dir"] += 1
        self.candidate = new_event
        return None, f"CANDIDATE_{new_event['direction']}"

    def _detect_event(
        self,
        idx_15m: int,
        bar: Dict[str, Any],
        td15: TdBar,
        new_h1: bool,
        h1_bar: Optional[Dict[str, Any]],
        h1_td: Optional[TdBar],
        regime_4h: str,
    ) -> Optional[Dict[str, Any]]:
        if self.sid == "R01":
            if td15.buy_setup == 9:
                return self._mk_cand("LONG", "TD9", "15m", idx_15m, bar["close_at"], bar["l"])
            if td15.sell_setup == 9:
                return self._mk_cand("SHORT", "TD9", "15m", idx_15m, bar["close_at"], bar["h"])
            return None

        if self.sid == "R02":
            if td15.buy_setup == 9 and td15.buy_perfected:
                return self._mk_cand("LONG", "TD9_PERFECTED", "15m", idx_15m, bar["close_at"], bar["l"])
            if td15.sell_setup == 9 and td15.sell_perfected:
                return self._mk_cand("SHORT", "TD9_PERFECTED", "15m", idx_15m, bar["close_at"], bar["h"])
            return None

        if self.sid == "R03":
            # Strictly == 13 (never deferred, -12, or >= 14)
            if td15.buy_countdown == 13:
                return self._mk_cand("LONG", "TD13", "15m", idx_15m, bar["close_at"], bar["l"])
            if td15.sell_countdown == 13:
                return self._mk_cand("SHORT", "TD13", "15m", idx_15m, bar["close_at"], bar["h"])
            return None

        if self.sid == "R04":
            if not new_h1 or h1_bar is None or h1_td is None:
                return None
            if h1_td.buy_setup == 9:
                return self._mk_cand("LONG", "TD9", "1h", idx_15m, h1_bar["close_at"], h1_bar["l"])
            if h1_td.sell_setup == 9:
                return self._mk_cand("SHORT", "TD9", "1h", idx_15m, h1_bar["close_at"], h1_bar["h"])
            return None

        if self.sid == "R05":
            if not new_h1 or h1_bar is None or h1_td is None:
                return None
            if h1_td.buy_countdown == 13:
                return self._mk_cand("LONG", "TD13", "1h", idx_15m, h1_bar["close_at"], h1_bar["l"])
            if h1_td.sell_countdown == 13:
                return self._mk_cand("SHORT", "TD13", "1h", idx_15m, h1_bar["close_at"], h1_bar["h"])
            return None

        if self.sid == "R06":
            if not new_h1 or h1_bar is None or h1_td is None:
                return None
            if regime_4h == "UPTREND":
                if h1_td.buy_countdown == 13:
                    return self._mk_cand("LONG", "TD13", "1h", idx_15m, h1_bar["close_at"], h1_bar["l"])
                if h1_td.buy_setup == 9:
                    return self._mk_cand("LONG", "TD9", "1h", idx_15m, h1_bar["close_at"], h1_bar["l"])
            elif regime_4h == "DOWNTREND":
                if h1_td.sell_countdown == 13:
                    return self._mk_cand("SHORT", "TD13", "1h", idx_15m, h1_bar["close_at"], h1_bar["h"])
                if h1_td.sell_setup == 9:
                    return self._mk_cand("SHORT", "TD9", "1h", idx_15m, h1_bar["close_at"], h1_bar["h"])
            else:
                if h1_td.buy_setup == 9 or h1_td.sell_setup == 9 or h1_td.buy_countdown == 13 or h1_td.sell_countdown == 13:
                    self.stats["rejected_range_regime"] += 1
            return None

        return None

    def _mk_cand(
        self,
        direction: str,
        signal_source: str,
        tf: str,
        idx_15m: int,
        signal_close_at: str,
        extreme: float,
    ) -> Dict[str, Any]:
        return {
            "signal_id": f"{self.sid}:{tf}:{direction}:{signal_source}:{signal_close_at}",
            "direction": direction,
            "signal_source": signal_source,
            "signal_15m_idx": idx_15m,
            "signal_close_at": signal_close_at,
            "extreme": extreme,
        }
