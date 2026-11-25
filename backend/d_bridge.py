"""Isolated adapter between the existing market feed and DRuntime.

This module does not open a market connection or wire itself to production.
Callers must explicitly start it and supply trustworthy live health callbacks.
"""
import asyncio
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone

from backend.d_dispatch import DDispatcher
from backend.d_executor import validate_quote
from backend.d_runtime import DRuntime
from backend.d_paper import read_status
from backend.d_status import read_execution_account


class _EntryGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.blocked = threading.Event()
        self.epoch = 0

    def invalidate(self):
        # Never wait for ledger I/O on the shared event loop. A transaction
        # already holding the lock precedes this invalidation; otherwise it
        # observes the latch before it can begin the entry transaction.
        self.epoch += 1
        self.blocked.set()

    def rearm(self, epoch):
        with self.lock:
            if self.epoch != epoch:
                return False
            self.blocked.clear()
            if self.epoch != epoch:
                self.blocked.set()
                return False
            return True

    def commit(self, db):
        # The only entry linearization point is the final SQLite commit.
        # Everything before this point is rolled back on invalidation.
        with self.lock:
            if self.blocked.is_set():
                return False
            db.commit()
            return True


class DBridge:
    def __init__(self, observation_path, ledger_path, archive, *, feed_healthy,
                 candles_complete, clock=None, max_pending=1024):
        self.observation_path = observation_path
        self.ledger_path = ledger_path
        self.archive = archive
        self.feed_healthy = feed_healthy
        self.candles_complete = candles_complete
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_pending = max_pending
        self.runtime = None
        self.dispatcher = None
        self.enabled = True
        self._watchdog_task = None
        self._stream_down = True
        self._entry_gate = _EntryGate()
        self._fault_persisting = 0
        self._gap_quotes = []
        self._last_verified_quote = None

    async def start(self):
        if self.runtime is not None:
            raise RuntimeError('D_BRIDGE_ALREADY_STARTED')
        runtime = await asyncio.to_thread(DRuntime, self.observation_path, self.ledger_path, self.archive)
        runtime.entry_gate = self._entry_gate
        try:
            if await asyncio.to_thread(runtime.has_data_loss):
                self._entry_gate.invalidate()
            dispatcher = DDispatcher(self._handle, max_pending=self.max_pending,
                                     on_failure=runtime.mark_dispatch_failure,
                                     invalidate=self._entry_gate.invalidate)
            self.runtime = runtime
            self.dispatcher = dispatcher
            dispatcher.start()
            self._watchdog_task = asyncio.create_task(self._watch_feed())
        except BaseException:
            self.runtime = None
            self.dispatcher = None
            await asyncio.to_thread(runtime.close)
            raise

    @staticmethod
    def _event(quote):
        """Derive a stable identity only from original market event fields."""
        data = dict(quote)
        if not data.get('event_id'):
            fields = ('symbol', 'source', 'event_at', 'bid', 'ask')
            if not all(data.get(field) is not None for field in fields):
                raise ValueError('D_QUOTE_ID_INPUT_MISSING')
            material = '|'.join(str(data[field]) for field in fields)
            data['event_id'] = 'dquote:' + hashlib.sha256(material.encode('utf8')).hexdigest()
        return data

    def submit_quote(self, quote):
        if self.dispatcher is None:
            return False
        return self.dispatcher.submit('quote', self._event(quote))

    def submit_bar(self, close_at):
        if self.dispatcher is None:
            return False
        return self.dispatcher.submit('bar', close_at)

    def set_enabled(self, enabled):
        """Pause/resume new entries without stopping quote-based exits."""
        enabled = bool(enabled)
        if enabled == self.enabled:
            return {'enabled': self.enabled, 'changed': False}
        if not enabled:
            # Block the entry commit before expiring any queued candidate.
            self.enabled = False
            self._entry_gate.invalidate()
            if self.runtime is not None:
                self.runtime.cancel_pending_entry()
            return {'enabled': False, 'changed': True}

        self.enabled = True
        epoch = self._entry_gate.epoch
        if (self.dispatcher is not None and self.dispatcher.failure is None
                and not self._fault_persisting and self._feed_ready()
                and self.dispatcher._queue.empty() and self.runtime is not None
                and self.runtime.status()['protection'] == 'READY'
                and not self.runtime.has_data_loss()):
            self._entry_gate.rearm(epoch)
        return {'enabled': True, 'changed': True}

    def fail_dispatch(self, reason):
        """Immediately latch entry failure and persist protection asynchronously."""
        self._stream_down = True
        if self.dispatcher is not None:
            self.dispatcher._fail(reason)
        else:
            self._entry_gate.invalidate()

    async def stream_unavailable(self, reason='STREAM_UNAVAILABLE'):
        # Disable opening synchronously before scheduling the durable update.
        self._stream_down = True
        self._gap_quotes = []
        self._last_verified_quote = None
        self._entry_gate.invalidate()
        self._fault_persisting += 1
        try:
            if self.runtime is not None:
                await asyncio.to_thread(self.runtime.mark_stream_unavailable, reason)
        finally:
            self._fault_persisting -= 1

    def stream_validated(self):
        """Caller invokes only after the existing feed reports fresh/healthy."""
        if self.dispatcher is None or self.dispatcher.failure or self._fault_persisting:
            return False
        if not self.feed_healthy() or not self.candles_complete():
            return False
        self._stream_down = False
        return True

    async def _watch_feed(self):
        """Detect disconnect/staleness even when no market events arrive."""
        while True:
            try:
                healthy = bool(self.feed_healthy()) and bool(self.candles_complete())
            except Exception:
                healthy = False
            if not healthy and not self._stream_down:
                await self.stream_unavailable('MARKET_STREAM_UNAVAILABLE')
            elif healthy and self._stream_down:
                self.stream_validated()
            await asyncio.sleep(1)

    def _ready(self):
        return self.enabled and not self._entry_gate.blocked.is_set() and self._feed_ready()

    def _feed_ready(self):
        return (not self._stream_down and not self._fault_persisting and self.dispatcher is not None
                and self.dispatcher.failure is None and bool(self.feed_healthy())
                and bool(self.candles_complete()))

    def _handle(self, kind, value):
        now = self.clock()
        if kind == 'bar':
            if not self.enabled:
                return {'status': 'PAUSED', 'reason': 'STRATEGY_PAUSED'}
            return self.runtime.on_bar(value, now, market_ready=self._ready())
        if kind == 'quote':
            ready = self._feed_ready()
            if not ready:
                self._gap_quotes = []
            recovery_epoch = self._entry_gate.epoch
            position_before_quote = bool(self.runtime.ledger.account()['position'])
            if ready and self.runtime.status()['protection'] != 'READY':
                self.runtime.confirm_stream_recovered(value, now, market_ready=True,
                                                      candles_complete=bool(self.candles_complete()))
            result = self.runtime.on_quote(value, now, market_ready=ready)
            if ready and validate_quote(value, now):
                self._last_verified_quote = dict(value)
            if position_before_quote or result.get('reason') == 'DUPLICATE_OR_OUT_OF_ORDER_QUOTE':
                self._gap_quotes = []
            elif ready and self._entry_gate.blocked.is_set() and self.runtime.has_data_loss():
                self._observe_gap_recovery(value, now, recovery_epoch)
            if (ready and self.enabled and self._entry_gate.blocked.is_set() and self.dispatcher.failure is None
                    and self.dispatcher._queue.empty() and self._feed_ready()
                    and self.runtime.status()['protection'] == 'READY'):
                self._entry_gate.rearm(recovery_epoch)
            return result
        raise ValueError('INVALID_D_EVENT')

    def _observe_gap_recovery(self, quote, now, epoch):
        # This establishes a healthy recent stream, not historical tick completeness.
        if (not validate_quote(quote, now) or self.runtime.ledger.account()['position']
                or not self._feed_ready() or self.dispatcher.failure):
            self._gap_quotes = []
            return
        event = datetime.fromisoformat(quote['event_at'].replace('Z', '+00:00')).astimezone(timezone.utc)
        with sqlite3.connect(self.runtime.ledger.path, timeout=10) as db:
            last = db.execute('SELECT event_at FROM d_quotes ORDER BY seq DESC LIMIT 1').fetchone()
        if not last or event <= datetime.fromisoformat(last[0].replace('Z', '+00:00')).astimezone(timezone.utc):
            self._gap_quotes = []
            return
        if not self.runtime.accept_gap_quote(event.isoformat()):
            self._gap_quotes = []
            return
        if self._gap_quotes:
            delta = (event - self._gap_quotes[-1]).total_seconds()
            if delta <= 0 or delta > 10:
                self._gap_quotes = []
        self._gap_quotes.append(event)
        self._gap_quotes = self._gap_quotes[-20:]
        if (len(self._gap_quotes) < 3 or (event - self._gap_quotes[0]).total_seconds() < 10
                or not self.dispatcher._queue.empty() or self._entry_gate.epoch != epoch
                or not self._feed_ready()):
            return
        if self.runtime.recover_gap_after_flat(now, self._gap_quotes[0].isoformat(),
                                               event.isoformat(), len(self._gap_quotes)):
            self._gap_quotes = []

    async def status(self, quote=None):
        if self.runtime is None or self.dispatcher is None:
            return {'status': 'UNAVAILABLE', 'reason': 'D_BRIDGE_NOT_STARTED', 'execution_ready': False}
        from backend.d_signal import RULE_VERSION, SOURCE_COMMIT
        runtime = await asyncio.to_thread(self.runtime.status)
        account = await asyncio.to_thread(read_execution_account, self.ledger_path, quote, self.clock())
        observation = await asyncio.to_thread(read_status, self.observation_path)
        failure = self.dispatcher.failure
        # Use the exact quote snapshot rendered by this status request for both
        # valuation and circuit checks. The previously dispatched WS quote may
        # be older even while the shared market layer has a newer validated quote.
        risk_quote = self._event(quote) if isinstance(quote, dict) else None
        risk = await asyncio.to_thread(self.runtime.circuit_status, risk_quote, self.clock())
        reason = failure or runtime['reason'] or (risk.get('reason') if risk.get('block_entries') else None)
        ready = (self.enabled and self._ready() and runtime['protection'] == 'READY' and not failure
                 and not risk.get('block_entries', True)
                 and account['status'] == 'READY'
                 and account.get('reason') != 'VALUATION_UNAVAILABLE')
        status = 'PAUSED' if not self.enabled else 'AUTO_PAPER' if ready else 'PROTECTION_UNCERTAIN'
        reason = 'STRATEGY_PAUSED' if not self.enabled else reason
        return {**account, 'status': status, 'enabled': self.enabled,
                'reason': reason, 'execution_ready': ready,
                'circuit': risk,
                'control_state': 'PAUSED' if not self.enabled else 'RUNNING' if ready else 'PROTECTION_UNCERTAIN',
                'rule_version': RULE_VERSION, 'source_commit': SOURCE_COMMIT,
                'decision': observation.get('decision'), 'signal': observation.get('signal'),
                'decision_state': runtime['state']}

    async def close(self):
        self._stream_down = True
        self._entry_gate.invalidate()
        dispatcher, runtime = self.dispatcher, self.runtime
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
            self._watchdog_task = None
        if runtime is not None:
            await asyncio.to_thread(runtime.mark_stream_unavailable, 'BRIDGE_SHUTDOWN')
        if dispatcher is not None:
            await dispatcher.close()
        if runtime is not None:
            await asyncio.to_thread(runtime.close)
        self.dispatcher = None
        self.runtime = None
