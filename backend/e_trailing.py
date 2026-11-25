"""E-only persisted protective-stop adapter; no shared-ledger modifications."""
import json
import sqlite3
from contextlib import closing
from decimal import Decimal,InvalidOperation
from backend.execution import recover_pending_stop


def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS e_protective_stops(trade_id TEXT PRIMARY KEY,stop TEXT NOT NULL,event_id TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS e_protection_events(event_id TEXT PRIMARY KEY,trade_id TEXT NOT NULL,stop TEXT NOT NULL)')


def set_protective_stop(store,*,stop,event_id,expected_trade_id=None):
    try:
        value=Decimal(str(stop))
        if not value.is_finite() or value<=0 or not isinstance(event_id,str) or not event_id:raise ValueError()
        with closing(sqlite3.connect(store.path,timeout=10)) as db,db:
            db.execute('BEGIN IMMEDIATE');_schema(db)
            row=db.execute("SELECT state,position_json FROM accounts WHERE agent='A'").fetchone()
            if not row or row[0] not in ('OPEN_LONG','OPEN_SHORT') or not row[1]:return {'accepted':False,'reason':'NO_OPEN_POSITION'}
            position=json.loads(row[1]);trade=position['trade_id'];direction=position['direction']
            if expected_trade_id is not None and trade!=expected_trade_id:return {'accepted':False,'reason':'POSITION_CHANGED'}
            if db.execute('SELECT 1 FROM e_protection_events WHERE event_id=?',(event_id,)).fetchone():return {'accepted':False,'reason':'DUPLICATE_EVENT'}
            current=db.execute('SELECT stop FROM e_protective_stops WHERE trade_id=?',(trade,)).fetchone()
            old=Decimal(current[0] if current else position['stop_price'])
            if (direction=='LONG' and value<=old) or (direction=='SHORT' and value>=old):return {'accepted':False,'reason':'STOP_NOT_TIGHTER'}
            db.execute('INSERT INTO e_protective_stops VALUES(?,?,?) ON CONFLICT(trade_id) DO UPDATE SET stop=excluded.stop,event_id=excluded.event_id',(trade,str(value),event_id))
            db.execute('INSERT INTO e_protection_events VALUES(?,?,?)',(event_id,trade,str(value)))
            return {'accepted':True,'stop':str(value),'trade_id':trade}
    except (ValueError,TypeError,KeyError,InvalidOperation,sqlite3.Error):return {'accepted':False,'reason':'INVALID_STOP_CONTEXT'}


def monitor_protective_stop(store,quote,cost):
    try:
        account=store.account('E')
        if account['state']=='STOP_EXIT_PENDING':
            return recover_pending_stop(ledger=store,agent='E',quote=quote,cost=cost,event_id=str((quote or {}).get('event_id','E-PENDING')))
        if account['state'] not in ('OPEN_LONG','OPEN_SHORT') or not account['position_json']:return {'accepted':False,'reason':'NO_OPEN_POSITION'}
        position=json.loads(account['position_json']);trade=position['trade_id']
        with closing(sqlite3.connect(store.path,timeout=10)) as db,db:
            _schema(db);row=db.execute('SELECT stop FROM e_protective_stops WHERE trade_id=?',(trade,)).fetchone()
        if not row:return {'accepted':False,'reason':'NO_TRAILING_STOP'}
        if not quote or quote.get('source')!='BINANCE_USDM_PERP' or quote.get('currency')!='USD_RESEARCH' or quote.get('timestamp_valid') is not True or not 0<=int(quote.get('age_ms',10**9))<=cost.max_quote_age_ms:return {'accepted':False,'reason':'QUOTE_INVALID'}
        stop=Decimal(row[0]);bid=Decimal(str(quote['bid']));ask=Decimal(str(quote['ask']))
        if not bid.is_finite() or not ask.is_finite() or bid<=0 or ask<bid:return {'accepted':False,'reason':'QUOTE_INVALID'}
        px=bid if position['direction']=='LONG' else ask
        if (px>stop if position['direction']=='LONG' else px<stop):return {'accepted':False,'reason':'STOP_NOT_TRIGGERED'}
        event=str(quote.get('event_id') or quote.get('event_at') or '')
        if not event:return {'accepted':False,'reason':'QUOTE_EVENT_MISSING'}
        evidence={k:quote.get(k) for k in ('source','event_at','age_ms','timestamp_valid','bid','ask')}
        evidence['protective_stop']=str(stop)
        pending=store.request_stop_exit(agent='E',trigger_price=px,event_key='E-trailing:'+trade,quote_event_id=event,expected_version=account['version'],trigger_evidence=evidence)
        if not pending.get('accepted'):return pending
        return recover_pending_stop(ledger=store,agent='E',quote=quote,cost=cost,event_id=event,reason='STOP_E_TRAILING')
    except (ValueError,TypeError,KeyError,InvalidOperation,sqlite3.Error):return {'accepted':False,'reason':'INVALID_PROTECTION_CONTEXT'}

