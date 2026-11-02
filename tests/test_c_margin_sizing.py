from decimal import Decimal as D

import pytest

from backend.domain import BookLevel, CostModel, calculate_position_size


@pytest.mark.parametrize(("direction", "stop"), [("LONG", "99.90"), ("SHORT", "100.10")])
def test_five_times_sizing_uses_margin_not_full_notional(direction, stop):
    result = calculate_position_size(
        equity=D("1000"),
        available_cash=D("1000"),
        entry_price=D("100"),
        stop_price=D(stop),
        direction=direction,
        bids=[BookLevel(D("100"), D("100"))],
        asks=[BookLevel(D("100"), D("100"))],
        cost=CostModel(),
        leverage=D("5"),
    )
    assert result.accepted
    assert D("1000") < result.notional <= D("5000")
    assert result.planned_loss <= D("20")


def test_one_times_default_remains_fully_funded():
    result = calculate_position_size(
        equity=D("1000"),
        available_cash=D("1000"),
        entry_price=D("100"),
        stop_price=D("99.90"),
        direction="LONG",
        bids=[BookLevel(D("100"), D("100"))],
        asks=[BookLevel(D("100"), D("100"))],
        cost=CostModel(),
    )
    assert result.accepted and result.notional <= D("1000")
