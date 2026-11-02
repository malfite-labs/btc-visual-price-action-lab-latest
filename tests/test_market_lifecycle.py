from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

from backend.market import AlpacaMarketData


class ScriptedSocket:
    def __init__(self, subscription=None, after_subscription=None, on_close=None):
        self.messages = [
            json.dumps([{"T": "success", "msg": "authenticated"}]),
            json.dumps(
                subscription or [{"T": "subscription", "quotes": ["BTC/USD"], "bars": ["BTC/USD"]}]
            ),
        ]
        self.after_subscription = after_subscription
        self.on_close = on_close
        self.reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        if self.on_close:
            self.on_close()
        return False

    async def send(self, _raw):
        return None

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        self.reads += 1
        if self.after_subscription:
            return await self.after_subscription(self.reads)
        await asyncio.Future()


def test_protected_timeouts_retry_serially_without_releasing_protection(monkeypatch):
    import backend.market as market
    attempts = []
    active = [0]
    stop = asyncio.Event()
    class TimeoutSocket:
        async def __aenter__(self):
            assert active[0] == 0
            active[0] += 1
            return self
        async def __aexit__(self, *_):
            active[0] -= 1
        async def send(self, _):
            pass
        async def recv(self):
            attempts.append(1)
            if len(attempts) == 3:
                stop.set()
            raise TimeoutError()
    monkeypatch.setitem(sys.modules, 'websockets', SimpleNamespace(connect=lambda *a, **k: TimeoutSocket()))
    monkeypatch.setattr(market, 'STREAM_RETRY_BACKOFF_SECONDS', (0,))
    adapter = AlpacaMarketData(credentials=('test','test'))
    adapter.status['websocket_recovery_state'] = 'LONG_PROTECTION'
    asyncio.run(asyncio.wait_for(adapter.websocket(lambda e: asyncio.sleep(0), stop), timeout=2))
    assert len(attempts) == 3 and active[0] == 0
    assert adapter.status['websocket_recovery_state'] == 'LONG_PROTECTION'
    assert adapter.status['websocket_failure_reason'] == 'CONNECTION_TIMEOUT'


def test_protected_406_remains_blocked_and_does_not_retry(monkeypatch):
    attempts = []
    def connect(*a, **kw):
        attempts.append(1)
        return ScriptedSocket(subscription=[{'T':'error','code':406}])
    monkeypatch.setitem(sys.modules, 'websockets', SimpleNamespace(connect=connect))
    adapter = AlpacaMarketData(credentials=('test','test'))
    adapter.status['websocket_recovery_state'] = 'LONG_PROTECTION'
    asyncio.run(asyncio.wait_for(adapter.websocket(lambda e: asyncio.sleep(0), asyncio.Event()), timeout=2))
    assert len(attempts) == 1
    assert adapter.status['websocket_error_code'] == 406
    assert adapter.status['websocket'] == 'CONNECTION_LIMIT_BLOCKED'
    assert adapter.status['websocket_recovery_state'] == 'LONG_PROTECTION'


def run_task_until(task, predicate, timeout=1.0):
    async def wait():
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("stream state condition not reached")

    return wait


def test_protected_stream_receives_quotes_and_validates_before_release(monkeypatch):
    from datetime import UTC, datetime

    async def quote(_reads):
        return json.dumps([{"T": "q", "S": "BTC/USD", "t": datetime.now(UTC).isoformat(), "bp": 1, "ap": 2}])

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=lambda *a, **kw: ScriptedSocket(after_subscription=quote)))
    adapter = AlpacaMarketData(credentials=("test-key", "test-secret"))
    adapter.status["websocket_recovery_state"] = "LONG_PROTECTION"

    async def scenario():
        stop = asyncio.Event()
        async def received(_event):
            assert adapter.status["websocket"] == "CONNECTED"
            assert adapter.status["websocket_recovery_state"] == "VALIDATING"
            assert adapter.confirm_market_recovered(quote_fresh=True, candles_complete=True, candles_gap_count=0)
            assert adapter.status["websocket_recovery_state"] == "HEALTHY"
            stop.set()
        await asyncio.wait_for(adapter.websocket(received, stop), timeout=1)
    asyncio.run(scenario())
    assert adapter.status["websocket_last_quote_received_at"]
    assert adapter.status["websocket_recovery_state"] == "HEALTHY"
    assert not adapter.confirm_market_recovered(quote_fresh=True, candles_complete=True, candles_gap_count=0)


