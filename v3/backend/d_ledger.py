"""Independent D paper ledger. It does not connect to a broker or submit orders.

All operations are durable, serialised by SQLite and scoped to one BTC/USD
paper account. The caller is responsible for signal, quote and risk gates.
"""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from uuid import uuid4

CENT = Decimal('0.01')
QUANTITY_STEP = Decimal('0.00000001')
FEE_RATE = Decimal('0.0005')


def _number(value, name, *, positive=True):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('INVALID_' + name) from exc
    if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
        raise ValueError('INVALID_' + name)
    return result


def _quantity(value):
    number = _number(value, 'QUANTITY')
    if number % QUANTITY_STEP:
        raise ValueError('INVALID_QUANTITY_STEP')
    return number


def _money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _fee(quantity, price):
    return (quantity * price * FEE_RATE).quantize(CENT, rounding=ROUND_UP)


def _bar_close(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError) as exc:
        raise ValueError('INVALID_BAR_CLOSE') from exc
    if parsed.tzinfo is None:
        raise ValueError('INVALID_BAR_CLOSE')
    parsed = parsed.astimezone(timezone.utc)
    if parsed.minute % 15 or parsed.second or parsed.microsecond:
        raise ValueError('INVALID_BAR_CLOSE')
    return parsed.isoformat()


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


