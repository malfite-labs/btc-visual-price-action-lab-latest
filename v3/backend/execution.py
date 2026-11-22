from __future__ import annotations

from datetime import datetime
from decimal import ROUND_UP, Decimal

from backend.domain import (
    BookLevel,
    CostModel,
    calculate_position_size,
    ceil_step,
    floor_step,
    planned_loss_for_fill,
    simulate_fill,
    stop_triggered,
)
from backend.ledger import Ledger


def prepare_entry(
    *,
    equity: Decimal,
    available_cash: Decimal,
    direction: str,
    stop: Decimal,
    quote: dict,
    orderbook: dict,
    cost: CostModel,
    quote_age_ms: int,
    book_age_ms: int,
    leverage: Decimal = Decimal("1"),
    risk_fraction: Decimal = Decimal("0.02"),
) -> dict:
    """Preflight only: validates a hypothetical fill; caller must still pass all gates before ledger mutation."""
    if (
        not 0 <= quote_age_ms <= cost.max_quote_age_ms
        or quote.get("timestamp_valid", True) is not True
    ):
        return {"accepted": False, "reason": "QUOTE_STALE"}
    if quote.get("source") != "BINANCE_USDM_PERP":
        return {"accepted": False, "reason": "SOURCE_MISMATCH"}
    bid, ask = Decimal(str(quote['bid'])), Decimal(str(quote['ask']))
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
        return {"accepted": False, "reason": "INVALID_QUOTE"}
    bids = [BookLevel(bid, Decimal('0'))]
    asks = [BookLevel(ask, Decimal('0'))]
    direction = direction.upper()
    entry_price = Decimal(str(quote["ask" if direction == "LONG" else "bid"]))
    r = calculate_position_size(
        equity=equity,
        available_cash=available_cash,
        entry_price=entry_price,
        stop_price=stop,
        direction=direction,
        bids=bids,
        asks=asks,
        cost=cost,
        leverage=leverage,
        risk_fraction=risk_fraction,
    )
    if not r.accepted:
        return {"accepted": False, "reason": r.reason}
    # Revalidate the slipped price and cent-rounded fees against cash and risk.
    low, high = 1, int(r.quantity / cost.quantity_step)
    best = None
    slip = cost.adverse_slippage_bps / Decimal("10000")
    while low <= high:
        middle = (low + high) // 2
        quantity = cost.quantity_step * middle
        fill = simulate_fill("BUY" if direction == "LONG" else "SELL", quantity, bids, asks, cost)
        if fill is None:
            high = middle - 1
            continue
        price, fee = fill
        if (direction == "LONG" and stop >= price) or (direction == "SHORT" and stop <= price):
            return {"accepted": False, "reason": "STOP_WRONG_SIDE_OF_ACTUAL_FILL"}
        planned = planned_loss_for_fill(quantity, price, stop, cost, fee)
        cash_needed = (
            quantity * price / leverage
            + fee.quantize(Decimal("0.01"), rounding=ROUND_UP)
            + quantity * stop * (cost.fee_rate + slip)
        )
        if (
            quantity * price <= equity * leverage
            and cash_needed <= available_cash
            and planned <= equity * risk_fraction
        ):
            best = (quantity, price, fee, planned)
            low = middle + 1
        else:
            high = middle - 1
    if best is None:
        return {"accepted": False, "reason": "POST_FILL_RISK_LIMIT"}
    quantity, price, fee, planned = best
    if quantity < cost.minimum_quantity or quantity * price < cost.minimum_notional:
        return {"accepted": False, "reason": "BELOW_MINIMUM"}
    return {
        "accepted": True,
        "quantity": str(quantity),
        "notional": str(quantity * price),
        "planned_loss": str(planned),
        "fill_price": str(price),
        "fee": str(fee),
        "slippage_in_price": True,
        "quote_id": quote.get("event_id"),
    }


