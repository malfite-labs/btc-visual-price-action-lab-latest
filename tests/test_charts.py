from datetime import UTC, datetime, timedelta

from backend.charts import render_ai_snapshot
from backend.market import Candle


def test_snapshot_uses_only_288_closed_bars_and_96_local(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    for i in range(289):
        opened = start + timedelta(minutes=15 * i)
        bars.append(
            Candle(
                "BTC/USD",
                "15Min",
                opened.isoformat(),
                (opened + timedelta(minutes=15)).isoformat(),
                "100",
                "101",
                "99",
                "100.5",
                "0" if i == 3 else "1",
                0 if i == 3 else 2,
                "100.2",
                i < 288,
            )
        )
    result = render_ai_snapshot(bars, tmp_path / "snapshot.png")
    assert result["global_count"] == 288 and result["local_count"] == 96
    assert result["indicators"] == [] and len(result["sha256"]) == 64
    assert result["price_axes"]["global"] == ["101.00", "100.50", "100.00", "99.50", "99.00"]
    assert result["price_axes"]["local"] == ["101.00", "100.50", "100.00", "99.50", "99.00"]
    assert (tmp_path / "snapshot.png").exists()


def test_snapshot_rejects_short_history(tmp_path):
    with __import__("pytest").raises(ValueError):
        render_ai_snapshot([], tmp_path / "snapshot.png")


def test_v2_candidate_chart_has_clear_time_and_scale_metadata(tmp_path):
    from tests.vision_fixture_factory import candles_for

    output = tmp_path / "v2.png"
    result = render_ai_snapshot(candles_for("downtrend_then_reversal"), output, layout_version="v2")

    assert result["layout_version"] == "v2"
    assert result["image_size"] == [1920, 1280]
    assert result["global_count"] == 288 and result["local_count"] == 96
    assert result["panels"]["global"]["label"].startswith("GLOBAL WINDOW")
    assert result["panels"]["local"]["label"].startswith("LOCAL WINDOW")
    assert result["panels"]["global"]["price_scale"] == "independent USD"
    assert result["panels"]["local"]["price_scale"] == "independent USD"
    assert all(tick.endswith("Z") for tick in result["panels"]["global"]["utc_ticks"])
    assert all(tick.endswith("Z") for tick in result["panels"]["local"]["utc_ticks"])
