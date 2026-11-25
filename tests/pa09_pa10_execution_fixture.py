import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from fastapi import HTTPException
import socket
import sqlite3
import sys

def offline(*args,**kwargs):
    raise AssertionError('network forbidden in acceptance')

import backend.app as app
from test_pa09_pa10_execution import candles
from test_pa09_pa10_candidates import bar
from scripts.pa09_pa10_candidates import pa09
from backend.d_bridge import DBridge
from backend.e_paper_store import EPaperStore, read_e_store
from backend import e_auto_cycle

e_results=[]
run_e=e_auto_cycle.run_e_auto_cycle
async def observed_e(**kwargs):
    outcome=await run_e(**kwargs)
    e_results.append(outcome)
    return outcome
e_auto_cycle.run_e_auto_cycle=observed_e

UTC=timezone.utc
STEP=timedelta(minutes=15)
root=app.DATA_ROOT
seed=sys.argv[1]=='seed'
expiry=sys.argv[1]=='expiry'
base=datetime(2026,1,2,tzinfo=UTC)
clock=base
RealDatetime=datetime

class ReplayClock(datetime):
    @classmethod
    def now(cls,tz=None):
        return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)

app.datetime=ReplayClock
app._e_scheduler_started_at=base-10*STEP
seq,price=candles('PA-09',False)
pa10_seq,pa10_price=candles('PA-10',True)

def make(i):
    origin=base+i*STEP
    fields=pa10_seq[i-100] if 100<=i<100+len(pa10_seq) else seq[i] if 0<=i<len(seq) else bar(i,price,price,price,price) if i>=len(seq) else bar(i)
    return dict(fields,open_at=origin.isoformat(),close_at=(origin+STEP).isoformat(),
                symbol='BTC/USD',source=app.market.source,currency='USD_RESEARCH',interval='15min',volume=10)

def normalize(obj):
    if isinstance(obj,dict):
        return {k:normalize(v) for k,v in obj.items() if k not in
                {'updated_at','created_at','timestamp','running','status','uploaded_at','enabled_at','fill_id','trade_id','position_id','occurred_at'}}
    if isinstance(obj,list):
        return [normalize(v) for v in obj]
    return obj

async def cycle(i,*,healthy=True):
    global clock
    close=base+(i+1)*STEP
    clock=close+timedelta(seconds=2)
    px=pa10_price if i>=140 else price
    q=dict(source=app.market.source,symbol='BTC/USD',currency='USD_RESEARCH',
           bid=str(px),ask=str(px),timestamp_valid=True,event_at=clock.isoformat(),
           received_at=clock.isoformat(),age_ms=0)
    app.candles=[make(j) for j in range(i-287,i+1)]
    app.market_status.update(feed_health='LIVE',gap_count_288=0,candle_archive='OK',
                             startup_position_monitor_checked=True,stream_exit_checked_at=q['event_at'])
    app.market.status.update(last_quote=q,websocket='CONNECTED',
        websocket_recovery_state='HEALTHY' if healthy else 'VALIDATING',
        websocket_sub_reply_types=['subscription'],websocket_last_quote_received_at=q['received_at'])
    async def quote():return dict(q)
    app.market.latest_quote=quote
    app.candle_archive.store(app.candles)
    app._schedule_d_market_from_loop(close)
    app._schedule_e_market_from_loop(app.candles,close)
    result=await app.process_closed_bar(close,now=clock)
    while app._e_market_tasks or app._e_observation_tasks:
        await asyncio.gather(*tuple(app._e_market_tasks | app._e_observation_tasks))
    await app.d_bridge.dispatcher._queue.join()
    return result,q

async def runtime_enable(sid, token=None, host='127.0.0.1'):
    async def body():
        return {'version':'1.0.0','enabled':True}
    req=SimpleNamespace(client=SimpleNamespace(host=host),headers={'x-btc-recovery-token':token or ''},json=body)
    return await app.strategy_pool_enable(sid,req)

