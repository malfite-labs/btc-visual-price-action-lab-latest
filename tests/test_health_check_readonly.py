from datetime import datetime, timezone

from scripts.health_check_readonly import assess


def test_healthy_snapshot_and_stale_cycle():
    now = datetime(2026, 10, 31, 9, 20, tzinfo=timezone.utc)
    status = {"websocket": "CONNECTED", "websocket_quote_health": "FRESH", "scheduler": "ENABLED_INTERNAL_PAPER", "shadow_evolution": {"state": "RUNNING"}}
    stats = {"agents": {a: {"last_decision": {"candle_close": "2026-10-31T09:15:00+00:00"}} for a in "ABC"}}
    assert assess(status, stats, now) == []
    stats["agents"]["B"]["last_decision"]["candle_close"] = "2026-10-31T08:30:00+00:00"
    assert "FORMAL_CYCLE_STALE_B" in assess(status, stats, now)


def test_offline_components_report_specific_causes():
    status = {"websocket": "LONG_PROTECTION", "websocket_quote_health": "STREAM_UNAVAILABLE", "scheduler": "DISABLED_NOT_ARMED", "shadow_evolution": {"state": "STALE"}}
    reasons = assess(status, {}, datetime.now(timezone.utc))
    assert {"ALPACA_WS_NOT_CONNECTED", "ALPACA_QUOTE_NOT_FRESH", "FORMAL_SCHEDULER_OFF", "SHADOW_NOT_RUNNING"}.issubset(reasons)
