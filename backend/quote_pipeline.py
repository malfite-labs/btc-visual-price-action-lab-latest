"""Bounded, protection-preserving quote pipeline, ActivePositionIndex & anti-stale-overwrite contract.

Fixes WebSocket @bookTicker consumer backpressure without loosening COST_MODEL.max_quote_age_ms (2000ms)
and without creating a second stop-loss/take-profit calculation engine:
- Unified `accept_quote_update()` prevents stale or older WS/REST quotes from overwriting fresher valid quotes.
- Lightweight `ActivePositionIndex` (aliased as `ProtectionWatchlist` for full backward compatibility)
  reuses `backend.domain.stop_triggered()` in memory (<0.05ms) to pinpoint ONLY strategies that:
    (a) have a stop/target touched by the incoming quote,
    (b) have a pending_entry, pending_exit, or protection recovery / fault state, or
    (c) have an open position whose staggered mark-price persistence interval is due.
  Flat idle strategies incur ZERO SQLite I/O on incoming quotes.
- `BoundedQuoteDispatcher` routes protection-triggering quotes into a bounded FIFO `protection_queue`
  (never coalesced away!) and coalesces routine non-triggering quotes into a 1-slot `latest_quote`.
- If `protection_queue` ever overflows, `overload_blocked` latches True so `common_market_guard`
  blocks new entries until the backlog clears (zero silent drop).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D, InvalidOperation
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple

from backend.domain import stop_triggered

UTC = timezone.utc


def _parse_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(UTC) if dt.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def live_quote_age_ms(quote: Optional[Dict[str, Any]], *, now: Optional[datetime] = None) -> int:
    if not isinstance(quote, dict):
        return 10**9
    if quote.get("timestamp_valid") is not True:
        return 10**9
    rec_dt = _parse_utc(quote.get("received_at"))
    if rec_dt is None:
        return 10**9
    cur = (now or datetime.now(UTC)).astimezone(UTC)
    if rec_dt > cur:
        return 10**9
    try:
        base_age = int(quote.get("age_ms", 10**9))
    except (TypeError, ValueError):
        return 10**9
    if base_age < 0:
        return 10**9
    elapsed = int((cur - rec_dt).total_seconds() * 1000)
    return base_age + max(0, elapsed)


def accept_quote_update(
    current: Optional[Dict[str, Any]],
    incoming: Optional[Dict[str, Any]],
    *,
    max_age_ms: int = 2000,
    now: Optional[datetime] = None,
) -> Tuple[Optional[Dict[str, Any]], bool, str]:
    """Unified WS/REST quote acceptance contract.

    Returns (resulting_quote, accepted_bool, reason_str).
    Invariants:
    - Never substitutes received_at for event_at.
    - Never allows an invalid or already-stale (>max_age_ms) quote to overwrite a fresh valid quote.
    - Never allows an older event_at to regress a fresher valid quote (WS-over-REST or out-of-order WS).
    - Allows WS reconnect / sequence reset seamlessly whenever event_at >= current.event_at and fresh.
    """
    if not isinstance(incoming, dict):
        return current, False, "INCOMING_NONE"

    cur_now = (now or datetime.now(UTC)).astimezone(UTC)
    inc_age = live_quote_age_ms(incoming, now=cur_now)
    inc_fresh = incoming.get("timestamp_valid") is True and 0 <= inc_age <= max_age_ms
    inc_ev = _parse_utc(incoming.get("event_at"))

    cur_age = live_quote_age_ms(current, now=cur_now) if current else 10**9
    cur_fresh = bool(current and current.get("timestamp_valid") is True and 0 <= cur_age <= max_age_ms)
    cur_ev = _parse_utc(current.get("event_at")) if current else None

    if not inc_fresh:
        if cur_fresh:
            return current, False, "STALE_OVERWRITE_BLOCKED"
        # Neither current nor incoming is fresh: keep diagnostic visibility of latest failure/staleness
        return incoming, False, "INCOMING_STALE_OR_INVALID"

    if cur_fresh and cur_ev is not None and inc_ev is not None and inc_ev < cur_ev:
        return current, False, "REGRESSIVE_EVENT_TIME_BLOCKED"

    return incoming, True, "ACCEPTED"


@dataclass(frozen=True)
class WatchEntry:
    strategy_id: str
    direction: str  # "LONG" or "SHORT" or "PENDING"
    stop: Optional[D]
    target: Optional[D]
    pending: bool = False
    fault: bool = False


class ActivePositionIndex:
    """Lightweight in-memory active position & pending-event index reusing backend.domain.stop_triggered.

    Supports:
    - O(active_positions) trigger detection (<0.05ms) without touching SQLite for flat strategies.
    - Exact strategy_id targeting (`matching_strategies()`) so `process_stop_quote` only visits
      strategies that actually need immediate protection/execution or staggered mark-price updates.
    - Atomic per-strategy `upsert()` and `remove()` for dynamic registration, pause, close, and unload.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: Tuple[WatchEntry, ...] = ()
        self._by_id: Dict[str, WatchEntry] = {}
        self._pending_ids: Set[str] = set()
        self._has_pending: bool = False
        self._last_mark_mono: Dict[str, float] = {}
        self.zero_io_skips: int = 0

    def update(
        self,
        entries: Iterable[WatchEntry],
        *,
        has_pending: bool = False,
        pending_ids: Optional[Iterable[str]] = None,
    ) -> None:
        ent_list = tuple(entries)
        by_id = {e.strategy_id: e for e in ent_list}
        p_ids = {e.strategy_id for e in ent_list if e.pending or e.fault}
        if pending_ids:
            p_ids.update(str(x) for x in pending_ids if x)
        with self._lock:
            self._entries = ent_list
            self._by_id = by_id
            self._pending_ids = p_ids
            self._has_pending = bool(has_pending) or bool(p_ids)
            # Clean up mark timestamps for strategies no longer holding
            stale_keys = [k for k in self._last_mark_mono if k not in by_id]
            for k in stale_keys:
                self._last_mark_mono.pop(k, None)

    def upsert(self, entry: WatchEntry) -> None:
        with self._lock:
            self._by_id[entry.strategy_id] = entry
            if entry.pending or entry.fault:
                self._pending_ids.add(entry.strategy_id)
            else:
                self._pending_ids.discard(entry.strategy_id)
            self._entries = tuple(self._by_id.values())
            self._has_pending = bool(self._pending_ids)

    def remove(self, strategy_id: str) -> None:
        sid = str(strategy_id)
        with self._lock:
            self._by_id.pop(sid, None)
            self._pending_ids.discard(sid)
            self._last_mark_mono.pop(sid, None)
            self._entries = tuple(self._by_id.values())
            self._has_pending = bool(self._pending_ids)

    def has_strategy(self, strategy_id: str) -> bool:
        with self._lock:
            return str(strategy_id) in self._by_id or str(strategy_id) in self._pending_ids

    def triggers(self, quote: Dict[str, Any]) -> bool:
        urgent, _ = self.matching_strategies(quote, include_mark_due=False)
        return bool(urgent) or self._has_pending

    def matching_strategies(
        self,
        quote: Dict[str, Any],
        *,
        include_mark_due: bool = True,
        mark_interval_s: float = 5.0,
        now_mono: Optional[float] = None,
    ) -> Tuple[Set[str], Set[str]]:
        """Return (urgent_ids, mark_due_ids) in <0.05ms without touching disk.

        - urgent_ids: stop/target touched OR pending_entry/pending_exit/fault.
        - mark_due_ids: open-position strategies whose staggered mark write interval has elapsed.
        """
        with self._lock:
            entries = self._entries
            urgent: Set[str] = set(self._pending_ids)
        mark_due: Set[str] = set()
        if not entries:
            return urgent, mark_due

        try:
            bid = D(str(quote["bid"]))
            ask = D(str(quote["ask"]))
            if not (bid.is_finite() and ask.is_finite() and 0 < bid <= ask):
                return urgent, mark_due
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return urgent, mark_due

        now_m = time.monotonic() if now_mono is None else now_mono
        for entry in entries:
            if entry.pending or entry.fault:
                urgent.add(entry.strategy_id)
                continue
            hit = False
            if entry.stop is not None and entry.stop > 0:
                hit_s, _, _ = stop_triggered(entry.direction, entry.stop, bid, ask)
                if hit_s:
                    hit = True
            if not hit and entry.target is not None and entry.target > 0:
                if entry.direction == "LONG" and bid >= entry.target:
                    hit = True
                elif entry.direction == "SHORT" and ask <= entry.target:
                    hit = True
            if hit:
                urgent.add(entry.strategy_id)
            elif include_mark_due:
                with self._lock:
                    last_m = self._last_mark_mono.get(entry.strategy_id, 0.0)
                    # Deterministic stagger (0..1.5s) prevents simultaneous SQLite fsync spikes at 100-200 strategies
                    stagger = (hash(entry.strategy_id) % 15) * 0.1
                    if (now_m - last_m) >= (mark_interval_s + stagger):
                        self._last_mark_mono[entry.strategy_id] = now_m
                        mark_due.add(entry.strategy_id)
        return urgent, mark_due

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "watched_positions": len(self._entries),
                "pending_strategies": len(self._pending_ids),
                "has_pending": self._has_pending,
                "zero_io_skips": self.zero_io_skips,
            }


