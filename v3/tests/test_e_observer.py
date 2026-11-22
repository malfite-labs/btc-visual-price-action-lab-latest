import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from backend.e_observer import EObserver
from backend.market import Candle
from backend.strategy import StrategyRegistry


def test_e_observer_records_one_decision_per_close_and_uses_e_strategy(tmp_path):
    root=Path(__file__).resolve().parents[1]
    registry=StrategyRegistry(root/'strategies')
    end=datetime(2026,1,4,tzinfo=UTC)
    candles=[]
    for i in range(288):
        start=end-timedelta(minutes=15*(288-i))
        candles.append(Candle('BTC/USD','15Min',start.isoformat(),(start+timedelta(minutes=15)).isoformat(),
            '100','110','90','105','0',0,None,True).__dict__)
    class Adapter:
        configured=True
        calls=0
        async def _send_image_request(self,image,prompt,request_id):
            self.calls+=1
            assert '憨巴 E 策略 V1.0' in prompt
            return {'action':'SKIP','side':'FLAT','setup':'CHOP','confidence':'LOW','reasons':['无清晰结构']}
    adapter=Adapter()
    observer=EObserver(tmp_path/'e.sqlite',tmp_path/'snapshots',registry)
    first=asyncio.run(observer.observe(candles,end,adapter))
    second=asyncio.run(observer.observe(candles,end,adapter))
    assert first['status']=='OBSERVE_ONLY'
    assert first['decision']['action']=='SKIP'
    assert second['status']=='DUPLICATE_CYCLE'
    assert adapter.calls==1


def test_e_observer_refuses_unconfigured_model_without_call(tmp_path):
    root=Path(__file__).resolve().parents[1]
    registry=StrategyRegistry(root/'strategies')
    end=datetime(2026,1,4,tzinfo=UTC)
    rows=[]
    for i in range(288):
        start=end-timedelta(minutes=15*(288-i))
        rows.append(Candle('BTC/USD','15Min',start.isoformat(),(start+timedelta(minutes=15)).isoformat(),
            '100','110','90','105','0',0,None,True).__dict__)
    class Disabled:
        configured=False
        async def _send_image_request(self,*args): raise AssertionError('must not call')
    observer=EObserver(tmp_path/'e.sqlite',tmp_path/'snapshots',registry)
    assert asyncio.run(observer.observe(rows,end,Disabled()))['status']=='MODEL_UNAVAILABLE'


def test_e_dispatch_uses_copy_and_does_not_propagate_observer_failures():
    from backend.e_observer import dispatch_e_observation
    class Observer:
        async def observe(self, bars, close, adapter):
            bars[0]['close'] = 'changed'
            raise RuntimeError('failed')
    original=[{'close':'original'}]
    result=asyncio.run(dispatch_e_observation(Observer(),original,datetime.now(UTC),None))
    assert result['status']=='OBSERVATION_FAILED'
    assert original[0]['close']=='original'


def test_read_observations_is_read_only_and_reports_missing(tmp_path):
    from backend.e_observer import read_observations
    path=tmp_path/'missing.sqlite'
    assert read_observations(path)=={'status':'NOT_STARTED','items':[]}
    assert not path.exists()


def test_read_observations_returns_persisted_skip(tmp_path):
    from backend.e_observer import read_observations
    import sqlite3, json
    path=tmp_path/'obs.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('create table e_observations(close_at text primary key,status text,result_json text)')
        db.execute('insert into e_observations values(?,?,?)',('2026-01-04T00:00:00+00:00','OBSERVE_ONLY',json.dumps({'decision':{'action':'SKIP'}})))
    result=read_observations(path)
    assert result['items'][0]['result']['decision']['action']=='SKIP'


def test_interrupted_observation_is_marked_and_not_replayed(tmp_path):
    import sqlite3
    root=Path(__file__).resolve().parents[1]
    path=tmp_path/'e.sqlite'; registry=StrategyRegistry(root/'strategies')
    EObserver(path,tmp_path/'snapshots',registry)
    with sqlite3.connect(path) as db:
        db.execute("insert into e_observations values(?,?,?)",('2026-01-04T00:00:00+00:00','PENDING','{}'))
    EObserver(path,tmp_path/'snapshots',registry,recover_interrupted=True)
    with sqlite3.connect(path) as db:
        assert db.execute('select status from e_observations').fetchone()[0]=='INTERRUPTED'


def test_observer_records_traceable_event_on_model_unavailable(tmp_path):
    from test_e_visual import bars
    root=Path(__file__).resolve().parents[1]
    class Disabled: configured=False
    end=datetime(2026,1,4,tzinfo=UTC)
    result=asyncio.run(EObserver(tmp_path/'e.sqlite',tmp_path/'snapshots',StrategyRegistry(root/'strategies')).observe(bars(),end,Disabled()))
    assert result['event_id']=='E:2026-01-04T00:00:00+00:00'
    assert result['status']=='MODEL_UNAVAILABLE'


