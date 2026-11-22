"""Reconcile D circuit trade history from committed paper-ledger operations.

This isolated helper makes no trading decisions and opens no connections.
Callers must run it before permitting entry, and fail closed on any exception.
"""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from backend.d_executor import validate_quote
from backend.d_margin import account_metrics


def replay_closed_trades(ledger, circuit):
    """Replay complete trades only; circuit uses trade_id for idempotence."""
    with sqlite3.connect(ledger.path, timeout=10) as db:
        db.execute('BEGIN')
        position_row = db.execute('SELECT position_json FROM d_account WHERE id=1').fetchone()
        if position_row is None:
            raise RuntimeError('D_ACCOUNT_MISSING')
        position = json.loads(position_row[0]) if position_row[0] else None
        current_trade = position.get('trade_id') if position else None
        rows = db.execute('''SELECT f.trade_id,f.effect,f.quantity,f.occurred_at,
                                    f.idempotency_key,o.operation,o.response_json
                             FROM d_fills f LEFT JOIN d_operations o
                               ON o.idempotency_key=f.idempotency_key ORDER BY f.rowid''').fetchall()
    trades = {}
    for trade_id,effect,quantity,occurred_at,key,operation,response in rows:
        if effect not in ('OPEN','CLOSE') or operation != effect or not response:
            raise RuntimeError('D_TRADE_HISTORY_INCOMPLETE')
        details = json.loads(response)
        if not details.get('accepted') or details.get('trade_id') != trade_id:
            raise RuntimeError('D_TRADE_HISTORY_CONFLICT')
        item = trades.setdefault(trade_id, {'opened':None,'closed':Decimal('0'),'net':Decimal('0'),'last':None})
        amount = Decimal(quantity)
        if not amount.is_finite() or amount <= 0:
            raise RuntimeError('D_TRADE_QUANTITY_INVALID')
        if effect == 'OPEN':
            if item['opened'] is not None or item['last']:
                raise RuntimeError('D_TRADE_OPEN_CONFLICT')
            item['opened'] = amount
        else:
            if item['opened'] is None:
                raise RuntimeError('D_TRADE_OPEN_MISSING')
            item['closed'] += amount
            pnl = Decimal(str(details['net_pnl']))
            if not pnl.is_finite() or item['closed'] > item['opened']:
                raise RuntimeError('D_TRADE_CLOSE_CONFLICT')
            item['net'] += pnl
            item['last'] = datetime.fromisoformat(occurred_at)
    complete = []
    for trade_id,item in trades.items():
        if item['closed'] == item['opened'] and item['opened'] is not None:
            if trade_id == current_trade:
                raise RuntimeError('D_TRADE_POSITION_CONFLICT')
            complete.append((item['last'],trade_id,item['net']))
        elif trade_id != current_trade:
            raise RuntimeError('D_TRADE_POSITION_MISSING')
    complete.sort(key=lambda item:item[0])
    for closed_at,trade_id,net in complete:
        result = circuit.record_closed_trade(trade_id=trade_id, net_pnl=net, closed_at=closed_at)
        if not result.get('accepted'):
            raise RuntimeError('D_CIRCUIT_REPLAY_'+str(result.get('reason')))
    return len(complete)


