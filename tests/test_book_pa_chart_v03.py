from datetime import UTC, datetime, timedelta

from backend.book_pa_chart_v03 import render_numbered_chart
from backend.market import Candle


def test_numbered_chart_maps_all_48_labels_to_source_ohlc(tmp_path):
    end = datetime(2026, 10, 29, 12, tzinfo=UTC)
    rows = []
    for i in range(288):
        start = end - timedelta(minutes=15 * (288 - i))
        close = start + timedelta(minutes=15)
        rows.append(
            Candle(
                "BTC/USD",
                "15Min",
                start.isoformat(),
                close.isoformat(),
                "100",
                "102",
                "99",
                "101",
                "0",
                0,
                None,
                True,
            )
        )
    out = render_numbered_chart(rows, tmp_path / "chart.png")
    assert len(out["numbered_candles"]) == 48
    assert out["numbered_candles"][0]["id"] == "K01"
    assert out["numbered_candles"][-1]["open_at"] == rows[-1].open_at
    assert out["numbered_candles"][-1]["close_at"] == rows[-1].close_at
    assert out["numbered_candles"][0]["low"] == "99"
