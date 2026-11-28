"""Causal multi-timeframe aggregation and technical helpers for R-Series strategies.

All functions strictly consume closed bars ordered oldest-first and never look ahead.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

UTC = timezone.utc
STEP_15M_MS = 15 * 60 * 1000
STEP_1H_MS = 60 * 60 * 1000
STEP_4H_MS = 4 * 60 * 60 * 1000


def parse_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("NAIVE_TIMESTAMP")
        return value.astimezone(UTC)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return dt.astimezone(UTC)


def normalize_closed_15m_bar(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a V3.1 candle dict into a uniform closed 15m bar record."""
    if raw.get("is_closed") is False:
        raise ValueError("UNCLOSED_BAR_REJECTED")
    open_dt = parse_utc(raw["open_at"])
    close_dt = parse_utc(raw["close_at"]) if raw.get("close_at") else datetime.fromtimestamp(
        open_dt.timestamp() + 900, UTC
    )
    if int((close_dt - open_dt).total_seconds()) != 900:
        raise ValueError("INVALID_15M_BAR_SPAN")
    return {
        "t": int(open_dt.timestamp() * 1000),
        "close_ms": int(close_dt.timestamp() * 1000),
        "open_at": open_dt.isoformat(),
        "close_at": close_dt.isoformat(),
        "o": float(raw["open"]),
        "h": float(raw["high"]),
        "l": float(raw["low"]),
        "c": float(raw["close"]),
        "v": float(raw.get("volume", 0.0)),
        "open": str(raw["open"]),
        "high": str(raw["high"]),
        "low": str(raw["low"]),
        "close": str(raw["close"]),
        "volume": str(raw.get("volume", "0")),
        "is_closed": True,
    }


def aggregate_closed_htf(bars_15m: List[Dict[str, Any]], factor: int) -> List[Dict[str, Any]]:
    """Aggregate closed 15m bars into UTC-aligned complete higher-timeframe closed bars.

    Incomplete buckets (fewer than `factor` contiguous 15m bars) are strictly excluded
    so unclosed higher-timeframe bars can never leak into signal generation.
    """
    bucket_ms = factor * STEP_15M_MS
    buckets: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for b in bars_15m:
        bucket_start = (b["t"] // bucket_ms) * bucket_ms
        buckets[bucket_start].append(b)

    out: List[Dict[str, Any]] = []
    for bucket_start in sorted(buckets.keys()):
        grp = buckets[bucket_start]
        if len(grp) != factor:
            continue
        # Ensure strict contiguity inside bucket
        if any(grp[k]["t"] - grp[k - 1]["t"] != STEP_15M_MS for k in range(1, factor)):
            continue
        out.append({
            "t": bucket_start,
            "close_ms": grp[-1]["close_ms"],
            "open_at": grp[0]["open_at"],
            "close_at": grp[-1]["close_at"],
            "o": grp[0]["o"],
            "h": max(x["h"] for x in grp),
            "l": min(x["l"] for x in grp),
            "c": grp[-1]["c"],
            "v": sum(x["v"] for x in grp),
            "is_closed": True,
        })
    return out


def compute_atr_series(bars: List[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    """Causal simple-moving ATR(period) matching V3.1 StrategyPool._v2_value('atr')."""
    n = len(bars)
    out: List[Optional[float]] = [None] * n
    if n == 0:
        return out
    trs: List[float] = [bars[0]["h"] - bars[0]["l"]]
    for i in range(1, n):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    running = 0.0
    for i in range(n):
        running += trs[i]
        if i >= period:
            running -= trs[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def compute_ema_series(closes: List[float], period: int) -> List[Optional[float]]:
    """Causal EMA(period) seeded with SMA of first `period` closes (matching StrategyPool._series)."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    alpha = 2.0 / (period + 1)
    cur = sum(closes[:period]) / period
    out[period - 1] = cur
    for i in range(period, n):
        cur = alpha * closes[i] + (1.0 - alpha) * cur
        out[i] = cur
    return out


def classify_4h_trend_series(
    bars_4h: List[Dict[str, Any]],
    *,
    fast_period: int = 20,
    slow_period: int = 60,
    min_spread_ratio: float = 0.003,
) -> List[str]:
    """Classify each closed 4h bar into 'UPTREND', 'DOWNTREND', or 'RANGE'.

    Baseline rules (Section 05 R06):
    - UPTREND  : EMA20 > EMA60, close > EMA20 and close > EMA60, |EMA20 - EMA60| / close >= 0.3%
    - DOWNTREND: EMA20 < EMA60, close < EMA20 and close < EMA60, |EMA20 - EMA60| / close >= 0.3%
    - RANGE    : otherwise (including warmup bars before EMA60 is ready)
    """
    closes = [b["c"] for b in bars_4h]
    ema_fast = compute_ema_series(closes, fast_period)
    ema_slow = compute_ema_series(closes, slow_period)
    regimes: List[str] = []
    for i, c in enumerate(closes):
        ef, es = ema_fast[i], ema_slow[i]
        if ef is None or es is None or c <= 0:
            regimes.append("RANGE")
            continue
        spread = abs(ef - es) / c
        if ef > es and c > ef and c > es and spread >= min_spread_ratio:
            regimes.append("UPTREND")
        elif ef < es and c < ef and c < es and spread >= min_spread_ratio:
            regimes.append("DOWNTREND")
        else:
            regimes.append("RANGE")
    return regimes
