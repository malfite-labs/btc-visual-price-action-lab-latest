"""Paper strategy entry gate and durable pause state; no order execution."""
import sqlite3
import threading
import os
import time
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from pathlib import Path

_registry_lock = threading.Lock()
_locks = {}
_locals = {}


def guarded_formal_open(control, store, control_agent, **kwargs):
    """Hold the shared pause gate through the ledger open transaction."""
    with control.entry_guard(control_agent) as allowed:
        if not allowed:
            return {'accepted': False, 'reason': 'STRATEGY_PAUSED'}
        return store.open_position(**kwargs)


def close_paper_account(store, agent, quote, cost, now):
    """Close one paper account with a fresh executable quote; no retries or bypasses."""
    from backend.domain import BookLevel, simulate_fill
    account = store.account(agent)
    if account['state'] == 'FLAT':
        return {'accepted': True, 'reason': 'ALREADY_FLAT'}
    if account['state'] not in ('OPEN_LONG', 'OPEN_SHORT'):
        return {'accepted': False, 'reason': 'ACCOUNT_NOT_CLOSABLE'}
    try:
        if quote.get('source') != 'ALPACA_US_BTCUSD_SPOT' or quote.get('currency') != 'USD_RESEARCH' or quote.get('timestamp_valid') is not True:
            return {'accepted': False, 'reason': 'QUOTE_INVALID'}
        event = datetime.fromisoformat(quote['event_at'].replace('Z', '+00:00'))
        received = datetime.fromisoformat(quote['received_at'].replace('Z', '+00:00'))
        if event.tzinfo is None or received.tzinfo is None or now.tzinfo is None:
            return {'accepted': False, 'reason': 'QUOTE_INVALID'}
        ages = [(now.astimezone(timezone.utc) - t.astimezone(timezone.utc)).total_seconds() * 1000 for t in (event, received)]
        if any(age < 0 or age > cost.max_quote_age_ms for age in ages):
            return {'accepted': False, 'reason': 'QUOTE_STALE'}
        bid, ask = Decimal(str(quote['bid'])), Decimal(str(quote['ask']))
        if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
            return {'accepted': False, 'reason': 'QUOTE_INVALID'}
        position = json.loads(account['position_json'])
        direction, qty, trade = position['direction'], Decimal(position['quantity']), position['trade_id']
        if direction not in ('LONG', 'SHORT') or not qty.is_finite() or qty <= 0 or not trade:
            return {'accepted': False, 'reason': 'POSITION_INVALID'}
        side = 'SELL' if direction == 'LONG' else 'BUY'
        ref = bid if side == 'SELL' else ask
        fill = simulate_fill(side, qty, [BookLevel(bid, Decimal('0'))], [BookLevel(ask, Decimal('0'))], cost)
        if fill is None: return {'accepted': False, 'reason': 'FILL_UNAVAILABLE'}
        price, fee = fill
        return store.close_position(agent=agent, price=price, fee=fee, slippage=abs(price-ref)*qty,
                                    idempotency_key='strategy-pause:' + agent + ':' + trade,
                                    expected_version=account['version'], reason='STRATEGY_PAUSE',
                                    rationale='STRATEGY_PAUSE', quote_id=quote.get('event_id'), cost_model=cost,
                                    execution_quote=quote)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return {'accepted': False, 'reason': 'INVALID_CONTEXT'}


def close_forward_account(store, agent, quote, cost, now):
    """Close an ABC shadow account and reconcile its forward accounting."""
    with sqlite3.connect(store.path, timeout=10) as db:
        row = db.execute('SELECT trade_id,signal_close,initial_cash FROM forward_positions WHERE agent=?', (agent,)).fetchone()
    account = store.account(agent)
    if row is None:
        return {'accepted': account['state'] == 'FLAT', 'reason': 'ALREADY_FLAT' if account['state'] == 'FLAT' else 'SHADOW_META_MISSING'}
    trade, signal_close, initial_cash = row
    if account['state'] != 'FLAT':
        try:
            if json.loads(account['position_json'])['trade_id'] != trade:
                return {'accepted': False, 'reason': 'SHADOW_POSITION_MISMATCH'}
        except (KeyError, ValueError, TypeError):
            return {'accepted': False, 'reason': 'SHADOW_POSITION_INVALID'}
        result = close_paper_account(store, agent, quote, cost, now)
        if not result.get('accepted'): return result
    with sqlite3.connect(store.path, timeout=10) as db:
        db.execute('BEGIN IMMEDIATE')
        account_row = db.execute('SELECT state,cash FROM accounts WHERE agent=?', (agent,)).fetchone()
        fill = db.execute("SELECT occurred_at FROM fills WHERE trade_id=? AND agent=? AND position_effect='CLOSE'", (trade, agent)).fetchone()
        if not account_row or account_row[0] != 'FLAT' or not fill:
            db.rollback()
            return {'accepted': False, 'reason': 'SHADOW_RECONCILIATION_REQUIRED'}
        pnl = str(Decimal(account_row[1]) - Decimal(initial_cash))
        db.execute('INSERT OR IGNORE INTO forward_trades VALUES(?,?,?,?,?,?)', (trade, agent, signal_close, fill[0], pnl, 'STRATEGY_PAUSE'))
        db.execute('DELETE FROM forward_positions WHERE agent=? AND trade_id=?', (agent, trade))
        has_reviews = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forward_reviews'").fetchone()
        count = db.execute('SELECT count(*) FROM forward_trades WHERE agent=?', (agent,)).fetchone()[0]
        if has_reviews and count and count % 40 == 0:
            db.execute('INSERT OR IGNORE INTO forward_reviews VALUES(?,?,?,?)', (agent, count, 'REVIEW_DUE', '40 closed forward trades; waiting for bounded review'))
        db.commit()
    return {'accepted': True, 'reason': 'SHADOW_CLOSED'}


