"""Observation-only candle and trade-event primitives; never opens a trading session."""
from __future__ import annotations
import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import httpx

INTERVALS=('1m','3m','5m','15m','30m','1h','2h','4h','6h','8h','12h','1d','3d','1w','1M')

class CandleReader:
    def __init__(self, client=None, clock=time.time):
        self.client=client;self.clock=clock;self.cache={};self.lock=asyncio.Lock()
    async def get(self, interval, limit=500, end_time=None):
        if interval not in INTERVALS or not 1<=limit<=500:raise ValueError('INVALID_INTERVAL_OR_LIMIT')
        if end_time is not None and (not isinstance(end_time,int) or end_time<=0):raise ValueError('INVALID_END_TIME')
        key=(interval,limit,end_time);now=self.clock()
        async with self.lock:
            if key in self.cache and now-self.cache[key][0]<10:return self.cache[key][1]
            params={'symbol':'BTCUSDT','interval':interval,'limit':limit}
            if end_time is not None:params['endTime']=end_time
            url='https://fapi.binance.com/fapi/v1/klines'
            if self.client is not None:r=await self.client.get(url,params=params)
            else:
                async with httpx.AsyncClient(timeout=12) as client:r=await client.get(url,params=params)
            r.raise_for_status();payload=r.json()
            if not isinstance(payload,list):raise ValueError('INVALID_CANDLE_RESPONSE')
            items=[]
            for row in payload:
                if not isinstance(row,list) or len(row)<9:raise ValueError('INVALID_CANDLE_ROW')
                opened=int(row[0]);closed=int(row[6])+1
                vals=[Decimal(str(row[i])) for i in (1,2,3,4,5)]
                if opened<0 or closed<=opened or not all(v.is_finite() for v in vals) or min(vals[:4])<=0 or vals[4]<0 or vals[1]<max(vals[0],vals[2],vals[3]) or vals[2]>min(vals[0],vals[1],vals[3]):raise ValueError('INVALID_CANDLE_VALUES')
                items.append({'open_at_ms':opened,'close_at_ms':closed,'open':str(vals[0]),'high':str(vals[1]),'low':str(vals[2]),'close':str(vals[3]),'volume':str(vals[4]),'is_closed':closed<=int(self.clock()*1000)})
            if any(a['open_at_ms']>=b['open_at_ms'] for a,b in zip(items,items[1:])):raise ValueError('UNORDERED_CANDLES')
            result={'symbol':'BTCUSDT','source':'BINANCE_USDM_PERP','interval':interval,'items':items}
            self.cache[key]=(now,result)
            if len(self.cache)>20:self.cache.pop(next(iter(self.cache)))
            return result


def normalize_event(strategy_id,row):
    effect=row.get('position_effect') or row.get('effect');direction=row.get('position_direction') or row.get('direction');event_id=row.get('fill_id');trade_id=row.get('g_trade_id') or row.get('trade_id');at=row.get('occurred_at');qty=row.get('quantity');price=row.get('price');verified=False
    try:
        dt=datetime.fromisoformat(str(at).replace('Z','+00:00'));q=Decimal(str(qty));p=Decimal(str(price))
        verified=bool(event_id and trade_id and dt.utcoffset() is not None and q.is_finite() and p.is_finite() and q>0 and p>0 and effect in ('OPEN','CLOSE','PARTIAL_CLOSE') and direction in ('LONG','SHORT'))
    except (ValueError,TypeError,InvalidOperation):pass
    return {'event_id':event_id,'strategy_id':strategy_id,'strategy_version':row.get('strategy_version'),'trade_id':trade_id,'occurred_at':at,'position_effect':effect,'position_direction':direction,'quantity':qty,'price':price,'reason_code':row.get('reason'), 'reason_text':row.get('rationale') or row.get('reason') or None,'realized_pnl':row.get('pnl'),'source':'FORMAL_PAPER_LEDGER','verification_status':'VERIFIED' if verified else 'UNVERIFIED'}


def mark_position_lifecycle(events):
    def utc_time(event):
        try:
            timestamp=datetime.fromisoformat(str(event.get('occurred_at')).replace('Z','+00:00'))
            if timestamp.utcoffset() is not None:return timestamp.astimezone(timezone.utc)
        except (TypeError,ValueError):pass
        return datetime.min.replace(tzinfo=timezone.utc)
    ordered=sorted((dict(x) for x in events),key=utc_time)
    active={};seen=set()
    for item in ordered:
        eid=item.get('event_id');key=(item.get('strategy_id'),item.get('trade_id'));q=None
        try:q=Decimal(str(item.get('quantity')))
        except (InvalidOperation,TypeError,ValueError):pass
        if not eid or eid in seen or q is None or not q.is_finite() or q<=0:
            item['verification_status']='UNVERIFIED';continue
        seen.add(eid)
        if item.get('verification_status')!='VERIFIED':continue
        if not key[1]:
            if item.get('position_effect')!='OPEN':item['verification_status']='UNVERIFIED'
            continue
        if item.get('position_effect')=='OPEN':
            old=active.get(key)
            if old and old['direction']!=item.get('position_direction'):item['verification_status']='UNVERIFIED';continue
            active[key]={'quantity':(old['quantity'] if old else Decimal(0))+q,'direction':item.get('position_direction')}
        elif item.get('position_effect') in ('CLOSE','PARTIAL_CLOSE'):
            old=active.get(key)
            if not old or old['direction']!=item.get('position_direction') or q>old['quantity']:
                item['verification_status']='UNVERIFIED';continue
            left=old['quantity']-q;item['remaining_quantity']=str(left);item['position_effect']='PARTIAL_CLOSE' if left>0 else 'CLOSE'
            if left:old['quantity']=left
            else:active.pop(key)
        else:item['verification_status']='UNVERIFIED'
    return ordered


