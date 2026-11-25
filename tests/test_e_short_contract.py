"""E-only research checks; these do not authorize E4 or invent liquidation settings."""
from decimal import Decimal as D
from backend.domain import CostModel
from backend.e_paper_store import EPaperStore
from backend.e_protection import protect_e_accounts

def test_private_one_x_short_marks_profit_and_stop_survives_restart(tmp_path):
    path=tmp_path/'e.sqlite';store=EPaperStore(path)
    result=store.open_position(agent='E',direction='SHORT',quantity=D('.1'),price=D('100'),stop=D('110'),fee=D('.01'),slippage=D('0'),idempotency_key='short',expected_version=0)
    assert result['accepted'] and store.account('E')['state']=='OPEN_SHORT'
    quote={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'89','ask':'90','event_id':'q1'}
    m=store.account_metrics('E',quote,CostModel())
    assert D(m['unrealized_pnl'])>0 and D(m['used_margin'])==D('10')
    stopped={**quote,'bid':'110','ask':'111','event_id':'q2'}
    assert protect_e_accounts({'formal':path},stopped,CostModel())['formal']['accepted']
    assert EPaperStore(path).account('E')['state']=='FLAT' and len(store.fills())==2

def test_private_short_rejects_excess_risk_and_funds(tmp_path):
    store=EPaperStore(tmp_path/'e.sqlite')
    result=store.open_position(agent='E',direction='SHORT',quantity=D('1000'),price=D('100'),stop=D('110'),fee=D('50'),slippage=D('0'),idempotency_key='oversized',expected_version=0)
    assert not result['accepted'] and store.fills()==[]

def test_e_one_x_liquidation_uses_authorized_maintenance_and_recovers(tmp_path):
    from backend.e_short_liquidation import protect_e_short_margin
    path=tmp_path/'e.sqlite';store=EPaperStore(path)
    assert store.open_position(agent='E',direction='SHORT',quantity=D('99'),price=D('100'),stop=D('100.5'),fee=D('4.95'),slippage=D('0'),idempotency_key='short',expected_version=0)['accepted']
    quote={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'199','ask':'200','event_at':'2026-01-01T00:00:00+00:00','event_id':'q1'}
    result=protect_e_short_margin(store,quote,CostModel())
    assert result['accepted'] and EPaperStore(path).account('E')['state']=='FLAT'
    assert len(store.fills())==2

def test_e4_uses_private_short_matcher_only_when_enabled(tmp_path):
    from scripts.e_v11_model_smoke import fixture
    from backend.e_quant_execution import run_quant_cycle
    from datetime import datetime,timedelta
    rows=fixture('E4')
    for b in rows:b.update(symbol='BTC/USD',interval='15Min',source='BINANCE_USDM_PERP',currency='USD_RESEARCH')
    now=datetime.fromisoformat(rows[-1]['close_at'])+timedelta(seconds=3)
    q={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'99.5','ask':'99.51','event_at':now.isoformat(),'event_id':'q'}
    store=EPaperStore(tmp_path/'e.sqlite')
    result=run_quant_cycle(store=store,bars=rows,quote=q,now=now,cost=CostModel(),allow_short=True)
    assert result['status']=='PAPER_FILLED' and store.account('E')['state']=='OPEN_SHORT'

def test_e_short_trailing_stop_only_tightens_and_recovers(tmp_path):
    from backend.e_trailing import set_protective_stop
    path=tmp_path/'e.sqlite';store=EPaperStore(path)
    assert store.open_position(agent='E',direction='SHORT',quantity=D('.1'),price=D('100'),stop=D('110'),fee=D('.01'),slippage=D('0'),idempotency_key='short',expected_version=0)['accepted']
    assert set_protective_stop(store,stop=D('105'),event_id='bar1')['accepted']
    assert not set_protective_stop(store,stop=D('106'),event_id='bar2')['accepted']
    q={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'105','ask':'106','event_at':'2026-01-01T00:00:00+00:00','event_id':'q'}
    assert protect_e_accounts({'shadow':path},q,CostModel())['shadow']['accepted']
    assert EPaperStore(path).account('E')['state']=='FLAT' and len(store.fills())==2

def test_e_short_liquidation_pending_survives_restart(tmp_path,monkeypatch):
    import backend.e_short_liquidation as guard
    path=tmp_path/'e.sqlite';store=EPaperStore(path)
    store.open_position(agent='E',direction='SHORT',quantity=D('99'),price=D('100'),stop=D('100.5'),fee=D('4.95'),slippage=D('0'),idempotency_key='short',expected_version=0)
    q={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'199','ask':'200','event_at':'2026-01-01T00:00:00+00:00','event_id':'q1'}
    with monkeypatch.context() as m:
        m.setattr(guard,'recover_pending_stop',lambda **kwargs:{'accepted':False,'reason':'SIMULATED_INTERRUPTION'})
        assert guard.protect_e_short_margin(store,q,CostModel())['reason']=='SIMULATED_INTERRUPTION'
    assert EPaperStore(path).account('E')['state']=='STOP_EXIT_PENDING'
    assert protect_e_accounts({'formal':path},{**q,'event_id':'q2'},CostModel())['formal']['accepted']
    assert EPaperStore(path).account('E')['state']=='FLAT' and len(store.fills())==2

