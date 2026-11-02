"""Confirmed seven-candle E structure; never consumes unclosed right-hand bars."""
from decimal import Decimal
import json
import sqlite3
from contextlib import closing
from datetime import datetime,UTC,timedelta


def confirmed_pivots(bars,kind):
    if kind not in {'LOW','HIGH'}:raise ValueError('INVALID_STRUCTURE_KIND')
    if any(row.get('is_closed') is not True for row in bars):raise ValueError('UNCLOSED_CANDLE')
    field='low' if kind=='LOW' else 'high'
    values=[Decimal(str(row[field])) for row in bars]
    if any(not value.is_finite() or value<=0 for value in values):raise ValueError('INVALID_PRICE')
    return [i for i in range(3,len(values)-3)
            if all((values[i]<values[j] if kind=='LOW' else values[i]>values[j])
                   for j in (*range(i-3,i),*range(i+1,i+4)))]


def structure_action(bars,*,direction,scale):
    if direction not in {'LONG','SHORT'}:raise ValueError('INVALID_DIRECTION')
    scale=Decimal(str(scale))
    if not scale.is_finite() or scale<=0:raise ValueError('INVALID_SCALE')
    low=direction=='LONG';kind='LOW' if low else 'HIGH';field='low' if low else 'high'
    pivots=confirmed_pivots(bars,kind)
    if not pivots:return {'action':'HOLD','reason':'NO_CONFIRMED_STRUCTURE'}
    current=pivots[-1];level=Decimal(str(bars[current][field]));close=Decimal(str(bars[-1]['close']))
    if not close.is_finite() or close<=0:raise ValueError('INVALID_CLOSE')
    if (close<level if low else close>level):return {'action':'EXIT','pivot_index':current,'level':level}
    if len(pivots)<2:return {'action':'HOLD','reason':'NO_PRIOR_STRUCTURE'}
    previous=Decimal(str(bars[pivots[-2]][field]))
    if not (level>previous if low else level<previous):return {'action':'HOLD','reason':'NO_IMPROVEMENT'}
    return {'action':'TIGHTEN','pivot_index':current,'level':level,'stop':level+Decimal('0.15')*scale*(-1 if low else 1)}


