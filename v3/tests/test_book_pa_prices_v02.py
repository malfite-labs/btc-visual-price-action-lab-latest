import pytest

from backend.book_pa_prices_v02 import resolve_price

BARS = [
    {
        "open_at": "2026-01-01T00:00:00+00:00",
        "is_closed": True,
        "close_at": "2026-01-01T00:15:00+00:00",
        "open": "100",
        "high": "105.125",
        "low": "99",
        "close": "104",
    },
    {
        "open_at": "2026-01-01T00:15:00+00:00",
        "is_closed": True,
        "close_at": "2026-01-01T00:30:00+00:00",
        "open": "104",
        "high": "106",
        "low": "102",
        "close": "103",
    },
]


def test_anchor_and_boundary_derive_exact_prices_from_bars():
    a = resolve_price(
        {"type": "ohlc", "candle_open_at": BARS[0]["open_at"], "field": "high"},
        BARS,
        cutoff="2026-01-01T00:30:00+00:00",
    )
    assert a["price"] == "105.125" and a["formula"] == "OHLC.high"
    b = resolve_price(
        {
            "type": "boundary",
            "field": "high",
            "operation": "max",
            "candle_open_ats": [x["open_at"] for x in BARS],
        },
        BARS,
        cutoff="2026-01-01T00:30:00+00:00",
    )
    assert b["price"] == "106" and b["formula"] == "max(OHLC.high)"


def test_reject_unclosed_unknown_and_computed_without_explicit_formula():
    with pytest.raises(ValueError):
        resolve_price(
            {"type": "ohlc", "candle_open_at": "2026-01-01T00:30:00+00:00", "field": "high"},
            BARS,
            cutoff="2026-01-01T00:30:00+00:00",
        )
    with pytest.raises(ValueError):
        resolve_price(
            {
                "type": "boundary",
                "field": "high",
                "operation": "average",
                "candle_open_ats": [BARS[0]["open_at"]],
            },
            BARS,
            cutoff="2026-01-01T00:30:00+00:00",
        )
    with pytest.raises(ValueError):
        resolve_price(
            {"type": "ohlc", "candle_open_at": BARS[0]["open_at"], "field": "high"},
            [{**BARS[0], "is_closed": False}],
            cutoff="2026-01-01T00:30:00+00:00",
        )


def test_cutoff_excludes_future_candle():
    with pytest.raises(ValueError, match="CANDLE_AFTER_CUTOFF"):
        resolve_price(
            {"type": "ohlc", "candle_open_at": BARS[1]["open_at"], "field": "high"},
            [{**BARS[1], "close_at": "2026-01-01T00:30:00+00:00"}],
            cutoff="2026-01-01T00:15:00+00:00",
        )
