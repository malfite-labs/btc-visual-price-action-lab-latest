from decimal import Decimal as D
from backend.d_margin import account_metrics


def test_five_x_margin_and_mark_to_market():
    p = {'quantity': '0.5', 'entry_price': '100', 'leverage': '5'}
    r = account_metrics('9999.97', p, '110')
    assert r['margin'] == D('10')
    assert r['unrealized_pnl'] == D('5')
    assert r['equity'] == D('10004.97')
    assert r['available_cash'] == D('9989.97')
    assert not r['liquidation_due']


def test_paper_liquidation_and_invalid_mark_block_open():
    p = {'quantity': '0.5', 'entry_price': '100', 'leverage': '5'}
    assert account_metrics('9999.97', p, '80')['liquidation_due']
    assert account_metrics('9999.97', p, 'NaN')['status'] == 'VALUATION_UNAVAILABLE'
    assert not account_metrics('0', None, '100')['allow_open']
