import asyncio
from datetime import UTC,datetime
from pathlib import Path
from backend.e_quant_observer import EQuantObserver
from backend.strategy import StrategyRegistry
from test_e_quant_v11 import e2_window,range_window


def test_quant_candidate_is_recorded_even_when_vision_is_uncertain(tmp_path):
    root=Path(__file__).resolve().parents[1];rows=e2_window();seen=[]
    class Fake:
        configured=True
        async def _send_image_request(self,image,prompt,request_id):
            seen.append(prompt);return {'verdict':'UNCERTAIN','anchor_candle':None,'reason':'ambiguous'}
    observer=EQuantObserver(tmp_path/'quant.sqlite',tmp_path/'images',StrategyRegistry(root/'strategies'))
    close=datetime.fromisoformat(rows[-1]['close_at'])
    result=asyncio.run(observer.observe(rows,close,Fake()))
    assert result['status']=='SETUP_DETECTED'
    assert result['candidate']['setup']=='E2'
    assert result['visual_status']=='MODEL_UNCERTAIN'
    assert result['order_eligible'] is False
    assert len(seen)==1
    assert asyncio.run(observer.observe(rows,close,Fake()))['status']=='DUPLICATE_BAR'


def test_quant_no_setup_does_not_consume_model_call(tmp_path):
    root=Path(__file__).resolve().parents[1];rows=range_window()
    class Fake:
        configured=True
        async def _send_image_request(self,*args):raise AssertionError('no call expected')
    observer=EQuantObserver(tmp_path/'quant.sqlite',tmp_path/'images',StrategyRegistry(root/'strategies'))
    result=asyncio.run(observer.observe(rows,datetime.fromisoformat(rows[-1]['close_at']),Fake()))
    assert result['status']=='NO_SETUP' and result['order_eligible'] is False


def test_quant_contradiction_does_not_erase_deterministic_setup(tmp_path):
    root=Path(__file__).resolve().parents[1];rows=e2_window()
    class Fake:
        configured=True
        async def _send_image_request(self,*args):return {'verdict':'CONTRADICT','anchor_candle':'K01','reason':'not certain'}
    observer=EQuantObserver(tmp_path/'q.sqlite',tmp_path/'images',StrategyRegistry(root/'strategies'))
    result=asyncio.run(observer.observe(rows,datetime.fromisoformat(rows[-1]['close_at']),Fake()))
    assert result['status']=='SETUP_DETECTED'
    assert result['candidate']['setup']=='E2'
    assert result['visual_status']=='MODEL_CONTRADICTION_UNVERIFIED'
    assert result['order_eligible'] is False


def test_quant_uses_same_snapshot_and_rejects_wrong_close(tmp_path):
    from datetime import timedelta
    import pytest
    root=Path(__file__).resolve().parents[1];rows=e2_window()
    class Fake:configured=False
    observer=EQuantObserver(tmp_path/'q.sqlite',tmp_path/'images',StrategyRegistry(root/'strategies'))
    with pytest.raises(ValueError,match='SNAPSHOT_MISMATCH'):
        asyncio.run(observer.observe(rows,datetime.fromisoformat(rows[-1]['close_at'])+timedelta(minutes=15),Fake()))


def test_observe_strategy_selection_requires_explicit_v11(tmp_path,monkeypatch):
    from backend.e_quant_observer import selected_observe_strategy
    monkeypatch.delenv('E_OBSERVE_STRATEGY',raising=False)
    assert selected_observe_strategy(tmp_path)=='E-V1.0'
    (tmp_path/'observe-version.json').write_text('{"version":"E-V1.1-QUANT","mode":"OBSERVE_ONLY"}',encoding='utf8')
    assert selected_observe_strategy(tmp_path)=='E-V1.1-QUANT'
    monkeypatch.setenv('E_OBSERVE_STRATEGY','BAD')
    assert selected_observe_strategy(tmp_path) is None


def test_quant_interrupted_observation_never_replayed(tmp_path):
    import sqlite3
    root=Path(__file__).resolve().parents[1];path=tmp_path/'q.sqlite';registry=StrategyRegistry(root/'strategies')
    EQuantObserver(path,tmp_path/'images',registry)
    with sqlite3.connect(path) as db:db.execute("insert into e_quant_observations values('2026-01-04T00:00:00+00:00','PENDING','{}')")
    EQuantObserver(path,tmp_path/'images',registry,recover_interrupted=True)
    with sqlite3.connect(path) as db:assert db.execute('select status from e_quant_observations').fetchone()[0]=='INTERRUPTED'


def test_quant_observer_records_preflight_without_granting_order(tmp_path):
    root=Path(__file__).resolve().parents[1];rows=e2_window();seen=[]
    class Fake:
        configured=False
    async def preflight(bars,candidate):
        seen.append((bars[-1]['close_at'],candidate['setup']))
        return {'order_eligible':False,'reason':'RR_BELOW_2'}
    observer=EQuantObserver(tmp_path/'q.sqlite',tmp_path/'images',StrategyRegistry(root/'strategies'),preflight=preflight)
    result=asyncio.run(observer.observe(rows,datetime.fromisoformat(rows[-1]['close_at']),Fake()))
    assert result['risk_preflight']['reason']=='RR_BELOW_2'
    assert result['order_eligible'] is False
    assert seen==[(rows[-1]['close_at'],'E2')]
