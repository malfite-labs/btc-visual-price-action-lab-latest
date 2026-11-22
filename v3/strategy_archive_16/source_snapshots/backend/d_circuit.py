"""Durable D paper-trading circuit state; caller supplies verified equity snapshots.

This component never places orders. Its decisions block entries or request
flattening while keeping stop and close operations available.
"""
import sqlite3
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

UTC = timezone.utc
DAILY_LOSS = Decimal('0.01')
LOSS_LIMIT = 3
PAUSE = timedelta(hours=24)


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return value.astimezone(UTC)


def _decimal(value, *, positive=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('INVALID_EQUITY_OR_PNL') from exc
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError('INVALID_EQUITY_OR_PNL')
    return number


class DCircuit:
    """D-specific state store. Trade events are whole-trade net results."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            tables = {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables and 'd_circuit_identity' not in tables:
                raise RuntimeError('D_CIRCUIT_IDENTITY_MISMATCH')
            db.execute('CREATE TABLE IF NOT EXISTS d_circuit_identity (id INTEGER PRIMARY KEY CHECK(id=1), kind TEXT NOT NULL)')
            identity = db.execute('SELECT kind FROM d_circuit_identity WHERE id=1').fetchone()
            if identity and identity[0] != 'D_CIRCUIT_V1':
                raise RuntimeError('D_CIRCUIT_IDENTITY_MISMATCH')
            db.execute("INSERT OR IGNORE INTO d_circuit_identity VALUES(1, 'D_CIRCUIT_V1')")
            db.execute('CREATE TABLE IF NOT EXISTS d_days (day TEXT PRIMARY KEY, opening_equity TEXT NOT NULL, provenance TEXT NOT NULL, latched INTEGER NOT NULL DEFAULT 0, last_observed TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS d_closed_trades (trade_id TEXT PRIMARY KEY, net_pnl TEXT NOT NULL, closed_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS d_circuit_state (id INTEGER PRIMARY KEY CHECK(id=1), streak INTEGER NOT NULL, paused_until TEXT, last_closed TEXT)')
            db.execute('INSERT OR IGNORE INTO d_circuit_state VALUES(1,0,NULL,NULL)')
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record_day_start(self, *, midnight, equity, provenance):
        """Record a verified exact UTC-midnight equity once; conflicting writes fail.

        The caller must validate provenance and reconcile all prior-day fills and
        marks before passing the amount. Current equity is not a substitute.
        """
        stamp = _timestamp(midnight)
        if stamp.time() != time(0, 0):
            raise ValueError('MIDNIGHT_REQUIRED')
        amount = _decimal(equity, positive=True)
        if not isinstance(provenance, str) or not provenance.strip():
            raise ValueError('PROVENANCE_REQUIRED')
        day = stamp.date().isoformat()
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT opening_equity,provenance FROM d_days WHERE day=?', (day,)).fetchone()
            if row:
                if Decimal(row['opening_equity']) != amount or row['provenance'] != provenance:
                    db.rollback()
                    return {'accepted': False, 'reason': 'DAY_START_CONFLICT'}
                db.rollback()
                return {'accepted': True, 'duplicate': True}
            db.execute('INSERT INTO d_days(day,opening_equity,provenance) VALUES(?,?,?)', (day, str(amount), provenance))
            db.commit()
            return {'accepted': True, 'duplicate': False}

    def record_closed_trade(self, *, trade_id, net_pnl, closed_at):
        """Record one final result per trade, never a partial-close result."""
        if not isinstance(trade_id, str) or not trade_id.strip():
            raise ValueError('INVALID_TRADE_ID')
        pnl = _decimal(net_pnl)
        timestamp = _timestamp(closed_at).isoformat()
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT net_pnl,closed_at FROM d_closed_trades WHERE trade_id=?', (trade_id,)).fetchone()
            if prior:
                if Decimal(prior['net_pnl']) != pnl or prior['closed_at'] != timestamp:
                    db.rollback()
                    return {'accepted': False, 'reason': 'TRADE_CONFLICT'}
                db.rollback()
                return {'accepted': True, 'duplicate': True}
            state = db.execute('SELECT * FROM d_circuit_state WHERE id=1').fetchone()
            if state['last_closed'] and timestamp <= state['last_closed']:
                db.rollback()
                return {'accepted': False, 'reason': 'OUT_OF_ORDER_TRADE'}
            streak = state['streak'] + 1 if pnl < 0 else 0
            until = _timestamp(closed_at) + PAUSE if streak >= LOSS_LIMIT else None
            old_until = datetime.fromisoformat(state['paused_until']) if state['paused_until'] else None
            if old_until and (until is None or old_until > until):
                until = old_until
            db.execute('INSERT INTO d_closed_trades VALUES(?,?,?)', (trade_id, str(pnl), timestamp))
            db.execute('UPDATE d_circuit_state SET streak=?,paused_until=?,last_closed=? WHERE id=1',
                       (streak, until.isoformat() if until else None, timestamp))
            db.commit()
            return {'accepted': True, 'duplicate': False, 'loss_streak': streak,
                    'paused_until': until.isoformat() if until else None}

    def assess(self, *, now, equity):
        """Assess current verified equity; returns gates and never prevents exits."""
        stamp = _timestamp(now)
        day = stamp.date().isoformat()
        try:
            amount = _decimal(equity)
        except ValueError:
            with closing(self._connect()) as db:
                row = db.execute('SELECT latched FROM d_days WHERE day=?', (day,)).fetchone()
            return {'block_entries': True, 'flatten_required': bool(row and row['latched']),
                    'allow_exits': True, 'reason': 'EQUITY_UNAVAILABLE'}
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM d_days WHERE day=?', (day,)).fetchone()
            state = db.execute('SELECT * FROM d_circuit_state WHERE id=1').fetchone()
            paused = bool(state['paused_until'] and stamp < datetime.fromisoformat(state['paused_until']))
            latest = db.execute('SELECT MAX(day) FROM d_days WHERE last_observed IS NOT NULL').fetchone()[0]
            if latest and day < latest:
                db.rollback()
                return {'block_entries': True, 'flatten_required': bool(row and row['latched']),
                        'allow_exits': True, 'reason': 'OUT_OF_ORDER_EQUITY'}
            if row is None:
                db.rollback()
                return {'block_entries': True, 'flatten_required': False, 'allow_exits': True,
                        'reason': 'DAY_START_EQUITY_UNAVAILABLE', 'paused_until': state['paused_until']}
            if row['last_observed'] and stamp.isoformat() < row['last_observed']:
                db.rollback()
                return {'block_entries': True, 'flatten_required': bool(row['latched']),
                        'allow_exits': True, 'reason': 'OUT_OF_ORDER_EQUITY'}
            latched = bool(row['latched']) or amount <= Decimal(row['opening_equity']) * (Decimal(1) - DAILY_LOSS)
            db.execute('UPDATE d_days SET latched=?,last_observed=? WHERE day=?', (int(latched), stamp.isoformat(), day))
            db.commit()
            return {'block_entries': latched or paused, 'flatten_required': latched,
                    'allow_exits': True, 'reason': 'DAILY_LOSS_LIMIT' if latched else 'LOSS_STREAK_PAUSE' if paused else 'READY',
                    'paused_until': state['paused_until'], 'loss_streak': state['streak']}
