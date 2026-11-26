"""Read-only C fill feed and isolated follower replay. No exchange transport."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import sqlite3
import json


@dataclass(frozen=True)
class Fill:
    cursor: int
    event_id: str
    trade_id: str
    version: str
    action: str
    direction: str
    quantity: Decimal
    price: Decimal
    occurred_at: datetime


def paper_fills(path, after=None, *, strategy='C'):
    """Without a cursor, return the current watermark for a new subscription."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if strategy.startswith('pa-'):
            db.execute('BEGIN')
            return pa_pool_fills(db,strategy,after)
        if strategy == 'G':
            if after is None:
                return db.execute('SELECT coalesce(max(rowid),0) FROM g_fills').fetchone()[0]
            rows = db.execute("""SELECT rowid,fill_id,g_trade_id,'MIRROR',effect,direction,
                quantity,price,occurred_at FROM g_fills WHERE rowid>? ORDER BY rowid LIMIT 500""",(after,))
        elif after is None:
            return db.execute("SELECT coalesce(max(rowid),0) FROM fills WHERE agent=?",(strategy,)).fetchone()[0]
        else:
            rows = db.execute("""SELECT f.rowid,f.fill_id,f.trade_id,
            coalesce(f.strategy_version, (SELECT min(o.strategy_version) FROM fills o
                WHERE o.agent=f.agent AND o.trade_id=f.trade_id AND o.position_effect='OPEN'
                AND o.position_direction=f.position_direction AND o.rowid<f.rowid
                HAVING count(*)=1)),f.position_effect,f.position_direction,f.quantity,f.price,f.occurred_at
                FROM fills f WHERE f.agent=? AND f.rowid>? ORDER BY f.rowid LIMIT 500""", (strategy,after))
        fills = []
        for cursor, fid, trade, version, effect, direction, qty, price, timestamp in rows:
            action = {'OPEN':'OPEN', 'CLOSE':'FULL_CLOSE', 'ADD':'ADD',
                      'PARTIAL_CLOSE':'PARTIAL_CLOSE'}.get(effect)
            qty, price = Decimal(str(qty)), Decimal(str(price))
            at = datetime.fromisoformat(timestamp)
            if (not all((fid, trade, version, action, at.utcoffset() is not None))
                    or direction not in ('LONG','SHORT')
                    or any(not n.is_finite() or n <= 0 for n in (qty, price))):
                raise ValueError(f'UNVERIFIED_{strategy}_FILL at row {cursor}')
            fills.append(Fill(cursor, f'{strategy}:{fid}', trade, version, action, direction, qty, price, at))
        return fills


def pa_pool_fills(db, strategy, after):
    """Use explicit writer action keys and the pool's single-position invariant."""
    rows = db.execute("""SELECT id,version,side,qty,price,reason,idempotency_key,created_at,cycle
        FROM fills WHERE strategy_id=? ORDER BY id""",(strategy,)).fetchall()
    account = db.execute('SELECT position FROM accounts WHERE strategy_id=?',(strategy,)).fetchone()
    if account is None:
        raise ValueError('PA_ACCOUNT_MISSING')
    position = json.loads(account[0]) if account[0] else None
    active = None
    events = []
    for fid,version,side,qty,price,reason,key,stamp,cycle in rows:
        qty,price = Decimal(qty),Decimal(price)
        at = datetime.fromisoformat(stamp)
        if (side not in ('BUY','SELL') or not version or at.utcoffset() is None
                or any(not n.is_finite() or n <= 0 for n in (qty,price))):
            raise ValueError(f'PA_INVALID_FILL:{fid}')
        if reason in ('ENTRY','ENTRY_NEXT_OPEN') and key.startswith(f'{strategy}:open:'):
            if active is not None:
                raise ValueError(f'PA_OVERLAPPING_OPEN:{fid}')
            direction = 'LONG' if side=='BUY' else 'SHORT'
            trade = f'{strategy}:open:{fid}'
            active = (trade,direction,qty,version,cycle)
            action = 'OPEN'
        elif ((reason in ('STOP','TARGET','TIME') and key==f'{strategy}:close:{cycle}')
              or (reason in ('STOP_QUOTE','TARGET_QUOTE') and key.startswith(f'{strategy}:quote-{reason.lower()}:'))):
            if active is None:
                raise ValueError(f'PA_OPEN_LINK_MISSING:{fid}')
            trade,direction,opened_qty,opened_version,_ = active
            if qty != opened_qty or version != opened_version or side != ('SELL' if direction=='LONG' else 'BUY'):
                raise ValueError(f'PA_FULL_CLOSE_MISMATCH:{fid}')
            active = None
            action = 'FULL_CLOSE'
        else:
            raise ValueError(f'PA_ACTION_UNVERIFIED:{fid}')
        events.append(Fill(fid,f'{strategy}:{fid}',trade,version,action,direction,qty,price,at))
    if active is None and position is not None or active is not None and position is None:
        raise ValueError('PA_ACCOUNT_POSITION_MISMATCH')
    if active is not None and (position.get('direction'),Decimal(position['qty']),position.get('version'),
            datetime.fromisoformat(position['opened_cycle'])) != (
                active[1],active[2],active[3],datetime.fromisoformat(active[4])):
        raise ValueError('PA_ACCOUNT_POSITION_MISMATCH')
    if after is None:
        return events[-1].cursor if events else 0
    return [event for event in events if event.cursor > after][:500]


