from datetime import datetime, timedelta, timezone
from decimal import Decimal
from backend.d_risk import preflight_entry


def sample():
    now = datetime(2026, 11, 5, 12, 0, 5, tzinfo=timezone.utc)
    signal = {'direction': 'LONG', 'setup': 'D_CONFIRM', 'signal_close': '2026-11-05T12:00:00+00:00',
              'atr': '2', 'low': '98', 'neckline': '100', 'stop': '98.9', 'close': '100'}
    quote = {'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH', 'timestamp_valid': True,
             'bid': '100.08', 'ask': '100.1', 'event_at': (now - timedelta(seconds=1)).isoformat()}
    return signal, quote, now


def test_preflight_revalidates_cent_rounded_risk_and_split():
    signal, quote, now = sample()
    result = preflight_entry(signal, quote, '10000', '10000', '4000', now)
    assert result['accepted']
    assert result['leverage'] == '5'
    assert Decimal(result['estimated_loss']) <= Decimal('200')
    assert Decimal(result['first_quantity']) + Decimal(result['second_quantity']) == Decimal(result['quantity'])
    assert Decimal(result['price']) > Decimal(quote['ask'])
    assert Decimal(result['tp2']) - Decimal(result['price']) >= 2 * (Decimal(result['price']) - Decimal(result['stop']))


def test_preflight_rejects_bad_quote_spread_lateness_and_volume():
    signal, quote, now = sample()
    def check(q, at=now, volume='4000'):
        return preflight_entry(signal, q, '10000', '10000', volume, at)
    assert check({**quote, 'ask': '101'})['reason'] == 'SPREAD_TOO_WIDE'
    assert check({**quote, 'timestamp_valid': False})['reason'] == 'QUOTE_STALE'
    assert check({**quote, 'event_at': (now + timedelta(seconds=1)).isoformat()})['reason'] == 'QUOTE_STALE'
    assert check(quote, at=now + timedelta(minutes=15))['reason'] == 'CANDIDATE_EXPIRED'
    assert not check(quote, volume='0')['accepted']
    assert not check(quote, volume='.000001')['accepted']

def test_five_x_margin_and_risk_remain_bounded_under_low_cash():
    signal, quote, now = sample()
    result = preflight_entry(signal, quote, '10000', '50', '4000', now)
    assert result['accepted']
    quantity = Decimal(result['quantity'])
    price = Decimal(result['price'])
    assert result['leverage'] == '5'
    assert quantity * price / Decimal('5') + Decimal(result['fee']) <= Decimal('50')
    assert Decimal(result['estimated_loss']) <= Decimal('200')
    assert quantity * price <= Decimal('5000')

