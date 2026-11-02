import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from backend.market import (
    AlpacaMarketData,
    claim_closed_bar_for_dispatch,
    load_market_credentials,
    market_open_gate_reason,
    quote_timing,
    websocket_quote_health,
)


def test_market_credentials_do_not_fall_back_when_configured_file_is_missing(tmp_path, monkeypatch):
    fallback = tmp_path / "market.env"
    fallback.write_text(
        "ALPACA_API_KEY=test-key\nALPACA_SECRET_KEY=test-secret\n", encoding="utf-8"
    )
    monkeypatch.setenv("ALPACA_CREDENTIALS_FILE", str(tmp_path / "missing.env"))
    assert load_market_credentials(fallback_path=fallback) is None
    assert load_market_credentials(tmp_path / "missing.env", fallback_path=fallback) is None


def test_quote_timing_rejects_future_and_naive_provider_timestamps():
    received = datetime(2026, 10, 27, 4, 0, tzinfo=UTC)

    future = quote_timing(received + timedelta(milliseconds=1), received)
    naive = quote_timing(datetime(2026, 10, 27, 4, 0), received)
    normal = quote_timing(received - timedelta(milliseconds=250), received)

    assert future == {"timestamp_valid": False, "age_ms": 10**9, "timestamp_status": "FUTURE"}
    assert naive == {
        "timestamp_valid": False,
        "age_ms": 10**9,
        "timestamp_status": "TIMEZONE_MISSING",
    }
    assert normal == {"timestamp_valid": True, "age_ms": 250, "timestamp_status": "VALID"}


def test_websocket_quote_health_separates_stopped_silent_invalid_and_disconnected():
    now = datetime(2026, 10, 27, 4, 0, tzinfo=UTC)
    recent = (now - timedelta(seconds=1)).isoformat()
    old = (now - timedelta(seconds=31)).isoformat()

    assert websocket_quote_health("STOPPED", None, now) == "SERVICE_STOPPED"
    assert websocket_quote_health("RECOVERING", None, now) == "STREAM_UNAVAILABLE"
    assert websocket_quote_health("CONNECTED", None, now) == "NO_QUOTE_YET"
    assert (
        websocket_quote_health("CONNECTED", recent, now, timestamp_valid=False)
        == "TIMESTAMP_INVALID"
    )
    assert websocket_quote_health("CONNECTED", old, now) == "NO_RECENT_QUOTE"
    assert websocket_quote_health("CONNECTED", recent, now) == "FRESH"
    assert (
        websocket_quote_health(
            "CONNECTED", recent, now, last_quote_event_at=(now - timedelta(seconds=31)).isoformat()
        )
        == "STALE_EVENT_TIMESTAMP"
    )


def test_new_open_gate_requires_complete_market_fresh_quote_and_validated_recovery():
    base = {
        "feed_health": "LIVE",
        "gap_count": 0,
        "candle_set_valid": True,
        "websocket_state": "CONNECTED",
        "recovery_state": "HEALTHY",
        "websocket_quote_health_status": "FRESH",
    }
    assert market_open_gate_reason(**base) is None
    assert (
        market_open_gate_reason(**{**base, "recovery_state": "VALIDATING"})
        == "MARKET_RECOVERY_NOT_VALIDATED"
    )
    assert (
        market_open_gate_reason(**{**base, "websocket_quote_health_status": "STALE_EVENT_TIMESTAMP"})
        == "MARKET_STREAM_QUOTE_STALE"
    )
    assert (
        market_open_gate_reason(**{**base, "gap_count": 1}) == "MARKET_DATA_INCOMPLETE"
    )


def test_closed_bar_watermark_never_replays_seeded_or_skipped_cycles(tmp_path):
    from datetime import UTC, datetime, timedelta

    path = tmp_path / "market-decision-watermark.json"
    first = datetime(2026, 10, 29, 0, 15, tzinfo=UTC)
    next_close = first + timedelta(minutes=15)

    assert claim_closed_bar_for_dispatch(first, path) == (False, "STARTUP_WATERMARK_SEEDED")
    assert claim_closed_bar_for_dispatch(first, path) == (False, "CYCLE_ALREADY_SEEN")
    # The next bar is claimed before its data gate runs, so an outage skip is final.
    assert claim_closed_bar_for_dispatch(next_close, path) == (True, "NEW_CLOSED_CYCLE")
    assert claim_closed_bar_for_dispatch(next_close, path) == (False, "CYCLE_ALREADY_SEEN")
    assert claim_closed_bar_for_dispatch(first, path) == (False, "CYCLE_ALREADY_SEEN")


