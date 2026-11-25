from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum

D = Decimal


class PositionState(StrEnum):
    FLAT = "FLAT"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    STOP_EXIT_PENDING = "STOP_EXIT_PENDING"
    CLOSING = "CLOSING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class Action(StrEnum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    HOLD = "HOLD"
    WAIT = "WAIT"


@dataclass(frozen=True)
class CostModel:
    fee_rate: Decimal = D("0.0005")  # Market/taker rate, research reference only.
    maker_reference_rate: Decimal = D("0.0002")  # Reference only; no maker fills.
    adverse_slippage_bps: Decimal = D("2")
    max_quote_age_ms: int = 60_000
    max_book_age_ms: int = 60_000
    simulated_tick: Decimal = D("0.01")
    quantity_step: Decimal = D("0.00000001")
    minimum_quantity: Decimal = D("0.00000001")
    minimum_notional: Decimal = D("1.00")
    max_price_deviation_bps: Decimal = D("50")

    def provenance(self) -> dict[str, str]:
        frozen = (
            self.fee_rate == D("0.0005")
            and self.maker_reference_rate == D("0.0002")
            and self.adverse_slippage_bps == D("2")
        )
        return {
            "model": "BINANCE_FUTURES_REFERENCE_RESEARCH_V1" if frozen else "CUSTOM_RESEARCH_MODEL",
            "market": "BINANCE_USDM_BTCUSDT_PERPETUAL",
            "reference": "BINANCE_USDT_M_FUTURES_STANDARD_TAKER" if frozen else "CUSTOM",
            "execution": "MARKET_TAKER",
            "fill_model": "QUOTE_SIDE_FULL_FILL_V1",
            "maker_reference_rate": str(self.maker_reference_rate),
            "taker_fee_rate": str(self.fee_rate),
            "slippage_per_fill": str(self.adverse_slippage_bps / D("10000")),
            "funding": "NOT_INCLUDED",
            "fee_rounding": "USD_CENT_UP",
        }


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class Position:
    direction: str
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal


@dataclass(frozen=True)
class RiskResult:
    accepted: bool
    quantity: Decimal
    notional: Decimal
    planned_loss: Decimal
    reason: str | None = None


