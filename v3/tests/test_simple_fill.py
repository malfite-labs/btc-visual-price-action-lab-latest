from decimal import Decimal as D
import pytest
from backend.domain import CostModel
from backend.execution import prepare_entry


@pytest.mark.parametrize('direction,stop,expected', [('LONG','90','100.13'),('SHORT','110','99.98')])
def test_entry_ignores_book_quantity_and_uses_quote(direction, stop, expected):
    q = dict(source='BINANCE_USDM_PERP', timestamp_valid=True, bid='100', ask='100.1', bid_qty='0', ask_qty='0')
    r = prepare_entry(equity=D('10000'), available_cash=D('10000'), direction=direction,
        stop=D(stop), quote=q, orderbook={}, cost=CostModel(), quote_age_ms=0, book_age_ms=999999)
    assert r['accepted']
    assert D(r['quantity']) > D('0.01')
    assert D(r['fill_price']) == D(expected)
    assert D(r['fee']) == D(r['quantity'])*D(expected)*D('.0005')
    assert D(r['planned_loss']) <= D('200')


def test_stale_quote_still_rejected():
    q = dict(source='BINANCE_USDM_PERP', timestamp_valid=True, bid='100', ask='100.1')
    r = prepare_entry(equity=D('10000'),available_cash=D('10000'), direction='LONG',stop=D('90'),
        quote=q,orderbook={},cost=CostModel(),quote_age_ms=60001,book_age_ms=0)
    assert not r['accepted']


@pytest.mark.parametrize('direction,bid,ask,expected', [('LONG','100','100.1','99.98'),('SHORT','100','100.1','100.13')])
def test_time_exit_full_fill_fee_and_idempotency(tmp_path, direction, bid, ask, expected):
    import sqlite3
    from backend.ledger import Ledger
    from backend.rule_cycle import RuleCycleRunner
    db = tmp_path/'paper.sqlite3'
    ledger = Ledger(db)
    opened = ledger.open_position(agent='B',direction=direction,quantity=D('1'),price=D('100'),
        stop=D('90') if direction=='LONG' else D('110'),fee=D('.05'),slippage=D('0'),
        idempotency_key='open',expected_version=0)
    assert opened['accepted']
    runner = RuleCycleRunner(ledger,db)
    with sqlite3.connect(db) as c:
        c.execute('insert into rule_position_meta values(?,?,?,?,?,?,?,?)',
            ('B','test',opened['trade_id'],'120' if direction=='LONG' else '80','2026-10-31T09:30:00+00:00',12,'V3.0','RULE_TIME_EXIT'))
    q = dict(source='BINANCE_USDM_PERP',currency='USD_RESEARCH',timestamp_valid=True,
        age_ms=60001,bid=bid,ask=ask,bid_qty='0',ask_qty='0')
    assert runner.monitor_targets(q) == {}
    assert ledger.account('B')['state'] != 'FLAT'
    q['age_ms'] = 0
    assert runner.monitor_targets(q)['B']['accepted']
    runner.monitor_targets(q)
    fills = ledger.fills('B')
    assert len(fills) == 2
    close = fills[-1]
    assert D(close['price']) == D(expected)
    from decimal import ROUND_UP
    assert D(close['fee']) == (D(expected)*D('.0005')).quantize(D('.01'),rounding=ROUND_UP)
    import json
    assert json.loads(close['cost_model_json'])['fill_model'] == 'QUOTE_SIDE_FULL_FILL_V1'
    assert D(close['slippage']) == abs(D(expected)-D(bid if direction=='LONG' else ask))

