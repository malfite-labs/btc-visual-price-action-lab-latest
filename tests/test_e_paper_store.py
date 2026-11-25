from decimal import Decimal as D
from backend.e_paper_store import EPaperStore
from backend.execution import handle_confirmed_stop
from backend.domain import CostModel


def test_e_independent_ledger_open_stop_and_shadow_isolation(tmp_path):
    formal=EPaperStore(tmp_path/'formal.sqlite')
    shadow=EPaperStore(tmp_path/'shadow.sqlite')
    assert formal.account('E')['agent']=='E'
    result=formal.open_position(agent='E',direction='LONG',quantity=D('0.1'),price=D('100'),stop=D('90'),fee=D('0.01'),slippage=D('0'),idempotency_key='e-open',expected_version=0)
    assert result['accepted']
    assert shadow.account('E')['state']=='FLAT'
    quote={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':0,'bid':'89','ask':'89.1','event_id':'q1'}
    result=handle_confirmed_stop(ledger=formal,agent='E',trigger_event_id='stop1',quote=quote,cost=CostModel())
    assert result['accepted'] and formal.account('E')['state']=='FLAT'
    assert len(formal.fills())==2
    assert all(f['agent']=='E' for f in formal.fills())
    assert shadow.fills()==[]


def test_e_shadow_review_progress_counts_40_not_50(tmp_path):
    ledger=EPaperStore(tmp_path/'shadow.sqlite')
    for i in range(80):
        account=ledger.account('E')
        assert ledger.open_position(agent='E',direction='LONG',quantity=D('0.1'),price=D('100'),stop=D('90'),fee=D('0.01'),slippage=D('0'),idempotency_key=f'o{i}',expected_version=account['version'])['accepted']
        account=ledger.account('E')
        assert ledger.close_position(agent='E',price=D('100'),fee=D('0.01'),slippage=D('0'),idempotency_key=f'c{i}',expected_version=account['version'])['accepted']
    progress=ledger.review_progress()
    assert progress['closed_trades']==80
    assert progress['due_batches']==[40,80]
    assert progress['next_review_at']==120


def test_pending_stop_survives_restart_and_uses_existing_recovery(tmp_path):
    from backend.execution import recover_pending_stop
    path=tmp_path/'e.sqlite';store=EPaperStore(path)
    assert store.open_position(agent='E',direction='LONG',quantity=D('0.1'),price=D('100'),stop=D('90'),fee=D('0.01'),slippage=D('0'),idempotency_key='open',expected_version=0)['accepted']
    account=store.account('E')
    store.request_stop_exit(agent='E',trigger_price=D('89'),event_key='trigger',quote_event_id='q1',expected_version=account['version'])
    reopened=EPaperStore(path)
    assert reopened.account('E')['state']=='STOP_EXIT_PENDING'
    quote={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':0,'bid':'89','ask':'89.1','event_id':'q2'}
    assert recover_pending_stop(ledger=reopened,agent='E',quote=quote,cost=CostModel(),event_id='restart')['accepted']
    assert reopened.account('E')['state']=='FLAT'


def test_rejects_existing_non_e_database(tmp_path):
    import pytest
    from backend.ledger import Ledger
    path=tmp_path/'abc.sqlite';other=Ledger(path);before=other.accounts()
    with pytest.raises(ValueError,match='E_STORE_IDENTITY_INVALID'):
        EPaperStore(path)
    assert other.accounts()==before


def test_e_readonly_status_exposes_real_empty_state_and_performance(tmp_path):
    from backend.e_paper_store import read_e_store
    path=tmp_path/'e.sqlite'
    assert read_e_store(path)['status']=='NOT_INITIALIZED'
    assert not path.exists()
    store=EPaperStore(path)
    result=read_e_store(path)
    assert result['account']['agent']=='E'
    assert result['closed_trades']==0 and result['win_rate'] is None
    assert result['fills']==[]


def test_e_live_valuation_uses_existing_ledger_metrics(tmp_path):
    from backend.e_paper_store import read_e_store
    path=tmp_path/'e.sqlite';ledger=EPaperStore(path)
    ledger.open_position(agent='E',direction='LONG',quantity=D('.1'),price=D('100'),stop=D('90'),fee=D('.01'),slippage=D('0'),idempotency_key='o',expected_version=0)
    quote={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':0,'bid':'105','ask':'105.1'}
    result=read_e_store(path,quote=quote)
    assert result['equity'] is not None
    assert D(result['unrealized_pnl'])>0