def handle_confirmed_stop(
    *,
    ledger: Ledger,
    agent: str,
    trigger_event_id: str,
    quote: dict | None,
    cost: CostModel,
    now: datetime | None = None,
) -> dict:
    """Persist a stop only after a valid quote proves the trigger."""
    account = ledger.account(agent)
    if not account.get("position_json"):
        return {"accepted": False, "reason": "NO_POSITION"}
    import json
    from decimal import Decimal

    position = json.loads(account["position_json"])
    side = "bid" if position["direction"] == "LONG" else "ask"
    trigger_price = Decimal(position["stop_price"])
    if quote is None:
        return {"accepted": False, "reason": "TRIGGER_QUOTE_UNAVAILABLE"}
    if quote.get("source") != "BINANCE_USDM_PERP":
        return {"accepted": False, "reason": "SOURCE_MISMATCH"}
    age = int(quote.get("age_ms", 10**9))
    if not 0 <= age <= cost.max_quote_age_ms or quote.get("timestamp_valid", True) is not True:
        return {"accepted": False, "reason": "TRIGGER_QUOTE_STALE"}
    px = Decimal(str(quote.get(side, "0")))
    triggered, trigger_px, _basis = stop_triggered(
        position["direction"],
        trigger_price,
        Decimal(str(quote["bid"])) if quote.get("bid") else None,
        Decimal(str(quote["ask"])) if quote.get("ask") else None,
    )
    if not triggered:
        return {"accepted": False, "reason": "STOP_NOT_TRIGGERED"}
    pending = ledger.request_stop_exit(
        agent=agent,
        trigger_price=trigger_px,
        event_key=trigger_event_id,
        quote_event_id=quote.get("event_id") or trigger_event_id,
        expected_version=account["version"],
        trigger_evidence={"source":quote.get("source"),"event_at":quote.get("event_at"),"age_ms":age,"timestamp_valid":quote.get("timestamp_valid",True),"bid":quote.get("bid"),"ask":quote.get("ask")},
    )
    if not pending.get("ready_to_close"):
        return pending
    qty = Decimal(position["quantity"])
    # Trigger quote is executable side; adverse slippage is reflected in the simulated exit price.
    slip = cost.adverse_slippage_bps / Decimal(10000)
    exit_price = px * (Decimal(1) - slip if side == "bid" else Decimal(1) + slip)
    exit_price = (
        floor_step(exit_price, cost.simulated_tick)
        if side == "bid"
        else ceil_step(exit_price, cost.simulated_tick)
    )
    fee = qty * exit_price * cost.fee_rate
    acct = ledger.account(agent)
    return ledger.close_position(
        agent=agent,
        price=exit_price,
        fee=fee,
        slippage=abs(exit_price - px) * qty,
        idempotency_key="stop:" + trigger_event_id,
        expected_version=acct["version"],
        reason="STOP_TRIGGER",
        cost_model=cost,
        execution_quote=quote,
    )


def recover_pending_stop(
    *,
    ledger: Ledger,
    agent: str,
    quote: dict | None,
    cost: CostModel,
    event_id: str,
    reason: str = "STOP_RECOVERY",
) -> dict:
    account = ledger.account(agent)
    if account["state"] != "STOP_EXIT_PENDING" or not account.get("position_json"):
        return {"accepted": False, "reason": "NO_PENDING_STOP"}
    import json
    pending = json.loads(account["pending_stop_json"]) if account.get("pending_stop_json") else {}
    if not pending.get("trigger_price") or not pending.get("quote_event_id"):
        return {"accepted": False, "reason": "STOP_TRIGGER_EVIDENCE_MISSING"}
    if not quote or quote.get("source") != "BINANCE_USDM_PERP":
        return {"accepted": False, "reason": "QUOTE_UNAVAILABLE_REMAINS_PENDING"}
    if (
        not 0 <= int(quote.get("age_ms", 10**9)) <= cost.max_quote_age_ms
        or quote.get("timestamp_valid", True) is not True
    ):
        return {"accepted": False, "reason": "QUOTE_STALE_REMAINS_PENDING"}
    from decimal import Decimal

    position = json.loads(account["position_json"])
    if reason == "STOP_RECOVERY" and account.get("pending_stop_json"):
        pending = json.loads(account["pending_stop_json"])
        if str(pending.get("event_key", "")).startswith("liquidation:"):
            reason = "STOP_LIQUIDATION_RECOVERY"
    side = "bid" if position["direction"] == "LONG" else "ask"
    px = Decimal(str(quote.get(side, "0")))
    if px <= 0:
        return {"accepted": False, "reason": "EXECUTABLE_SIDE_MISSING"}
    qty = Decimal(position["quantity"])
    slip = cost.adverse_slippage_bps / Decimal(10000)
    exit_price = px * (Decimal(1) - slip if side == "bid" else Decimal(1) + slip)
    exit_price = (
        floor_step(exit_price, cost.simulated_tick)
        if side == "bid"
        else ceil_step(exit_price, cost.simulated_tick)
    )
    fee = qty * exit_price * cost.fee_rate
    return ledger.close_position(
        agent=agent,
        price=exit_price,
        fee=fee,
        slippage=abs(exit_price - px) * qty,
        idempotency_key=f"stop-recovery:{agent}:{event_id}",
        expected_version=account["version"],
        reason=reason,
        cost_model=cost,
        execution_quote=quote,
    )


