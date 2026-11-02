from decimal import Decimal as D

from backend.domain import (
    BookLevel,
    CostModel,
    calculate_position_size,
    floor_step,
    simulate_fill,
    stop_triggered,
    walk_book,
)


def test_round_down_and_risk_limit_with_costs():
    cost = CostModel()
    bids = [BookLevel(D(99990), D("0.1"))]
    asks = [BookLevel(D(100010), D("0.1"))]
    r = calculate_position_size(
        equity=D(10000),
        available_cash=D(10000),
        entry_price=D(100010),
        stop_price=D(98000),
        direction="LONG",
        bids=bids,
        asks=asks,
        cost=cost,
    )
    assert r.accepted and r.quantity == floor_step(r.quantity, cost.quantity_step)
    assert r.planned_loss <= D(200) and r.notional <= D(10000)


def test_invalid_stop_and_empty_book_rejected():
    r = calculate_position_size(
        equity=D(10000),
        available_cash=D(10000),
        entry_price=D(100),
        stop_price=D(101),
        direction="LONG",
        bids=[],
        asks=[],
        cost=CostModel(),
    )
    assert not r.accepted and r.reason == "STOP_ON_WRONG_SIDE"


def test_book_walk_requires_full_depth_and_fill_has_adverse_slippage():
    bids = [BookLevel(D(99), D("0.1")), BookLevel(D(98), D("0.1"))]
    assert walk_book(bids, D("0.3"), buy=False) is None
    fill = simulate_fill("BUY", D("0.05"), bids, [BookLevel(D(100), D("0.1"))], CostModel())
    assert fill is not None and fill[0] > D(100) and fill[1] > 0


def test_alpaca_spot_stop_uses_executable_side():
    assert stop_triggered("LONG", D(98), D("97.9"), D("98.1")) == (
        True,
        D("97.9"),
        "ALPACA_SPOT_BID",
    )
    assert stop_triggered("SHORT", D(102), D("101.9"), D("102.1")) == (
        True,
        D("102.1"),
        "ALPACA_SPOT_ASK",
    )
    assert stop_triggered("LONG", D(98), None, D(97))[1] is None
