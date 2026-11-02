from decimal import Decimal as D
from backend.e_paper_store import EPaperStore
from backend.e_protection import protect_e_accounts
from backend.domain import CostModel


def test_e_quote_protection_runs_without_model_and_recovers_after_restart(tmp_path):
    p=tmp_path/'formal.sqlite';ledger=EPaperStore(p)
    ledger.open_position(agent='E',direction='LONG',quantity=D('0.1'),price=D('100'),stop=D('90'),fee=D('0.01'),slippage=D('0'),idempotency_key='open',expected_version=0)
    quote={'source':'ALPACA_US_BTCUSD_SPOT','timestamp_valid':True,'age_ms':0,'bid':'89','ask':'89.1','event_id':'q1'}
    result=protect_e_accounts({'formal':p,'shadow':tmp_path/'absent.sqlite'},quote,CostModel())
    assert result['formal']['accepted']
    assert EPaperStore(p).account('E')['state']=='FLAT'
    assert not (tmp_path/'absent.sqlite').exists()


def test_bad_e_database_does_not_block_other_e_protection(tmp_path):
    import sqlite3
    bad=tmp_path/'bad.sqlite'
    with sqlite3.connect(bad) as db: db.execute('create table unrelated(x)')
    good=tmp_path/'good.sqlite';ledger=EPaperStore(good)
    ledger.open_position(agent='E',direction='LONG',quantity=D('0.1'),price=D('100'),stop=D('90'),fee=D('0.01'),slippage=D('0'),idempotency_key='open',expected_version=0)
    quote={'source':'ALPACA_US_BTCUSD_SPOT','timestamp_valid':True,'age_ms':0,'bid':'89','ask':'89.1','event_id':'q1'}
    result=protect_e_accounts({'formal':bad,'shadow':good},quote,CostModel())
    assert result['formal']['reason']=='PROTECTION_ERROR'
    assert result['shadow']['accepted']