def handle_research_liquidation(
    *,
    ledger: Ledger,
    agent: str,
    quote: dict | None,
    cost: CostModel,
    event_id: str,
    maintenance_rate: Decimal,
) -> dict:
    """Isolated research prototype; caller owns supervision and deployment gating."""
    import json

    from backend.margin import assess_margin

    if agent != "C":
        return {"accepted": False, "reason": "RESEARCH_C_ONLY"}
    account = ledger.account(agent)
    if account["state"] not in {"OPEN_LONG", "OPEN_SHORT"} or not account.get("position_json"):
        return {"accepted": False, "reason": "NO_OPEN_POSITION"}
    if (
        not quote
        or quote.get("source") != "BINANCE_USDM_PERP"
        or quote.get("timestamp_valid", True) is not True
        or not 0 <= int(quote.get("age_ms", 10**9)) <= cost.max_quote_age_ms
    ):
        return {"accepted": False, "reason": "QUOTE_UNAVAILABLE_OR_STALE"}
    position = json.loads(account["position_json"])
    if position.get("research_only") is not True or position.get("leverage") != "5":
        return {"accepted": False, "reason": "NOT_RESEARCH_LEVERAGED_POSITION"}
    side = "bid" if position["direction"] == "LONG" else "ask"
    try:
        price = Decimal(str(quote.get(side, "0")))
        qty = Decimal(position["quantity"])
        assessment = assess_margin(
            cash=Decimal(account["cash"]),
            direction=position["direction"],
            quantity=qty,
            entry_price=Decimal(position["entry_price"]),
            exit_quote=price,
            leverage=Decimal("5"),
            maintenance_rate=maintenance_rate,
        )
    except (ValueError, ArithmeticError):
        return {"accepted": False, "reason": "INVALID_RISK_INPUT"}
    exit_reserve = qty * price * (cost.fee_rate + cost.adverse_slippage_bps / Decimal("10000"))
    if assessment.equity > assessment.maintenance_margin + exit_reserve:
        return {"accepted": False, "reason": "LIQUIDATION_NOT_TRIGGERED"}
    pending = ledger.request_stop_exit(
        agent=agent,
        trigger_price=price,
        event_key="liquidation:" + event_id,
        quote_event_id=quote.get("event_id") or event_id,
        expected_version=account["version"],
        trigger_evidence={"source":quote.get("source"),"event_at":quote.get("event_at"),"age_ms":quote.get("age_ms"),"timestamp_valid":quote.get("timestamp_valid",True),"bid":quote.get("bid"),"ask":quote.get("ask")},
    )
    if not pending.get("accepted") or pending.get("duplicate"):
        return pending
    return recover_pending_stop(
        ledger=ledger,
        agent=agent,
        quote=quote,
        cost=cost,
        event_id=event_id,
        reason="STOP_LIQUIDATION",
    )

