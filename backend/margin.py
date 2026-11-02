"""Isolated research margin calculations; not an exchange liquidation model.

This module alone does not authorize trading or execute exits. Maintenance rate
is an explicit caller-provided research parameter and is not yet frozen.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class MarginAssessment:
    unrealized_pnl: Decimal
    equity: Decimal
    used_margin: Decimal
    available_margin: Decimal
    maintenance_margin: Decimal
    liquidation_required: bool


def assess_margin(
    *,
    cash: Decimal,
    direction: str,
    quantity: Decimal,
    entry_price: Decimal,
    exit_quote: Decimal,
    leverage: Decimal,
    maintenance_rate: Decimal,
) -> MarginAssessment:
    values = (cash, quantity, entry_price, exit_quote, leverage, maintenance_rate)
    if not all(v.is_finite() for v in values):
        raise ValueError("NONFINITE_MARGIN_INPUT")
    if (
        direction not in {"LONG", "SHORT"}
        or min(quantity, entry_price, exit_quote) <= 0
        or not Decimal("1") <= leverage <= Decimal("5")
        or not Decimal("0") < maintenance_rate < Decimal("1")
    ):
        raise ValueError("INVALID_MARGIN_INPUT")
    sign = Decimal("1") if direction == "LONG" else Decimal("-1")
    pnl = (exit_quote - entry_price) * quantity * sign
    equity = cash + pnl
    used = entry_price * quantity / leverage
    maintenance = exit_quote * quantity * maintenance_rate
    return MarginAssessment(
        pnl, equity, used, max(Decimal("0"), equity - used), maintenance, equity <= maintenance
    )
