from datetime import datetime,timedelta
from backend.domain import CostModel
from backend.e_paper_store import EPaperStore
from backend.e_quant_execution import run_quant_cycle
from test_e_quant_v11 import range_window
import pytest


@pytest.mark.parametrize('setup,bid,ask,short', [
    ('E1', '99.39', '99.40', False),
    ('E2', '97.79', '97.80', False),
    ('E3', '100.39', '100.40', False),
    ('E4', '99.50', '99.51', True),
])
def test_each_original_setup_can_execute_without_ai_when_risk_passes(tmp_path, setup, bid, ask, short):
    from scripts.e_v11_model_smoke import fixture
    bars = fixture(setup)
    for bar in bars:
        bar.update(symbol='BTC/USD', interval='15Min', source='BINANCE_USDM_PERP', currency='USD_RESEARCH')
    now = datetime.fromisoformat(bars[-1]['close_at']) + timedelta(seconds=3)
    quote = {'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH', 'timestamp_valid': True,
             'age_ms': 0, 'bid': bid, 'ask': ask, 'event_at': now.isoformat(), 'event_id': setup}
    store = EPaperStore(tmp_path / (setup + '.sqlite'))
    result = run_quant_cycle(store=store, bars=bars, quote=quote, now=now, cost=CostModel(), allow_short=short)
    assert result['status'] == 'PAPER_FILLED', result
    assert result['candidate']['setup'] == setup
    assert len(store.fills()) == 1

def sample():
    rows=range_window()
    rows[-2].update(open=99.8,high=100,low=99.4,close=99.5)
    rows[-1].update(open=99.5,high=100.2,low=99.4,close=100)
    for b in rows:b.update(symbol='BTC/USD',interval='15Min',source='BINANCE_USDM_PERP',currency='USD_RESEARCH')
    now=datetime.fromisoformat(rows[-1]['close_at'])+timedelta(seconds=3)
    q={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'99.39','ask':'99.4','event_at':now.isoformat(),'event_id':'test'}
    return rows,now,q

def test_quant_entry_and_dedup_are_independent_of_ai(tmp_path):
    bars,now,quote=sample();store=EPaperStore(tmp_path/'e.sqlite')
    result=run_quant_cycle(store=store,bars=bars,quote=quote,now=now,cost=CostModel())
    assert result['status']=='PAPER_FILLED' and store.account('E')['state']=='OPEN_LONG'
    assert result['preflight']['reason']=='QUANT_PREFLIGHT_PASSED'
    assert run_quant_cycle(store=store,bars=bars,quote=quote,now=now,cost=CostModel())['status']=='DUPLICATE_BAR'
    assert len(store.fills())==1

def test_quant_rejects_corrupt_snapshot_without_fill(tmp_path):
    bars,now,quote=sample();store=EPaperStore(tmp_path/'e.sqlite');bars[1]['source']='OTHER'
    result=run_quant_cycle(store=store,bars=bars,quote=quote,now=now,cost=CostModel())
    assert result['status']=='REJECTED' and result['reason']=='SNAPSHOT_SOURCE_INVALID'
    assert store.fills()==[]

def test_quant_rejects_expired_quote_and_persists_reason(tmp_path):
    import sqlite3,json
    bars,now,quote=sample();store=EPaperStore(tmp_path/'e.sqlite');quote['age_ms']=60001
    result=run_quant_cycle(store=store,bars=bars,quote=quote,now=now,cost=CostModel())
    assert result['reason']=='QUOTE_STALE' and store.fills()==[]
    with sqlite3.connect(store.path) as db:
        assert json.loads(db.execute('select result_json from e_quant_cycles').fetchone()[0])['reason']=='QUOTE_STALE'

