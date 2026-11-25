"""Pure D position decisions for isolated spot paper execution.

No orders or account mutations occur here. The caller must serialize quote
processing and durably recover each pending quote and any triggered stop.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from backend.domain import CostModel

D = Decimal
STEP = D('0.00000001')
TICK = D('.01')


def _dt(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return result.astimezone(timezone.utc)


def _dec(value):
    result = D(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('INVALID_NUMBER')
    return result


def plan_position_event(position, quote, now, *, cost=None):
    """Plan the next single state transition for a fresh bid quote.

    The calling dispatcher must loop after each committed action. In particular,
    one quote above both targets requires TP1 then TP2, each idempotent.
    """
    cost = cost or CostModel()
    try:
        if now.tzinfo is None:
            raise ValueError('INVALID_NOW')
        now = now.astimezone(timezone.utc)
        if quote.get('source') != 'BINANCE_USDM_PERP' or quote.get('currency') != 'USD_RESEARCH':
            return {'action': 'BLOCKED', 'reason': 'SOURCE_MISMATCH'}
        if quote.get('timestamp_valid') is not True:
            return {'action': 'BLOCKED', 'reason': 'QUOTE_STALE'}
        event_at = _dt(quote['event_at'])
        if not 0 <= (now - event_at).total_seconds() * 1000 <= cost.max_quote_age_ms:
            return {'action': 'BLOCKED', 'reason': 'QUOTE_STALE'}
        bid, ask = _dec(quote['bid']), _dec(quote['ask'])
        if bid > ask:
            return {'action': 'BLOCKED', 'reason': 'INVALID_QUOTE'}
        entry = _dec(position['entry_price'])
        stop = _dec(position['stop_price'])
        tp1, tp2 = _dec(position['tp1']), _dec(position['tp2'])
        initial, remaining = _dec(position['initial_quantity']), _dec(position['quantity'])
        if remaining > initial or position.get('direction') != 'LONG':
            return {'action': 'BLOCKED', 'reason': 'INVALID_POSITION'}
        entry_bar_close = _dt(position['entry_bar_close'])
        if event_at < entry_bar_close - timedelta(minutes=15):
            return {'action': 'BLOCKED', 'reason': 'QUOTE_BEFORE_ENTRY'}
        bar_open = event_at.replace(minute=event_at.minute // 15 * 15, second=0, microsecond=0)
        pending_stop = position.get('pending_stop')
        if pending_stop is not None:
            effective_after = _dt(position['pending_stop_after'])
            if bar_open >= effective_after:
                return {'action': 'UPDATE_STOP', 'stop': str(max(stop, _dec(pending_stop))),
                        'current_bar_open': bar_open.isoformat()}
        slip = cost.adverse_slippage_bps / D('10000')
        sell_price = ((bid * (1 - slip)) / TICK).to_integral_value(rounding=ROUND_DOWN) * TICK
        if bid <= stop:
            return {'action': 'STOP', 'quantity': str(remaining), 'price': str(sell_price)}
        if not position.get('tp1_filled') and bar_open >= entry_bar_close + timedelta(minutes=15 * 7):
            return {'action': 'TIME_EXIT', 'reason': 'NO_TP1_8_BARS', 'quantity': str(remaining), 'price': str(sell_price)}
        if bar_open >= entry_bar_close + timedelta(minutes=15 * 15):
            return {'action': 'TIME_EXIT', 'reason': 'MAX_HOLD_16_BARS', 'quantity': str(remaining), 'price': str(sell_price)}
        if not position.get('tp1_filled') and bid >= tp1:
            quantity = (initial / 2 / STEP).to_integral_value(rounding=ROUND_DOWN) * STEP
            if quantity <= 0 or quantity >= remaining:
                return {'action': 'BLOCKED', 'reason': 'INVALID_TP1_QUANTITY'}
            return {'action': 'TP1', 'quantity': str(quantity), 'price': str(sell_price),
                    'bar_close': (bar_open + timedelta(minutes=15)).isoformat()}
        if position.get('tp1_filled') and bid >= tp2:
            return {'action': 'TP2', 'quantity': str(remaining), 'price': str(sell_price)}
        return {'action': 'HOLD'}
    except (KeyError, TypeError, ValueError, ArithmeticError, InvalidOperation):
        return {'action': 'BLOCKED', 'reason': 'INVALID_INPUT'}

