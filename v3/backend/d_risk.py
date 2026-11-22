"""Deterministic, side-effect-free entry preflight for D spot paper research.

This is NOT an order endpoint. The caller must durably consume each candidate
once, enforce account-wide loss limits and protect the position before enabling
any execution. Funding and liquidation are not applicable to this spot model.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP

from backend.domain import CostModel

D = Decimal
TICK = D('0.01')
STEP = D('0.00000001')
RISK_FRACTION = D('0.02')
LEVERAGE = D('5')
NOTIONAL_FRACTION = D('0.50')
VOLUME_FRACTION = D('0.005')
MAX_SPREAD = D('0.0005')


def _decimal(value):
    number = D(str(value))
    if not number.is_finite():
        raise ValueError('NONFINITE_INPUT')
    return number


def _time(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return result.astimezone(timezone.utc)


def _floor(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil(value, step):
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def preflight_entry(signal, quote, equity, available_cash, prior_bar_volume, now, *, cost=None):
    """Return proposed entry/stop/targets; reject invalid data and candidates.

    The first valid post-close quote within the following bar is the only
    candidate entry opportunity. Callers must persist that attempt before use.
    """
    cost = cost or CostModel()
    try:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError('INVALID_NOW')
        now = now.astimezone(timezone.utc)
        if signal.get('direction') != 'LONG' or signal.get('setup') != 'D_CONFIRM':
            return {'accepted': False, 'reason': 'INVALID_SIGNAL'}
        close_at = _time(signal['signal_close'])
        event_at = _time(quote['event_at'])
        if not close_at <= event_at < close_at + timedelta(minutes=15):
            return {'accepted': False, 'reason': 'CANDIDATE_EXPIRED'}
        if not close_at <= now < close_at + timedelta(minutes=15):
            return {'accepted': False, 'reason': 'CANDIDATE_EXPIRED'}
        if quote.get('source') != 'BINANCE_USDM_PERP' or quote.get('currency') != 'USD_RESEARCH':
            return {'accepted': False, 'reason': 'SOURCE_MISMATCH'}
        if quote.get('timestamp_valid') is not True:
            return {'accepted': False, 'reason': 'QUOTE_STALE'}
        age_ms = (now - event_at).total_seconds() * 1000
        if not 0 <= age_ms <= cost.max_quote_age_ms:
            return {'accepted': False, 'reason': 'QUOTE_STALE'}
        bid, ask = _decimal(quote['bid']), _decimal(quote['ask'])
        equity, cash, volume = map(_decimal, (equity, available_cash, prior_bar_volume))
        atr, low, neckline, stop, close = map(_decimal, (signal['atr'], signal['low'], signal['neckline'], signal['stop'], signal['close']))
        if min(bid, ask, equity, cash, volume, atr, low, neckline, stop, close) <= 0 or ask < bid:
            return {'accepted': False, 'reason': 'INVALID_INPUT'}
        mid = (bid + ask) / 2
        if (ask - bid) / mid > MAX_SPREAD:
            return {'accepted': False, 'reason': 'SPREAD_TOO_WIDE'}
        stop = _floor(stop, TICK)
        slip = cost.adverse_slippage_bps / D('10000')
        fee_rate = cost.fee_rate
        entry = _ceil(ask * (1 + slip), TICK)
        risk = entry - stop
        if abs(ask - close) > D('.30') * atr or ask <= neckline or ask <= stop:
            return {'accepted': False, 'reason': 'ENTRY_GAP_OR_LEVEL'}
        if not (0 < entry - low <= D('3.5') * atr):
            return {'accepted': False, 'reason': 'ENTRY_TOO_FAR_FROM_LOW'}
        if not (D('.6') * atr <= risk <= D('3') * atr) or risk / entry > D('.02'):
            return {'accepted': False, 'reason': 'STOP_DISTANCE'}
        stop_fill = stop * (1 - slip)
        unit_loss = entry - stop_fill + fee_rate * (entry + stop_fill)
        if unit_loss <= 0:
            return {'accepted': False, 'reason': 'INVALID_RISK'}
        # Simulated 5x margin using spot quotes; no exchange funding model.
        quantity = _floor(min(equity * RISK_FRACTION / unit_loss,
                              equity * NOTIONAL_FRACTION / entry,
                              volume * VOLUME_FRACTION,
                              cash / (entry / LEVERAGE + entry * fee_rate)), STEP)
        # Fees round up to cents, so the exact pre-rounding risk budget may
        # need a slightly smaller quantity. Find the largest valid lot count.
        low_steps, high_steps = 0, int(quantity / STEP)
        while low_steps < high_steps:
            steps = (low_steps + high_steps + 1) // 2
            candidate = STEP * steps
            entry_fee = (candidate * entry * fee_rate).quantize(D('.01'), rounding=ROUND_UP)
            exit_fee = (candidate * stop_fill * fee_rate).quantize(D('.01'), rounding=ROUND_UP)
            loss = candidate * (entry - stop_fill) + entry_fee + exit_fee
            if loss <= equity * RISK_FRACTION and candidate * entry / LEVERAGE + entry_fee <= cash:
                low_steps = steps
            else:
                high_steps = steps - 1
        quantity = STEP * low_steps
        first = _floor(quantity / 2, STEP)
        second = quantity - first
        tp1 = _ceil(entry + risk, TICK)
        tp2 = _ceil(entry + 2 * risk, TICK)
        if min(first, second) < cost.minimum_quantity or min(first * tp1, second * tp2) < cost.minimum_notional:
            return {'accepted': False, 'reason': 'INVALID_SPLIT_QUANTITY'}
        fee = (quantity * entry * fee_rate).quantize(D('.01'), rounding=ROUND_UP)
        estimated_loss = quantity * (entry - stop_fill) + fee + (quantity * stop_fill * fee_rate).quantize(D('.01'), rounding=ROUND_UP)
        if estimated_loss > equity * RISK_FRACTION or quantity * entry / LEVERAGE + fee > cash:
            return {'accepted': False, 'reason': 'ROUNDED_RISK_OR_CASH_LIMIT'}
        return {'accepted': True, 'quantity': str(quantity), 'first_quantity': str(first),
                'second_quantity': str(second), 'price': str(entry), 'stop': str(stop),
                'tp1': str(tp1), 'tp2': str(tp2), 'estimated_loss': str(estimated_loss),
                'leverage': str(LEVERAGE), 'fee': str(fee), 'signal_close': close_at.isoformat()}
    except (KeyError, TypeError, ValueError, ArithmeticError, InvalidOperation):
        return {'accepted': False, 'reason': 'INVALID_INPUT'}

