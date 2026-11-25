import asyncio
from backend.domain import CostModel
from backend.e_paper_store import EPaperStore
from backend.e_auto_cycle import run_e_auto_cycle
from test_e_quant_execution import sample

def test_auto_cycle_uses_quant_entry_without_waiting_for_ai(tmp_path):
    bars,now,quote=sample();store=EPaperStore(tmp_path/'formal.sqlite')
    async def verify():
        async def explain():await asyncio.sleep(60)
        result=await run_e_auto_cycle(store=store,bars=bars,close=now,quote=quote,cost=CostModel(),explain=explain)
        assert result['status']=='PAPER_FILLED' and store.account('E')['state']=='OPEN_LONG'
    asyncio.run(verify())

def test_open_position_never_reopens_after_stop_on_same_bar(tmp_path):
    bars,now,quote=sample();store=EPaperStore(tmp_path/'formal.sqlite')
    async def verify():
        assert (await run_e_auto_cycle(store=store,bars=bars,close=now,quote=quote,cost=CostModel()))['status']=='PAPER_FILLED'
        stop={**quote,'bid':'89','ask':'89.1'}
        result=await run_e_auto_cycle(store=store,bars=bars,close=now,quote=stop,cost=CostModel())
        assert result['status']=='POSITION_MANAGED' and store.account('E')['state']=='FLAT'
        assert len(store.fills())==2
    asyncio.run(verify())

def test_missing_quote_does_not_block_structure_processing(tmp_path):
    bars,now,quote=sample();store=EPaperStore(tmp_path/'formal.sqlite')
    async def verify():
        assert (await run_e_auto_cycle(store=store,bars=bars,close=now,quote=quote,cost=CostModel()))['status']=='PAPER_FILLED'
        result=await run_e_auto_cycle(store=store,bars=bars,close=now,quote=None,cost=CostModel())
        assert result['status']=='POSITION_MANAGED' and 'structure' in result
    asyncio.run(verify())

def test_each_long_setup_reaches_isolated_matcher(tmp_path):
    from scripts.e_v11_model_smoke import fixture
    from backend.e_quant_v11 import detect_setups
    from datetime import datetime,timedelta
    async def verify():
        for setup in ('E1','E2','E3'):
            rows=fixture(setup)
            for b in rows:b.update(symbol='BTC/USD',interval='15Min',source='BINANCE_USDM_PERP',currency='USD_RESEARCH')
            candidate=detect_setups(rows)['selected'];assert candidate['setup']==setup
            now=datetime.fromisoformat(rows[-1]['close_at'])+timedelta(seconds=3)
            ask=float(candidate['stop'])+.04
            quote={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':str(ask-.01),'ask':str(ask),'event_at':now.isoformat(),'event_id':'synthetic'}
            store=EPaperStore(tmp_path/(setup+'.sqlite'))
            result=await run_e_auto_cycle(store=store,bars=rows,close=now,quote=quote,cost=CostModel())
            assert result['status']=='PAPER_FILLED' and store.account('E')['state']=='OPEN_LONG'
    asyncio.run(verify())

