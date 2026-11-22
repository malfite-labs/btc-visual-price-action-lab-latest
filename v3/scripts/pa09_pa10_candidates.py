"""Offline PA-09/10 candidate signals; not StrategyPool upload documents."""
from datetime import datetime, timedelta
from math import isfinite
from statistics import mean

STEP = timedelta(minutes=15)
PARAMETERS = {
    'provenance': 'IMPLEMENTATION_PROPOSAL',
    'atr_period': 14, 'atr_method': 'simple mean of true range',
    'pa09_sma_denominator': 'SMA60', 'pa09_touches': 'low/high',
    'pa09_close_back_inside': True, 'pa09_zero_body': 'reject',
    'pa10_level_lookback': 40, 'pa10_level_band_atr': .2,
    'pa10_level_touches': 2, 'pa10_touch_spacing': 3,
    'pa10_breakout_atr': .2, 'pa10_breakout_body_fraction': .5,
    'pa10_extension_atr': 1, 'pa10_retest_atr': .2,
    'pa10_close_through_atr': .1, 'pa10_stop_buffer_atr': .1,
    'pa10_window': 'breakout+1 through breakout+16 inclusive',
    'pa10_confirmation': 'directional body and close beyond retest high/low',
    'pa10_entry': 'next open; never backfill',
}


def time(bar):
    t = datetime.fromisoformat(bar['open_at'].replace('Z', '+00:00'))
    if t.utcoffset() is None or t.minute % 15 or t.second or t.microsecond:
        raise ValueError('INVALID_BAR_TIME')
    return t


def validate(bars):
    previous = None
    for bar in bars:
        t = time(bar)
        o, h, low, c = (bar[k] for k in ('open', 'high', 'low', 'close'))
        if (bar.get('is_closed') is not True or not all(isfinite(x) and x > 0 for x in (o,h,low,c))
                or low > min(o,c) or h < max(o,c) or (previous is not None and t-previous != STEP)):
            raise ValueError('INVALID_OR_NONCONTIGUOUS_CLOSED_BARS')
        previous = t


def atr(bars):
    return mean(max(b['high']-b['low'], abs(b['high']-a['close']), abs(b['low']-a['close']))
                for a,b in zip(bars[-15:-1], bars[-14:]))


def touches(window, field, level, tolerance):
    last, count = -3, 0
    for i, bar in enumerate(window):
        if i-last >= 3 and abs(bar[field]-level) <= tolerance:
            last, count = i, count+1
    return count


def pa09(bars, *, audit=None):
    def reject(reason):
        if audit is not None:
            audit(reason)
        return None

    validate(bars)
    if len(bars) < 61:
        return reject('warmup')
    window, b, a = bars[-41:-1], bars[-1], atr(bars[:-1])
    s,r = min(x['low'] for x in window), max(x['high'] for x in window)
    sma60 = mean(x['close'] for x in bars[-61:-1])
    if a <= 0 or not .015 <= (r-s)/((r+s)/2) <= .06:
        return reject('atr_or_width')
    if abs(mean(x['close'] for x in bars[-21:-1])-sma60)/sma60 > .015:
        return reject('sma')
    if min(touches(window,'low',s,.2*a),touches(window,'high',r,.2*a)) < 2:
        return reject('touches')
    if not s <= b['close'] <= r:
        return reject('close_outside')
    o,c,h,low = (b[k] for k in ('open','close','high','low'))
    body = abs(c-o)
    if c > o and o-low >= 2*body and h-c <= body and abs(low-s) <= .2*a:
        result = dict(side='LONG', stop=min(low,s)-.1*a, target=r-.15*a)
    elif c < o and h-o >= 2*body and c-low <= body and abs(h-r) <= .2*a:
        result = dict(side='SHORT', stop=max(h,r)+.1*a, target=s+.15*a)
    else:
        return reject('boundary_or_pinbar')
    result.update(signal=time(b).isoformat(), max_hold=96, min_r=2)
    return result if entry(result, time(b)+STEP, c) else reject('planned_rr')


def pa10(bars, state=None):
    """Return JSON-serializable state and optional signal. Process one new close."""
    validate(bars)
    if len(bars) < 41:
        return state, None
    b, window = bars[-1], bars[-41:-1]
    t = time(b)
    a = atr(window)
    if state:
        last = datetime.fromisoformat(state['last'])
        if t <= last:
            return state, None
        if t-last != STEP:
            return None, None
        state = {**state, 'last':t.isoformat()}
        if state['phase'] == 'CONFIRMED':
            return None, None
        if t-datetime.fromisoformat(state['breakout']) > 16*STEP:
            return None, None
        p, u, d = state['level'], state['atr'], state['direction']
        if d*(b['close']-p) < -.1*u:
            return None, None
        extreme = b['high'] if d==1 else b['low']
        test = b['low'] if d==1 else b['high']
        if state['phase']=='BREAKOUT_CANDIDATE' and d*(extreme-p)>=u:
            state['phase']='EXTENSION_CONFIRMED'
        elif state['phase']=='EXTENSION_CONFIRMED' and abs(test-p)<=.2*u:
            state.update(phase='RETEST_OBSERVED', test=test, confirm=extreme)
        elif state['phase']=='RETEST_OBSERVED':
            state['test'] = min(state['test'],test) if d==1 else max(state['test'],test)
            if d*(b['close']-state['confirm'])>0 and d*(b['close']-b['open'])>0:
                state['phase']='CONFIRMED'
                return state, dict(side='LONG' if d==1 else 'SHORT', stop=state['test']-d*.1*u,
                                   target_r=2.5, max_hold=192, signal=t.isoformat())
        return state, None
    if a <= 0:
        return None,None
    for d, field, extremum in ((1,'high',max),(-1,'low',min)):
        p=extremum(x[field] for x in window)
        if (touches(window,field,p,.2*a)>=2 and d*(b['close']-p)>=.2*a
                and d*(b['close']-b['open'])>0 and abs(b['close']-b['open'])>=.5*(b['high']-b['low'])):
            return dict(phase='BREAKOUT_CANDIDATE', level=p, atr=a, direction=d,
                        breakout=t.isoformat(), last=t.isoformat()),None
    return None,None


def entry(signal, at, fill, *, validating=False):
    """Check one candidate against a supplied fill; no order or accounting side effects."""
    if validating or at != datetime.fromisoformat(signal['signal'])+STEP or not isfinite(fill) or fill<=0:
        return None
    d=1 if signal['side']=='LONG' else -1
    risk=d*(fill-signal['stop'])
    if risk<=0 or signal['stop']<=0:
        return None
    target=signal['target'] if 'target' in signal else fill+d*risk*signal['target_r']
    return target if target>0 and d*(target-fill)>=signal.get('min_r',0)*risk else None
