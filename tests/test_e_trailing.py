from decimal import Decimal as D
from backend.domain import CostModel
from backend.e_paper_store import EPaperStore
from backend.e_trailing import set_protective_stop,monitor_protective_stop

def opened(tmp_path):
    store=EPaperStore(tmp_path/'e.sqlite')
    assert store.open_position(agent='E',direction='LONG',quantity=D('.1'),price=D('100'),stop=D('90'),fee=D('.01'),slippage=D('0'),idempotency_key='e1',expected_version=0)['accepted']
    return store

def test_protective_stop_survives_reopen_and_closes_once(tmp_path):
    store=opened(tmp_path)
    assert set_protective_stop(store,stop=D('95'),event_id='bar1')['accepted']
    assert not set_protective_stop(store,stop=D('94'),event_id='bar2')['accepted']
    quote={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'94','ask':'94.1','event_at':'2026-01-01T00:00:00+00:00','event_id':'q1'}
    result=monitor_protective_stop(EPaperStore(store.path),quote,CostModel())
    assert result['accepted'] and EPaperStore(store.path).account('E')['state']=='FLAT'
    assert not monitor_protective_stop(EPaperStore(store.path),quote,CostModel())['accepted']
    assert len(store.fills())==2

def test_stale_quote_does_not_trigger_trailing_exit(tmp_path):
    store=opened(tmp_path);assert set_protective_stop(store,stop=D('95'),event_id='bar1')['accepted']
    q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':60001,'bid':'94','ask':'94.1','event_id':'q1'}
    assert monitor_protective_stop(store,q,CostModel())['reason']=='QUOTE_INVALID'
    assert store.account('E')['state']=='OPEN_LONG'

def test_pending_trailing_stop_recovers_after_restart_and_quote_interruption(tmp_path):
    store=opened(tmp_path);assert set_protective_stop(store,stop=D('95'),event_id='bar1')['accepted']
    account=store.account('E')
    assert store.request_stop_exit(agent='E',trigger_price=D('94'),event_key='E-trailing:test',quote_event_id='q1',expected_version=account['version'],trigger_evidence={'bid':'94','source':'ALPACA_US_BTCUSD_SPOT'})['accepted']
    restarted=EPaperStore(store.path)
    assert not monitor_protective_stop(restarted,None,CostModel())['accepted']
    assert restarted.account('E')['state']=='STOP_EXIT_PENDING'
    q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'93','ask':'93.1','event_id':'q2'}
    assert monitor_protective_stop(EPaperStore(store.path),q,CostModel())['accepted']
    assert EPaperStore(store.path).account('E')['state']=='FLAT' and len(store.fills())==2

def test_existing_quote_protection_dispatches_trailing_stop(tmp_path):
    from backend.e_protection import protect_e_accounts
    store=opened(tmp_path);assert set_protective_stop(store,stop=D('95'),event_id='bar1')['accepted']
    q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'94','ask':'94.1','event_id':'q1'}
    assert protect_e_accounts({'formal':store.path},q,CostModel())['formal']['accepted']
    assert EPaperStore(store.path).account('E')['state']=='FLAT'

def test_trailing_error_does_not_suppress_existing_fixed_stop(tmp_path,monkeypatch):
    import backend.e_trailing as trailing
    from backend.e_protection import protect_e_accounts
    store=opened(tmp_path)
    def fail(*args):raise RuntimeError('test trailing failure')
    monkeypatch.setattr(trailing,'monitor_protective_stop',fail)
    q={'source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'bid':'89','ask':'89.1','event_id':'q1'}
    assert protect_e_accounts({'formal':store.path},q,CostModel())['formal']['accepted']
    assert EPaperStore(store.path).account('E')['state']=='FLAT'
