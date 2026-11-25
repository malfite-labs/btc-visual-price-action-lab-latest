from decimal import Decimal as D
from backend.e_paper_entry import submit_e_entry
from backend.e_paper_store import EPaperStore
from backend.domain import CostModel
from test_e_visual import bars


def test_unverified_mapping_blocks_but_verified_test_signal_uses_existing_preflight(tmp_path):
    ledger=EPaperStore(tmp_path/'e.sqlite')
    decision={'action':'OPEN','side':'LONG','setup':'E1','confidence':'HIGH','reasons':['reclaim'],
        'stop_anchor':{'candle':'K01','field':'low'},'reward_anchor':{'candle':'K48','field':'high'}}
    window=bars();window[-1]['high']='130'
    quote={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
        'age_ms':0,'bid':'99.9','ask':'100','event_id':'q1'}
    rejected=submit_e_entry(ledger=ledger,decision=decision,bars=window,quote=quote,cycle_id='x',mapping_validated=False,cost=CostModel())
    assert rejected['reason']=='VISUAL_MAPPING_NOT_VALIDATED'
    accepted=submit_e_entry(ledger=ledger,decision=decision,bars=window,quote=quote,cycle_id='x',mapping_validated=True,cost=CostModel())
    assert accepted['accepted'] and ledger.account('E')['state']=='OPEN_LONG'
    assert accepted['mapping']['stop_price']=='90'


def test_entry_stop_and_performance_chain(tmp_path):
    from backend.execution import handle_confirmed_stop
    from backend.e_paper_store import read_e_store
    path=tmp_path/'e.sqlite'; ledger=EPaperStore(path)
    decision={'action':'OPEN','side':'LONG','setup':'E1','confidence':'HIGH','reasons':['reclaim'],
        'stop_anchor':{'candle':'K01','field':'low'},'reward_anchor':{'candle':'K48','field':'high'}}
    window=bars();window[-1]['high']='130'
    quote={'source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,
        'age_ms':0,'bid':'99.9','ask':'100','event_id':'q1'}
    assert submit_e_entry(ledger=ledger,decision=decision,bars=window,quote=quote,cycle_id='x',mapping_validated=True,cost=CostModel())['accepted']
    stop_quote={**quote,'bid':'89','ask':'89.1','event_id':'q2'}
    assert handle_confirmed_stop(ledger=ledger,agent='E',trigger_event_id='q2',quote=stop_quote,cost=CostModel())['accepted']
    snapshot=read_e_store(path)
    assert snapshot['closed_trades']==1 and snapshot['win_rate']==0
    assert D(snapshot['realized_pnl'])<0 and len(snapshot['fills'])==2