@dataclass(frozen=True)
class MarginEvidence:
    event_id: str
    margin: Decimal
    balance_before: Decimal
    source: str


def planned_margin(event, *, budget, fixed=None, evidence=None):
    if not budget.is_finite() or budget <= 0:
        raise ValueError('INVALID_BUDGET')
    if fixed is not None:
        if not fixed.is_finite() or not 0 < fixed <= budget:
            raise ValueError('INVALID_FIXED_MARGIN')
        return fixed
    if (not evidence or evidence.event_id != event.event_id or not evidence.source.strip()
            or not evidence.margin.is_finite() or not evidence.balance_before.is_finite()
            or not 0 < evidence.margin <= evidence.balance_before):
        raise ValueError('MARGIN_EVIDENCE_MISSING')
    return budget * evidence.margin / evidence.balance_before


def order_quantity(*, margin, available, price, planned_leverage, actual_leverage,
                   step, minimum, maximum, min_notional, fee, reserve, risk_cap, stop_distance):
    from scripts.live_copy_env import MAX_COPY_LEVERAGE
    values = (margin, available, price, step, minimum, maximum, min_notional,
              fee, reserve, risk_cap)
    if (any(not n.is_finite() or n < 0 for n in values) or min(price, step, risk_cap) <= 0
            or stop_distance is not None and (not stop_distance.is_finite() or stop_distance <= 0)):
        raise ValueError('INVALID_ORDER_INPUT')
    if not 1 <= actual_leverage <= planned_leverage <= MAX_COPY_LEVERAGE:
        raise ValueError('LEVERAGE_MISMATCH')
    cash = max(Decimal(0), min(margin, available) - reserve)
    cost = price / actual_leverage + price * fee * 2
    qty = min(cash / cost, maximum)
    if stop_distance is not None:
        qty = min(qty, risk_cap / (stop_distance + price * fee * 2))
    qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    if qty <= 0 or qty < minimum or qty * price < min_notional:
        raise ValueError('SKIPPED_MIN_ORDER')
    return qty


