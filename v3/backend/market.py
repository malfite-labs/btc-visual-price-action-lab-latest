from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

BASE = "https://data.alpaca.markets/v1beta3/crypto/us"
WS_URL = "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
STREAM_IDLE_TIMEOUT_SECONDS = 30
STREAM_MAX_RECOVERY_ATTEMPTS = 5
STREAM_MAX_RECOVERY_ROUNDS = 3
STREAM_RECOVERY_COOLDOWNS_SECONDS = (60, 120, 300)
STREAM_RETRY_BACKOFF_SECONDS = (1, 2, 4, 8)
DEFAULT_ALPACA_CREDENTIALS_PATH = Path(__file__).resolve().parents[1] / "data" / "alpaca-market-new.env"


def quote_timing(event_at: datetime, received_at: datetime) -> dict[str, Any]:
    """Compare Alpaca event time to local UTC receive time; reject naive/future stamps."""
    if event_at.tzinfo is None or event_at.utcoffset() is None:
        return {
            "timestamp_valid": False,
            "age_ms": 10**9,
            "timestamp_status": "TIMEZONE_MISSING",
        }
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        return {
            "timestamp_valid": False,
            "age_ms": 10**9,
            "timestamp_status": "RECEIVE_TIMEZONE_MISSING",
        }
    delta_ms = int((received_at.astimezone(UTC) - event_at.astimezone(UTC)).total_seconds() * 1000)
    if delta_ms < 0:
        return {"timestamp_valid": False, "age_ms": 10**9, "timestamp_status": "FUTURE"}
    return {"timestamp_valid": True, "age_ms": delta_ms, "timestamp_status": "VALID"}


def websocket_quote_health(
    connection_state: str,
    last_quote_received_at: str | None,
    now: datetime,
    *,
    timestamp_valid: bool = True,
    last_quote_event_at: str | None = None,
) -> str:
    if connection_state == "STOPPED":
        return "SERVICE_STOPPED"
    if connection_state != "CONNECTED":
        return "STREAM_UNAVAILABLE"
    if not timestamp_valid:
        return "TIMESTAMP_INVALID"
    if not last_quote_received_at:
        return "NO_QUOTE_YET"
    try:
        received = datetime.fromisoformat(last_quote_received_at)
    except (TypeError, ValueError):
        return "RECEIVE_TIMESTAMP_INVALID"
    if received.tzinfo is None or now.tzinfo is None:
        return "RECEIVE_TIMESTAMP_INVALID"
    age_seconds = (now.astimezone(UTC) - received.astimezone(UTC)).total_seconds()
    if age_seconds < 0:
        return "RECEIVE_TIMESTAMP_INVALID"
    if age_seconds > STREAM_IDLE_TIMEOUT_SECONDS:
        return "NO_RECENT_QUOTE"
    if last_quote_event_at:
        try:
            event_at = datetime.fromisoformat(last_quote_event_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return "EVENT_TIMESTAMP_INVALID"
        if event_at.tzinfo is None:
            return "EVENT_TIMESTAMP_INVALID"
        event_age = (now.astimezone(UTC) - event_at.astimezone(UTC)).total_seconds()
        if event_age < 0:
            return "EVENT_TIMESTAMP_INVALID"
        if event_age > STREAM_IDLE_TIMEOUT_SECONDS:
            return "STALE_EVENT_TIMESTAMP"
    return "FRESH"


def market_open_gate_reason(
    *,
    feed_health: str,
    gap_count: int | None,
    candle_set_valid: bool,
    websocket_state: str,
    recovery_state: str,
    websocket_quote_health_status: str,
) -> str | None:
    """Return a fail-closed reason for new entries; exits do not use this gate."""
    if feed_health != "LIVE" or gap_count != 0 or not candle_set_valid:
        return "MARKET_DATA_INCOMPLETE"
    if websocket_state != "CONNECTED":
        return "MARKET_STREAM_DISCONNECTED"
    if recovery_state != "HEALTHY":
        return "MARKET_RECOVERY_NOT_VALIDATED"
    if websocket_quote_health_status != "FRESH":
        return "MARKET_STREAM_QUOTE_STALE"
    return None


def claim_closed_bar_for_dispatch(close_at: datetime, watermark_path: str | Path) -> tuple[bool, str]:
    """Persist a once-only close watermark before dispatch; skipped bars never replay."""
    path = Path(watermark_path)
    close = close_at.astimezone(UTC)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            _write_watermark(path, close)
            return False, "STARTUP_WATERMARK_SEEDED"
        previous = json.loads(path.read_text(encoding="utf-8"))
        previous_close = datetime.fromisoformat(previous["close_at"])
        if previous_close.tzinfo is None:
            return False, "WATERMARK_INVALID"
        if close <= previous_close.astimezone(UTC):
            return False, "CYCLE_ALREADY_SEEN"
        _write_watermark(path, close)
        return True, "NEW_CLOSED_CYCLE"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "WATERMARK_IO_OR_FORMAT_ERROR"


def _write_watermark(path: Path, close: datetime) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"close_at": close.isoformat()}, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, path)


