"""D research account and closed-candle observation, isolated from ABC/E ledgers.

Trading is deliberately unavailable until D's partial-exit and recovery state
machine is implemented and verified. This module never places an order.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

D_EXECUTION_REASON = 'EXECUTION_NOT_IMPLEMENTED'


def initialize_store(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with sqlite3.connect(path, timeout=10) as db:
        if exists:
            try:
                marker = db.execute("SELECT value FROM d_identity WHERE key='owner'").fetchone()
            except sqlite3.Error as exc:
                raise ValueError('D_STORE_IDENTITY_INVALID') from exc
            if marker != ('D',):
                raise ValueError('D_STORE_IDENTITY_INVALID')
        else:
            db.execute('CREATE TABLE d_identity(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute("INSERT INTO d_identity VALUES('owner','D')")
            db.execute('CREATE TABLE d_state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.execute("INSERT INTO d_state VALUES(1, '{\"phase\":\"IDLE\"}')")
            db.execute('CREATE TABLE d_cycles(close_at TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE d_account(id INTEGER PRIMARY KEY CHECK(id=1), currency TEXT NOT NULL, cash TEXT NOT NULL)')
            db.execute("INSERT INTO d_account VALUES(1, 'USD_RESEARCH', '10000.00')")


def observe_cycle(path, archive, close_at, now, *, common_guard=False):
    """Atomically process only this current natural cycle; never backfill orders."""
    from backend.d_signal import evaluate
    if close_at.tzinfo is None or now.tzinfo is None:
        raise ValueError('NAIVE_TIMESTAMP')
    close_at = close_at.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    if close_at.minute % 15 or close_at.second or close_at.microsecond:
        raise ValueError('INVALID_CLOSE')
    if not close_at <= now < close_at + timedelta(minutes=15):
        return {'status': 'CYCLE_EXPIRED', 'reason': 'CYCLE_EXPIRED'}
    key = close_at.isoformat()
    # Avoid rereading the complete candle archive every minute after a
    # successful cycle. The write transaction below still rechecks this key
    # because another worker may have won the race after this read.
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        if db.execute('SELECT 1 FROM d_cycles WHERE close_at=?', (key,)).fetchone():
            return {'status': 'DUPLICATE_BAR', 'reason': 'DUPLICATE_BAR'}
    # No future bars. The signal module enforces only its indicator lookback,
    # not the research JSON's 960-bar preload requirement.
    bars = archive.read(close_at - timedelta(minutes=15 * 1000), close_at)
    bars = [bar for bar in bars if datetime.fromisoformat(bar['close_at'].replace('Z', '+00:00')).astimezone(timezone.utc) <= close_at]
    with sqlite3.connect(path, timeout=10, isolation_level=None) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if db.execute('SELECT 1 FROM d_cycles WHERE close_at=?', (key,)).fetchone():
                db.rollback()
                return {'status': 'DUPLICATE_BAR', 'reason': 'DUPLICATE_BAR'}
            row = db.execute('SELECT payload FROM d_state WHERE id=1').fetchone()
            state = json.loads(row[0])
            last = state.get('last_close')
            if last and datetime.fromisoformat(last).astimezone(timezone.utc) >= close_at:
                db.rollback()
                return {'status': 'STALE_BAR', 'reason': 'STALE_BAR'}
            # If execution is locked, a pending signal expires on the following
            # cycle and enters the specified four-bar cooldown; no order is sent.
            if state.get('phase') == 'PENDING':
                state = {'phase': 'COOLDOWN', 'remaining': 5, 'last_close': state.get('last_close')}
            if common_guard and last and datetime.fromisoformat(last) < close_at - timedelta(minutes=15):
                if state.get('phase') == 'WATCH':
                    state = {'phase': 'COOLDOWN', 'remaining': 5}
                else:
                    state.pop('last_close', None)
            if not bars or datetime.fromisoformat(bars[-1]['close_at'].replace('Z', '+00:00')).astimezone(timezone.utc) != close_at:
                output = {'status': 'DATA_NOT_READY', 'reason': 'LATEST_CANDLE_MISSING', 'state': state, 'signal': None}
            else:
                output = evaluate(bars, state)
            if output['status'] == 'DATA_NOT_READY':
                if common_guard:
                    db.rollback()
                    return output
                # Invalid data invalidates any old setup; no delayed replay.
                state = {'phase': 'IDLE'}
            else:
                state = output['state']
            payload = json.dumps(output, ensure_ascii=False, allow_nan=False)
            db.execute('INSERT INTO d_cycles VALUES(?,?,?,?)', (key, output['status'], output['reason'], payload))
            db.execute('UPDATE d_state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False, allow_nan=False),))
            if common_guard and output.get('signal'):
                db.execute('INSERT INTO d_signal_decision_times VALUES (?,?)',
                           (output['signal']['signal_close'], now.isoformat()))
            db.commit()
            return output
        except Exception:
            db.rollback()
            raise


def read_status(path):
    """Read D data without changing any account or protection state."""
    path = Path(path).resolve()
    result = {'status': 'UNAVAILABLE', 'reason': 'D_STORE_NOT_INITIALIZED',
              'account': None, 'equity': None, 'realized_pnl': None,
              'unrealized_pnl': None, 'fills': [], 'decision': None,
              'control_state': 'OBSERVE_ONLY', 'execution_ready': False}
    from backend.d_signal import RULE_VERSION, SOURCE_COMMIT
    result.update(rule_version=RULE_VERSION, source_commit=SOURCE_COMMIT)
    if not path.is_file():
        return result
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            marker = db.execute("SELECT value FROM d_identity WHERE key='owner'").fetchone()
            if marker != ('D',):
                result['reason'] = 'D_STORE_IDENTITY_INVALID'
                return result
            account = db.execute('SELECT currency,cash FROM d_account WHERE id=1').fetchone()
            latest = db.execute('SELECT close_at,status,reason,payload FROM d_cycles ORDER BY close_at DESC LIMIT 1').fetchone()
            if not account:
                result['reason'] = 'D_ACCOUNT_MISSING'
                return result
            result.update(status='OBSERVE_ONLY', reason=D_EXECUTION_REASON,
                          account={'agent': 'D', 'state': 'FLAT', 'currency': account[0], 'cash': account[1], 'position_json': None},
                          equity=account[1], realized_pnl='0.00', unrealized_pnl='0.00')
            if latest:
                result['decision'] = {'candle_close': latest[0], 'action': latest[1], 'reason': latest[2]}
                result['signal'] = json.loads(latest[3]).get('signal')
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        result.update(status='UNAVAILABLE', reason='D_STORE_READ_ERROR', account=None,
                      equity=None, realized_pnl=None, unrealized_pnl=None, decision=None)
    return result