class ReplayLedger:
    """One strategy and account per replay database. Quantities are simulated."""
    def __init__(self, path, *, start_cursor):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""CREATE TABLE IF NOT EXISTS cursor_state(id INTEGER PRIMARY KEY,
                cursor INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, trade TEXT, result TEXT, quantity TEXT);
                CREATE TABLE IF NOT EXISTS positions(trade TEXT PRIMARY KEY, direction TEXT,
                paper TEXT, follower TEXT, state TEXT);""")
            db.execute('INSERT OR IGNORE INTO cursor_state VALUES (1,?)', (start_cursor,))

    def consume(self, fill, *, replay_open_quantity=None, exit_step=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM events WHERE id=?', (fill.event_id,)).fetchone():
                return 'DUPLICATE'
            if fill.cursor <= db.execute('SELECT cursor FROM cursor_state').fetchone()[0]:
                return 'BEFORE_CURSOR'
            row = db.execute('SELECT direction,paper,follower,state FROM positions WHERE trade=?',
                             (fill.trade_id,)).fetchone()
            executed = Decimal(0)
            result = 'SKIPPED'
            if fill.action == 'OPEN' and row is None:
                if replay_open_quantity is not None and replay_open_quantity > 0:
                    executed, result = replay_open_quantity, 'OPEN'
                db.execute('INSERT INTO positions VALUES (?,?,?,?,?)',
                           (fill.trade_id,fill.direction,str(fill.quantity),str(executed),
                            'ACTIVE' if executed else 'SKIPPED'))
            elif row and row[3] == 'ACTIVE':
                direction, paper, follower, _ = row
                paper, follower = Decimal(paper), Decimal(follower)
                next_paper, next_follower, state = paper, follower, 'ACTIVE'
                if direction != fill.direction:
                    result = 'RECONCILE_REQUIRED'
                elif fill.action == 'FULL_CLOSE' and fill.quantity == paper:
                    executed, result = follower, 'FULL_CLOSE'
                    next_paper, next_follower, state = Decimal(0), Decimal(0), 'CLOSED'
                elif fill.action == 'PARTIAL_CLOSE' and 0 < fill.quantity < paper and exit_step and exit_step > 0:
                    executed = (follower * fill.quantity / paper / exit_step).to_integral_value(
                        rounding=ROUND_DOWN) * exit_step
                    result = 'PARTIAL_CLOSE' if executed else 'RECONCILE_REQUIRED'
                    next_paper, next_follower = paper - fill.quantity, follower - executed
                elif fill.action == 'ADD' and replay_open_quantity and replay_open_quantity > 0:
                    executed, result = replay_open_quantity, 'ADD'
                    next_paper, next_follower = paper + fill.quantity, follower + executed
                else:
                    result = 'RECONCILE_REQUIRED'
                db.execute('UPDATE positions SET paper=?, follower=?, state=? WHERE trade=?',
                           (str(next_paper),str(next_follower),
                            result if result == 'RECONCILE_REQUIRED' else state,fill.trade_id))
            elif row and row[3] == 'RECONCILE_REQUIRED':
                result = 'RECONCILE_REQUIRED'
            db.execute('INSERT INTO events VALUES (?,?,?,?)',
                       (fill.event_id,fill.trade_id,result,str(executed)))
            db.execute('UPDATE cursor_state SET cursor=? WHERE id=1', (fill.cursor,))
            return result

    def early_exit(self, trade_id):
        """Record a confirmed isolated replay exit; later paper adds remain skipped."""
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("""UPDATE positions SET state='FOLLOWER_EARLY_EXIT', follower='0'
                WHERE trade=? AND state='ACTIVE'""", (trade_id,)).rowcount == 1


if __name__ == '__main__':
    from argparse import ArgumentParser
    from collections import Counter
    from tempfile import TemporaryDirectory

    parser = ArgumentParser(description='Replay persisted C fills offline')
    parser.add_argument('paper_db', type=Path)
    parser.add_argument('--fixture-quantity', required=True, type=Decimal)
    args = parser.parse_args()
    if not args.fixture_quantity.is_finite() or args.fixture_quantity <= 0:
        parser.error('fixture quantity must be positive and finite')
    with TemporaryDirectory(prefix='v3-copy-replay-') as folder:
        ledger = ReplayLedger(Path(folder) / 'replay.sqlite3', start_cursor=0)
        cursor = 0
        end = paper_fills(args.paper_db)
        results = Counter()
        while cursor < end:
            batch = [fill for fill in paper_fills(args.paper_db, cursor) if fill.cursor <= end]
            if not batch:
                raise SystemExit('SOURCE_CURSOR_CHANGED')
            for fill in batch:
                results[ledger.consume(fill, replay_open_quantity=args.fixture_quantity)] += 1
            cursor = batch[-1].cursor
        print('ISOLATED_REPLAY', dict(results))