async def main():
    assert app.paper_scheduler.enabled,app.paper_gate_reason
    app.lifespan_active=True
    e_dir=root/'e'
    e_dir.mkdir(exist_ok=True)
    e_path=e_dir/'formal.sqlite3'
    EPaperStore(e_path)
    if seed:
        (e_dir/'shadow-config.json').write_text('{"mode":"BASELINE","enabled":false}')
        (e_dir/'auto-paper.json').write_text('{"mode":"AUTO_PAPER","long_enabled":true,"short_enabled":false}')
    d_dir=root/'d'
    d_dir.mkdir(exist_ok=True)
    app.d_bridge=DBridge(d_dir/'formal.sqlite3',d_dir/'trading.sqlite3',app.candle_archive,
        feed_healthy=app._d_feed_healthy,candles_complete=app._d_candles_complete,clock=lambda:clock)
    await app.d_bridge.start()
    if sys.argv[1] in ('phase-seed','phase-resume'):
        db10=app.pa_extension.ledgers['PA-10']
        if sys.argv[1]=='phase-seed':
            for i in (139,140,141,142):
                await cycle(i)
            phase=db10.snapshot()['phase']
            assert phase['phase']=='RETEST_OBSERVED',phase
            (root/'pa10-phase').write_text(json.dumps(phase))
        else:
            assert db10.snapshot()['phase']==json.loads((root/'pa10-phase').read_text())
            await cycle(143)
            assert db10.snapshot()['pending'],db10.snapshot()
        await app.d_bridge.close()
        return
    if sys.argv[1] in ('phase-fill','phase-validating'):
        global clock
        db=app.pa_extension.ledgers['PA-10']
        st=db.snapshot()
        assert st['pending'] and st['phase']['phase']=='CONFIRMED'
        clock=datetime.fromisoformat(st['pending']['signal'])+STEP+timedelta(seconds=3)
        q=dict(source=app.market.source,symbol='BTC/USD',currency='USD_RESEARCH',
               bid=str(pa10_price),ask=str(pa10_price),timestamp_valid=True,
               event_at=clock.isoformat(),received_at=clock.isoformat(),age_ms=0)
        healthy=sys.argv[1]=='phase-fill'
        app.market_status.update(feed_health='LIVE',gap_count_288=0,candle_archive='OK',
                                 startup_position_monitor_checked=True)
        app.market.status.update(last_quote=q,websocket='CONNECTED',
            websocket_recovery_state='HEALTHY' if healthy else 'VALIDATING',
            websocket_sub_reply_types=['subscription'],websocket_last_quote_received_at=q['received_at'])
        await app.process_stop_quote(q,stream=True)
        await app.process_stop_quote(q,stream=True)
        assert len(db.fills())==int(healthy),db.snapshot()
        if healthy:
            pos=db.snapshot()['position']
            assert pos['direction']=='SHORT'
            clock+=timedelta(seconds=1)
            stop=pos['stop']
            sq={**q,'bid':stop,'ask':stop,'event_at':clock.isoformat(),'received_at':clock.isoformat()}
            await app.process_stop_quote(sq,stream=True)
            await app.process_stop_quote(sq,stream=True)
            assert [x['reason'] for x in db.fills()]==['ENTRY_NEXT_OPEN','STOP_QUOTE']
            (root/'pa10-fills.json').write_text(json.dumps(db.fills()))
        await app.d_bridge.close()
        return
    if expiry:
        if app.pa_extension.ledgers:
            db=app.pa_extension.ledgers['PA-10']
            pending=db.snapshot()['pending']
            assert pending and db.snapshot()['phase']['phase']=='CONFIRMED'
            _,q=await cycle(144)
            assert db.snapshot()['pending'] is None
            await app.process_stop_quote(q,stream=True)
            assert not db.fills()
        assert app.d_bridge.runtime.has_data_loss()
        await app.d_bridge.close()
        return
    if seed:
        app.d_bridge.runtime.mark_dispatch_failure('ACCEPTANCE_D_PROTECTION')
        for doc in json.loads(Path('pa-definitions.json').read_text(encoding='utf-8-sig')):
            app.strategy_pool.upload(doc)
            app.strategy_pool.enable(doc['strategy_id'],doc['version'])
        for i in range(58,61):
            result,q=await cycle(i)
            assert set(result)==set('ABC'),result
            assert all(v['status']=='VALID' for v in result.values()),result
            if i==58 and app.pa_extension.ledgers:
                assert all(not row['enabled'] for row in app.pa_extension.list())
                token='isolated-acceptance-token-32-characters'
                assert not any(x.fills() for x in app.pa_extension.ledgers.values())
                for sid in app.pa_extension.ledgers:
                    for supplied,host,status in ((None,'127.0.0.1',403),(token,'192.0.2.2',403),
                                                 (token,'127.0.0.1',409)):
                        try:
                            await runtime_enable(sid,supplied,host)
                        except HTTPException as error:
                            assert error.status_code==status,(error.status_code,error.detail)
                        else:
                            raise AssertionError('Enable should fail before proof or authorization')
                assert await app.process_stop_quote(q,stream=True)
                for sid in app.pa_extension.ledgers:
                    assert (await runtime_enable(sid,token))['action']=='ENABLED'
                    assert (await runtime_enable(sid,token))['action']=='UNCHANGED'
        # Feed G through the committed C outbox; close C before the next bar.
        now=datetime.now(UTC).isoformat()
        q_c={**q,'event_at':now,'received_at':now,'bid':'99.99','ask':'100.01','event_id':'c-enter'}
        opened=app.ledger.open_position(agent='C',direction='LONG',quantity=Decimal('.2'),
            price=Decimal('100'),stop=Decimal('90'),fee=Decimal('.01'),slippage=Decimal('0'),
            idempotency_key='acceptance-c-open',expected_version=app.ledger.account('C')['version'],
            execution_quote=q_c)
        assert opened['accepted'],opened
        if app.ledger._mirror_dispatch_thread:
            app.ledger._mirror_dispatch_thread.join(5)
        with app.ledger._connect() as conn:
            event=json.loads(conn.execute('select payload from mirror_outbox where source_fill_id=?',(opened['fill_id'],)).fetchone()[0])
        assert len(app.g_mirror.fills())==1,app.g_mirror.status()
        app.g_mirror.handle_c_fill(event)
        assert len(app.g_mirror.fills())==1
        assert app.g_mirror.status()['position_direction']=='SHORT'
        # Preserve the actual fill identities for restart comparison without requiring matching UUIDs between runs.
        (root/'mirror-before').write_text(json.dumps(app.g_mirror.fills(),default=str))
        stop_q={**q_c,'bid':'89.99','ask':'90','event_id':'c-stop'}
        from backend.execution import handle_confirmed_stop
        assert handle_confirmed_stop(ledger=app.ledger,agent='C',trigger_event_id='acceptance-C-stop',
            quote=stop_q,cost=app.COST_MODEL)['accepted']
        if app.ledger._mirror_dispatch_thread:
            app.ledger._mirror_dispatch_thread.join(5)
        assert len(app.g_mirror.fills())==2,app.g_mirror.status()
        (root/'mirror-before').write_text(json.dumps(app.g_mirror.fills(),default=str))
        if app.pa_extension.ledgers:
            db=app.pa_extension.ledgers['PA-09']
            pending=db.snapshot()['pending']
            assert pending,pending
            await app.process_stop_quote(q,stream=True)
            assert len(db.fills())==1,db.snapshot()
            (root/'fixed-target').write_text(db.snapshot()['position']['target'])
        # Seed a protected historical position only inside this isolated pool.
        pa08='pa-three-bar-hhhl'
        with sqlite3.connect(app.strategy_pool.path) as sql:
            pos=dict(version='1.0.0',direction='SHORT',qty='0.2',entry='100',stop='130',target='70',
                     mark='100',fee_rate='0.0005',slippage_rate='0.0002',bars_held=1)
            gap=(base+56*STEP).isoformat()
            sql.execute('update accounts set position=?,last_cycle=?,last_status=? where strategy_id=?',
                        (json.dumps(pos),gap,'BLOCKED_GAP_RECONCILIATION_REQUIRED',pa08))
    else:
        assert all(row['enabled'] for row in app.pa_extension.list())
        assert json.loads((root/'mirror-before').read_text())==json.loads(json.dumps(app.g_mirror.fills(),default=str))
        if app.pa_extension.ledgers:
            db=app.pa_extension.ledgers['PA-09']
            assert db.snapshot()['position']['target']==(root/'fixed-target').read_text()
        result,q=await cycle(60)
        if app.pa_extension.ledgers:
            assert len(db.fills())==1
        result,q=await cycle(61)
        assert all(v['status']=='VALID' for v in result.values()),result
        target=Decimal(str(pa09([make(i) for i in range(61)])['target']))
        if app.pa_extension.ledgers:
            assert target==Decimal(db.snapshot()['position']['target'])
        await app.process_stop_quote(dict(q,bid=str(target),ask=str(target)),stream=True)
        await app.process_stop_quote(dict(q,bid=str(target),ask=str(target)),stream=True)
        if app.pa_extension.ledgers:
            assert [f['reason'] for f in db.fills()]==['ENTRY_NEXT_OPEN','TARGET_QUOTE'],db.fills()
        pos8=next(x for x in app.strategy_pool.list() if x['id']=='pa-three-bar-hhhl')
        assert pos8['last_status']=='BLOCKED_GAP_RECONCILIATION_REQUIRED'
        assert pos8['position_direction']=='SHORT'
        assert json.loads(pos8['position'])['stop']=='130'
        with sqlite3.connect(e_path) as sql:
            e_cycles=sql.execute('select close_at,status from e_market_dispatch order by close_at').fetchall()
        assert e_results and e_cycles,e_results
        g=app.g_mirror.status()
        (root/'result.json').write_text(json.dumps(normalize({
            'D':app.d_bridge.runtime.status(),
            'E':{'cycles':e_cycles,'results':e_results,'ledger':read_e_store(e_path)},
            'G':{'cash':g['cash'],'state':g['follow_state'],'fills':[{k:f[k] for k in ('price','fee','qty','reason') if k in f} for f in app.g_mirror.fills()]},

            'ABC':{a:{'account':app.ledger.account(a),'fills':app.ledger.fills(a)} for a in 'ABC'},
            'PA':[{**item,'fills':app.strategy_pool.fills(item['id'])} for item in app.strategy_pool.list()],
        }),default=str,sort_keys=True))
    assert app.d_bridge.runtime.status()['reason']=='ACCEPTANCE_D_PROTECTION'
    assert app.d_bridge.runtime.has_data_loss()
    await app.d_bridge.close()
with asyncio.Runner() as runner:
    runner.get_loop()
    socket.socket.connect=offline
    socket.socket.connect_ex=offline
    runner.run(main())