def test_observer_timeout_is_recorded_and_cannot_replay(tmp_path):
    root=Path(__file__).resolve().parents[1]
    end=datetime(2026,1,4,tzinfo=UTC); rows=[]
    for i in range(288):
        t=end-timedelta(minutes=15*(288-i))
        rows.append(Candle('BTC/USD','15Min',t.isoformat(),(t+timedelta(minutes=15)).isoformat(),'100','110','90','105','0',0,None,True).__dict__)
    class Slow:
        configured=True
        class config: timeout_seconds=0.01
        async def _send_image_request(self,*args): await asyncio.sleep(1)
    observer=EObserver(tmp_path/'e.sqlite',tmp_path/'snapshots',StrategyRegistry(root/'strategies'))
    result=asyncio.run(observer.observe(rows,end,Slow()))
    assert result['status']=='MODEL_TIMEOUT'
    assert asyncio.run(observer.observe(rows,end,Slow()))['status']=='DUPLICATE_CYCLE'


def test_e_observe_adapter_has_separate_bounded_budget_and_is_off_by_default(tmp_path,monkeypatch):
    from backend.e_observer import create_e_observe_adapter
    monkeypatch.delenv('E_OBSERVE_ENABLED',raising=False)
    assert create_e_observe_adapter(tmp_path,False) is None
    monkeypatch.setenv('E_OBSERVE_ENABLED','true')
    adapter=create_e_observe_adapter(tmp_path,True)
    assert adapter.config.enabled and adapter.config.calls_authorized
    assert adapter.config.max_requests_per_day==4
    assert str(adapter.config.total_budget_usd)=='0.20'
    assert Path(adapter.config.budget_db_path).parent==tmp_path
    assert adapter.image_capability_verified is True


def test_e_observe_config_survives_service_restart_without_global_environment(tmp_path,monkeypatch):
    import json
    from backend.e_observer import create_e_observe_adapter
    monkeypatch.delenv('E_OBSERVE_ENABLED',raising=False)
    (tmp_path/'observe-config.json').write_text(json.dumps({'enabled':True,'mode':'OBSERVE_ONLY'}),encoding='utf8')
    adapter=create_e_observe_adapter(tmp_path,True)
    assert adapter is not None and adapter.config.calls_authorized
    assert str(adapter.config.total_budget_usd)=='0.20'


def test_invalid_model_output_is_persisted_for_audit(tmp_path):
    from backend.e_observer import read_observations
    root=Path(__file__).resolve().parents[1]
    end=datetime(2026,1,4,tzinfo=UTC); rows=[]
    for i in range(288):
        t=end-timedelta(minutes=15*(288-i))
        rows.append(Candle('BTC/USD','15Min',t.isoformat(),(t+timedelta(minutes=15)).isoformat(),'100','110','90','105','0',0,None,True).__dict__)
    class Invalid:
        configured=True
        async def _send_image_request(self,*args): return {'action':'INVALID','rationale':'bad schema'}
    path=tmp_path/'e.sqlite';observer=EObserver(path,tmp_path/'snapshots',StrategyRegistry(root/'strategies'))
    result=asyncio.run(observer.observe(rows,end,Invalid()))
    assert result['status']=='OBSERVATION_FAILED'
    assert read_observations(path)['items'][0]['result']['provider_response']['action']=='INVALID'


def test_observer_accepts_equivalent_non_utc_close_timestamp(tmp_path):
    from datetime import timezone
    from test_e_visual import bars
    root=Path(__file__).resolve().parents[1];end=datetime(2026,1,4,tzinfo=UTC)
    rows=bars()
    for row in rows:
        for field in ('open_at','close_at'):
            row[field]=datetime.fromisoformat(row[field]).astimezone(timezone(timedelta(hours=8))).isoformat()
    class Disabled: configured=False
    result=asyncio.run(EObserver(tmp_path/'e.sqlite',tmp_path/'snapshots',StrategyRegistry(root/'strategies')).observe(rows,end,Disabled()))
    assert result['status']=='MODEL_UNAVAILABLE'


def test_observer_closes_sqlite_handles_immediately(tmp_path):
    from test_e_visual import bars
    root=Path(__file__).resolve().parents[1];end=datetime(2026,1,4,tzinfo=UTC)
    class Disabled: configured=False
    path=tmp_path/'e.sqlite';observer=EObserver(path,tmp_path/'snapshots',StrategyRegistry(root/'strategies'))
    asyncio.run(observer.observe(bars(),end,Disabled()))
    path.unlink()
    assert not path.exists()


def test_v11_observation_budget_is_separate_from_v10(tmp_path,monkeypatch):
    from backend.e_observer import create_e_observe_adapter
    monkeypatch.setenv('E_OBSERVE_ENABLED','true')
    old=create_e_observe_adapter(tmp_path,True,strategy_version='E-V1.0')
    new=create_e_observe_adapter(tmp_path,True,strategy_version='E-V1.1-QUANT')
    assert old.config.budget_db_path != new.config.budget_db_path
    assert str(new.config.total_budget_usd)=='0.20'
