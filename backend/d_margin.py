"""Five-times paper-account margin valuation; no broker liquidation model."""
from decimal import Decimal, InvalidOperation

D = Decimal
LEVERAGE = D('5')


def account_metrics(cash, position, bid, *, fee_rate=D('0.0005')):
    """Mark a long position at executable bid and calculate isolated margin.

    A liquidation threshold is a local simulation based on margin exhaustion
    and the estimated closing fee. It is not an exchange liquidation price.
    """
    try:
        balance, mark, fee = D(str(cash)), D(str(bid)), D(str(fee_rate))
        if not all(x.is_finite() for x in (balance, mark, fee)) or mark <= 0 or fee < 0:
            raise ValueError('INVALID_VALUATION')
        if position is None:
            if balance <= 0:
                return {'status': 'EQUITY_INVALID', 'allow_open': False}
            return {'status': 'FLAT', 'cash': balance, 'equity': balance,
                    'margin': D(0), 'available_cash': balance, 'unrealized_pnl': D(0),
                    'liquidation_due': False, 'allow_open': True}
        qty = D(str(position['quantity']))
        entry = D(str(position['entry_price']))
        leverage = D(str(position['leverage']))
        if not all(x.is_finite() and x > 0 for x in (qty, entry, leverage)) or leverage != LEVERAGE:
            raise ValueError('INVALID_POSITION')
        margin = qty * entry / LEVERAGE
        pnl = qty * (mark - entry)
        equity = balance + pnl
        close_fee = qty * mark * fee
        isolated_remaining = margin + pnl
        liquidation = isolated_remaining <= close_fee or equity <= close_fee
        return {'status': 'LIQUIDATION_DUE' if liquidation else 'OPEN_LONG',
                'cash': balance, 'equity': equity, 'margin': margin,
                'available_cash': max(D(0), balance - margin), 'unrealized_pnl': pnl,
                'estimated_close_fee': close_fee, 'liquidation_due': liquidation,
                'allow_open': False}
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return {'status': 'VALUATION_UNAVAILABLE', 'allow_open': False}
