import asyncio,json,shutil
from datetime import datetime,UTC,timedelta
from types import SimpleNamespace
from pathlib import Path
from backend.e_paper_store import EPaperStore
from test_e_quant_execution import sample

def test_scheduler_auto_path_uses_private_ledger_and_existing_market(tmp_path,monkeypatch):
    from backend import app
    src=Path(__file__).resolve().parents[1]
    target=tmp_path/'strategies'/'E';target.mkdir(parents=True)
    for name in ('E-V1.1-STRUCTURE-ADDENDUM.md','E-V1.1-QUANT.metadata.json'):
        shutil.copyfile(src/'strategies'/'E'/name,target/name)
    path=tmp_path/'data'/'e'/'formal.sqlite3';store=EPaperStore(path)
    rows,_,quote=sample();now=datetime.now(UTC);close=now.replace(minute=(now.minute//15)*15,second=0,microsecond=0)
    for i,b in enumerate(rows):
        b['close_at']=(close-timedelta(minutes=15*(287-i))).isoformat();b['open_at']=(close-timedelta(minutes=15*(288-i))).isoformat()
    quote['event_at']=now.isoformat();quote['event_id']='isolated-quote'
    class Market:
        status={'last_quote':{'timestamp_valid':True,'event_at':now.isoformat()},'websocket':'CONNECTED','websocket_last_quote_received_at':now.isoformat(),'websocket_recovery_state':'HEALTHY'}
        async def latest_quote(self):return dict(quote)
    class Observer:
        async def observe(self,*args):return {'status':'TEST_ONLY'}
    monkeypatch.setattr(app,'ROOT',tmp_path);monkeypatch.setattr(app,'DATA_ROOT',tmp_path/'data');monkeypatch.setattr(app,'market',Market())
    monkeypatch.setattr(app,'strategies',SimpleNamespace(list=lambda:[{'agent':'E','candidate_version':'E-V1.1-QUANT'}]))
    monkeypatch.setitem(app.market_status,'feed_health','LIVE');monkeypatch.setitem(app.market_status,'gap_count_288',0)
    async def verify():
        await app._run_e_auto_observation(rows,close,Observer(),object())
        assert store.account('E')['state']=='OPEN_LONG' and len(store.fills())==1
    asyncio.run(verify())

def test_auto_dispatch_does_not_initialize_ai_before_quant_task(tmp_path,monkeypatch):
    from backend import app
    import backend.e_quant_observer as observer
    root=tmp_path/'data'/'e';root.mkdir(parents=True)
    (root/'observe-version.json').write_text('{"version":"E-V1.1-QUANT","mode":"OBSERVE_ONLY"}',encoding='utf8')
    (root/'auto-paper.json').write_text('{"mode":"AUTO_PAPER","long_enabled":true,"short_enabled":false}',encoding='utf8')
    monkeypatch.setattr(app,'ROOT',tmp_path)
    monkeypatch.setattr(app,'DATA_ROOT',tmp_path/'data')
    monkeypatch.setattr(observer,'EQuantObserver',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('AI unavailable')))
    called=[]
    async def fake_auto(*args,**kwargs):called.append('quant')
    monkeypatch.setattr(app,'_run_e_auto_observation',fake_auto)
    async def verify():
        app._schedule_e_observation([],datetime.now(UTC))
        await asyncio.sleep(0)
        assert called==['quant']
    asyncio.run(verify())

def test_e_market_dispatch_is_durable_and_ignores_abc_result(tmp_path,monkeypatch):
    from backend import app
    from backend.e_quant_v11 import detect_setups
    import sqlite3
    rows,_,_=sample();close=datetime.fromisoformat(rows[-1]['close_at'])
    path=tmp_path/'data'/'e'/'formal.sqlite3';EPaperStore(path)
    monkeypatch.setattr(app,'ROOT',tmp_path)
    monkeypatch.setattr(app,'DATA_ROOT',tmp_path/'data')
    seen=[];monkeypatch.setattr(app,'_schedule_e_observation',lambda bars,at:seen.append((bars,at)))
    first=app._dispatch_e_market_snapshot(rows,close,now=close+timedelta(seconds=1))
    again=app._dispatch_e_market_snapshot(rows,close,now=close+timedelta(seconds=2))
    assert first=='DISPATCHED' and again=='DUPLICATE_BAR'
    assert len(seen)==1 and seen[0][0]==rows and seen[0][1]==close
    with sqlite3.connect(path) as db:assert db.execute('select snapshot_sha256 from e_market_dispatch').fetchone()[0]==detect_setups(rows)['snapshot_sha256']

def test_slow_e_claim_does_not_block_event_loop(monkeypatch):
    import time
    from backend import app
    monkeypatch.setattr(app,'_dispatch_e_market_snapshot',lambda *a,**k:(time.sleep(.4) or 'DUPLICATE_BAR'))
    async def verify():
        task=asyncio.create_task(app._dispatch_e_market_async([],datetime.now(UTC)))
        await asyncio.sleep(.02)
        assert not task.done()
        await task
    asyncio.run(verify())

def test_shadow_baseline_uses_distinct_account(tmp_path,monkeypatch):
    from backend import app
    src=Path(__file__).resolve().parents[1];target=tmp_path/'strategies'/'E';target.mkdir(parents=True)
    for name in ('E-V1.1-STRUCTURE-ADDENDUM.md','E-V1.1-QUANT.metadata.json'):shutil.copyfile(src/'strategies'/'E'/name,target/name)
    root=tmp_path/'data'/'e';formal=EPaperStore(root/'formal.sqlite3');shadow=EPaperStore(root/'shadow.sqlite3')
    (root/'shadow-config.json').write_text('{"mode":"BASELINE","enabled":true}',encoding='utf8')
    rows,_,quote=sample();now=datetime.now(UTC);close=now.replace(minute=(now.minute//15)*15,second=0,microsecond=0)
    for i,b in enumerate(rows):
        b['close_at']=(close-timedelta(minutes=15*(287-i))).isoformat();b['open_at']=(close-timedelta(minutes=15*(288-i))).isoformat()
    quote['event_at']=now.isoformat()
    class Market:
        status={'last_quote':{'timestamp_valid':True,'event_at':now.isoformat()},'websocket':'CONNECTED','websocket_last_quote_received_at':now.isoformat(),'websocket_recovery_state':'HEALTHY'}
        async def latest_quote(self):return dict(quote)
    class Observer:
        async def observe(self,*args):return {'status':'TEST_ONLY'}
    monkeypatch.setattr(app,'ROOT',tmp_path);monkeypatch.setattr(app,'DATA_ROOT',tmp_path/'data');monkeypatch.setattr(app,'market',Market());monkeypatch.setattr(app,'strategies',SimpleNamespace(list=lambda:[{'agent':'E','candidate_version':'E-V1.1-QUANT'}]))
    monkeypatch.setitem(app.market_status,'feed_health','LIVE');monkeypatch.setitem(app.market_status,'gap_count_288',0)
    async def verify():
        await app._run_e_auto_observation(rows,close,Observer(),object())
        await asyncio.gather(*list(app._e_observation_tasks))
        assert formal.account('E')['state']=='OPEN_LONG' and shadow.account('E')['state']=='OPEN_LONG'
        assert formal.path!=shadow.path and len(formal.fills())==len(shadow.fills())==1
    asyncio.run(verify())

def test_strategies_api_shows_live_auto_paper_without_dropping_candidate(tmp_path,monkeypatch):
    from backend import app
    root=tmp_path/'data'/'e';root.mkdir(parents=True)
    (root/'auto-paper.json').write_text('{"mode":"AUTO_PAPER","long_enabled":true,"short_enabled":false}',encoding='utf8')
    (root/'observe-version.json').write_text('{"version":"E-V1.1-QUANT","mode":"OBSERVE_ONLY"}',encoding='utf8')
    monkeypatch.setattr(app,'ROOT',tmp_path)
    monkeypatch.setattr(app,'DATA_ROOT',tmp_path/'data')
    monkeypatch.setattr(app,'strategies',SimpleNamespace(list=lambda:[{'agent':'A','status':'SEED_VERSION_MODEL_BLOCKED','current_version':'v1.0.0'},{'agent':'E','current_version':'E-V1.0','status':'READY_FOR_VALIDATION','candidate_version':'E-V1.1-QUANT','candidate_status':'CANDIDATE_NOT_ACTIVE'}]))
    payload=asyncio.run(app.get_strategies())
    echo,hanba=payload['items']
    assert echo['status']=='SEED_VERSION_MODEL_BLOCKED'
    assert hanba['runtime_mode']=='AUTO_PAPER' and hanba['driving_version']=='E-V1.1-QUANT'
    assert hanba['candidate_version']=='E-V1.1-QUANT' and hanba['candidate_status']=='AUTO_PAPER'

def test_short_requires_exact_authorized_margin_contract(tmp_path,monkeypatch):
    from backend import app
    root=tmp_path/'data'/'e';root.mkdir(parents=True)
    (root/'auto-paper.json').write_text('{"mode":"AUTO_PAPER","long_enabled":true,"short_enabled":true}',encoding='utf8')
    monkeypatch.setattr(app,'ROOT',tmp_path)
    monkeypatch.setattr(app,'DATA_ROOT',tmp_path/'data')
    assert app._e_auto_settings()==(True,False)
    (root/'short-risk.json').write_text('{"leverage":"1","maintenance_rate":"0.01","risk_fraction":"0.02","authorized":true}',encoding='utf8')
    assert app._e_auto_settings()==(True,True)
