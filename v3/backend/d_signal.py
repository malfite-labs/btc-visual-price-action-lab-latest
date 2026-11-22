"""D-R deterministic 15-minute signals for the local BTC/USD paper feed.

The caller owns durable state, order placement and exits. This module only
processes one newly closed candle at a time and never submits an order.
"""
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median


class DataNotReady(ValueError):
    pass


# Minimum history required by the indicators and rolling windows below.
# This intentionally replaces the research JSON's conservative 960/240
# preload: D can run from the Binance archive once its actual calculations
# are defined (15m EMA20/RSI/ATR/RVOL windows and 1h EMA50/ATR14).
MIN_15M_BARS = 25
MIN_COMPLETED_HOURS = 50
RULE_VERSION = 'D-SOURCE-RECOVERY-2026-11-21'
SOURCE_COMMIT = '82ce4a0bf89e54e066dda63fd97be1e3324a0204'


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise DataNotReady('TIMEZONE_REQUIRED')
    return parsed.astimezone(timezone.utc)


def _validate(bars):
    if len(bars) < MIN_15M_BARS:
        raise DataNotReady('INSUFFICIENT_15M_INDICATOR_HISTORY')
    converted = []
    previous = None
    for bar in bars:
        try:
            start, end = _time(bar['open_at']), _time(bar['close_at'])
            values = {key: float(bar[key]) for key in ('open', 'high', 'low', 'close', 'volume')}
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DataNotReady('INVALID_CANDLE') from exc
        if bar.get('symbol') != 'BTC/USD' or bar.get('source') != 'BINANCE_USDM_PERP':
            raise DataNotReady('SOURCE_OR_SYMBOL_MISMATCH')
        if start.second or start.microsecond or start.minute % 15 or end != start + timedelta(minutes=15):
            raise DataNotReady('INVALID_INTERVAL')
        if previous is not None and start != previous:
            raise DataNotReady('CANDLE_GAP_OR_DUPLICATE')
        if bar.get('is_closed') is not True or not all(isfinite(v) for v in values.values()):
            raise DataNotReady('INVALID_CANDLE')
        o, h, l, c, v = (values[k] for k in ('open', 'high', 'low', 'close', 'volume'))
        if l <= 0 or not l <= min(o, c) <= max(o, c) <= h:
            raise DataNotReady('INVALID_OHLC')
        if v < 0:
            raise DataNotReady('NEGATIVE_VOLUME')
        converted.append({**values, 'open_at': start, 'close_at': end})
        previous = end
    return converted


def _ema(values, period):
    output = [None] * len(values)
    if len(values) < period:
        return output
    value = sum(values[:period]) / period
    output[period - 1] = value
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        value = alpha * values[i] + (1 - alpha) * value
        output[i] = value
    return output


def _wilder(values, period=14):
    output = [None] * len(values)
    if len(values) <= period:
        return output
    value = sum(values[1:period + 1]) / period
    output[period] = value
    for i in range(period + 1, len(values)):
        value = (value * (period - 1) + values[i]) / period
        output[i] = value
    return output


def _indicators(bars):
    closes = [b['close'] for b in bars]
    ranges = [0.0] + [max(bars[i]['high'] - bars[i]['low'], abs(bars[i]['high'] - closes[i - 1]), abs(bars[i]['low'] - closes[i - 1])) for i in range(1, len(bars))]
    gain = [0.0] + [max(closes[i] - closes[i - 1], 0) for i in range(1, len(bars))]
    loss = [0.0] + [max(closes[i - 1] - closes[i], 0) for i in range(1, len(bars))]
    ag, al = _wilder(gain), _wilder(loss)
    rsi = [None if g is None else (50.0 if g == 0 and l == 0 else 100.0 if l == 0 else 100 - 100 / (1 + g / l)) for g, l in zip(ag, al)]
    return {'ema9': _ema(closes, 9), 'ema20': _ema(closes, 20), 'ema50': _ema(closes, 50), 'atr': _wilder(ranges), 'rsi': rsi}


def _hours(bars):
    hours = []
    for index in range(len(bars) - 3):
        chunk = bars[index:index + 4]
        if chunk[0]['open_at'].minute != 0:
            continue
        hours.append({'open': chunk[0]['open'], 'high': max(x['high'] for x in chunk), 'low': min(x['low'] for x in chunk), 'close': chunk[-1]['close'], 'volume': sum(x['volume'] for x in chunk), 'close_at': chunk[-1]['close_at']})
    if len(hours) < MIN_COMPLETED_HOURS:
        raise DataNotReady('INSUFFICIENT_1H_INDICATOR_HISTORY')
    return hours


def _output(status, reason, state, signal=None):
    return {'status': status, 'reason': reason, 'state': state, 'signal': signal}