def test_quant_entry_stop_and_restart_without_ai(tmp_path):
    from backend.e_protection import protect_e_accounts
    bars,now,quote=sample();path=tmp_path/'e.sqlite';store=EPaperStore(path)
    assert run_quant_cycle(store=store,bars=bars,quote=quote,now=now,cost=CostModel())['status']=='PAPER_FILLED'
    stopped=protect_e_accounts({'formal':path},{**quote,'bid':'99.0','ask':'99.01'},CostModel())
    assert stopped['formal']['accepted']
    restarted=EPaperStore(path)
    assert restarted.account('E')['state']=='FLAT' and len(restarted.fills())==2

def test_quant_prevents_repeat_e1_trade_in_same_range(tmp_path):
    from backend.e_protection import protect_e_accounts
    bars,now,quote=sample();path=tmp_path/'e.sqlite';store=EPaperStore(path)
    assert run_quant_cycle(store=store,bars=bars,quote=quote,now=now,cost=CostModel())['status']=='PAPER_FILLED'
    assert protect_e_accounts({'formal':path},{**quote,'bid':'99.0','ask':'99.01'},CostModel())['formal']['accepted']
    for bar in bars:
        for field in ('open_at','close_at'):
            bar[field]=(datetime.fromisoformat(bar[field])+timedelta(minutes=15)).isoformat()
    now+=timedelta(minutes=15);quote['event_at']=now.isoformat()
    result=run_quant_cycle(store=store,bars=bars,quote=quote,now=now,cost=CostModel())
    assert result['reason']=='E1_RANGE_ALREADY_TRADED' and len(store.fills())==2

def test_quant_rejects_unaligned_candles(tmp_path):
    bars,now,quote=sample();store=EPaperStore(tmp_path/'e.sqlite')
    for bar in bars:
        for field in ('open_at','close_at'):
            bar[field]=(datetime.fromisoformat(bar[field])+timedelta(minutes=1)).isoformat()
    now+=timedelta(minutes=1);quote['event_at']=now.isoformat()
    assert run_quant_cycle(store=store,bars=bars,quote=quote,now=now,cost=CostModel())['reason']=='SNAPSHOT_TIME_UNALIGNED'

def test_e4_does_not_short_without_verified_account_capability(tmp_path):
    from scripts.e_v11_model_smoke import fixture
    bars=fixture('E4')
    for b in bars:b.update(symbol='BTC/USD',interval='15Min',source='BINANCE_USDM_PERP',currency='USD_RESEARCH')
    now=datetime.fromisoformat(bars[-1]['close_at'])+timedelta(seconds=3)
    quote={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'99.5','ask':'99.51','event_at':now.isoformat()}
    store=EPaperStore(tmp_path/'e.sqlite')
    result=run_quant_cycle(store=store,bars=bars,quote=quote,now=now,cost=CostModel())
    assert result['reason']=='SHORT_UNSUPPORTED' and store.fills()==[]

def test_slow_explanation_does_not_delay_or_change_quant_fill(tmp_path):
    import asyncio
    from backend.e_quant_execution import run_quant_with_explanation
    bars,now,quote=sample();store=EPaperStore(tmp_path/'e.sqlite')
    async def verify():
        started=asyncio.Event()
        async def explanation():
            started.set()
            await asyncio.sleep(60)
        result=await asyncio.wait_for(run_quant_with_explanation(store=store,bars=bars,quote=quote,now=now,cost=CostModel(),explain=explanation),timeout=5)
        await asyncio.wait_for(started.wait(),timeout=2)
        assert result['status']=='PAPER_FILLED'
        assert store.account('E')['state']=='OPEN_LONG'
    asyncio.run(verify())

def test_failing_explanation_does_not_change_deterministic_cycle(tmp_path):
    import asyncio
    from backend.e_quant_execution import run_quant_with_explanation
    bars,now,quote=sample();store=EPaperStore(tmp_path/'e.sqlite')
    async def verify():
        async def explanation():raise RuntimeError('MODEL_FAILED')
        result=await run_quant_with_explanation(store=store,bars=bars,quote=quote,now=now,cost=CostModel(),explain=explanation)
        await asyncio.sleep(0)
        assert result['status']=='PAPER_FILLED' and len(store.fills())==1
    asyncio.run(verify())