def proven_flat_midnight_equity(ledger, now):
    """Derive UTC opening equity only when ledger proves flat at midnight."""
    if now.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    now = now.astimezone(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with sqlite3.connect(ledger.path, timeout=10) as db:
        db.execute('BEGIN')
        account = db.execute('SELECT cash,version,position_json FROM d_account WHERE id=1').fetchone()
        rows = db.execute('''SELECT f.trade_id,f.effect,f.quantity,f.fee,f.occurred_at,o.operation,o.response_json
                             FROM d_fills f LEFT JOIN d_operations o ON o.idempotency_key=f.idempotency_key
                             ORDER BY f.rowid''').fetchall()
    if not account:
        raise RuntimeError('D_ACCOUNT_MISSING')
    cash, position, previous, opening = Decimal('10000.00'), None, None, None
    for trade_id,effect,quantity,fee,stamp,operation,response in rows:
        at = datetime.fromisoformat(stamp)
        if at.tzinfo is None or at > now or (previous and at <= previous):
            raise RuntimeError('D_LEDGER_TIME_UNTRUSTED')
        previous = at
        if opening is None and at >= midnight:
            if position is not None:
                raise RuntimeError('D_MIDNIGHT_POSITION_UNVALUED')
            opening = cash
        if operation != effect or not response:
            raise RuntimeError('D_TRADE_HISTORY_INCOMPLETE')
        details = json.loads(response)
        if not details.get('accepted') or details.get('trade_id') != trade_id:
            raise RuntimeError('D_TRADE_HISTORY_CONFLICT')
        quantity,fee = Decimal(quantity),Decimal(fee)
        if not quantity.is_finite() or quantity <= 0 or not fee.is_finite() or fee < 0:
            raise RuntimeError('D_TRADE_HISTORY_CONFLICT')
        if effect == 'OPEN':
            if position is not None:
                raise RuntimeError('D_TRADE_OPEN_CONFLICT')
            position = [trade_id,quantity]
            cash -= fee
        elif effect == 'CLOSE':
            if position is None or position[0] != trade_id or quantity > position[1]:
                raise RuntimeError('D_TRADE_CLOSE_CONFLICT')
            gross = Decimal(str(details['gross_pnl']))
            if not gross.is_finite():
                raise RuntimeError('D_TRADE_CLOSE_CONFLICT')
            cash += gross - fee
            position[1] -= quantity
            if position[1] == 0:
                position = None
        else:
            raise RuntimeError('D_TRADE_HISTORY_CONFLICT')
    if opening is None:
        if position is not None:
            raise RuntimeError('D_MIDNIGHT_POSITION_UNVALUED')
        opening = cash
    actual = json.loads(account[2]) if account[2] else None
    if (cash != Decimal(account[0]) or bool(actual) != bool(position)
            or (actual and (actual['trade_id'] != position[0] or Decimal(actual['quantity']) != position[1]))):
        raise RuntimeError('D_ACCOUNT_REPLAY_MISMATCH')
    return midnight, opening


def assess_from_committed_ledger(ledger, circuit, quote, now):
    """Fail closed if day-start equity, a fill, or the live mark is unproven."""
    try:
        if not validate_quote(quote, now):
            raise RuntimeError('D_CIRCUIT_QUOTE_INVALID')
        replay_closed_trades(ledger, circuit)
        day = now.astimezone(timezone.utc).date().isoformat()
        with sqlite3.connect(circuit.path, timeout=10) as db:
            existing = db.execute('SELECT provenance FROM d_days WHERE day=?', (day,)).fetchone()
        if existing and existing[0] != 'D_LEDGER_FLAT_MIDNIGHT_V1':
            raise RuntimeError('D_DAY_START_PROVENANCE_UNTRUSTED')
        if not existing:
            midnight, equity = proven_flat_midnight_equity(ledger, now)
            result = circuit.record_day_start(midnight=midnight,equity=equity,
                                               provenance='D_LEDGER_FLAT_MIDNIGHT_V1')
            if not result.get('accepted'):
                raise RuntimeError('D_DAY_START_CONFLICT')
        account = ledger.account()
        metrics = account_metrics(account['cash'], account['position'], quote['bid'])
        if metrics.get('equity') is None:
            raise RuntimeError('D_CIRCUIT_EQUITY_UNAVAILABLE')
        return circuit.assess(now=now,equity=metrics['equity'])
    except Exception as exc:
        return {'block_entries':True, 'flatten_required':False, 'allow_exits':True,
                'reason':str(exc) if isinstance(exc, RuntimeError) else 'D_CIRCUIT_UNAVAILABLE',
                'detail':type(exc).__name__}