class DLedger:
    """A versioned, atomic, one-position paper account in its own SQLite file."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if existing and 'd_ledger_identity' not in existing:
                raise RuntimeError('D_LEDGER_IDENTITY_MISMATCH')
            if 'd_ledger_identity' in existing:
                identity = db.execute('SELECT kind FROM d_ledger_identity WHERE id=1').fetchone()
                if identity is None or identity[0] != 'D_RESEARCH_PAPER_V1':
                    raise RuntimeError('D_LEDGER_IDENTITY_MISMATCH')
            if 'd_fills' in existing and 'd_used_signals' not in existing:
                if db.execute('SELECT 1 FROM d_fills LIMIT 1').fetchone():
                    raise RuntimeError('D_LEDGER_SIGNAL_HISTORY_UNRECOVERABLE')
            if 'd_fills' in existing and 'd_quotes' not in existing:
                if db.execute('SELECT 1 FROM d_fills LIMIT 1').fetchone():
                    raise RuntimeError('D_LEDGER_QUOTE_HISTORY_UNRECOVERABLE')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS d_quotes (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE, event_at TEXT NOT NULL,
                    bid TEXT NOT NULL, ask TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS d_quotes_pending_idx ON d_quotes(status, seq);
                CREATE TABLE IF NOT EXISTS d_used_signals (
                    signal_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS d_ledger_identity (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    kind TEXT NOT NULL CHECK (kind = 'D_RESEARCH_PAPER_V1')
                );
                CREATE TABLE IF NOT EXISTS d_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash TEXT NOT NULL, version INTEGER NOT NULL,
                    position_json TEXT, realized_pnl TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS d_fills (
                    fill_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL,
                    effect TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
                    fee TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS d_operations (
                    idempotency_key TEXT PRIMARY KEY, operation TEXT NOT NULL,
                    request_hash TEXT NOT NULL, response_json TEXT NOT NULL
                );
            ''')
            db.execute("INSERT OR IGNORE INTO d_ledger_identity VALUES (1, 'D_RESEARCH_PAPER_V1')")
            if db.execute('SELECT kind FROM d_ledger_identity WHERE id=1').fetchone()[0] != 'D_RESEARCH_PAPER_V1':
                raise RuntimeError('D_LEDGER_IDENTITY_MISMATCH')
            db.execute("INSERT OR IGNORE INTO d_account VALUES (1, '10000.00', 0, NULL, '0.00')")
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _account(row):
        position = json.loads(row['position_json']) if row['position_json'] else None
        return {'agent': 'D', 'currency': 'USD_RESEARCH', 'cash': row['cash'],
                'version': row['version'], 'realized_pnl': row['realized_pnl'],
                'state': 'OPEN_LONG' if position else 'FLAT', 'position': position}

    def account(self):
        with closing(self._connect()) as db:
            return self._account(db.execute('SELECT * FROM d_account WHERE id=1').fetchone())

    def fills(self):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM d_fills ORDER BY rowid')]

    @staticmethod
    def _prepare(db, key, operation, request):
        if not isinstance(key, str) or not key.strip():
            raise ValueError('INVALID_IDEMPOTENCY_KEY')
        encoded = json.dumps(request, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(encoded.encode('utf8')).hexdigest()
        previous = db.execute('SELECT * FROM d_operations WHERE idempotency_key=?', (key,)).fetchone()
        if previous:
            if previous['operation'] != operation or previous['request_hash'] != digest:
                return digest, {'accepted': False, 'reason': 'IDEMPOTENCY_KEY_CONFLICT'}
            result = json.loads(previous['response_json'])
            result['duplicate'] = True
            return digest, result
        return digest, None

    @staticmethod
    def _record(db, key, operation, digest, result):
        db.execute('INSERT INTO d_operations VALUES (?,?,?,?)',
                   (key, operation, digest, json.dumps(result, sort_keys=True)))

    def enqueue_quote(self, *, event_id, event_at, bid, ask):
        if not isinstance(event_id, str) or not event_id:
            raise ValueError('INVALID_QUOTE_ID')
        try:
            stamp = datetime.fromisoformat(str(event_at).replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('timezone required')
            stamp = stamp.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError('INVALID_QUOTE_TIME') from exc
        bid, ask = _number(bid, 'BID'), _number(ask, 'ASK')
        if bid > ask:
            raise ValueError('INVALID_SPREAD')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM d_quotes WHERE event_id=?', (event_id,)).fetchone()
            if prior:
                same = prior['event_at'] == stamp and Decimal(prior['bid']) == bid and Decimal(prior['ask']) == ask
                db.rollback()
                return {'accepted': same, 'duplicate': same, 'reason': 'DUPLICATE_QUOTE' if same else 'QUOTE_ID_CONFLICT'}
            newest = db.execute('SELECT event_at FROM d_quotes ORDER BY seq DESC LIMIT 1').fetchone()
            if newest and stamp <= newest['event_at']:
                db.rollback(); return {'accepted': False, 'reason': 'STALE_OR_CONFLICTING_QUOTE'}
            db.execute("INSERT INTO d_quotes (event_id,event_at,bid,ask,status) VALUES (?,?,?,?,'PENDING')",
                       (event_id, stamp, str(bid), str(ask)))
            db.commit()
            return {'accepted': True, 'duplicate': False}

    def pending_quote(self):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM d_quotes WHERE status='PENDING' ORDER BY seq LIMIT 1").fetchone()
            return dict(row) if row else None

    @staticmethod
    def _pending_quote(db, event_id):
        row = db.execute("SELECT * FROM d_quotes WHERE status='PENDING' ORDER BY seq LIMIT 1").fetchone()
        if not row or row['event_id'] != event_id:
            return None
        return row

    def mark_stop_due(self, *, event_id, expected_version, trade_id):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            quote = self._pending_quote(db, event_id)
            if quote is None:
                db.rollback(); return {'accepted': False, 'reason': 'QUOTE_NOT_PENDING'}
            row = db.execute('SELECT * FROM d_account WHERE id=1').fetchone()
            if row['version'] != expected_version:
                db.rollback(); return {'accepted': False, 'reason': 'VERSION_CONFLICT'}
            if not row['position_json']:
                db.rollback(); return {'accepted': False, 'reason': 'NO_POSITION'}
            pos = json.loads(row['position_json'])
            if pos['trade_id'] != trade_id:
                db.rollback(); return {'accepted': False, 'reason': 'TRADE_ID_MISMATCH'}
            if pos.get('stop_due'):
                db.rollback(); return {'accepted': True, 'duplicate': True, 'stop_due': pos['stop_due']}
            if Decimal(quote['bid']) > Decimal(pos['stop_price']):
                db.rollback(); return {'accepted': False, 'reason': 'STOP_NOT_TRIGGERED'}
            pos['stop_due'] = {'event_id': event_id, 'event_at': quote['event_at'], 'bid': quote['bid'], 'stop': pos['stop_price']}
            version = row['version'] + 1
            db.execute('UPDATE d_account SET position_json=?,version=? WHERE id=1', (json.dumps(pos),version))
            db.commit()
            return {'accepted': True, 'duplicate': False, 'stop_due': pos['stop_due'], 'account_version': version}

    def complete_quote(self, event_id):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT status FROM d_quotes WHERE event_id=?', (event_id,)).fetchone()
            if existing and existing['status'] == 'COMPLETE':
                db.rollback(); return {'accepted': True, 'duplicate': True}
            quote = self._pending_quote(db, event_id)
            if quote is None:
                db.rollback(); return {'accepted': False, 'reason': 'QUOTE_NOT_PENDING'}
            row = db.execute('SELECT position_json FROM d_account WHERE id=1').fetchone()
            if row['position_json']:
                pos = json.loads(row['position_json'])
                if Decimal(quote['bid']) <= Decimal(pos['stop_price']) and not pos.get('stop_due'):
                    db.rollback(); return {'accepted': False, 'reason': 'STOP_TRIGGER_UNHANDLED'}
            db.execute("UPDATE d_quotes SET status='COMPLETE' WHERE event_id=?", (event_id,))
            db.commit()
            return {'accepted': True, 'duplicate': False}

    def open_position(self, *, quantity, price, stop, leverage, idempotency_key,
                      expected_version, signal_id, atr, entry_bar_close, quote_event_id, entry_gate=None):
        quantity = _quantity(quantity)
        price, stop = _number(price, 'PRICE'), _number(stop, 'STOP')
        leverage = _number(leverage, 'LEVERAGE')
        atr = _number(atr, 'ATR')
        entry_bar_close = _bar_close(entry_bar_close)
        if stop >= price or leverage > 5 or not isinstance(signal_id, str) or not signal_id:
            raise ValueError('INVALID_OPEN_PARAMETERS')
        request = {'quantity': str(quantity), 'price': str(price), 'stop': str(stop),
                   'leverage': str(leverage), 'signal_id': signal_id, 'atr': str(atr),
                   'entry_bar_close': entry_bar_close, 'quote_event_id': quote_event_id}
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            digest, previous = self._prepare(db, idempotency_key, 'OPEN', request)
            if previous is not None:
                db.rollback()
                return previous
            if self._pending_quote(db, quote_event_id) is None:
                db.rollback(); return {'accepted': False, 'reason': 'QUOTE_NOT_PENDING'}
            row = db.execute('SELECT * FROM d_account WHERE id=1').fetchone()
            if row['version'] != expected_version:
                db.rollback(); return {'accepted': False, 'reason': 'VERSION_CONFLICT'}
            if row['position_json']:
                db.rollback(); return {'accepted': False, 'reason': 'POSITION_EXISTS'}
            if db.execute('SELECT 1 FROM d_used_signals WHERE signal_id=?', (signal_id,)).fetchone():
                db.rollback(); return {'accepted': False, 'reason': 'SIGNAL_ALREADY_USED'}
            cash = Decimal(row['cash']); fee = _fee(quantity, price)
            if cash < quantity * price / leverage + fee:
                db.rollback(); return {'accepted': False, 'reason': 'INSUFFICIENT_MARGIN'}
            trade_id, fill_id = str(uuid4()), str(uuid4())
            db.execute('INSERT INTO d_used_signals VALUES (?,?)', (signal_id, trade_id))
            position = {'trade_id': trade_id, 'direction': 'LONG', 'symbol': 'BTC/USD',
                        'quantity': str(quantity), 'initial_quantity': str(quantity),
                        'entry_price': str(price), 'stop_price': str(stop),
                        'initial_stop': str(stop), 'entry_fee': str(fee),
                        'entry_fee_allocated': '0.00', 'leverage': str(leverage),
                        'signal_id': signal_id, 'atr': str(atr),
                        'entry_bar_close': entry_bar_close, 'tp1_filled': False,
                        'pending_stop': None, 'pending_stop_after': None,
                        'tp1': str((price + price - stop).quantize(CENT, rounding=ROUND_UP)),
                        'tp2': str((price + 2 * (price - stop)).quantize(CENT, rounding=ROUND_UP))}
            new_version = row['version'] + 1
            db.execute('UPDATE d_account SET cash=?,version=?,position_json=? WHERE id=1',
                       (str(cash-fee), new_version, json.dumps(position)))
            db.execute('INSERT INTO d_fills VALUES (?,?,?,?,?,?,?,?,?)',
                       (fill_id, trade_id, 'OPEN', str(quantity), str(price), str(fee),
                        'D_ENTRY', _iso_now(), idempotency_key))
            result = {'accepted': True, 'duplicate': False, 'fill_id': fill_id,
                      'trade_id': trade_id, 'fee': str(fee), 'account_version': new_version}
            self._record(db, idempotency_key, 'OPEN', digest, result)
            if entry_gate is not None:
                if not entry_gate.commit(db):
                    db.rollback()
                    return {'accepted': False, 'reason': 'ENTRY_GATE_INVALIDATED'}
            else:
                db.commit()
            return result

    def close_position(self, *, quantity, price, reason, idempotency_key,
                       expected_version, trade_id, quote_event_id, bar_close=None):
        quantity = _quantity(quantity); price = _number(price, 'PRICE')
        if not isinstance(reason, str) or not reason or not isinstance(trade_id, str) or not trade_id:
            raise ValueError('INVALID_CLOSE_PARAMETERS')
        if reason == 'TP1':
            bar_close = _bar_close(bar_close)
        request = {'quantity': str(quantity), 'price': str(price), 'reason': reason, 'trade_id': trade_id, 'bar_close': bar_close, 'quote_event_id': quote_event_id}
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            digest, previous = self._prepare(db, idempotency_key, 'CLOSE', request)
            if previous is not None:
                db.rollback(); return previous
            if self._pending_quote(db, quote_event_id) is None:
                db.rollback(); return {'accepted': False, 'reason': 'QUOTE_NOT_PENDING'}
            row = db.execute('SELECT * FROM d_account WHERE id=1').fetchone()
            if row['version'] != expected_version:
                db.rollback(); return {'accepted': False, 'reason': 'VERSION_CONFLICT'}
            if not row['position_json']:
                db.rollback(); return {'accepted': False, 'reason': 'NO_POSITION'}
            pos = json.loads(row['position_json'])
            if pos['trade_id'] != trade_id:
                db.rollback(); return {'accepted': False, 'reason': 'TRADE_ID_MISMATCH'}
            if pos.get('stop_due') and reason not in ('STOP_DUE', 'STOP', 'STRATEGY_PAUSE', 'DAILY_LOSS'):
                db.rollback(); return {'accepted': False, 'reason': 'STOP_EXIT_PENDING'}
            if reason == 'STOP_DUE' and not pos.get('stop_due'):
                db.rollback(); return {'accepted': False, 'reason': 'STOP_NOT_PENDING'}
            remaining = Decimal(pos['quantity'])
            if quantity > remaining:
                db.rollback(); return {'accepted': False, 'reason': 'QUANTITY_EXCEEDS_POSITION'}
            initial = Decimal(pos['initial_quantity']); after = remaining - quantity
            if reason == 'TP1':
                half = (initial / 2 // QUANTITY_STEP) * QUANTITY_STEP
                if pos['tp1_filled'] or quantity != half or after <= 0:
                    db.rollback(); return {'accepted': False, 'reason': 'INVALID_TP1_QUANTITY_OR_STATE'}
                pos.update(tp1_filled=True, pending_stop=pos['entry_price'], pending_stop_after=bar_close)
            elif reason == 'TP2':
                if not pos['tp1_filled'] or after != 0:
                    db.rollback(); return {'accepted': False, 'reason': 'INVALID_TP2_QUANTITY_OR_STATE'}
            elif after != 0:
                db.rollback(); return {'accepted': False, 'reason': 'NON_TP1_PARTIAL_CLOSE_REJECTED'}
            fee = _fee(quantity, price); gross = _money((price-Decimal(pos['entry_price']))*quantity)
            allocated_total = Decimal(pos['entry_fee']) if after == 0 else _money(Decimal(pos['entry_fee'])*(initial-after)/initial)
            allocated = allocated_total - Decimal(pos['entry_fee_allocated'])
            net = gross - fee - allocated
            if after == 0:
                updated = None
            else:
                pos.update(quantity=str(after), entry_fee_allocated=str(allocated_total))
                updated = json.dumps(pos)
            cash = Decimal(row['cash']) + gross - fee
            realized = Decimal(row['realized_pnl']) + net
            version = row['version'] + 1; fill_id = str(uuid4())
            db.execute('UPDATE d_account SET cash=?,version=?,position_json=?,realized_pnl=? WHERE id=1',
                       (str(cash), version, updated, str(realized)))
            db.execute('INSERT INTO d_fills VALUES (?,?,?,?,?,?,?,?,?)',
                       (fill_id, trade_id, 'CLOSE', str(quantity), str(price), str(fee),
                        reason, _iso_now(), idempotency_key))
            result = {'accepted': True, 'duplicate': False, 'fill_id': fill_id,
                      'trade_id': trade_id, 'fee': str(fee), 'gross_pnl': str(gross),
                      'net_pnl': str(net), 'remaining_quantity': str(after), 'account_version': version}
            self._record(db, idempotency_key, 'CLOSE', digest, result); db.commit()
            return result

    def update_stop(self, *, stop, trade_id, idempotency_key, expected_version, current_bar_open, quote_event_id):
        stop = _number(stop, 'STOP')
        if not isinstance(trade_id, str) or not trade_id:
            raise ValueError('INVALID_TRADE_ID')
        current_bar_open = _bar_close(current_bar_open)
        request = {'stop': str(stop), 'trade_id': trade_id, 'current_bar_open': current_bar_open, 'quote_event_id': quote_event_id}
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            digest, previous = self._prepare(db, idempotency_key, 'STOP', request)
            if previous is not None:
                db.rollback(); return previous
            if self._pending_quote(db, quote_event_id) is None:
                db.rollback(); return {'accepted': False, 'reason': 'QUOTE_NOT_PENDING'}
            row = db.execute('SELECT * FROM d_account WHERE id=1').fetchone()
            if row['version'] != expected_version:
                db.rollback(); return {'accepted': False, 'reason': 'VERSION_CONFLICT'}
            if not row['position_json']:
                db.rollback(); return {'accepted': False, 'reason': 'NO_POSITION'}
            pos = json.loads(row['position_json'])
            if pos['trade_id'] != trade_id:
                db.rollback(); return {'accepted': False, 'reason': 'TRADE_ID_MISMATCH'}
            if pos.get('pending_stop') is None or current_bar_open < pos['pending_stop_after']:
                db.rollback(); return {'accepted': False, 'reason': 'STOP_NOT_EFFECTIVE_YET'}
            if stop != max(Decimal(pos['stop_price']), Decimal(pos['pending_stop'])):
                db.rollback(); return {'accepted': False, 'reason': 'INVALID_PENDING_STOP'}
            if stop < Decimal(pos['stop_price']):
                db.rollback(); return {'accepted': False, 'reason': 'STOP_CANNOT_DECREASE'}
            pos['stop_price'] = str(stop); pos['pending_stop'] = None; pos['pending_stop_after'] = None
            version = row['version'] + 1
            db.execute('UPDATE d_account SET position_json=?,version=? WHERE id=1', (json.dumps(pos), version))
            result = {'accepted': True, 'duplicate': False, 'trade_id': trade_id,
                      'stop': str(stop), 'account_version': version}
            self._record(db, idempotency_key, 'STOP', digest, result); db.commit()
            return result