def process_structure_close(*,store,bars,now):
    """Persist a validated closed-bar structure action independently of model and quote."""
    from backend.e_visual import resolve_e_decision
    from backend.e_quant_v11 import _scale
    from backend.e_trailing import set_protective_stop
    account=store.account('E')
    if account['state'] not in {'OPEN_LONG','OPEN_SHORT'} or not account['position_json']:
        return {'action':'NO_OPEN_POSITION'}
    try:
        resolve_e_decision({'action':'SKIP'},bars,mapping_verified=False)
        if any(b.get('symbol')!='BTC/USD' or b.get('source')!='ALPACA_US_BTCUSD_SPOT' or b.get('currency')!='USD_RESEARCH' or str(b.get('interval','')).lower() not in {'15min','15m'} for b in bars):raise ValueError('SNAPSHOT_SOURCE_INVALID')
        close=datetime.fromisoformat(bars[-1]['close_at'].replace('Z','+00:00')).astimezone(UTC)
        if now.tzinfo is None or not close<=now.astimezone(UTC)<close+timedelta(minutes=15) or close.minute%15 or close.second or close.microsecond:raise ValueError('SNAPSHOT_TIME_INVALID')
        key=close.isoformat();position=json.loads(account['position_json']);trade=position['trade_id']
        with closing(sqlite3.connect(store.path,timeout=10)) as db,db:
            entry=db.execute("SELECT occurred_at FROM fills WHERE trade_id=? AND position_effect='OPEN'",(trade,)).fetchone()
            if not entry:raise ValueError('ENTRY_FILL_MISSING')
            entry_at=datetime.fromisoformat(entry[0].replace('Z','+00:00'))
            if entry_at.tzinfo is None or close<=entry_at.astimezone(UTC):raise ValueError('BAR_NOT_AFTER_ENTRY')
            db.execute('CREATE TABLE IF NOT EXISTS e_structure_events(trade_id TEXT NOT NULL,close_at TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(trade_id,close_at))')
            db.execute('CREATE TABLE IF NOT EXISTS e_structure_due(trade_id TEXT PRIMARY KEY,close_at TEXT NOT NULL,payload TEXT NOT NULL)')
            if not db.execute('INSERT OR IGNORE INTO e_structure_events VALUES(?,?,?,?)',(trade,key,'PROCESSING','{}')).rowcount:return {'action':'DUPLICATE_BAR'}
        numeric=[{**b,**{k:Decimal(str(b[k])) for k in ('open','high','low','close')}} for b in bars]
        action=structure_action(bars,direction=position['direction'],scale=_scale(numeric,len(numeric)-1)[2])
        if action['action']=='EXIT':
            with closing(sqlite3.connect(store.path,timeout=10)) as db,db:
                db.execute('INSERT OR IGNORE INTO e_structure_due VALUES(?,?,?)',(trade,key,json.dumps(action,default=str)))
            action={**action,'action':'EXIT_PENDING'}
        elif action['action']=='TIGHTEN':
            confirmed_at=datetime.fromisoformat(bars[action['pivot_index']+3]['close_at'].replace('Z','+00:00'))
            if confirmed_at.astimezone(UTC)<=entry_at.astimezone(UTC):
                action={'action':'HOLD','reason':'STRUCTURE_CONFIRMED_BEFORE_ENTRY'}
            else:
                action={**action,'protection':set_protective_stop(store,stop=action['stop'],event_id='E-structure:'+trade+':'+key,expected_trade_id=trade)}
        with closing(sqlite3.connect(store.path,timeout=10)) as db,db:
            db.execute('UPDATE e_structure_events SET action=?,payload=? WHERE trade_id=? AND close_at=?',(action['action'],json.dumps(action,default=str),trade,key))
        return action
    except (ValueError,KeyError,TypeError,ArithmeticError,sqlite3.Error) as exc:
        return {'action':'REJECTED','reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__}


def execute_structure_due(store,quote,cost):
    """Use the frozen quote-side fill primitive for an already persisted structure exit."""
    from backend.domain import BookLevel,simulate_fill
    account=store.account('E')
    if account['state'] not in {'OPEN_LONG','OPEN_SHORT'} or not account['position_json']:return {'accepted':False,'reason':'NO_OPEN_POSITION'}
    try:
        position=json.loads(account['position_json']);trade=position['trade_id']
        with closing(sqlite3.connect(store.path,timeout=10)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='e_structure_due'").fetchone():return {'accepted':False,'reason':'NO_STRUCTURE_EXIT_DUE'}
            due=db.execute('SELECT close_at FROM e_structure_due WHERE trade_id=?',(trade,)).fetchone()
        if not due:return {'accepted':False,'reason':'NO_STRUCTURE_EXIT_DUE'}
        if not quote or quote.get('source')!='ALPACA_US_BTCUSD_SPOT' or quote.get('currency')!='USD_RESEARCH' or quote.get('timestamp_valid') is not True or not 0<=int(quote.get('age_ms',10**9))<=cost.max_quote_age_ms:return {'accepted':False,'reason':'QUOTE_INVALID'}
        event_at=datetime.fromisoformat(str(quote['event_at']).replace('Z','+00:00'))
        due_at=datetime.fromisoformat(due[0])
        if event_at.tzinfo is None or event_at.astimezone(UTC)<due_at.astimezone(UTC):return {'accepted':False,'reason':'QUOTE_BEFORE_STRUCTURE_CLOSE'}
        bid=Decimal(str(quote['bid']));ask=Decimal(str(quote['ask']));quantity=Decimal(position['quantity'])
        if not bid.is_finite() or not ask.is_finite() or bid<=0 or ask<bid:return {'accepted':False,'reason':'QUOTE_INVALID'}
        side='SELL' if position['direction']=='LONG' else 'BUY';fill=simulate_fill(side,quantity,[BookLevel(bid,Decimal(0))],[BookLevel(ask,Decimal(0))],cost)
        if not fill:return {'accepted':False,'reason':'FILL_UNAVAILABLE'}
        price,fee=fill;reference=bid if side=='SELL' else ask
        return store.close_position(agent='E',price=price,fee=fee,slippage=abs(price-reference)*quantity,idempotency_key='E-structure-exit:'+trade,expected_version=account['version'],reason='E_STRUCTURE_EXIT',quote_id=quote.get('event_id'),rationale='E confirmed structure exit '+due[0],cost_model=cost)
    except (KeyError,ValueError,TypeError,ArithmeticError,sqlite3.Error):return {'accepted':False,'reason':'STRUCTURE_EXIT_ERROR'}