def test_system_quote_age_uses_utc_receive_time_and_keeps_invalid_time_stale():
    from backend.app import _quote_with_live_age

    received = datetime.now(UTC) - timedelta(seconds=3)
    aged = _quote_with_live_age(
        {
            "event_at": (received - timedelta(milliseconds=250)).isoformat(),
            "received_at": received.isoformat(),
            "age_ms": 250,
            "timestamp_valid": True,
        }
    )
    invalid = _quote_with_live_age(
        {
            "event_at": (received + timedelta(seconds=10)).isoformat(),
            "received_at": received.isoformat(),
            "age_ms": 10**9,
            "timestamp_valid": False,
            "timestamp_status": "FUTURE",
        }
    )

    assert aged["age_ms"] >= 3250
    assert aged["timestamp_valid"] is True
    assert invalid["age_ms"] == 10**9
    assert invalid["timestamp_valid"] is False


def test_system_status_surfaces_quote_age_and_websocket_failure_reason(monkeypatch):
    from backend import app

    now = datetime.now(UTC)
    old_received = now - timedelta(seconds=61)
    monkeypatch.setattr(
        app,
        "market_status",
        {
            "quote": {
                "event_at": (old_received - timedelta(milliseconds=2000)).isoformat(),
                "received_at": old_received.isoformat(),
                "age_ms": 2000,
                "timestamp_valid": True,
            },
            "feed_health": "LIVE",
        },
    )
    monkeypatch.setattr(
        app.market,
        "status",
        {
            "websocket": "RECOVERING",
            "websocket_error_code": 406,
            "websocket_failure_reason": "CONNECTION_LIMIT_EXCEEDED",
            "websocket_last_error_class": "RuntimeError",
            "websocket_reconnect_count": 1,
            "websocket_last_quote_received_at": None,
        },
    )

    result = asyncio.run(app.system_status())

    assert result["quote_health"] == "STALE"
    assert result["feed_health"] == "STALE"
    assert result["websocket"] == "RECOVERING"
    assert result["websocket_error_code"] == 406
    assert result["websocket_failure_reason"] == "CONNECTION_LIMIT_EXCEEDED"
    assert result["websocket_quote_health"] == "STREAM_UNAVAILABLE"


def test_websocket_connection_limit_is_classified_and_not_reported_as_auth_failure(monkeypatch):
    class MockSocket:
        def __init__(self):
            self.step = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, raw):
            return None

        async def recv(self):
            self.step += 1
            if self.step == 1:
                return json.dumps([{"T": "success", "msg": "authenticated"}])
            return json.dumps([{"T": "error", "code": 406, "msg": "connection limit exceeded"}])

    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(connect=lambda *_args, **_kwargs: MockSocket()),
    )
    adapter = AlpacaMarketData(credentials=("local-test-key", "local-test-secret"))

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(adapter.websocket(lambda _event: asyncio.sleep(0), stop))
        try:
            for _ in range(100):
                if adapter.status.get("websocket_failure_reason"):
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("websocket failure was not classified")
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())

    assert adapter.status["websocket_error_code"] == 406
    assert adapter.status["websocket_failure_reason"] == "CONNECTION_LIMIT_EXCEEDED"
    assert adapter.status["websocket"] == "CONNECTION_LIMIT_BLOCKED"


def test_websocket_normal_shutdown_is_distinguished_from_recovery(monkeypatch):
    class MockSocket:
        def __init__(self):
            self.step = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _raw):
            return None

        async def recv(self):
            self.step += 1
            if self.step == 1:
                return json.dumps([{"T": "success", "msg": "authenticated"}])
            if self.step == 2:
                return json.dumps([{"T": "subscription", "quotes": ["BTC/USD"]}])
            await asyncio.Future()

    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(connect=lambda *_args, **_kwargs: MockSocket()),
    )
    adapter = AlpacaMarketData(credentials=("local-test-key", "local-test-secret"))

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(adapter.websocket(lambda _event: asyncio.sleep(0), stop))
        try:
            for _ in range(100):
                if adapter.status.get("websocket") == "CONNECTED":
                    break
                await asyncio.sleep(0.01)
            assert adapter.status["websocket"] == "CONNECTED"
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            stop.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert adapter.status["websocket"] == "STOPPED"
