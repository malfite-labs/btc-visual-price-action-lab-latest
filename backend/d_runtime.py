"""Serialized D paper orchestration for isolated testing; no app wiring.

The caller provides the existing market feed and invokes this module only when
explicitly armed. Ledger operations own their durable idempotency keys.
"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from backend.d_executor import (IntentStore, enqueue_verified_quote,
                                handoff_observed_intent, process_pending_entry,
                                pending_verified_quote, process_verified_quote, validate_quote)
from backend.d_ledger import DLedger
from backend.d_circuit import DCircuit
from backend.d_circuit_replay import assess_from_committed_ledger
from backend.market import _StreamLease
from backend.d_paper import initialize_store, observe_cycle


class DRuntime:
    def __init__(self, observation_path, ledger_path, archive, *, common_guard=False):
        self.observation_path = Path(observation_path).resolve()
        ledger_path = Path(ledger_path).resolve()
        self.lock = threading.RLock()
        self.entry_gate = None
        self.common_guard = common_guard
        self._leases = []
        self._closed = True
        lock_paths = sorted({str(self.observation_path) + '.runtime.lock', str(ledger_path) + '.runtime.lock'})
        try:
            for path in lock_paths:
                lease = _StreamLease(path)
                if not lease.acquire():
                    raise RuntimeError('D_RUNTIME_LEASE_UNAVAILABLE:' + lease.failure_reason)
                self._leases.append(lease)
            self.ledger = DLedger(ledger_path)
            self.circuit = DCircuit(str(ledger_path) + '.circuit.sqlite')
            self.intents = IntentStore(ledger_path)
            self.archive = archive
            initialize_store(self.observation_path)
            self._initialize_state()
            self._closed = False
        except BaseException:
            for lease in reversed(self._leases):
                lease.release()
            self._leases.clear()
            raise

    def _initialize_state(self):
        # A process may have died before it could persist a lost quote.  An
        # existing position at startup therefore proves a monitoring gap, not
        # that the first later quote describes everything that happened.
        position_at_start = self.ledger.account()['position']
        with sqlite3.connect(self.observation_path, timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS d_runtime_status (id INTEGER PRIMARY KEY CHECK(id=1), protection TEXT NOT NULL, reason TEXT)')
            db.execute("INSERT OR IGNORE INTO d_runtime_status VALUES (1, 'READY', NULL)")
            db.execute('CREATE TABLE IF NOT EXISTS d_data_loss_latch (id INTEGER PRIMARY KEY CHECK(id=1), reason TEXT NOT NULL)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(d_data_loss_latch)')}
            if 'trade_id' not in columns:
                db.execute('ALTER TABLE d_data_loss_latch ADD COLUMN trade_id TEXT')
            db.execute('CREATE TABLE IF NOT EXISTS d_gap_recovery_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, reason TEXT NOT NULL, trade_id TEXT NOT NULL, close_fill_id TEXT NOT NULL, recovered_at TEXT NOT NULL, quote_start TEXT NOT NULL, quote_end TEXT NOT NULL, quote_count INTEGER NOT NULL, history_verified INTEGER NOT NULL CHECK(history_verified=0))')
            db.execute('CREATE TABLE IF NOT EXISTS d_gap_recovery_watermark (id INTEGER PRIMARY KEY CHECK(id=1), event_at TEXT NOT NULL)')
            if position_at_start and not self.common_guard:
                db.execute("INSERT OR IGNORE INTO d_data_loss_latch (id,reason,trade_id) VALUES (1, 'RESTART_POSITION_PRICE_GAP', ?)", (position_at_start['trade_id'],))
                db.execute('UPDATE d_data_loss_latch SET trade_id=COALESCE(trade_id,?) WHERE id=1', (position_at_start['trade_id'],))
                db.execute("UPDATE d_runtime_status SET protection='UNCERTAIN',reason=(SELECT reason FROM d_data_loss_latch WHERE id=1) WHERE id=1")
            if self.common_guard:
                latch = db.execute('SELECT reason,trade_id FROM d_data_loss_latch WHERE id=1').fetchone()
                if latch:
                    if (latch[1] or position_at_start or self.ledger.fills() or self.intents.pending()
                            or pending_verified_quote(self.ledger, datetime.now(timezone.utc),
                                                      allow_expired=True)['status'] != 'EMPTY'):
                        raise RuntimeError('D_LEGACY_EXPOSURE_RECONCILIATION_REQUIRED')
                    for path in (self.observation_path, self.ledger.path, self.circuit.path):
                        with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True) as check:
                            if check.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                                raise RuntimeError('D_LEDGER_INTEGRITY_CHECK_FAILED')
                    db.execute('CREATE TABLE IF NOT EXISTS d_common_guard_migration '
                               '(id INTEGER PRIMARY KEY CHECK(id=1), reason TEXT, migrated_at TEXT)')
                    db.execute('INSERT INTO d_common_guard_migration VALUES (1,?,?)',
                               (latch[0], datetime.now(timezone.utc).isoformat()))
                    db.execute('DELETE FROM d_data_loss_latch WHERE id=1')
                db.execute("UPDATE d_runtime_status SET protection='UNCERTAIN',"
                           "reason='STARTUP_AWAITING_FEED_VALIDATION' WHERE id=1")
            db.execute('CREATE TABLE IF NOT EXISTS d_signal_decision_times (signal_id TEXT PRIMARY KEY, decided_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS d_first_entry_quotes (signal_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS d_stream_evidence (id INTEGER PRIMARY KEY CHECK(id=1), last_at TEXT, last_id TEXT)')
            db.execute('INSERT OR IGNORE INTO d_stream_evidence VALUES (1, NULL, NULL)')
            db.execute('UPDATE d_stream_evidence SET last_at=NULL,last_id=NULL WHERE id=1')
        self._reconcile()
        # An open can commit before its intent is marked FILLED. Always
        # consult the ledger's used-signal record, including with a position.
        for item in self.intents.pending():
            reconciled = self.intents.reconcile(item['signal_id'], datetime.now(timezone.utc))
            if reconciled.get('status') == 'PENDING':
                self.intents.resolve(item['signal_id'], 'PENDING', 'EXPIRED')
            elif reconciled.get('status') == 'BLOCKED':
                self._protection('UNCERTAIN', 'INTENT_RECONCILIATION_FAILED')
        if self.ledger.account()['position']:
            self._protection('UNCERTAIN', 'RESTART_AWAITING_FRESH_QUOTE')
        else:
            if self.status()['protection'] == 'READY':
                self._protection('UNCERTAIN', 'STARTUP_AWAITING_FEED_VALIDATION')
            # A restarted process cannot prove whether the first quote after
            # the old decision was missed while it was offline. Never replay it.
            with sqlite3.connect(self.observation_path, timeout=10) as db:
                state = json.loads(db.execute('SELECT payload FROM d_state WHERE id=1').fetchone()[0])
                if state.get('phase') == 'PENDING':
                    state.update(phase='COOLDOWN', remaining=4)
                    state.pop('signal', None)
                    db.execute('UPDATE d_state SET payload=? WHERE id=1', (json.dumps(state),))

    def close(self):
        with self.lock:
            self._closed = True
            for lease in reversed(self._leases):
                lease.release()
            self._leases.clear()

    def _require_open(self):
        if self._closed:
            raise RuntimeError('D_RUNTIME_CLOSED')

    def _protection(self, state, reason=None):
        with sqlite3.connect(self.observation_path, timeout=10) as db:
            latch = db.execute('SELECT reason FROM d_data_loss_latch WHERE id=1').fetchone()
            if latch:
                state, reason = 'UNCERTAIN', latch[0]
            db.execute('UPDATE d_runtime_status SET protection=?,reason=? WHERE id=1', (state, reason))

    def status(self):
        with sqlite3.connect(self.observation_path, timeout=10) as db:
            row = db.execute('SELECT protection,reason FROM d_runtime_status WHERE id=1').fetchone()
            state = json.loads(db.execute('SELECT payload FROM d_state WHERE id=1').fetchone()[0])
        return {'protection': row[0], 'reason': row[1], 'state': state,
                'account': self.ledger.account()}

    def has_data_loss(self):
        with sqlite3.connect(self.observation_path, timeout=10) as db:
            return db.execute('SELECT 1 FROM d_data_loss_latch WHERE id=1').fetchone() is not None

    def circuit_status(self, quote, now):
        with self.lock:
            self._require_open()
            return assess_from_committed_ledger(self.ledger, self.circuit, quote or {}, now)

    def accept_gap_quote(self, event_at):
        with self.lock, sqlite3.connect(self.observation_path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            last = db.execute('SELECT event_at FROM d_gap_recovery_watermark WHERE id=1').fetchone()
            if last and event_at <= last[0]:
                return False
            db.execute('INSERT OR REPLACE INTO d_gap_recovery_watermark VALUES (1, ?)', (event_at,))
            return True

    def mark_stream_unavailable(self, reason='STREAM_UNAVAILABLE'):
        """The dispatcher must call this on disconnect or queue failure."""
        with self.lock, sqlite3.connect(self.observation_path, timeout=10) as db:
            self._require_open()
            db.execute('UPDATE d_stream_evidence SET last_at=NULL,last_id=NULL WHERE id=1')
            latch = db.execute('SELECT reason FROM d_data_loss_latch WHERE id=1').fetchone()
            db.execute("UPDATE d_runtime_status SET protection='UNCERTAIN',reason=? WHERE id=1", (latch[0] if latch else reason,))

    def mark_dispatch_failure(self, reason):
        """Durably preserve evidence loss until an explicit reconciliation exists."""
        with self.lock, sqlite3.connect(self.observation_path, timeout=10) as db:
            self._require_open()
            position = self.ledger.account()['position']
            db.execute('INSERT OR IGNORE INTO d_data_loss_latch (id,reason,trade_id) VALUES (1, ?, ?)', (reason, position['trade_id'] if position else None))
            latch = db.execute('SELECT reason FROM d_data_loss_latch WHERE id=1').fetchone()[0]
            db.execute('UPDATE d_stream_evidence SET last_at=NULL,last_id=NULL WHERE id=1')
            db.execute("UPDATE d_runtime_status SET protection='UNCERTAIN',reason=? WHERE id=1", (latch,))

    def recover_gap_after_flat(self, now, quote_start, quote_end, quote_count):
        """Clear only future-entry restriction; historical gap stays unverified."""
        with self.lock:
            self._require_open()
            if self.ledger.account()['position'] or self.intents.pending():
                return False
            if pending_verified_quote(self.ledger, now, allow_expired=True)['status'] != 'EMPTY':
                return False
            with sqlite3.connect(self.observation_path, timeout=10) as db:
                db.execute('BEGIN IMMEDIATE')
                latch = db.execute('SELECT reason,trade_id FROM d_data_loss_latch WHERE id=1').fetchone()
                if not latch or not latch[1]:
                    return False
                fills = self.ledger.fills()
                close = next((f for f in reversed(fills) if f['trade_id'] == latch[1] and f['effect'] == 'CLOSE'), None)
                if close is None:
                    return False
                state = json.loads(db.execute('SELECT payload FROM d_state WHERE id=1').fetchone()[0])
                if state.get('phase') == 'PENDING':
                    return False
                db.execute('INSERT INTO d_gap_recovery_audit (reason,trade_id,close_fill_id,recovered_at,quote_start,quote_end,quote_count,history_verified) VALUES (?,?,?,?,?,?,?,0)',
                           (latch[0], latch[1], close['fill_id'], now.isoformat(), quote_start, quote_end, quote_count))
                db.execute('DELETE FROM d_data_loss_latch WHERE id=1')
                db.execute("UPDATE d_runtime_status SET protection='READY',reason=NULL WHERE id=1")
                return True

    def confirm_stream_recovered(self, quote, now, *, market_ready, candles_complete):
        """Permit flat-account entries only after an explicit complete feed check."""
        with self.lock:
            self._require_open()
            if not market_ready or not candles_complete or not validate_quote(quote, now):
                return False
            with sqlite3.connect(self.observation_path, timeout=10) as db:
                if db.execute('SELECT 1 FROM d_data_loss_latch WHERE id=1').fetchone():
                    return False
            if self.ledger.account()['position']:
                # An open position must process this quote through on_quote so
                # any triggered stop is handled before protection is cleared.
                return False
            if self.common_guard and pending_verified_quote(self.ledger, now, allow_expired=True)['status'] != 'EMPTY':
                return False
            for item in self.intents.pending():
                result = self.intents.reconcile(item['signal_id'], now)
                if result.get('status') == 'PENDING':
                    result = self.intents.resolve(item['signal_id'], 'PENDING', 'EXPIRED')
                    if not result.get('accepted'):
                        return False
                elif result.get('status') == 'BLOCKED':
                    return False
            with sqlite3.connect(self.observation_path, timeout=10) as db:
                state = json.loads(db.execute('SELECT payload FROM d_state WHERE id=1').fetchone()[0])
                if state.get('phase') == 'PENDING':
                    state.update(phase='COOLDOWN', remaining=4)
                    state.pop('signal', None)
                    db.execute('UPDATE d_state SET payload=? WHERE id=1', (json.dumps(state),))
            self._protection('READY')
            return True

    def _record_quote(self, quote, market_ready):
        event_at = datetime.fromisoformat(quote['event_at'].replace('Z', '+00:00')).astimezone(timezone.utc)
        with sqlite3.connect(self.observation_path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            last_at, last_id = db.execute('SELECT last_at,last_id FROM d_stream_evidence WHERE id=1').fetchone()
            last = datetime.fromisoformat(last_at) if last_at else None
            if last and event_at <= last:
                return False
            state = json.loads(db.execute('SELECT payload FROM d_state WHERE id=1').fetchone()[0])
            signal = state.get('signal') if state.get('phase') == 'PENDING' else None
            if market_ready and signal:
                signal_id = signal.get('signal_close')
                decision = db.execute('SELECT decided_at FROM d_signal_decision_times WHERE signal_id=?', (signal_id,)).fetchone()
                if decision:
                    decision_at = datetime.fromisoformat(decision[0])
                    signal_close = datetime.fromisoformat(signal_id.replace('Z', '+00:00'))
                    if decision_at <= event_at < signal_close + timedelta(minutes=15):
                        payload = json.dumps(quote, sort_keys=True, separators=(',', ':'), allow_nan=False)
                        db.execute('INSERT OR IGNORE INTO d_first_entry_quotes VALUES (?,?,?)',
                                   (signal_id, quote['event_id'], payload))
            db.execute('UPDATE d_stream_evidence SET last_at=?,last_id=? WHERE id=1', (event_at.isoformat(), quote['event_id']))
            return True

    def _reconcile(self):
        """Use durable positions and fills as truth after interruption."""
        with self.lock:
            account = self.ledger.account()
            fills = self.ledger.fills()
            last_close = next((f for f in reversed(fills) if f['effect'] == 'CLOSE'), None)
            with sqlite3.connect(self.observation_path, timeout=10) as db:
                db.execute('BEGIN IMMEDIATE')
                state = json.loads(db.execute('SELECT payload FROM d_state WHERE id=1').fetchone()[0])
                if account['position']:
                    state['phase'] = 'LONG'
                    state.pop('signal', None)
                elif last_close and state.get('closed_trade_id') != last_close['trade_id']:
                    state.update(phase='COOLDOWN', remaining=8, closed_trade_id=last_close['trade_id'])
                    state.pop('signal', None)
                db.execute('UPDATE d_state SET payload=? WHERE id=1', (json.dumps(state, allow_nan=False),))
            return state

    def on_bar(self, close_at, now, *, market_ready):
        with self.lock:
            self._require_open()
            self._reconcile()
            if not market_ready:
                self.mark_stream_unavailable('MARKET_NOT_READY')
                return {'status': 'BLOCKED', 'reason': 'MARKET_NOT_READY'}
            result = observe_cycle(self.observation_path, self.archive, close_at, now,
                                   common_guard=self.common_guard)
            if not self.common_guard and result.get('status') == 'PENDING' and result.get('signal'):
                with sqlite3.connect(self.observation_path, timeout=10) as db:
                    db.execute('INSERT OR IGNORE INTO d_signal_decision_times VALUES (?,?)',
                               (result['signal']['signal_close'], now.astimezone(timezone.utc).isoformat()))
            return result

    def cancel_pending_entry(self):
        """Expire an unfilled candidate when the operator pauses new entries."""
        with self.lock:
            self._require_open()
            state = self.status()['state']
            signal = state.get('signal') if state.get('phase') == 'PENDING' else None
            signal_id = signal.get('signal_close') if isinstance(signal, dict) else None
            for item in self.intents.pending():
                self.intents.resolve(item['signal_id'], 'PENDING', 'EXPIRED')
            if signal_id:
                self._finish_candidate(signal_id, 'STRATEGY_PAUSED')
            return {'status': 'PAUSED', 'cancelled_signal': signal_id}

    def _signal_bar(self, signal_id):
        end = datetime.fromisoformat(signal_id.replace('Z', '+00:00'))
        if end.tzinfo is None:
            raise ValueError('NAIVE_SIGNAL_TIME')
        rows = self.archive.read(end - timedelta(minutes=15), end)
        matches = [row for row in rows if datetime.fromisoformat(row['close_at'].replace('Z', '+00:00')) == end]
        if len(matches) != 1:
            raise ValueError('SIGNAL_CANDLE_UNAVAILABLE')
        return matches[0]

    def _finish_candidate(self, signal_id, reason):
        with sqlite3.connect(self.observation_path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            state = json.loads(db.execute('SELECT payload FROM d_state WHERE id=1').fetchone()[0])
            if state.get('phase') == 'PENDING' and (state.get('signal') or {}).get('signal_close') == signal_id:
                state.update(phase='COOLDOWN', remaining=4, terminal_reason=reason)
                state.pop('signal', None)
                db.execute('UPDATE d_state SET payload=? WHERE id=1', (json.dumps(state),))

    def _drain_quotes(self, now):
        """Finish every persisted quote before claiming protection is current."""
        for _ in range(1000):
            pending = pending_verified_quote(self.ledger, now, allow_expired=True)
            if pending.get('status') == 'EMPTY':
                return {'status': 'COMPLETE'}
            if pending.get('status') not in ('READY', 'EXPIRED'):
                return {'status': 'BLOCKED', 'reason': pending.get('reason') or 'QUOTE_QUEUE_UNAVAILABLE'}
            if pending.get('status') == 'READY':
                quote = pending['quote']
                account = self.ledger.account()
                position = account['position']
                if position:
                    risk = assess_from_committed_ledger(self.ledger, self.circuit, quote, now)
                    # A real stop trigger takes priority over the daily flatten.
                    stop_due = bool(position.get('stop_due')) or Decimal(str(quote['bid'])) <= Decimal(position['stop_price'])
                    if risk.get('flatten_required') and not stop_due:
                        price = (Decimal(str(quote['bid'])) * Decimal('0.9998') / Decimal('0.01')).to_integral_value(rounding=ROUND_DOWN) * Decimal('0.01')
                        result = self.ledger.close_position(quantity=position['quantity'], price=str(price),
                            reason='DAILY_LOSS', trade_id=position['trade_id'],
                            idempotency_key='D-daily-loss:' + position['trade_id'],
                            expected_version=account['version'], quote_event_id=quote['event_id'])
                        if not result.get('accepted'):
                            return {'status':'BLOCKED','reason':'DAILY_FLATTEN_FAILED'}
            result = process_verified_quote(self.ledger, now)
            if result.get('status') not in ('COMPLETE', 'EMPTY'):
                return result
        return {'status': 'BLOCKED', 'reason': 'QUOTE_QUEUE_DRAIN_LIMIT'}

    def on_quote(self, quote, now, *, market_ready):
        """Process existing position first; only fresh healthy feed can open."""
        with self.lock:
            self._require_open()
            if not validate_quote(quote, now):
                self.mark_stream_unavailable('INVALID_QUOTE')
                return {'status': 'BLOCKED', 'reason': 'INVALID_QUOTE'}
            if not market_ready:
                self.mark_stream_unavailable('MARKET_NOT_READY')
            if self.ledger.account()['position']:
                # Persist executable quote evidence before advancing any
                # observation watermark. A crash must not lose a stop trigger.
                queued = enqueue_verified_quote(self.ledger, quote, now)
                if not queued.get('accepted'):
                    self._protection('UNCERTAIN', queued.get('reason'))
                    return {'status': 'BLOCKED', 'reason': queued.get('reason')}
                self._record_quote(quote, market_ready)
                self._reconcile()
                result = self._drain_quotes(now)
                if result.get('status') not in ('COMPLETE', 'EMPTY'):
                    self._protection('UNCERTAIN', result.get('reason') or 'POSITION_PROCESSING_FAILED')
                elif market_ready:
                    self._protection('READY')
                self._reconcile()
                return result
            if not self._record_quote(quote, market_ready):
                return {'status': 'BLOCKED', 'reason': 'DUPLICATE_OR_OUT_OF_ORDER_QUOTE'}
            self._reconcile()
            if not market_ready:
                return {'status': 'BLOCKED', 'reason': 'MARKET_NOT_READY'}
            if self.status()['protection'] != 'READY':
                return {'status': 'BLOCKED', 'reason': 'PROTECTION_UNCERTAIN'}
            risk = assess_from_committed_ledger(self.ledger, self.circuit, quote, now)
            if risk.get('block_entries'):
                return {'status': 'BLOCKED', 'reason': risk.get('reason') or 'D_CIRCUIT_UNAVAILABLE'}
            try:
                state = self.status()['state']
                signal = state.get('signal') if state.get('phase') == 'PENDING' else None
                if not signal:
                    return {'status': 'IDLE', 'reason': 'NO_PENDING_SIGNAL'}
                signal_id = signal['signal_close']
                with sqlite3.connect(self.observation_path, timeout=10) as db:
                    decision = db.execute('SELECT decided_at FROM d_signal_decision_times WHERE signal_id=?', (signal_id,)).fetchone()
                    first = db.execute('SELECT event_id FROM d_first_entry_quotes WHERE signal_id=?', (signal_id,)).fetchone()
                event_at = datetime.fromisoformat(quote['event_at'].replace('Z', '+00:00'))
                if not decision:
                    with sqlite3.connect(self.observation_path, timeout=10) as db:
                        state.update(phase='COOLDOWN', remaining=4)
                        state.pop('signal', None)
                        db.execute('UPDATE d_state SET payload=? WHERE id=1', (json.dumps(state),))
                    return {'status': 'REJECTED', 'reason': 'DECISION_TIME_UNAVAILABLE'}
                if event_at < datetime.fromisoformat(decision[0]):
                    return {'status': 'PENDING', 'reason': 'QUOTE_BEFORE_CONFIRMATION'}
                if not first:
                    return {'status': 'BLOCKED', 'reason': 'FIRST_QUOTE_UNAVAILABLE'}
                if first[0] != quote['event_id']:
                    self._finish_candidate(signal_id, 'FIRST_ATTEMPT_INCOMPLETE')
                    return {'status': 'REJECTED', 'reason': 'FIRST_ATTEMPT_INCOMPLETE'}
                captured = handoff_observed_intent(self.observation_path, self.intents, quote, now)
                if not captured.get('accepted') and captured.get('reason') != 'NO_PENDING_SIGNAL':
                    return {'status': 'BLOCKED', 'reason': captured.get('reason')}
                if captured.get('duplicate'):
                    reconciled = self.intents.reconcile(signal_id, now)
                    if reconciled.get('status') == 'PENDING':
                        resolved = self.intents.resolve(signal_id, 'PENDING', 'EXPIRED')
                        if not resolved.get('accepted'):
                            return {'status': 'BLOCKED', 'reason': 'INTENT_EXPIRY_FAILED'}
                        reconciled = {'status': 'EXPIRED', 'reason': 'FIRST_ATTEMPT_INCOMPLETE'}
                    if reconciled.get('status') in ('REJECTED', 'EXPIRED'):
                        self._finish_candidate(signal_id, reconciled.get('reason') or reconciled['status'])
                    if reconciled.get('status') == 'FILLED':
                        self._reconcile()
                    return reconciled
                item = next((item for item in self.intents.pending() if item['signal_id'] == signal_id), None)
                if item is None:
                    return {'status': 'IDLE', 'reason': 'NO_PENDING_SIGNAL'}
                result = process_pending_entry(self.ledger, self.intents, item['signal_id'],
                                               self._signal_bar(item['signal_id']), now,
                                               entry_gate=self.entry_gate)
                if result.get('status') in ('REJECTED', 'EXPIRED'):
                    self._finish_candidate(signal_id, result.get('reason') or result['status'])
                if self.ledger.account()['position']:
                    protection = result.get('protection') or {}
                    if protection.get('status') not in ('COMPLETE', 'EMPTY'):
                        self._protection('UNCERTAIN', protection.get('reason') or 'OPEN_PROTECTION_FAILED')
                        self._reconcile()
                        return {'status': 'BLOCKED', 'reason': 'OPEN_PROTECTION_UNCERTAIN',
                                'execution': result.get('execution')}
                    self._reconcile()
                return result
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                if self.ledger.account()['position']:
                    self._protection('UNCERTAIN', 'OPEN_OUTCOME_UNKNOWN')
                return {'status': 'BLOCKED', 'reason': type(exc).__name__}