# Backward-compatible alias
ProtectionWatchlist = ActivePositionIndex


class BoundedQuoteDispatcher:
    """Decouples fast WS/REST quote ingestion from slower SQLite protection processing.

    - Protection-triggering quotes go into a bounded FIFO queue (maxsize=64) and are NEVER coalesced away.
    - Routine non-triggering quotes coalesce into a single latest-quote slot.
    - Single background worker task (zero unbounded tasks).
    - Overload latches `overload_blocked = True` so new entries are blocked until drained.
    """

    def __init__(
        self,
        handler: Callable[[Dict[str, Any]], Awaitable[None]],
        *,
        watchlist: Optional[ActivePositionIndex] = None,
        protection_maxsize: int = 64,
        max_age_ms: int = 2000,
    ) -> None:
        self._handler = handler
        self.watchlist = watchlist or ActivePositionIndex()
        self._max_age_ms = max_age_ms
        self._protection_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=protection_maxsize)
        self._latest_routine: Optional[Dict[str, Any]] = None
        self._wakeup = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self.overload_blocked: bool = False

        # Telemetry
        self.received_total: int = 0
        self.coalesced_total: int = 0
        self.protection_enqueued: int = 0
        self.protection_processed: int = 0
        self.routine_processed: int = 0
        self.stale_rejected: int = 0
        self.regressive_rejected: int = 0
        self.overload_events: int = 0
        self._worker_latencies_ms: deque[float] = deque(maxlen=200)
        self._accepted_ages_ms: deque[int] = deque(maxlen=200)

    def start(self, stop_event: asyncio.Event) -> asyncio.Task:
        self._stop = stop_event
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker_loop(), name="quote-protection-worker")
        return self._task

    def submit(
        self,
        current_quote: Optional[Dict[str, Any]],
        incoming_quote: Dict[str, Any],
        *,
        force_protection: bool = False,
        now: Optional[datetime] = None,
    ) -> Tuple[Optional[Dict[str, Any]], bool, str]:
        """Synchronously accept & route a quote in <0.05ms (zero awaits, zero disk I/O)."""
        self.received_total += 1
        new_cur, accepted, reason = accept_quote_update(
            current_quote, incoming_quote, max_age_ms=self._max_age_ms, now=now
        )
        if not accepted:
            if reason in ("STALE_OVERWRITE_BLOCKED", "INCOMING_STALE_OR_INVALID"):
                self.stale_rejected += 1
            elif reason == "REGRESSIVE_EVENT_TIME_BLOCKED":
                self.regressive_rejected += 1
            return new_cur, False, reason

        age = live_quote_age_ms(incoming_quote, now=now)
        self._accepted_ages_ms.append(age)

        is_prot = force_protection or self.watchlist.triggers(incoming_quote)
        if is_prot:
            try:
                self._protection_queue.put_nowait(incoming_quote)
                self.protection_enqueued += 1
            except asyncio.QueueFull:
                self.overload_events += 1
                self.overload_blocked = True
                logging.getLogger(__name__).error(
                    "Protection quote queue full (%d); latching overload_blocked=True",
                    self._protection_queue.maxsize,
                )
        else:
            if self._latest_routine is not None:
                self.coalesced_total += 1
            self._latest_routine = incoming_quote
        self._wakeup.set()
        return new_cur, True, "ENQUEUED_PROTECTION" if is_prot else "COALESCED_ROUTINE"

    async def process_one(self) -> bool:
        """Process next highest-priority quote (protection FIFO first, then latest routine)."""
        item: Optional[Dict[str, Any]] = None
        is_prot = False
        if not self._protection_queue.empty():
            item = self._protection_queue.get_nowait()
            is_prot = True
        elif self._latest_routine is not None:
            item = self._latest_routine
            self._latest_routine = None
        if item is None:
            return False

        t0 = time.perf_counter()
        try:
            await self._handler(item)
        except Exception:
            logging.getLogger(__name__).exception("Quote protection worker failed")
        finally:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self._worker_latencies_ms.append(dt_ms)
            if is_prot:
                self.protection_processed += 1
                self._protection_queue.task_done()
                if self._protection_queue.empty():
                    self.overload_blocked = False
            else:
                self.routine_processed += 1
        return True

    async def _worker_loop(self) -> None:
        while self._stop is None or not self._stop.is_set():
            did_work = await self.process_one()
            if did_work:
                await asyncio.sleep(0)
                continue
            self._wakeup.clear()
            if not self._protection_queue.empty() or self._latest_routine is not None:
                continue
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=0.5)
            except TimeoutError:
                pass

    def stats(self) -> Dict[str, Any]:
        ages = sorted(self._accepted_ages_ms)
        lats = sorted(self._worker_latencies_ms)

        def _pct(arr, p):
            if not arr:
                return None
            idx = min(len(arr) - 1, int(round((len(arr) - 1) * p)))
            return round(arr[idx], 1)

        return {
            "received_total": self.received_total,
            "coalesced_total": self.coalesced_total,
            "protection_enqueued": self.protection_enqueued,
            "protection_processed": self.protection_processed,
            "routine_processed": self.routine_processed,
            "stale_rejected": self.stale_rejected,
            "regressive_rejected": self.regressive_rejected,
            "overload_events": self.overload_events,
            "overload_blocked": self.overload_blocked,
            "protection_queue_depth": self._protection_queue.qsize(),
            "age_ms_p50": _pct(ages, 0.50),
            "age_ms_p95": _pct(ages, 0.95),
            "age_ms_p99": _pct(ages, 0.99),
            "age_ms_max": max(ages) if ages else None,
            "worker_ms_p50": _pct(lats, 0.50),
            "worker_ms_p95": _pct(lats, 0.95),
            "worker_ms_max": round(max(lats), 1) if lats else None,
            "active_index": self.watchlist.stats(),
        }
