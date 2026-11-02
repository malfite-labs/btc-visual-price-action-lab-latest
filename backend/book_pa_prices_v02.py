"""Deterministic research-only prices; never modifies or repairs model output."""

from datetime import datetime
from decimal import Decimal, InvalidOperation


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return result


def resolve_price(spec: dict, bars: list[dict], *, cutoff: str) -> dict:
    by_time = {}
    for bar in bars:
        key = _time(bar["open_at"])
        if key in by_time:
            raise ValueError("DUPLICATE_CANDLE")
        by_time[key] = bar
    kind, field = spec.get("type"), spec.get("field")
    if field not in {"open", "high", "low", "close"}:
        raise ValueError("INVALID_OHLC_FIELD")
    if kind == "ohlc":
        ids = [spec.get("candle_open_at")]
        formula = f"OHLC.{field}"
    elif kind == "boundary" and spec.get("operation") in {"min", "max"}:
        ids = spec.get("candle_open_ats")
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
            raise ValueError("INVALID_BOUNDARY_CANDLES")
        formula = f"{spec['operation']}(OHLC.{field})"
    else:
        raise ValueError("INVALID_PRICE_FORMULA")
    try:
        selected = [by_time[_time(item)] for item in ids]
        if any(bar.get("is_closed") is not True for bar in selected):
            raise ValueError("CANDLE_NOT_CLOSED")
        if any(_time(bar["close_at"]) > _time(cutoff) for bar in selected):
            raise ValueError("CANDLE_AFTER_CUTOFF")
        values = [Decimal(str(bar[field])) for bar in selected]
        if any(not value.is_finite() or value <= 0 for value in values):
            raise ValueError("INVALID_PRICE")
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("INVALID_PRICE_REFERENCE") from exc
    result = (
        values[0]
        if kind == "ohlc"
        else (max(values) if spec["operation"] == "max" else min(values))
    )
    return {"price": str(result), "formula": formula, "candle_open_ats": ids}