def test_same_adapter_rejects_second_stream_entry(monkeypatch):
    connections = []

    def connect(*_args, **_kwargs):
        socket = ScriptedSocket()
        connections.append(socket)
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    adapter = AlpacaMarketData(credentials=("test-key", "test-secret"))

    async def scenario():
        stop = asyncio.Event()
        first = asyncio.create_task(adapter.websocket(lambda _event: asyncio.sleep(0), stop))
        try:
            await run_task_until(first, lambda: adapter.status.get("websocket") == "CONNECTED")()
            second = asyncio.create_task(adapter.websocket(lambda _event: asyncio.sleep(0), stop))
            await asyncio.sleep(0.05)
            assert len(connections) == 1
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        finally:
            stop.set()
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    asyncio.run(scenario())
    assert adapter.status["websocket"] == "STOPPED"


def test_process_lease_blocks_second_adapter_and_releases_on_shutdown(monkeypatch, tmp_path):
    connections = []

    def connect(*_args, **_kwargs):
        socket = ScriptedSocket()
        connections.append(socket)
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    lock_path = tmp_path / "alpaca-stream.lock"
    first_adapter = AlpacaMarketData(credentials=("test-key", "test-secret"))
    second_adapter = AlpacaMarketData(credentials=("test-key", "test-secret"))
    first_adapter.stream_lock_path = lock_path
    second_adapter.stream_lock_path = lock_path

    async def scenario():
        stop = asyncio.Event()
        first = asyncio.create_task(first_adapter.websocket(lambda _event: asyncio.sleep(0), stop))
        try:
            await run_task_until(
                first, lambda: first_adapter.status.get("websocket") == "CONNECTED"
            )()
            second = asyncio.create_task(
                second_adapter.websocket(lambda _event: asyncio.sleep(0), asyncio.Event())
            )
            await asyncio.sleep(0.05)
            assert len(connections) == 1
            assert second_adapter.status["websocket"] == "LOCAL_MANAGER_CONFLICT"
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        finally:
            stop.set()
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

        retry_stop = asyncio.Event()
        retry = asyncio.create_task(
            second_adapter.websocket(lambda _event: asyncio.sleep(0), retry_stop)
        )
        await run_task_until(retry, lambda: second_adapter.status.get("websocket") == "CONNECTED")()
        retry_stop.set()
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)

    asyncio.run(scenario())
    assert len(connections) == 2


def test_connection_limit_406_is_terminal_without_retry(monkeypatch):
    connections = []

    def connect(*_args, **_kwargs):
        connections.append(1)
        return ScriptedSocket(
            subscription=[{"T": "error", "code": 406, "msg": "connection limit exceeded"}]
        )

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    adapter = AlpacaMarketData(credentials=("test-key", "test-secret"))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(adapter.websocket(lambda _event: asyncio.sleep(0), stop))
        try:
            await run_task_until(
                task, lambda: adapter.status.get("websocket_error_code") == 406, timeout=0.5
            )()
            assert len(connections) == 1
            assert adapter.status["websocket"] == "CONNECTION_LIMIT_BLOCKED"
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert len(connections) == 1
    assert adapter.status["websocket"] == "CONNECTION_LIMIT_BLOCKED"
    assert adapter.status["websocket_failure_reason"] == "CONNECTION_LIMIT_EXCEEDED"


def test_transient_disconnect_reconnects_and_stop_closes_socket(monkeypatch):
    connections = []
    close_count = 0

    async def fail_first_read(_read):
        raise ConnectionError("test disconnect")

    def on_close():
        nonlocal close_count
        close_count += 1

    def connect(*_args, **_kwargs):
        sockets = [
            ScriptedSocket(after_subscription=fail_first_read, on_close=on_close),
            ScriptedSocket(on_close=on_close),
        ]
        socket = sockets[min(len(connections), 1)]
        connections.append(socket)
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    adapter = AlpacaMarketData(credentials=("test-key", "test-secret"))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(adapter.websocket(lambda _event: asyncio.sleep(0), stop))
        try:
            await run_task_until(
                task,
                lambda: len(connections) >= 2 and adapter.status.get("websocket") == "CONNECTED",
                timeout=3,
            )()
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            stop.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(connections) == 2
    assert close_count == 2
    assert adapter.status["websocket"] == "STOPPED"


