from decimal import Decimal as D

from backend.domain import CostModel
from backend.execution import handle_confirmed_stop, recover_pending_stop
from backend.ledger import Ledger
import pytest


def setup_position(tmp_path):
    ledger = Ledger(tmp_path / "paper.sqlite3")
    a = ledger.account("A")
    ledger.open_position(
        agent="A",
        direction="LONG",
        quantity=D("0.01"),
        price=D(100000),
        stop=D(99000),
        fee=D("0.50"),
        slippage=D(1),
        idempotency_key="open",
        expected_version=a["version"],
    )
    return ledger

@pytest.mark.parametrize('bid',['99500','98000'])
def test_stale_quote_cannot_create_a_stop_trigger(tmp_path,bid):
    ledger=setup_position(tmp_path)
    q={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':60001,'bid':bid,'ask':'100000','bid_qty':'1','ask_qty':'1'}
    result=handle_confirmed_stop(ledger=ledger,agent='A',trigger_event_id='stale',quote=q,cost=CostModel())
    assert not result['accepted'] and ledger.account('A')['state']=='OPEN_LONG'
    assert ledger.account('A')['pending_stop_json'] is None

def test_old_pending_without_trigger_evidence_does_not_close(tmp_path):
    ledger=setup_position(tmp_path)
    ledger.request_stop_exit(agent='A',trigger_price=None,event_key='legacy',quote_event_id=None,expected_version=1)
    q={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':0,'bid':'100000','ask':'100001','bid_qty':'1'}
    r=recover_pending_stop(ledger=ledger,agent='A',quote=q,cost=CostModel(),event_id='fresh')
    assert not r['accepted'] and ledger.account('A')['state']=='STOP_EXIT_PENDING'
    assert len(ledger.fills('A'))==1

def test_b_depth_recovery_rejects_legacy_unconfirmed_stop(tmp_path):
    from backend.b_stop_depth import recover_b_stop_depth
    ledger=Ledger(tmp_path/'b.sqlite3')
    assert ledger.open_position(agent='B',direction='LONG',quantity=D('1'),price=D('100'),stop=D('99'),fee=D('.05'),slippage=D('0'),idempotency_key='open',expected_version=0)['accepted']
    ledger.request_stop_exit(agent='B',trigger_price=None,event_key='old',quote_event_id=None,expected_version=1)
    r=recover_b_stop_depth(ledger=ledger,quote={},orderbook={},cost=CostModel(),event_id='new')
    assert r['reason']=='STOP_TRIGGER_EVIDENCE_MISSING' and len(ledger.fills('B'))==1


def test_missing_quote_does_not_trigger_stop(tmp_path):
    ledger = setup_position(tmp_path)
    pending = handle_confirmed_stop(
        ledger=ledger, agent="A", trigger_event_id="trigger-1", quote=None, cost=CostModel()
    )
    assert pending['reason'] == 'TRIGGER_QUOTE_UNAVAILABLE'
    assert ledger.account('A')['state'] == 'OPEN_LONG'
    quote = {
        "source": "BINANCE_USDM_PERP",
        "bid": "98950",
        "ask": "98960",
        "age_ms": 50,
        "event_id": "q-2",
        "bid_qty": "1",
    }
    closed = handle_confirmed_stop(ledger=ledger, agent='A', quote=quote,cost=CostModel(),trigger_event_id='fresh-trigger')
    assert closed["accepted"] and ledger.account("A")["state"] == "FLAT"
    duplicate = recover_pending_stop(
        ledger=ledger, agent="A", quote=quote, cost=CostModel(), event_id="recovery-1"
    )
    assert duplicate["reason"] == "NO_PENDING_STOP"


def test_stale_quotes_never_close_pending_stop(tmp_path):
    ledger = setup_position(tmp_path)
    ledger.request_stop_exit(agent='A',trigger_price=D('98000'),event_key='trigger-2',quote_event_id='valid-trigger',expected_version=1)
    stale = {"source": "BINANCE_USDM_PERP", "bid": "98000", "ask": "98010", "age_ms": 60001}
    result = recover_pending_stop(
        ledger=ledger, agent="A", quote=stale, cost=CostModel(), event_id="r"
    )
    assert result["reason"] == "QUOTE_STALE_REMAINS_PENDING"
    assert ledger.account("A")["state"] == "STOP_EXIT_PENDING"


def test_stop_fully_fills_despite_small_quote_quantity(tmp_path):
    ledger = setup_position(tmp_path)
    quote = {"source":"BINANCE_USDM_PERP", "bid":"98000", "ask":"98010",
             "bid_qty":"0", "ask_qty":"0", "age_ms":10, "event_id":"small"}
    result = handle_confirmed_stop(ledger=ledger,agent="A",trigger_event_id="stop-small",quote=quote,cost=CostModel())
    assert result["accepted"] and ledger.account("A")["state"] == "FLAT"
    assert len(ledger.fills("A")) == 2
    recover_pending_stop(ledger=ledger,agent="A",quote=quote,cost=CostModel(),event_id="repeat")
    assert len(ledger.fills("A")) == 2


def test_short_stop_uses_ask_adverse_slippage_and_taker_fee(tmp_path):
    ledger = Ledger(tmp_path / "short-stop.sqlite")
    opened = ledger.open_position(
        agent="B",
        direction="SHORT",
        quantity=D("0.01"),
        price=D("100000"),
        stop=D("101000"),
        fee=D("0.50"),
        slippage=D("0"),
        idempotency_key="short-open",
        expected_version=0,
    )
    assert opened["accepted"]
    quote = {
        "source": "BINANCE_USDM_PERP",
        "bid": "101990",
        "ask": "102000",
        "bid_qty": "1",
        "ask_qty": "1",
        "age_ms": 10,
        "event_id": "q-short",
    }
    result = handle_confirmed_stop(
        ledger=ledger, agent="B", trigger_event_id="short-stop", quote=quote, cost=CostModel()
    )
    assert result["accepted"]
    fill = ledger.fills("B")[-1]
    assert fill["side"] == "BUY" and D(fill["price"]) == D("102020.40")
    assert D(fill["fee"]) == D("0.52")

def test_two_successive_b_stop_recoveries_have_distinct_trade_identity(tmp_path):
 from types import SimpleNamespace
 from backend.scheduler import PaperCycleScheduler
 from backend.ledger import Ledger
 from backend.domain import CostModel
 from decimal import Decimal as D
 l=Ledger(tmp_path/'two-stops.sqlite3');scheduler=PaperCycleScheduler(tmp_path/'two-stops.sqlite3',SimpleNamespace(ledger=l),enabled=True)
 quote={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':0,'bid':'98','ask':'98.1','bid_qty':'10','ask_qty':'10'}
 for n in range(2):
  acct=l.account('B');o=l.open_position(agent='B',direction='LONG',quantity=D('1'),price=D('100'),stop=D('99'),fee=D('.05'),slippage=D('0'),idempotency_key=f'open-{n}',expected_version=acct['version']);assert o['accepted']
  acct=l.account('B');l.request_stop_exit(agent='B',trigger_price=D('98'),event_key=f'trigger-{n}',quote_event_id="confirmed-quote",expected_version=acct['version'])
  result=scheduler.recover_stops(quote=quote,cost=CostModel());assert result['B']['accepted'] and l.account('B')['state']=='FLAT'
 assert len([f for f in l.fills('B') if f['position_effect']=='CLOSE'])==2

def test_legacy_b_stop_uses_quote_not_depth(tmp_path):
 from decimal import Decimal as D
 from backend.ledger import Ledger
 from backend.b_stop_depth import recover_b_stop_depth
 from backend.domain import CostModel
 l=Ledger(tmp_path/'depth.sqlite3');l.open_position(agent='B',direction='LONG',quantity=D('1'),price=D('100'),stop=D('99'),fee=D('.05'),slippage=D('0'),idempotency_key='x',expected_version=0)
 l.request_stop_exit(agent='B',trigger_price=D('98'),event_key='stop',quote_event_id="confirmed-quote",expected_version=1)
 q={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':0,'bid':'98','ask':'98.1','bid_qty':'0.1'}
 book={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':0,'bids':[{'p':'98','s':'0.1'},{'p':'97.99','s':'0.2'}],'asks':[{'p':'98.1','s':'3'}]}
 r=recover_b_stop_depth(ledger=l,quote=q,cost=CostModel(),event_id='trade',orderbook=book);assert r['accepted'] and l.account('B')['state']=='FLAT';fills=l.fills('B');assert len([f for f in fills if f['position_effect']=='CLOSE'])==1
 close=next(f for f in fills if f['position_effect']=='CLOSE');assert D(close['quantity'])==D('1') and D(close['price'])==D('97.98');assert D(l.account('B')['cash'])==D('10000')-D('.05')+(D('97.98')-D('100'))-D(close['fee'])
 assert not recover_b_stop_depth(ledger=l,quote=q,cost=CostModel(),event_id='trade',orderbook=book)['accepted'];assert len(l.fills('B'))==2

def test_b_stop_ignores_stale_book_with_fresh_quote(tmp_path):
 from decimal import Decimal as D
 from backend.ledger import Ledger
 from backend.b_stop_depth import recover_b_stop_depth
 from backend.domain import CostModel
 l=Ledger(tmp_path/'stale-book.sqlite3');l.open_position(agent='B',direction='LONG',quantity=D('1'),price=D('100'),stop=D('99'),fee=D('.05'),slippage=D('0'),idempotency_key='x',expected_version=0);l.request_stop_exit(agent='B',trigger_price=D('98'),event_key='stop',quote_event_id="confirmed-quote",expected_version=1)
 q={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':0,'bid':'98','ask':'98.1','bid_qty':'0.1'};book={'source':'BINANCE_USDM_PERP','timestamp_valid':True,'age_ms':60001,'bids':[{'p':'98','s':'10'}],'asks':[]}
 r=recover_b_stop_depth(ledger=l,quote=q,cost=CostModel(),event_id='trade',orderbook=book);assert r['accepted'] and l.account('B')['state']=='FLAT'