def floor_step(quantity: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    floored = floor_step(value, step)
    return floored if floored == value else floored + step


def walk_book(levels: Iterable[BookLevel], quantity: Decimal, *, buy: bool) -> Decimal | None:
    """Return VWAP by consuming displayed depth; return None when depth is insufficient."""
    remaining, notional = quantity, D("0")
    for level in levels:
        if level.price <= 0 or level.quantity <= 0:
            continue
        take = min(remaining, level.quantity)
        notional += take * level.price
        remaining -= take
        if remaining <= 0:
            return notional / quantity
    return None


def simulate_fill(
    side: str,
    quantity: Decimal,
    bids: Iterable[BookLevel],
    asks: Iterable[BookLevel],
    cost: CostModel,
) -> tuple[Decimal, Decimal] | None:
    """Full simulated fill at the quoted side, with one adverse slip and fee."""
    levels = tuple(asks if side.upper() == "BUY" else bids)
    if not levels or quantity <= 0 or not levels[0].price.is_finite() or levels[0].price <= 0:
        return None
    vwap = levels[0].price
    slip = cost.adverse_slippage_bps / D("10000")
    price = vwap * (D("1") + slip if side.upper() == "BUY" else D("1") - slip)
    price = (
        ceil_step(price, cost.simulated_tick)
        if side.upper() == "BUY"
        else floor_step(price, cost.simulated_tick)
    )
    fee = quantity * price * cost.fee_rate
    return price, fee


def planned_loss_for_fill(
    quantity: Decimal, price: Decimal, stop: Decimal, cost: CostModel, fee: Decimal
) -> Decimal:
    """Conservative final 2% check including cent-up fees and adverse exit reserve."""
    slip = cost.adverse_slippage_bps / D("10000")
    per_unit = abs(price - stop) + price * (cost.fee_rate + slip) + stop * (cost.fee_rate + slip)
    entry_exact = quantity * price * cost.fee_rate
    entry_rounded = fee.quantize(D("0.01"), rounding=ROUND_UP)
    exit_exact = quantity * stop * cost.fee_rate
    exit_rounded = (quantity * stop * (D("1") + slip) * cost.fee_rate).quantize(
        D("0.01"), rounding=ROUND_UP
    )
    return (
        quantity * per_unit
        + max(D("0"), entry_rounded - entry_exact)
        + max(D("0"), exit_rounded - exit_exact)
    )


def calculate_position_size(
    *,
    equity: Decimal,
    available_cash: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    direction: str,
    bids: Iterable[BookLevel],
    asks: Iterable[BookLevel],
    cost: CostModel,
    risk_fraction: Decimal = D("0.02"),
    leverage: Decimal = D("1"),
) -> RiskResult:
    """Size to leverage, strategy risk and margin reserves; no liquidity cap."""
    if not risk_fraction.is_finite() or not D("0") < risk_fraction <= D("1"):
        return RiskResult(False, D("0"), D("0"), D("0"), "INVALID_RISK_FRACTION")
    if min(equity, available_cash, entry_price, stop_price) <= 0:
        return RiskResult(False, D("0"), D("0"), D("0"), "INVALID_INPUT")
    if not leverage.is_finite() or not D("1") <= leverage <= D("5"):
        return RiskResult(False, D("0"), D("0"), D("0"), "INVALID_LEVERAGE")
    direction = direction.upper()
    if direction not in {"LONG", "SHORT"}:
        return RiskResult(False, D("0"), D("0"), D("0"), "INVALID_DIRECTION")
    distance = entry_price - stop_price if direction == "LONG" else stop_price - entry_price
    if distance <= 0:
        return RiskResult(False, D("0"), D("0"), D("0"), "STOP_ON_WRONG_SIDE")
    fee = cost.fee_rate
    slip = cost.adverse_slippage_bps / D("10000")
    # Stop-price loss plus conservative entry and exit fee/slippage reserves per BTC.
    per_unit_loss = distance + entry_price * (fee + slip) + stop_price * (fee + slip)
    risk_cap = equity * risk_fraction / per_unit_loss
    target_cap = equity * leverage / entry_price
    cash_per_unit = entry_price * (D("1") / leverage + fee + slip) + stop_price * (fee + slip)
    cash_cap = available_cash / cash_per_unit
    qty = floor_step(min(risk_cap, target_cap, cash_cap), cost.quantity_step)
    if qty < cost.minimum_quantity or qty * entry_price < cost.minimum_notional:
        return RiskResult(False, D("0"), D("0"), D("0"), "BELOW_MINIMUM")
    notional = qty * entry_price
    planned_loss = qty * per_unit_loss
    if notional > equity * leverage or qty * cash_per_unit > available_cash:
        return RiskResult(False, D("0"), notional, planned_loss, "LEVERAGE_OR_CASH_LIMIT")
    if planned_loss > equity * risk_fraction:
        return RiskResult(False, D("0"), notional, planned_loss, "RISK_LIMIT_AFTER_ROUNDING")
    return RiskResult(True, qty, notional, planned_loss)


def stop_triggered(
    direction: str, stop_price: Decimal, bid: Decimal | None, ask: Decimal | None
) -> tuple[bool, Decimal | None, str]:
    """Alpaca spot reference: long exits at executable bid; short exits at executable ask."""
    direction = direction.upper()
    if direction == "LONG":
        if bid is None or bid <= 0:
            return False, None, "BID_UNAVAILABLE"
        return bid <= stop_price, bid, "ALPACA_SPOT_BID"
    if direction == "SHORT":
        if ask is None or ask <= 0:
            return False, None, "ASK_UNAVAILABLE"
        return ask >= stop_price, ask, "ALPACA_SPOT_ASK"
    return False, None, "INVALID_DIRECTION"