@dataclass(frozen=True)
class Candle:
    symbol: str
    interval: str
    open_at: str
    close_at: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    trade_count: int | None
    vwap: str | None
    is_closed: bool
    source: str = "ALPACA_US_BTCUSD_SPOT"
    currency: str = "USD_RESEARCH"
    data_basis: str = (
        "Alpaca US spot aggregate; zero-volume bars retain provider quote-derived OHLC"
    )


class CredentialError(RuntimeError):
    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


class ProviderStreamError(RuntimeError):
    def __init__(self, reason: str, code: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class _StreamLease:
    """Best-effort cross-process exclusive lease for this project's provider socket."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.handle = None
        self.failure_reason = "LOCAL_STREAM_LEASE_HELD"

    def acquire(self) -> bool:
        lock_attempted = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("a+b")
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            lock_attempted = True
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.failure_reason = (
                "LOCAL_STREAM_LEASE_HELD" if lock_attempted else "LOCAL_STREAM_LEASE_IO_ERROR"
            )
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class MarketDataAdapter(Protocol):
    """Provider-independent interface; models/UI/ledger consume only normalized BTC/USD-like values."""

    source: str
    symbol: str

    async def bars(
        self, start: datetime, end: datetime, timeframe: str = "15Min"
    ) -> list[Candle]: ...
    async def latest_quote(self) -> dict[str, Any]: ...
    async def orderbook(self) -> dict[str, Any]: ...


def load_market_credentials(
    path: str | Path | None = None,
    *,
    fallback_path: str | Path = DEFAULT_ALPACA_CREDENTIALS_PATH,
) -> tuple[str, str] | None:
    """Read only the two authorized keys into process memory; never log or persist them."""
    configured_path = Path(
        path if path is not None else os.getenv("ALPACA_CREDENTIALS_FILE", str(fallback_path))
    )
    candidates = [configured_path]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        values: dict[str, str] = {}
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key in {"ALPACA_API_KEY", "ALPACA_SECRET_KEY"}:
                values[key] = value.strip().strip("\"'")
        api_key, secret = values.get("ALPACA_API_KEY", ""), values.get("ALPACA_SECRET_KEY", "")
        if api_key and secret:
            return api_key, secret
    return None


class AlpacaMarketData:
    source = "ALPACA_US_BTCUSD_SPOT"
    symbol = "BTC/USD"

    def __init__(
        self,
        credentials: tuple[str, str] | None = None,
        *,
        stream_lock_path: str | Path | None = None,
        recovery_history_path: str | Path | None = None,
    ) -> None:
        self.credentials = credentials if credentials is not None else load_market_credentials()
        self.stream_lock_path = Path(stream_lock_path) if stream_lock_path else None
        self.recovery_history_path = (
            Path(recovery_history_path) if recovery_history_path else None
        )
        self._manager_guard = threading.Lock()
        self._manager_active = False
        self.status: dict[str, Any] = {
            "rest": "UNKNOWN",
            "websocket": "UNKNOWN",
            "last_error": None,
            "last_quote": None,
            "websocket_error_code": None,
            "websocket_failure_reason": None,
            "websocket_last_error_class": None,
            "websocket_reconnect_count": 0,
            "websocket_last_message_received_at": None,
            "websocket_last_quote_received_at": None,
            "websocket_recovery_state": "HEALTHY",
            "websocket_recovery_round": 0,
            "websocket_recovery_attempts_in_round": 0,
            "websocket_recovery_failed_rounds": 0,
            "websocket_recovery_first_failure_at": None,
            "websocket_recovery_success_at": None,
            "websocket_recovery_cooldown_until": None,
            "provider": "Alpaca US BTC/USD spot",
        }
        self._restore_recovery_state()

    def _restore_recovery_state(self) -> None:
        """Resume a cooldown/protection latch across process restarts."""
        path = self.recovery_history_path
        if path is None or not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            latest = next((json.loads(line) for line in reversed(lines) if line.strip()), None)
            if not isinstance(latest, dict):
                return
            for name in (
                "websocket_recovery_state",
                "websocket_recovery_round",
                "websocket_recovery_attempts_in_round",
                "websocket_recovery_failed_rounds",
                "websocket_recovery_first_failure_at",
                "websocket_recovery_success_at",
                "websocket_recovery_cooldown_until",
                "websocket_error_code",
                "websocket_failure_reason",
                "websocket_last_error_class",
            ):
                if name in latest:
                    self.status[name] = latest[name]
            if self.status["websocket_recovery_state"] == "LONG_PROTECTION":
                self.status["websocket"] = "LONG_PROTECTION"
            elif self.status["websocket_recovery_state"] == "COOLDOWN":
                self.status["websocket"] = "COOLDOWN"
        except (OSError, ValueError, TypeError):
            # Keep startup fail-safe at the normal feed gates; never erase the history file.
            self.status["websocket_recovery_history_error"] = "RESTORE_FAILED"

    def _record_recovery_event(self, event: str, **details: Any) -> None:
        path = self.recovery_history_path
        if path is None:
            return
        record = {
            "at": datetime.now(UTC).isoformat(),
            "event": event,
            "websocket_recovery_state": self.status.get("websocket_recovery_state"),
            "websocket_recovery_round": self.status.get("websocket_recovery_round", 0),
            "websocket_recovery_attempts_in_round": self.status.get(
                "websocket_recovery_attempts_in_round", 0
            ),
            "websocket_recovery_failed_rounds": self.status.get(
                "websocket_recovery_failed_rounds", 0
            ),
            "websocket_recovery_first_failure_at": self.status.get(
                "websocket_recovery_first_failure_at"
            ),
            "websocket_recovery_success_at": self.status.get(
                "websocket_recovery_success_at"
            ),
            "websocket_recovery_cooldown_until": self.status.get(
                "websocket_recovery_cooldown_until"
            ),
            "websocket_error_code": self.status.get("websocket_error_code"),
            "websocket_failure_reason": self.status.get("websocket_failure_reason"),
            "websocket_last_error_class": self.status.get("websocket_last_error_class"),
            **details,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            self.status["websocket_recovery_history_error"] = "APPEND_FAILED"

    def confirm_market_recovered(
        self,
        *,
        quote_fresh: bool,
        candles_complete: bool,
        candles_gap_count: int | None,
    ) -> bool:
        """Clear recovery protection only after fresh WS quote and complete closed bars."""
        if self.status.get("websocket_recovery_state") == "LONG_PROTECTION":
            return False
        if (
            self.status.get("websocket") != "CONNECTED"
            or not quote_fresh
            or not candles_complete
            or candles_gap_count != 0
        ):
            return False
        previous_state = self.status.get("websocket_recovery_state")
        quote = self.status.get("last_quote") or {}
        current_quote_health = websocket_quote_health(
            self.status.get("websocket", "UNKNOWN"),
            self.status.get("websocket_last_quote_received_at"),
            datetime.now(UTC),
            timestamp_valid=bool(quote.get("timestamp_valid")),
            last_quote_event_at=quote.get("event_at"),
        )
        if not quote_fresh or current_quote_health != "FRESH":
            return False
        pending_recovery_state = bool(
            self.status.get("websocket_recovery_round")
            or self.status.get("websocket_recovery_attempts_in_round")
            or self.status.get("websocket_recovery_failed_rounds")
        )
        if previous_state == "HEALTHY" and not pending_recovery_state:
            return True
        self.status.update(
            {
                "websocket_recovery_state": "HEALTHY",
                "websocket_recovery_round": 0,
                "websocket_recovery_attempts_in_round": 0,
                "websocket_recovery_failed_rounds": 0,
                "websocket_recovery_cooldown_until": None,
                "websocket_recovery_success_at": datetime.now(UTC).isoformat(),
                "websocket_recovery_first_failure_at": None,
                "websocket_recovery_consecutive_failures": 0,
                "websocket_error_code": None,
                "websocket_failure_reason": None,
            }
        )
        if previous_state != "HEALTHY" or pending_recovery_state:
            self._record_recovery_event("recovery_succeeded")
        return True

    def clear_recovery_protection(self, reason: str) -> None:
        """Explicit local/operator re-arm; never invoked by the retry loop."""
        if not reason or not reason.strip():
            raise ValueError("A controlled recovery reason is required")
        with self._manager_guard:
            if self._manager_active:
                raise RuntimeError("Stop the existing market manager before manual re-arm")
        self.status.update(
            {
                "websocket_recovery_state": "RECOVERING",
                "websocket_recovery_round": 1,
                "websocket_recovery_attempts_in_round": 0,
                "websocket_recovery_failed_rounds": 0,
                "websocket_recovery_cooldown_until": None,
                "websocket": "RECOVERING",
            }
        )
        self._record_recovery_event("operator_rearm", reason=reason.strip())

    def _headers(self) -> dict[str, str]:
        if not self.credentials:
            raise CredentialError("MODEL/DATA credentials not configured")
        return {"APCA-API-KEY-ID": self.credentials[0], "APCA-API-SECRET-KEY": self.credentials[1]}

    async def bars(self, start: datetime, end: datetime, timeframe: str = "15Min") -> list[Candle]:
        params: dict[str, str | int] = {
            "symbols": self.symbol,
            "timeframe": timeframe,
            "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "limit": 10000,
            "sort": "asc",
        }
        result: list[dict] = []
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                res = await client.get(f"{BASE}/bars", headers=self._headers(), params=params)
                res.raise_for_status()
                body = res.json()
                result.extend(body.get("bars", {}).get(self.symbol, []))
                token = body.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        now = datetime.now(UTC)
        candles = []
        for b in result:
            opened = datetime.fromisoformat(b["t"]).astimezone(UTC)
            closed = opened + timedelta(minutes=int(timeframe.removesuffix("Min")))
            candles.append(
                Candle(
                    self.symbol,
                    timeframe,
                    opened.isoformat(),
                    closed.isoformat(),
                    str(b["o"]),
                    str(b["h"]),
                    str(b["l"]),
                    str(b["c"]),
                    str(b.get("v", 0)),
                    int(b["n"]) if b.get("n") is not None else None,
                    str(b["vw"]) if b.get("vw") is not None else None,
                    closed <= now,
                )
            )
        self.status["rest"] = "PASS"
        return candles

    async def latest_quote(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{BASE}/latest/quotes", headers=self._headers(), params={"symbols": self.symbol}
            )
            res.raise_for_status()
            quote = res.json().get("quotes", {}).get(self.symbol)
        if not quote:
            raise RuntimeError("ALPACA_QUOTE_EMPTY")
        received = datetime.now(UTC)
        event_at = datetime.fromisoformat(quote["t"])
        timing = quote_timing(event_at, received)
        event_at = event_at.astimezone(UTC) if event_at.tzinfo else event_at
        item = {
            "symbol": self.symbol,
            "bid": str(quote["bp"]),
            "ask": str(quote["ap"]),
            "bid_qty": str(quote["bs"]),
            "ask_qty": str(quote["as"]),
            "event_at": event_at.isoformat(),
            "received_at": received.isoformat(),
            **timing,
            "source": self.source,
            "currency": "USD_RESEARCH",
        }
        self.status["last_quote"] = item
        return item

    async def orderbook(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{BASE}/latest/orderbooks",
                headers=self._headers(),
                params={"symbols": self.symbol},
            )
            res.raise_for_status()
            body = res.json().get("orderbooks", {}).get(self.symbol)
        if not body:
            raise RuntimeError("ALPACA_ORDERBOOK_EMPTY")
        received = datetime.now(UTC)
        event_at = datetime.fromisoformat(body["t"])
        timing = quote_timing(event_at, received)
        event_at = event_at.astimezone(UTC) if event_at.tzinfo else event_at
        return {
            "symbol": self.symbol,
            "bids": body.get("b", []),
            "asks": body.get("a", []),
            "event_at": event_at.isoformat(),
            "received_at": received.isoformat(),
            **timing,
            "source": self.source,
        }

    async def websocket(self, on_event, stop: asyncio.Event) -> None:
        with self._manager_guard:
            if self._manager_active:
                return
            self._manager_active = True
        lease = _StreamLease(self.stream_lock_path) if self.stream_lock_path else None
        try:
            if lease is not None and not lease.acquire():
                self.status["websocket"] = "LOCAL_MANAGER_CONFLICT"
                self.status["websocket_failure_reason"] = lease.failure_reason
                return
            await self._run_websocket(on_event, stop)
        finally:
            if lease is not None:
                lease.release()
            with self._manager_guard:
                self._manager_active = False
            if stop.is_set():
                self.status["websocket"] = "STOPPED"

    async def _run_websocket(self, on_event, stop: asyncio.Event) -> None:
        if not self.credentials:
            self.status["websocket"] = "MODEL_NOT_CONFIGURED"
            return
        protected = self.status.get("websocket_recovery_state") == "LONG_PROTECTION"
        import websockets

        recovery_round = int(self.status.get("websocket_recovery_round") or 1)
        attempts_in_round = int(
            self.status.get("websocket_recovery_attempts_in_round") or 0
        )
        failed_rounds = int(self.status.get("websocket_recovery_failed_rounds") or 0)
        if recovery_round < 1:
            recovery_round = 1
        cooldown_until = self.status.get("websocket_recovery_cooldown_until")
        if self.status.get("websocket_recovery_state") == "COOLDOWN" and cooldown_until:
            try:
                remaining = max(
                    0.0,
                    (datetime.fromisoformat(cooldown_until) - datetime.now(UTC)).total_seconds(),
                )
            except (TypeError, ValueError):
                remaining = 0.0
            if remaining:
                await asyncio.sleep(remaining)
            recovery_round += 1
            attempts_in_round = 0
            self.status.update(
                {
                    "websocket_recovery_round": recovery_round,
                    "websocket_recovery_attempts_in_round": 0,
                    "websocket_recovery_state": "RECOVERING",
                    "websocket_recovery_cooldown_until": None,
                    "websocket": "RECOVERING",
                }
            )
            self._record_recovery_event("cooldown_ended")
        while not stop.is_set():
            if not protected and self.status.get("websocket_recovery_state") == "LONG_PROTECTION":
                self.status["websocket"] = "LONG_PROTECTION"
                return
            if not protected and attempts_in_round >= STREAM_MAX_RECOVERY_ATTEMPTS:
                # A process restart may resume exactly at the attempt limit.
                failed_rounds += 1
                self.status["websocket_recovery_failed_rounds"] = failed_rounds
                if failed_rounds >= STREAM_MAX_RECOVERY_ROUNDS:
                    self._enter_long_protection("RECOVERY_ROUNDS_EXHAUSTED")
                    return
                cooldown = STREAM_RECOVERY_COOLDOWNS_SECONDS[
                    min(failed_rounds - 1, len(STREAM_RECOVERY_COOLDOWNS_SECONDS) - 1)
                ]
                cooldown_until = datetime.now(UTC) + timedelta(seconds=cooldown)
                self.status.update(
                    {
                        "websocket": "COOLDOWN",
                        "websocket_recovery_state": "COOLDOWN",
                        "websocket_recovery_cooldown_until": cooldown_until.isoformat(),
                    }
                )
                self._record_recovery_event(
                    "round_cooldown_started", cooldown_seconds=cooldown
                )
                await asyncio.sleep(cooldown)
                recovery_round += 1
                attempts_in_round = 0
                self.status.update(
                    {
                        "websocket": "RECOVERING",
                        "websocket_recovery_state": "RECOVERING",
                        "websocket_recovery_round": recovery_round,
                        "websocket_recovery_attempts_in_round": 0,
                        "websocket_recovery_cooldown_until": None,
                    }
                )
                self._record_recovery_event("cooldown_ended")
                continue
            if self.status.get("websocket_recovery_state") != "HEALTHY":
                if recovery_round < 1:
                    recovery_round = 1
                attempts_in_round += 1
                self.status.update(
                    {
                        "websocket_recovery_round": recovery_round,
                        "websocket_recovery_attempts_in_round": attempts_in_round,
                    }
                )
            try:
                self.status["websocket"] = "CONNECTING"
                self.status["last_quote"] = None
                self.status["websocket_last_quote_received_at"] = None
                async with websockets.connect(WS_URL, ping_interval=20, close_timeout=3) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "action": "auth",
                                "key": self.credentials[0],
                                "secret": self.credentials[1],
                            }
                        )
                    )
                    auth_reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    auth_events = auth_reply if isinstance(auth_reply, list) else [auth_reply]
                    self.status["websocket_auth_reply_types"] = [x.get("T") for x in auth_events]
                    auth_error = next((x for x in auth_events if x.get("T") == "error"), None)
                    if auth_error:
                        self.status["websocket_auth_error_code"] = auth_error.get("code")
                        self.status["websocket_error_code"] = auth_error.get("code")
                    # Alpaca's successful auth acknowledgement is provider-defined; the sole
                    # message after the auth command is authoritative even if `msg` wording varies.
                    if not any(x.get("T") == "success" for x in auth_events):
                        self.status["websocket_failure_reason"] = "AUTHENTICATION_REJECTED"
                        raise CredentialError(
                            "WEBSOCKET_AUTH_REJECTED",
                            code=(auth_error.get("code") if auth_error else None),
                        )
                    self.status["websocket"] = "AUTHENTICATED"
                    await ws.send(
                        json.dumps(
                            {"action": "subscribe", "quotes": [self.symbol], "bars": [self.symbol]}
                        )
                    )
                    self.status["websocket"] = "SUBSCRIBING"
                    sub_events = []
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        sub_reply = json.loads(
                            await asyncio.wait_for(
                                ws.recv(), timeout=max(0.1, deadline - time.monotonic())
                            )
                        )
                        sub_events = sub_reply if isinstance(sub_reply, list) else [sub_reply]
                        self.status["websocket_sub_reply_types"] = [x.get("T") for x in sub_events]
                        for event in sub_events:
                            if event.get("T") in {"q", "b", "u"}:
                                await on_event(event)
                        if any(x.get("T") == "subscription" for x in sub_events):
                            break
                        if any(x.get("T") == "error" for x in sub_events):
                            code = next(x.get("code") for x in sub_events if x.get("T") == "error")
                            self.status["websocket_sub_error_code"] = code
                            self.status["websocket_error_code"] = code
                            self.status["websocket_failure_reason"] = _subscription_failure_reason(
                                code
                            )
                            if code == 406:
                                self.status["websocket"] = "CONNECTION_LIMIT_BLOCKED"
                            raise ProviderStreamError(
                                _subscription_failure_reason(code), code=code
                            )
                    if not any(x.get("T") == "subscription" for x in sub_events):
                        raise ProviderStreamError("WEBSOCKET_SUBSCRIPTION_UNCONFIRMED")
                    self.status["websocket"] = "CONNECTED"
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=STREAM_IDLE_TIMEOUT_SECONDS)
                        self.status["websocket_last_message_received_at"] = datetime.now(
                            UTC
                        ).isoformat()
                        for event in json.loads(raw):
                            if event.get("T") in {"q", "b", "u", "error"}:
                                if event.get("T") == "q":
                                    event["source"] = self.source
                                    event["currency"] = "USD_RESEARCH"
                                    event_at = datetime.fromisoformat(event["t"])
                                    timing = quote_timing(event_at, datetime.now(UTC))
                                    try:
                                        bid, ask = float(event.get("bp", 0)), float(event.get("ap", 0))
                                        valid_price = event.get("S") == self.symbol and math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask
                                    except (TypeError, ValueError, OverflowError):
                                        valid_price = False
                                    timing["timestamp_valid"] = timing["timestamp_valid"] and valid_price
                                    event["event_at"] = (
                                        event_at.astimezone(UTC).isoformat()
                                        if event_at.tzinfo
                                        else event_at.isoformat()
                                    )
                                    event.update(timing)
                                    if timing["timestamp_valid"]:
                                        if self.status.get("websocket_recovery_state") not in {"HEALTHY", "LONG_PROTECTION"}:
                                            was_validating = (
                                                self.status.get("websocket_recovery_state")
                                                == "VALIDATING"
                                            )
                                            self.status["websocket_recovery_state"] = "VALIDATING"
                                            if not was_validating:
                                                self._record_recovery_event("fresh_quote_received")
                                    self.status["last_quote"] = event
                                    self.status["websocket_last_quote_received_at"] = datetime.now(
                                        UTC
                                    ).isoformat()
                                    if protected and timing["timestamp_valid"]:
                                        if websocket_quote_health("CONNECTED", self.status["websocket_last_quote_received_at"], datetime.now(UTC), timestamp_valid=True, last_quote_event_at=event["event_at"]) == "FRESH":
                                            self.status["websocket_recovery_state"] = "VALIDATING"
                                            self._record_recovery_event("fresh_quote_received")
                                            protected = False
                                elif event.get("T") == "error":
                                    code = event.get("code")
                                    self.status["websocket_error_code"] = code
                                    self.status["websocket_failure_reason"] = (
                                        _subscription_failure_reason(code)
                                    )
                                    raise ProviderStreamError(
                                        _subscription_failure_reason(code), code=code
                                    )
                                await on_event(event)
            except asyncio.CancelledError:
                if stop.is_set():
                    self.status["websocket"] = "STOPPED"
                raise
            except Exception as exc:
                if protected:
                    self.status["websocket_last_error_class"] = type(exc).__name__
                    category, code = _classify_websocket_failure(exc)
                    self.status["websocket"] = "CONNECTION_LIMIT_BLOCKED" if code == 406 else "LONG_PROTECTION"
                    self.status["websocket_failure_reason"] = category
                    self.status["websocket_error_code"] = code
                    self._record_recovery_event("protected_connection_failed")
                    if category == "CONNECTION_TIMEOUT":
                        await asyncio.sleep(STREAM_RETRY_BACKOFF_SECONDS[min(max(0, attempts_in_round - 1), len(STREAM_RETRY_BACKOFF_SECONDS) - 1)])
                        continue
                    return
                was_connected = self.status.get("websocket") == "CONNECTED"
                was_healthy = self.status.get("websocket_recovery_state") == "HEALTHY"
                self.status["websocket"] = "RECOVERING"
                self.status["websocket_last_error_class"] = type(exc).__name__
                self.status["last_error"] = type(exc).__name__
                self.status["websocket_reconnect_count"] += 1
                category, observed_code = _classify_websocket_failure(exc)
                if observed_code is not None:
                    self.status["websocket_error_code"] = observed_code
                if isinstance(exc, ProviderStreamError):
                    self.status["websocket_failure_reason"] = exc.reason
                else:
                    self.status["websocket_failure_reason"] = category
                if self.status.get("websocket_recovery_first_failure_at") is None:
                    self.status["websocket_recovery_first_failure_at"] = datetime.now(
                        UTC
                    ).isoformat()
                if was_healthy:
                    recovery_round = 1
                    attempts_in_round = 0 if was_connected else 1
                    failed_rounds = 0
                    self.status.update(
                        {
                            "websocket_recovery_round": recovery_round,
                            "websocket_recovery_attempts_in_round": attempts_in_round,
                            "websocket_recovery_failed_rounds": 0,
                        }
                    )
                self.status["websocket_recovery_state"] = "RECOVERING"
                self._record_recovery_event(
                    "connection_attempt_failed",
                    error_category=category,
                    error_class=type(exc).__name__,
                    provider_error_code=observed_code,
                )
                if isinstance(exc, CredentialError):
                    self.status["websocket"] = "AUTH_FAILED"
                    self._enter_long_protection("AUTHENTICATION_FAILED")
                    return
                if observed_code is not None and 400 <= observed_code < 500:
                    if observed_code in {401, 402}:
                        self.status["websocket"] = "AUTH_FAILED"
                    elif observed_code == 406:
                        self.status["websocket"] = "CONNECTION_LIMIT_BLOCKED"
                    else:
                        self.status["websocket"] = "SERVICE_REJECTED"
                    self._enter_long_protection("SERVICE_REJECTED")
                    return
                if attempts_in_round < STREAM_MAX_RECOVERY_ATTEMPTS:
                    await asyncio.sleep(
                        STREAM_RETRY_BACKOFF_SECONDS[
                            min(max(0, attempts_in_round - 1), len(STREAM_RETRY_BACKOFF_SECONDS) - 1)
                        ]
                    )
        if stop.is_set():
            self.status["websocket"] = "STOPPED"


class BinanceMarketData(AlpacaMarketData):
    """Binance USDⓈ-M public market data, normalized to the legacy paper contract.

    Strategy and ledger code continue to consume the same BTC/USD quote and Candle
    fields. Binance's BTCUSDT prices are carried as a USD-equivalent research basis;
    no Binance order or account endpoint is used.
    """

    source = "BINANCE_USDM_PERP"
    symbol = "BTC/USD"
    provider_symbol = "BTCUSDT"
    rest_base = "https://fapi.binance.com"
    stream_url = (
        "wss://fstream.binance.com/stream?streams="
        "btcusdt@bookTicker/btcusdt@kline_15m"
    )

    def __init__(self, *, stream_lock_path=None, recovery_history_path=None):
        super().__init__(credentials=("public-market-data", "no-auth"),
                         stream_lock_path=stream_lock_path,
                         recovery_history_path=recovery_history_path)
        self.status["provider"] = "Binance USDⓈ-M BTCUSDT public market data"
        self.status["market_type"] = "USDⓈ-M perpetual"

    async def bars(self, start: datetime, end: datetime, timeframe: str = "15Min") -> list[Candle]:
        interval = {"1Min": "1m", "5Min": "5m", "15Min": "15m", "1Hour": "1h"}.get(timeframe)
        if interval is None:
            raise ValueError(f"UNSUPPORTED_BINANCE_INTERVAL:{timeframe}")
        step_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}[interval]
        cursor = int(start.astimezone(UTC).timestamp() * 1000)
        finish = int(end.astimezone(UTC).timestamp() * 1000)
        rows: list[list[Any]] = []
        async with httpx.AsyncClient(timeout=15) as client:
            while cursor < finish:
                response = await client.get(
                    f"{self.rest_base}/fapi/v1/klines",
                    params={"symbol": self.provider_symbol, "interval": interval,
                            "startTime": cursor, "endTime": finish, "limit": 1500},
                )
                response.raise_for_status()
                batch = response.json()
                if not isinstance(batch, list):
                    raise RuntimeError("BINANCE_KLINES_BAD_RESPONSE")
                if not batch:
                    break
                rows.extend(batch)
                next_cursor = int(batch[-1][0]) + step_minutes * 60_000
                if next_cursor <= cursor:
                    raise RuntimeError("BINANCE_KLINES_PAGINATION_STALLED")
                cursor = next_cursor
                if len(batch) < 1500:
                    break
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        candles = []
        for row in rows:
            opened_ms = int(row[0])
            close_ms = int(row[6]) + 1
            opened = datetime.fromtimestamp(opened_ms / 1000, UTC)
            closed = datetime.fromtimestamp(close_ms / 1000, UTC)
            candles.append(Candle(
                self.symbol, timeframe, opened.isoformat(), closed.isoformat(),
                str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]),
                int(row[8]), str(row[7]), close_ms <= now_ms,
                source=self.source, currency="USD_RESEARCH",
                data_basis="Binance USDⓈ-M BTCUSDT kline; USDT treated as USD-equivalent for legacy paper ledger",
            ))
        self.status["rest"] = "PASS"
        return candles

    async def latest_quote(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.rest_base}/fapi/v1/ticker/bookTicker",
                                        params={"symbol": self.provider_symbol})
            response.raise_for_status()
            row = response.json()
        received = datetime.now(UTC)
        event_at = received  # REST bookTicker has no exchange event timestamp.
        timing = quote_timing(event_at, received)
        item = {"symbol": self.symbol, "provider_symbol": self.provider_symbol,
                "bid": str(row["bidPrice"]), "ask": str(row["askPrice"]),
                "bid_qty": str(row["bidQty"]), "ask_qty": str(row["askQty"]),
                "event_at": event_at.isoformat(), "received_at": received.isoformat(),
                **timing, "source": self.source, "currency": "USD_RESEARCH",
                "provider_currency": "USDT", "timestamp_basis": "local_receive_time_rest_bookTicker"}
        self.status["last_quote"] = item
        self.status["rest"] = "PASS"
        return item

    async def orderbook(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.rest_base}/fapi/v1/depth",
                                        params={"symbol": self.provider_symbol, "limit": 5})
            response.raise_for_status()
            row = response.json()
        received = datetime.now(UTC)
        event_at = received  # Binance depth REST snapshot has no exchange event timestamp.
        return {"symbol": self.symbol, "bids": row.get("bids", []), "asks": row.get("asks", []),
                "event_at": event_at.isoformat(), "received_at": received.isoformat(),
                **quote_timing(event_at, received), "source": self.source}

    async def _run_websocket(self, on_event, stop: asyncio.Event) -> None:
        import websockets
        self.status["websocket"] = "CONNECTING"
        while not stop.is_set():
            try:
                async with websockets.connect(self.stream_url, ping_interval=20, close_timeout=3) as ws:
                    self.status["websocket"] = "CONNECTED"
                    async for raw in ws:
                        if stop.is_set():
                            break
                        envelope = json.loads(raw)
                        event = envelope.get("data", {})
                        stream = envelope.get("stream", "")
                        received = datetime.now(UTC)
                        self.status["websocket_last_message_received_at"] = received.isoformat()
                        if "@bookTicker" in stream:
                            event_at = datetime.fromtimestamp(int(event["E"]) / 1000, UTC)
                            normalized = {"T": "q", "S": self.symbol, "t": event_at.isoformat(),
                                          "bp": event["b"], "ap": event["a"],
                                          "bs": event["B"], "as": event["A"],
                                          "source": self.source, "currency": "USD_RESEARCH"}
                            normalized.update(quote_timing(event_at, received))
                            self.status["last_quote"] = normalized
                            self.status["websocket_last_quote_received_at"] = received.isoformat()
                            await on_event(normalized)
                        elif "@kline_15m" in stream:
                            k = event.get("k", {})
                            opened = datetime.fromtimestamp(int(k["t"]) / 1000, UTC)
                            closed = datetime.fromtimestamp((int(k["T"]) + 1) / 1000, UTC)
                            await on_event({"T": "b", "S": self.symbol,
                                "bar": {"symbol": self.symbol, "interval": "15Min",
                                         "open_at": opened.isoformat(), "close_at": closed.isoformat(),
                                         "open": k["o"], "high": k["h"], "low": k["l"],
                                         "close": k["c"], "volume": k["v"],
                                         "trade_count": int(k["n"]), "vwap": None,
                                         "is_closed": bool(k["x"]), "source": self.source,
                                         "currency": "USD_RESEARCH"}})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status["websocket"] = "RECONNECTING"
                self.status["last_error"] = type(exc).__name__
                await asyncio.sleep(2)
        self.status["websocket"] = "STOPPED"

    def _enter_long_protection(self, reason: str) -> None:
        self.status["websocket_recovery_state"] = "LONG_PROTECTION"
        self.status["websocket"] = (
            self.status.get("websocket")
            if self.status.get("websocket") in {"AUTH_FAILED", "CONNECTION_LIMIT_BLOCKED", "SERVICE_REJECTED"}
            else "LONG_PROTECTION"
        )
        self.status["websocket_protection_reason"] = reason
        self.status["websocket_recovery_cooldown_until"] = None
        self._record_recovery_event("long_protection_entered", protection_reason=reason)


def _subscription_failure_reason(code: Any) -> str:
    return {
        400: "INVALID_SUBSCRIPTION",
        401: "NOT_AUTHENTICATED",
        402: "AUTHENTICATION_REJECTED",
        405: "SYMBOL_LIMIT_EXCEEDED",
        406: "CONNECTION_LIMIT_EXCEEDED",
        409: "SUBSCRIPTION_NOT_ENTITLED",
        410: "CHANNEL_UNAVAILABLE",
    }.get(code, "PROVIDER_SUBSCRIPTION_REJECTED")


def _websocket_failure_reason(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "STREAM_IDLE_TIMEOUT"
    if isinstance(exc, OSError):
        return "TRANSPORT_DISCONNECTED"
    return "STREAM_PROTOCOL_OR_PROVIDER_ERROR"


def _classify_websocket_failure(exc: Exception) -> tuple[str, int | None]:
    """Classify from exception type and only an HTTP status actually exposed by it."""
    code = None
    if isinstance(exc, (ProviderStreamError, CredentialError)):
        code = getattr(exc, "code", None)
    else:
        response = getattr(exc, "response", None)
        candidate = getattr(response, "status_code", None)
        if not isinstance(candidate, int):
            candidate = getattr(exc, "status_code", None)
        code = candidate if isinstance(candidate, int) else None
    if isinstance(code, str) and code.isdecimal():
        code = int(code)
    if not isinstance(code, int):
        code = None
    if isinstance(exc, CredentialError):
        return "AUTHENTICATION_FAILURE", code
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "CONNECTION_TIMEOUT", code
    if isinstance(exc, ProviderStreamError):
        if code in {401, 402}:
            return "AUTHENTICATION_FAILURE", code
        if code == 406:
            return "CONNECTION_LIMIT", code
        if code == 407:
            return "PROXY_AUTH_REJECTED", code
        if code is not None:
            return "SUBSCRIPTION_REJECTED", code
        return "SUBSCRIPTION_FAILURE", None
    if code in {401, 402}:
        return "AUTHENTICATION_FAILURE", code
    if code == 406:
        return "CONNECTION_LIMIT", code
    if code == 407:
        return "PROXY_AUTH_REJECTED", code
    if code is not None and 400 <= code < 500:
        return "SERVER_REJECTED", code
    if isinstance(exc, (OSError, ConnectionError)):
        return "NETWORK_ERROR", code
    if isinstance(exc, RuntimeError) and "SUBSCRIPTION" in str(exc).upper():
        return "SUBSCRIPTION_FAILURE", code
    return "STREAM_PROTOCOL_OR_PROVIDER_ERROR", code


def candle_hash(candles: list[Candle]) -> str:
    payload = json.dumps([asdict(c) for c in candles], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()

