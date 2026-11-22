from datetime import datetime,UTC,timedelta
from backend.domain import CostModel
from backend.e_paper_store import EPaperStore
from backend.e_quant_gate import evaluate_quant_candidate
from backend.e_quant_v11 import detect_setups
from test_e_quant_v11 import e2_window


def snapshot():
    rows=e2_window()
    for row in rows: row.update(symbol="BTC/USD",interval="15Min",source="BINANCE_USDM_PERP",currency="USD_RESEARCH")
    return rows


def test_v11_candidate_rejects_missing_and_stale_quote(tmp_path):
    bars=snapshot();candidate=detect_setups(bars)['selected'];store=EPaperStore(tmp_path/'e.sqlite')
    now=datetime.fromisoformat(bars[-1]['close_at'])+timedelta(seconds=3)
    assert evaluate_quant_candidate(store=store,bars=bars,candidate=candidate,quote=None,now=now,cost=CostModel())['reason']=='QUOTE_UNAVAILABLE'
    quote={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':60001,'bid':'99.1','ask':'99.2','event_at':now.isoformat()}
    assert evaluate_quant_candidate(store=store,bars=bars,candidate=candidate,quote=quote,now=now,cost=CostModel())['reason']=='QUOTE_STALE'


def test_v11_candidate_checks_cost_adjusted_rr_but_does_not_authorize_order(tmp_path):
    bars=snapshot();candidate=detect_setups(bars)['selected'];store=EPaperStore(tmp_path/'e.sqlite')
    now=datetime.fromisoformat(bars[-1]['close_at'])+timedelta(seconds=3)
    quote={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'99.1','ask':'99.2','event_at':now.isoformat()}
    result=evaluate_quant_candidate(store=store,bars=bars,candidate=candidate,quote=quote,now=now,cost=CostModel())
    assert result['reason']=='RR_BELOW_2'
    assert result['order_eligible'] is False and store.fills()==[]


def test_v11_candidate_rejects_expired_and_tampered_inputs(tmp_path):
    bars=snapshot();candidate=detect_setups(bars)['selected'];store=EPaperStore(tmp_path/'e.sqlite')
    close=datetime.fromisoformat(bars[-1]['close_at'])
    assert evaluate_quant_candidate(store=store,bars=bars,candidate=candidate,quote=None,now=close+timedelta(minutes=15),cost=CostModel())['reason']=='SIGNAL_EXPIRED'
    tampered={**candidate,'stop':'1'}
    assert evaluate_quant_candidate(store=store,bars=bars,candidate=tampered,quote=None,now=close+timedelta(seconds=1),cost=CostModel())['reason']=='SNAPSHOT_OR_CANDIDATE_MISMATCH'


def test_v11_gate_rejects_foreign_or_missing_market_provenance(tmp_path):
    bars=snapshot();candidate=detect_setups(bars)['selected'];store=EPaperStore(tmp_path/'e.sqlite')
    bars[0]['source']='OTHER'
    now=datetime.fromisoformat(bars[-1]['close_at'])+timedelta(seconds=1)
    assert evaluate_quant_candidate(store=store,bars=bars,candidate=candidate,quote=None,now=now,cost=CostModel())['reason']=='SNAPSHOT_SOURCE_INVALID'

