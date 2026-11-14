from datetime import datetime, timedelta, timezone

from backend import d_signal


def make_bars(count=964):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [dict(symbol='BTC/USD', source='ALPACA_US_BTCUSD_SPOT', open='99.9', high='100.5', low='99.5', close='100', volume='1',
                 open_at=(start + timedelta(minutes=15*i)).isoformat(),
                 close_at=(start + timedelta(minutes=15*(i+1))).isoformat(),
                 is_closed=True) for i in range(count)]


def test_data_gate_never_creates_a_signal():
    bars = make_bars()
    assert d_signal.evaluate(bars[:959])['reason'] == 'INSUFFICIENT_15M_HISTORY'
    bars[-5]['volume'] = '0'
    result = d_signal.evaluate(bars)
    assert result['status'] == 'DATA_NOT_READY'
    assert result['reason'] == 'SIGNAL_VOLUME_NOT_POSITIVE'
    assert result['signal'] is None


def test_gap_or_duplicate_rejected():
    bars = make_bars()
    bars[-2]['open_at'] = bars[-3]['open_at']
    assert d_signal.evaluate(bars)['status'] == 'DATA_NOT_READY'


def test_sma_seed_wilder_and_zero_rsi():
    assert d_signal._ema([1, 2, 3, 4], 3) == [None, None, 2, 3]
    b = [{'open': 100, 'high': 101, 'low': 99, 'close': 100} for _ in range(20)]
    assert d_signal._indicators(b)['rsi'][-1] == 50
    assert d_signal._indicators(b)['atr'][-1] == 2


def test_candidate_confirmation_and_cycle_idempotence(monkeypatch):
    bars = make_bars()
    bars[949]['high'] = '101.5'
    bars[959].update(open='99.4', high='100.3', low='98', close='99.5', volume='2')
    bars[960].update(open='100.5', high='100.8', low='100.3', close='100.7', volume='2')
    def indicators(items):
        n = len(items)
        out = {'ema9': [100.0]*n, 'ema20': [101.0]*n, 'ema50': [100.0]*n,
               'atr': [1.0]*n, 'rsi': [40.0]*n}
        if n > 900:
            out['ema20'][-6] = 102.0
            out['rsi'][-1] = 45.0
        return out
    monkeypatch.setattr(d_signal, '_indicators', indicators)
    first = d_signal.evaluate(bars[:960])
    assert first['status'] == 'WATCH'
    assert first['signal'] is None
    repeat = d_signal.evaluate(bars[:960], first['state'])
    assert repeat['status'] == 'DUPLICATE_BAR'
    confirmed = d_signal.evaluate(bars[:961], first['state'])
    assert confirmed['status'] == 'PENDING'
    assert confirmed['signal']['setup'] == 'D_CONFIRM'
    assert confirmed['signal']['stop'] == 97.75


def test_candidate_invalidation_takes_priority(monkeypatch):
    bars = make_bars()
    bars[-1].update(low='98', high='100.5', open='100', close='100.4')
    state = dict(phase='WATCH', bars_waited=0, atr=1, low=99, neckline=100,
                 stop=98.75, last_close=bars[-2]['close_at'])
    result = d_signal.evaluate(bars, state)
    assert result['status'] == 'COOLDOWN'
    assert result['reason'] == 'CANDIDATE_INVALIDATED'
    assert result['signal'] is None



def test_utc_hour_warmup_requires_240_full_hours():
    bars = make_bars(960)
    for b in bars:
        for key in ('open_at', 'close_at'):
            b[key] = (datetime.fromisoformat(b[key]) + timedelta(minutes=15)).isoformat()
    result = d_signal.evaluate(bars)
    assert result['status'] == 'DATA_NOT_READY'
    assert result['reason'] == 'INSUFFICIENT_1H_HISTORY'


def test_candidate_expiry_after_sixth_bar():
    bars = make_bars()
    state = {'phase': 'WATCH', 'bars_waited': 5, 'atr': 1, 'low': 99,
             'neckline': 110, 'stop': 98.75, 'last_close': bars[-2]['close_at']}
    result = d_signal.evaluate(bars, state)
    assert result['status'] == 'COOLDOWN'
    assert result['reason'] == 'CANDIDATE_EXPIRED'
    assert result['state']['remaining'] == 4
    assert result['signal'] is None


def test_nonfinite_and_open_bar_rejected():
    bars = make_bars()
    bars[-1]['close'] = 'NaN'
    assert d_signal.evaluate(bars)['reason'] == 'INVALID_CANDLE'
    bars[-1]['close'] = '100'
    bars[-1]['is_closed'] = False
    assert d_signal.evaluate(bars)['reason'] == 'INVALID_CANDLE'


def test_historic_zero_volume_allowed_but_not_recent():
    bars = make_bars()
    bars[30]['volume'] = '0'
    result = d_signal.evaluate(bars)
    assert result['reason'] == 'NO_SETUP'
    bars[-21]['volume'] = '0'
    assert d_signal.evaluate(bars)['reason'] == 'SIGNAL_VOLUME_NOT_POSITIVE'


def test_zero_volume_does_not_hide_invalidation():
    bars = make_bars()
    bars[-1].update(low='98', high='100.5', open='100', close='100.4', volume='0')
    state = dict(phase='WATCH', bars_waited=0, atr=1, low=99, neckline=100, stop=98.75, last_close=bars[-2]['close_at'])
    assert d_signal.evaluate(bars, state)['reason'] == 'CANDIDATE_INVALIDATED'


def test_zero_range_watch_still_invalidates_or_expires():
    bars = make_bars()
    bars[-1].update(open='98', high='98', low='98', close='98')
    state = dict(phase='WATCH', bars_waited=0, atr=1, low=99, neckline=100, stop=98.75, last_close=bars[-2]['close_at'])
    result = d_signal.evaluate(bars, state)
    assert result['reason'] == 'CANDIDATE_INVALIDATED'
    bars[-1].update(open='100', high='100', low='100', close='100')
    state['bars_waited'] = 5
    result = d_signal.evaluate(bars, state)
    assert result['reason'] == 'CANDIDATE_EXPIRED_ZERO_RANGE'
    assert result['state']['remaining'] == 4