def test_recovery_attempts_are_bounded_with_backoff(monkeypatch):
    import backend.market as market_module

    connections, delays = [], []
    real_sleep = asyncio.sleep

    def connect(*_args, **_kwargs):
        connections.append(1)
        raise OSError("offline")

    async def fast_sleep(seconds):
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    monkeypatch.setattr(market_module.asyncio, "sleep", fast_sleep)
    adapter = AlpacaMarketData(
        credentials=("test-key", "test-secret"), recovery_history_path=None
    )
    asyncio.run(adapter.websocket(lambda _event: None, asyncio.Event()))
    assert len(connections) == 15
    assert delays == [1, 2, 4, 8, 60, 1, 2, 4, 8, 120, 1, 2, 4, 8]
    assert adapter.status["websocket"] == "LONG_PROTECTION"


def test_recovery_has_three_bounded_rounds_and_persists_transitions(monkeypatch, tmp_path):
    import backend.market as market_module

    connections, delays = [], []
    real_sleep = asyncio.sleep

    def connect(*_args, **_kwargs):
        connections.append(1)
        raise OSError("offline")

    async def fast_sleep(seconds):
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    monkeypatch.setattr(market_module.asyncio, "sleep", fast_sleep)
    history = tmp_path / "recovery.jsonl"
    adapter = AlpacaMarketData(
        credentials=("test-key", "test-secret"), recovery_history_path=history
    )

    asyncio.run(adapter.websocket(lambda _event: None, asyncio.Event()))

    assert len(connections) == 15
    assert delays == [1, 2, 4, 8, 60, 1, 2, 4, 8, 120, 1, 2, 4, 8]
    assert adapter.status["websocket"] == "LONG_PROTECTION"
    assert adapter.status["websocket_recovery_round"] == 3
    assert adapter.status["websocket_recovery_state"] == "LONG_PROTECTION"
    records = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    assert any(row["event"] == "round_cooldown_started" for row in records)
    assert records[-1]["event"] == "long_protection_entered"
    assert records[-1]["websocket_last_error_class"] == "OSError"


def test_recovery_latch_resets_only_after_fresh_complete_market_validation(tmp_path):
    from datetime import UTC, datetime, timedelta

    adapter = AlpacaMarketData(
        credentials=("test-key", "test-secret"),
        recovery_history_path=tmp_path / "recovery.jsonl",
    )
    adapter.status.update(
        {
            "websocket": "CONNECTED",
            "websocket_recovery_state": "VALIDATING",
            "websocket_recovery_round": 2,
            "websocket_recovery_failed_rounds": 1,
            "websocket_recovery_first_failure_at": "2026-10-29T00:00:00+00:00",
            "websocket_last_quote_received_at": datetime.now(UTC).isoformat(),
            "last_quote": {
                "timestamp_valid": True,
                "event_at": (datetime.now(UTC) - timedelta(milliseconds=50)).isoformat(),
            },
        }
    )
    assert not adapter.confirm_market_recovered(
        quote_fresh=False, candles_complete=True, candles_gap_count=0
    )
    assert adapter.status["websocket_recovery_state"] == "VALIDATING"
    assert not adapter.confirm_market_recovered(
        quote_fresh=True, candles_complete=False, candles_gap_count=1
    )
    assert adapter.status["websocket_recovery_state"] == "VALIDATING"
    assert adapter.confirm_market_recovered(
        quote_fresh=True, candles_complete=True, candles_gap_count=0
    )
    assert adapter.status["websocket_recovery_state"] == "HEALTHY"
    assert adapter.status["websocket_recovery_round"] == 0
    assert adapter.status["websocket_recovery_success_at"]