def evaluate(bars, state=None):
    """Process latest bar using caller-persisted state. Invalid data never signals."""
    state = dict(state or {'phase': 'IDLE'})
    try:
        b = _validate(bars)
        hours = _hours(b)
    except (DataNotReady, TypeError, ValueError) as exc:
        return _output('DATA_NOT_READY', str(exc), state)
    key = b[-1]['close_at'].isoformat()
    if state.get('last_close') == key:
        return _output('DUPLICATE_BAR', 'DUPLICATE_BAR', state)
    if state.get('last_close'):
        try:
            if _time(state['last_close']) + timedelta(minutes=15) != b[-1]['close_at']:
                return _output('DATA_NOT_READY', 'STATE_CYCLE_GAP', state)
        except (ValueError, DataNotReady):
            return _output('DATA_NOT_READY', 'INVALID_STATE_TIME', state)
    state['last_close'] = key
    phase = state.get('phase', 'IDLE')
    if phase == 'PENDING':
        return _output('PENDING', 'AWAIT_EXECUTION', state)
    if phase == 'LONG':
        return _output('LONG', 'POSITION_MANAGED_EXTERNALLY', state)
    if phase == 'COOLDOWN':
        remaining = int(state.get('remaining', 0)) - 1
        state['remaining'] = max(remaining, 0)
        if remaining <= 0:
            state['phase'] = 'IDLE'
        return _output('COOLDOWN', 'COOLDOWN', state)
    if phase not in ('IDLE', 'WATCH'):
        return _output('DATA_NOT_READY', 'INVALID_STATE', state)
    i = len(b) - 1
    ind, hour_ind = _indicators(b), _indicators(hours)
    required_15m = (ind['ema9'][i], ind['ema20'][i - 1], ind['ema20'][i - 5],
                    ind['atr'][i - 1], ind['rsi'][i], ind['rsi'][i - 1])
    h_latest = len(hours) - 1
    required_1h = (hour_ind['ema50'][h_latest], hour_ind['ema20'][h_latest],
                   hour_ind['ema20'][h_latest - 3], hour_ind['atr'][h_latest])
    if any(value is None or not isfinite(value) for value in (*required_15m, *required_1h)):
        return _output('DATA_NOT_READY', 'INDICATOR_NOT_READY', state)
    bar = b[i]
    spread = bar['high'] - bar['low']
    clv = (bar['close'] - bar['low']) / spread if spread > 0 else None
    if phase == 'IDLE':
        if spread <= 0:
            return _output('IDLE', 'ZERO_RANGE', state)
        if any(x['volume'] <= 0 for x in b[i-20:i+1]):
            return _output('DATA_NOT_READY', 'SIGNAL_VOLUME_NOT_POSITIVE', state)
        rvol = bar['volume'] / median(x['volume'] for x in b[i-20:i])
        a = ind['atr'][i-1]
        if a <= 0:
            return _output('IDLE', 'INVALID_ATR', state)
        wick = (min(bar['open'], bar['close']) - bar['low']) / spread
        found = (b[i-1]['close'] < ind['ema20'][i-1] < ind['ema20'][i-5]
                 and (max(x['high'] for x in b[i-12:i]) - bar['low']) / a >= 2.5
                 and bar['low'] < min(x['low'] for x in b[i-8:i])
                 and (bar['close'] > bar['open'] or wick >= .35)
                 and clv >= .55 and rvol >= 1.3)
        if found:
            state.update(phase='WATCH', candidate_close=key, bars_waited=0, atr=a,
                         low=bar['low'], neckline=max(x['high'] for x in b[i-3:i]),
                         stop=bar['low'] - .25 * a)
            return _output('WATCH', 'CANDIDATE_DETECTED', state)
        return _output('IDLE', 'NO_SETUP', state)
    try:
        a, low, neck, stop = (float(state[k]) for k in ('atr', 'low', 'neckline', 'stop'))
        waited = int(state['bars_waited']) + 1
    except (KeyError, TypeError, ValueError):
        return _output('DATA_NOT_READY', 'INVALID_WATCH_STATE', state)
    state['bars_waited'] = waited
    if bar['low'] < low - .1 * a:
        state.update(phase='COOLDOWN', remaining=4)
        return _output('COOLDOWN', 'CANDIDATE_INVALIDATED', state)
    if spread <= 0:
        if waited >= 6:
            state.update(phase='COOLDOWN', remaining=4)
            return _output('COOLDOWN', 'CANDIDATE_EXPIRED_ZERO_RANGE', state)
        return _output('WATCH', 'ZERO_RANGE', state)
    if any(x['volume'] <= 0 for x in b[i-20:i+1]):
        if waited >= 6:
            state.update(phase='COOLDOWN', remaining=4)
            return _output('COOLDOWN', 'CANDIDATE_EXPIRED_VOLUME_UNAVAILABLE', state)
        return _output('WATCH', 'SIGNAL_VOLUME_NOT_POSITIVE', state)
    rvol = bar['volume'] / median(x['volume'] for x in b[i-20:i])
    h = len(hours) - 1
    hourly_bear = hours[h]['close'] < hour_ind['ema50'][h] and hour_ind['ema20'][h] < hour_ind['ema20'][h-3]
    confirmed = (1 <= waited <= 6 and bar['close'] > neck + .05*a and bar['close'] > ind['ema9'][i]
                 and bar['close'] > bar['open'] and clv >= .65 and rvol >= 1.2
                 and ind['rsi'][i] > ind['rsi'][i-1] and ind['rsi'][i] >= 35
                 and 0 < bar['close'] - low <= 3.5*a and not hourly_bear
                 and .6*a <= bar['close'] - stop <= 3*a
                 and hour_ind['atr'][h] / hours[h]['close'] <= .04)
    if confirmed:
        state['phase'] = 'PENDING'
        signal = {'direction': 'LONG', 'setup': 'D_CONFIRM', 'signal_close': key,
                  'close': bar['close'], 'atr': a, 'low': low, 'neckline': neck, 'stop': stop}
        state['signal'] = signal
        return _output('PENDING', 'D_CONFIRM', state, signal)
    if waited >= 6:
        state.update(phase='COOLDOWN', remaining=4)
        return _output('COOLDOWN', 'CANDIDATE_EXPIRED', state)
    return _output('WATCH', 'AWAIT_CONFIRMATION', state)