class EventReader:
    """Bounded formal-ledger reads only. No runtime service/engine imports."""
    def __init__(self, root, clock=time.time, data_root=None):
        from pathlib import Path
        self.root=Path(root).resolve();self.data=Path(data_root).resolve() if data_root else self.root/'data';self.clock=clock;self._cached=None;self._at=0;self.errors=[];import threading;self._lock=threading.Lock()
    def _rows(self,relative,table,limit=2000):
        import sqlite3
        if table not in {'fills','g_fills','audit_events'}:raise ValueError('INVALID_TABLE')
        path=(self.data/relative).resolve()
        if self.data not in path.parents or not path.is_file():return []
        db=None
        try:
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is None:return []
            return [dict(x) for x in db.execute('SELECT * FROM "'+table+'" ORDER BY rowid DESC LIMIT ?', (limit,))]
        except sqlite3.Error as exc:self.errors.append(relative+':'+type(exc).__name__);return []
        finally:
            if db is not None:db.close()
    def read(self):
        with self._lock:return self._read()
    def _read(self):
        if self._cached is not None and self.clock()-self._at<15:return self._cached
        from scripts.research_v2 import ReadOnlyStore,pair_pool_fills,PA
        self.errors=[];events=[];store=ReadOnlyStore(self.root);store.data=self.data
        for relative,strategy in [('paper.sqlite3',None),('e/formal.sqlite3','E')]:
            rows=self._rows(relative,'fills');audit=self._rows(relative,'audit_events');close_pnl={}
            for item in audit:
                if item.get('kind')!='POSITION_CLOSED':continue
                try:
                    import json
                    p=json.loads(item.get('payload') or '{}');close_pnl[(str(p.get('exit_fill_id')),str(p.get('trade_id')),str(item.get('agent')))]=p.get('net_pnl')
                except (TypeError,ValueError):pass
            for row in rows:
                sid=strategy or row.get('agent')
                if (strategy=='E' and row.get('agent')!='A') or (strategy is None and sid not in ('A','B','C')):continue
                item=normalize_event(sid,row);item['source']=relative
                if item['position_effect']=='CLOSE':item['realized_pnl']=close_pnl.get((str(item['event_id']),str(item['trade_id']),str(row.get('agent'))))
                events.append(item)
        for row in self._rows('g/mirror.sqlite3','g_fills'):
            item=normalize_event('G',row);item['source']='g/mirror.sqlite3';events.append(item)
        d=store.d_status()
        for row in d.get('fills',[]):
            item=normalize_event('D',row);item['source']='d/trading.sqlite3';events.append(item)
        pool=store.pool();self.errors.extend(store.errors)
        for short,sid in PA:
            data=pool.get(sid,{});raw=data.get('fills') or []
            if short in ('PA-09','PA-10'):
                for f in raw:
                    events.append({'event_id':str(f.get('kind') or '')+':'+str(f.get('at') or ''),'strategy_id':short,'strategy_version':data.get('active_version'),'trade_id':None,'occurred_at':f.get('at'),'position_effect':f.get('kind'),'position_direction':None,'quantity':f.get('qty'),'price':f.get('price'),'reason_text':f.get('reason'),'realized_pnl':None,'source':'pa-v1-extensions/'+sid+'.sqlite3','verification_status':'UNVERIFIED'})
                continue
            pairs,issues=pair_pool_fills(raw,sid)
            by_open={p['trade_id'].removeprefix('pool-lifecycle:'):p for p in pairs};by_close={p['closed_at']:p for p in pairs}
            for f in raw:
                key=f.get('idempotency_key');is_open=f.get('reason') in ('ENTRY','ENTRY_NEXT_OPEN');pair=by_open.get(key) if is_open else by_close.get(f.get('created_at'))
                direction=('LONG' if f.get('side')=='BUY' else 'SHORT') if is_open else (pair or {}).get('direction')
                adapted={'fill_id':key,'trade_id':(pair or {}).get('trade_id'),'occurred_at':f.get('created_at'),'position_effect':'OPEN' if is_open else 'CLOSE','position_direction':direction,'quantity':f.get('qty'),'price':f.get('price'),'reason':f.get('reason'),'strategy_version':f.get('version')}
                item=normalize_event(short,adapted)
                if is_open and not pair and f.get('side') in ('BUY','SELL') and isinstance(key,str) and key.startswith(sid+':open:'):
                    # The committed entry itself is evidence even before the lifecycle closes.
                    check=normalize_event(short,{**adapted,'trade_id':key})
                    item['verification_status']=check['verification_status']
                item['source']='strategy-pool.sqlite3';item['trade_id_source']='verified-single-position-lifecycle' if pair else 'unavailable'
                if pair and not is_open:item['realized_pnl']=pair.get('net_pnl')
                events.append(item)
        self._cached=mark_position_lifecycle(events);self._at=self.clock();return self._cached
