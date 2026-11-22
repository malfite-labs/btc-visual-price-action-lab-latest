"""Pre-registered OHLC scoring rules for blinded synthetic vision fixtures."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from backend.market import Candle

GLOBAL_SEGMENT_COUNT = 3
GLOBAL_SEGMENT_MIN_RETURN_PCT = Decimal("0.25")
GLOBAL_MIN_ENDPOINT_RETURN_PCT = Decimal("1.0")
RANGE_MAX_ENDPOINT_RETURN_PCT = Decimal("1.0")
RANGE_MAX_SEGMENT_RETURN_PCT = Decimal("1.5")
LOCAL_SEGMENT_COUNT = 3
LOCAL_SEGMENT_MIN_RETURN_PCT = Decimal("0.25")
LOCAL_MIN_ENDPOINT_RETURN_PCT = Decimal("1.0")


def _validate(candles: list[Candle]) -> None:
    if len(candles) != 384:
        raise ValueError("GROUND_TRUTH_REQUIRES_EXACTLY_384_BARS")
    selected = candles[-288:]
    previous_at = None
    for candle in selected:
        values = [
            Decimal(candle.open),
            Decimal(candle.high),
            Decimal(candle.low),
            Decimal(candle.close),
        ]
        if any(not value.is_finite() or value <= 0 for value in values):
            raise ValueError("GROUND_TRUTH_INVALID_OHLC_VALUE")
        open_, high, low, close = values
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise ValueError("GROUND_TRUTH_INCOHERENT_OHLC")
        from datetime import datetime

        opened_at = datetime.fromisoformat(candle.open_at.replace("Z", "+00:00"))
        if opened_at.tzinfo is None or opened_at.utcoffset().total_seconds() != 0:
            raise ValueError("GROUND_TRUTH_TIMESTAMP_MUST_BE_UTC")
        if previous_at is not None and (opened_at - previous_at).total_seconds() != 900:
            raise ValueError("GROUND_TRUTH_NONCONTIGUOUS_15M_BARS")
        previous_at = opened_at


def _return_pct(first_open: str, last_close: str) -> Decimal:
    return (Decimal(last_close) / Decimal(first_open) - Decimal(1)) * Decimal(100)


def _segments(candles: list[Candle], count: int) -> list[Decimal]:
    if len(candles) % count:
        raise ValueError("GROUND_TRUTH_WINDOW_NOT_DIVISIBLE")
    width = len(candles) // count
    return [
        _return_pct(candles[start].open, candles[start + width - 1].close)
        for start in range(0, len(candles), width)
    ]


def _direction(
    endpoint: Decimal,
    segment_returns: list[Decimal],
    *,
    segment_floor: Decimal,
    endpoint_floor: Decimal,
) -> str:
    up_segments = sum(value >= segment_floor for value in segment_returns)
    down_segments = sum(value <= -segment_floor for value in segment_returns)
    required = len(segment_returns) // 2 + 1
    if endpoint >= endpoint_floor and up_segments >= required:
        return "rising"
    if endpoint <= -endpoint_floor and down_segments >= required:
        return "falling"
    if abs(endpoint) < RANGE_MAX_ENDPOINT_RETURN_PCT and all(
        abs(value) < RANGE_MAX_SEGMENT_RETURN_PCT for value in segment_returns
    ):
        return "range"
    return "mixed"


def _window(candles: list[Candle], segment_count: int, segment_floor: Decimal) -> dict[str, Any]:
    first, last = candles[0], candles[-1]
    endpoint = _return_pct(first.open, last.close)
    highs = [Decimal(item.high) for item in candles]
    lows = [Decimal(item.low) for item in candles]
    high_value, low_value = max(highs), min(lows)
    high_index, low_index = highs.index(high_value), lows.index(low_value)
    segment_returns = _segments(candles, segment_count)
    return {
        "bar_count": len(candles),
        "start_open": first.open,
        "end_close": last.close,
        "endpoint_change_pct": str(endpoint),
        "high": {
            "price": str(high_value),
            "index": high_index,
            "open_at": candles[high_index].open_at,
        },
        "low": {"price": str(low_value), "index": low_index, "open_at": candles[low_index].open_at},
        "segment_returns_pct": [str(value) for value in segment_returns],
        "structure_direction": _direction(
            endpoint,
            segment_returns,
            segment_floor=segment_floor,
            endpoint_floor=GLOBAL_MIN_ENDPOINT_RETURN_PCT,
        ),
    }


def score_windows(candles: list[Candle]) -> dict[str, Any]:
    """Score last 288 global and nested 96 local candles using fixed rules.

    A local rise is a confirmed reversal only when the 288-bar structural
    direction is falling, the local 32-bar thirds confirm a rise, and the
    second local half has both a higher high and higher low than the first
    half, with the final close above the first-half high. Otherwise a positive
    local direction is classified only as a rally.
    """
    _validate(candles)
    global_bars = candles[-288:]
    local_bars = candles[-96:]
    global_result = _window(global_bars, GLOBAL_SEGMENT_COUNT, GLOBAL_SEGMENT_MIN_RETURN_PCT)
    local_result = _window(local_bars, LOCAL_SEGMENT_COUNT, LOCAL_SEGMENT_MIN_RETURN_PCT)
    local_endpoint = Decimal(local_result["endpoint_change_pct"])
    local_half = len(local_bars) // 2
    first_half, second_half = local_bars[:local_half], local_bars[local_half:]
    first_half_high = max(Decimal(item.high) for item in first_half)
    higher_high = max(Decimal(item.high) for item in second_half) > first_half_high
    higher_low = min(Decimal(item.low) for item in second_half) > min(
        Decimal(item.low) for item in first_half
    )
    breakout = Decimal(local_bars[-1].close) > first_half_high
    confirmed = (
        global_result["structure_direction"] == "falling"
        and local_result["structure_direction"] == "rising"
        and local_endpoint >= LOCAL_MIN_ENDPOINT_RETURN_PCT
        and higher_high
        and higher_low
        and breakout
    )
    local_result["structure_checks"] = {
        "higher_high_second_half": higher_high,
        "higher_low_second_half": higher_low,
        "close_breaks_first_half_high": breakout,
    }
    if confirmed:
        local_result["event"] = "confirmed_reversal"
        assessment = "reversal_structure"
    elif local_result["structure_direction"] == "rising":
        local_result["event"] = "rally_not_confirmed_reversal"
    else:
        local_result["event"] = "no_confirmed_reversal"
    if not confirmed:
        if global_result["structure_direction"] in {"rising", "falling"} and (
            local_result["structure_direction"] == global_result["structure_direction"]
        ):
            assessment = "trend_structure"
        elif global_result["structure_direction"] == local_result["structure_direction"] == "range":
            assessment = "range_or_wait"
        else:
            assessment = "mixed_transition"
    return {"global": global_result, "local": local_result, "assessment": assessment}