class StrategyControl:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _registry_lock:
            self._lock = _locks.setdefault(str(self.path), threading.RLock())
            self._local = _locals.setdefault(str(self.path), threading.local())
        with self._guard(), sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS strategy_control (agent TEXT PRIMARY KEY, state TEXT NOT NULL, formal TEXT, shadow TEXT)')
            db.executemany('INSERT OR IGNORE INTO strategy_control VALUES (?, ?, NULL, NULL)', [(a, 'RUNNING') for a in 'ABCE'])

    @staticmethod
    def _agent(agent):
        if agent not in ('A', 'B', 'C', 'E'):
            raise ValueError('UNKNOWN_STRATEGY')
        return agent

    @contextmanager
    def _guard(self):
        with self._lock:
            depth = getattr(self._local, 'depth', 0)
            if depth:
                self._local.depth = depth + 1
                try: yield
                finally: self._local.depth -= 1
                return
            lock_path = self.path.with_suffix(self.path.suffix + '.lock')
            with lock_path.open('a+b') as stream:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0, 2)
                    if stream.tell() == 0: stream.write(b'0'); stream.flush()
                    while True:
                        stream.seek(0)
                        try: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1); break
                        except OSError: time.sleep(.05)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                self._local.depth = 1
                try: yield
                finally:
                    self._local.depth = 0
                    if os.name == 'nt':
                        stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def status(self, agent):
        with self._guard(), sqlite3.connect(self.path) as db:
            row = db.execute('SELECT state,formal,shadow FROM strategy_control WHERE agent=?', (self._agent(agent),)).fetchone()
            return dict(zip(('state', 'formal', 'shadow'), row))

    def is_entry_allowed(self, agent):
        return self.status(agent)['state'] == 'RUNNING'

    @contextmanager
    def entry_guard(self, agent):
        with self._guard():
            yield self.is_entry_allowed(agent)

    def request_pause(self, agent):
        with self._guard(), sqlite3.connect(self.path) as db:
            agent = self._agent(agent)
            row = db.execute('SELECT state FROM strategy_control WHERE agent=?', (agent,)).fetchone()
            if row[0] == 'RUNNING':
                db.execute("UPDATE strategy_control SET state='PAUSING',formal=NULL,shadow=NULL WHERE agent=?", (agent,))

    def record_account_result(self, agent, account, result):
        if account not in ('formal', 'shadow') or result not in ('FLAT', 'UNAVAILABLE', 'ERROR', 'PENDING'):
            raise ValueError('INVALID_ACCOUNT_RESULT')
        with self._guard(), sqlite3.connect(self.path) as db:
            agent = self._agent(agent)
            state = db.execute('SELECT state FROM strategy_control WHERE agent=?', (agent,)).fetchone()[0]
            if state != 'PAUSING':
                raise ValueError('NOT_PAUSING')
            db.execute('UPDATE strategy_control SET ' + account + '=? WHERE agent=?', (result, agent))
            formal, shadow = db.execute('SELECT formal,shadow FROM strategy_control WHERE agent=?', (agent,)).fetchone()
            if formal == shadow == 'FLAT':
                db.execute("UPDATE strategy_control SET state='PAUSED' WHERE agent=?", (agent,))

    def resume(self, agent):
        with self._guard(), sqlite3.connect(self.path) as db:
            agent = self._agent(agent)
            if db.execute('SELECT state FROM strategy_control WHERE agent=?', (agent,)).fetchone()[0] != 'PAUSED':
                return False
            db.execute("UPDATE strategy_control SET state='RUNNING',formal=NULL,shadow=NULL WHERE agent=?", (agent,))
            return True

    def pause_accounts(self, agent, stores, quote, cost, now):
        """Block entries before attempting independent, retryable paper-account closes."""
        if set(stores) != {'formal', 'shadow'}:
            raise ValueError('BOTH_ACCOUNTS_REQUIRED')
        with self._guard():
            self.request_pause(agent)
            if self.status(agent)['state'] == 'PAUSED':
                return self.status(agent)
            for kind in ('formal', 'shadow'):
                if self.status(agent)['state'] == 'PAUSED':
                    break
                store = stores[kind]
                if store is None:
                    result = 'UNAVAILABLE'
                else:
                    try:
                        close = (close_forward_account(store, agent, quote, cost, now)
                                 if kind == 'shadow' and agent in 'ABC'
                                 else close_paper_account(store, agent, quote, cost, now))
                        result = 'FLAT' if close.get('accepted') and store.account(agent).get('state') == 'FLAT' else 'ERROR'
                    except Exception:
                        result = 'ERROR'
                self.record_account_result(agent, kind, result)
            return self.status(agent)

    def pause_formal(self, agent, store, quote, cost, now):
        """Pause the formal paper account; shadow accounts remain independent."""
        with self._guard():
            self.request_pause(agent)
            if self.status(agent)['state'] == 'PAUSED':
                return self.status(agent)
            try:
                closed = close_paper_account(store, agent, quote, cost, now)
                result = 'FLAT' if closed.get('accepted') and store.account(agent).get('state') == 'FLAT' else 'ERROR'
            except Exception:
                result = 'ERROR'
            with sqlite3.connect(self.path) as db:
                db.execute('UPDATE strategy_control SET state=?,formal=?,shadow=? WHERE agent=? AND state=?',
                           ('PAUSED' if result == 'FLAT' else 'PAUSING', result, 'UNCHANGED', self._agent(agent), 'PAUSING'))
            return self.status(agent)