def test_first_connection_attempt_counters_clear_after_initial_market_validation(tmp_path):
    from datetime import UTC, datetime, timedelta

    adapter = AlpacaMarketData(
        credentials=("test-key", "test-secret"),
        recovery_history_path=tmp_path / "recovery.jsonl",
    )
    adapter.status.update(
        {
            "websocket": "CONNECTED",
            "websocket_recovery_state": "HEALTHY",
            "websocket_recovery_round": 1,
            "websocket_recovery_attempts_in_round": 1,
            "websocket_last_quote_received_at": datetime.now(UTC).isoformat(),
            "last_quote": {
                "timestamp_valid": True,
                "event_at": (datetime.now(UTC) - timedelta(milliseconds=50)).isoformat(),
            },
        }
    )
    assert adapter.confirm_market_recovered(
        quote_fresh=True, candles_complete=True, candles_gap_count=0
    )
    assert adapter.status["websocket_recovery_round"] == 0
    assert adapter.status["websocket_recovery_attempts_in_round"] == 0


def test_websocket_failure_classification_keeps_only_observed_codes():
    from backend.market import ProviderStreamError, _classify_websocket_failure

    assert _classify_websocket_failure(TimeoutError()) == ("CONNECTION_TIMEOUT", None)
    assert _classify_websocket_failure(ConnectionResetError()) == ("NETWORK_ERROR", None)
    assert _classify_websocket_failure(RuntimeError("subscription unconfirmed")) == (
        "SUBSCRIPTION_FAILURE",
        None,
    )
    assert _classify_websocket_failure(ProviderStreamError("rejected", code=409)) == (
        "SUBSCRIPTION_REJECTED",
        409,
    )


def test_handshake_http_status_codes_are_recorded_without_inference():
    from backend.market import _classify_websocket_failure

    class HandshakeError(RuntimeError):
        def __init__(self, code):
            super().__init__("handshake rejected")
            self.status_code = code

    expected = {
        401: "AUTHENTICATION_FAILURE",
        402: "AUTHENTICATION_FAILURE",
        404: "SERVER_REJECTED",
        406: "CONNECTION_LIMIT",
        407: "PROXY_AUTH_REJECTED",
        409: "SERVER_REJECTED",
    }
    for code, category in expected.items():
        assert _classify_websocket_failure(HandshakeError(code)) == (category, code)


def test_persisted_long_protection_prevents_restart_from_bypassing_limit(monkeypatch, tmp_path):
    history = tmp_path / "recovery.jsonl"
    history.write_text(
        json.dumps(
            {
                "event": "long_protection_entered",
                "websocket_recovery_state": "LONG_PROTECTION",
                "websocket_recovery_round": 3,
                "websocket_recovery_attempts_in_round": 5,
                "websocket_recovery_failed_rounds": 3,
                "websocket_failure_reason": "NETWORK_ERROR",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(connect=lambda *_args, **_kwargs: calls.append(1)),
    )
    adapter = AlpacaMarketData(
        credentials=("test-key", "test-secret"), recovery_history_path=history
    )
    asyncio.run(adapter.websocket(lambda _event: None, asyncio.Event()))
    assert adapter.status["websocket"] == "LONG_PROTECTION"
    assert adapter.status["websocket_recovery_round"] == 3
    assert calls == [1]


def test_cross_process_lease_remains_held_during_cooldown(monkeypatch, tmp_path):
    import backend.market as market_module

    original_sleep = asyncio.sleep
    cooldown_started = asyncio.Event()
    finish_cooldown = asyncio.Event()
    connections = []

    def connect(*_args, **_kwargs):
        connections.append(1)
        raise OSError("reset")

    async def controlled_sleep(seconds):
        if seconds == 60:
            cooldown_started.set()
            await finish_cooldown.wait()
        else:
            await original_sleep(0)

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    monkeypatch.setattr(market_module.asyncio, "sleep", controlled_sleep)
    lease_path = tmp_path / "alpaca-stream.lock"
    first = AlpacaMarketData(credentials=("test-key", "test-secret"), stream_lock_path=lease_path)
    second = AlpacaMarketData(credentials=("test-key", "test-secret"), stream_lock_path=lease_path)

    async def scenario():
        task = asyncio.create_task(first.websocket(lambda _event: None, asyncio.Event()))
        try:
            await asyncio.wait_for(cooldown_started.wait(), timeout=1)
            await second.websocket(lambda _event: None, asyncio.Event())
            assert second.status["websocket"] == "LOCAL_MANAGER_CONFLICT"
            assert len(connections) == 5
        finally:
            finish_cooldown.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(connections) == 5
