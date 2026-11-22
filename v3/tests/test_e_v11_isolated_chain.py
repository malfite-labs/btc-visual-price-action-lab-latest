"""Synthetic integration exercise; never invokes a production account or model."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from backend.domain import CostModel
from backend.e_paper_store import EPaperStore, read_e_store
from backend.e_quant_gate import evaluate_quant_candidate
from backend.e_quant_observer import EQuantObserver
from backend.execution import handle_confirmed_stop
from backend.strategy import StrategyRegistry
from test_e_quant_v11 import range_window


def test_v11_isolated_candidate_visual_gate_fill_stop_and_performance(tmp_path):
    rows = range_window()
    rows[-2].update(open=99.8, high=100, low=99.4, close=99.5)
    rows[-1].update(open=99.5, high=100.2, low=99.4, close=100)
    for bar in rows:
        bar.update(symbol='BTC/USD', interval='15Min', source='BINANCE_USDM_PERP', currency='USD_RESEARCH')
    close = datetime.fromisoformat(rows[-1]['close_at'])
    now = close + timedelta(seconds=3)
    quote = {'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
             'age_ms':0,'bid':'99.39','ask':'99.4','event_at':now.isoformat(),'event_id':'SYNTHETIC-ENTRY'}
    class SyntheticVisual:
        configured = True
        async def _send_image_request(self, image, prompt, request_id):
            return {'verdict':'CONFIRM','anchor_candle':'K47','reason':'synthetic test response'}
    root = Path(__file__).resolve().parents[1]
    observer = EQuantObserver(tmp_path/'observation.sqlite', tmp_path/'charts', StrategyRegistry(root/'strategies'))
    observed = asyncio.run(observer.observe(rows, close, SyntheticVisual()))
    assert observed['status'] == 'SETUP_DETECTED'
    assert observed['visual_status'] == 'VISUAL_MATCH_UNVALIDATED'
    store = EPaperStore(tmp_path/'isolated-e.sqlite')
    cost = CostModel()
    candidate = observed['candidate']
    pre = evaluate_quant_candidate(store=store,bars=rows,candidate=candidate,quote=quote,now=now,cost=cost)
    assert pre['reason'] == 'VISUAL_AND_EXECUTION_NOT_VALIDATED'
    assert pre['order_eligible'] is False
    p = pre['preflight']; qty=Decimal(p['quantity']); price=Decimal(p['fill_price'])
    opened = store.open_position(agent='E',direction=candidate['side'],quantity=qty,price=price,
        stop=Decimal(candidate['stop']),fee=Decimal(p['fee']),slippage=abs(price-Decimal(quote['ask']))*qty,
        idempotency_key='isolated-v11-open',expected_version=0,strategy_version='E-V1.1-QUANT')
    assert opened['accepted'] and store.account('E')['state']=='OPEN_LONG'
    stop_quote={**quote,'bid':'99.0','ask':'99.01','event_id':'SYNTHETIC-STOP'}
    assert handle_confirmed_stop(ledger=store,agent='E',trigger_event_id='SYNTHETIC-STOP',quote=stop_quote,cost=cost)['accepted']
    result=read_e_store(store.path)
    assert result['account']['state']=='FLAT' and result['closed_trades']==1 and len(result['fills'])==2

