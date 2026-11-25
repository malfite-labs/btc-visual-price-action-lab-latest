from decimal import Decimal as D

from backend.domain import CostModel
from backend.execution import prepare_entry
from backend.rule_cycle import load_profile


def test_strategy_profile_retains_existing_limits_and_versions():
    profile, _, _ = load_profile()
    assert [profile[a]['risk_fraction'] for a in 'ABC'] == ['0.02'] * 3
    assert [profile[a]['max_leverage'] for a in 'ABC'] == ['1', '1', '5']
    assert [profile[a]['version'] for a in 'ABC'] == ['V2.6', 'V3.1', 'V3.1']
    assert profile['C']['target_r'] == '1.3' and profile['C']['max_hold'] == 12


def test_independent_risk_fraction_survives_depth_and_fee_revalidation():
    q = {'source':'BINANCE_USDM_PERP','bid':'100','ask':'100','timestamp_valid':True}
    book = {'bids':[{'p':'100','s':'10000'}], 'asks':[{'p':'100','s':'10000'}]}
    args = dict(equity=D('10000'),available_cash=D('10000'),direction='SHORT',stop=D('110'),
                quote=q,orderbook=book,cost=CostModel(),quote_age_ms=0,book_age_ms=0,leverage=D('5'))
    one = prepare_entry(**args, risk_fraction=D('.01'))
    two = prepare_entry(**args, risk_fraction=D('.02'))
    assert one['accepted'] and two['accepted']
    assert D(one['planned_loss']) <= D('100')
    assert D(two['planned_loss']) <= D('200')
    assert D(one['quantity']) < D(two['quantity'])

