from decimal import Decimal as D

import pytest

from backend.margin import assess_margin


@pytest.mark.parametrize(
    ("direction", "mark", "pnl"), [("LONG", "110", "100"), ("SHORT", "110", "-100")]
)
def test_margin_accounting_does_not_multiply_equity(direction, mark, pnl):
    r = assess_margin(
        cash=D("1000"),
        direction=direction,
        quantity=D("10"),
        entry_price=D("100"),
        exit_quote=D(mark),
        leverage=D("5"),
        maintenance_rate=D("0.005"),
    )
    assert r.unrealized_pnl == D(pnl)
    assert r.equity == D("1000") + D(pnl)
    assert r.used_margin == D("200")
    assert r.available_margin == max(D("0"), r.equity - D("200"))


@pytest.mark.parametrize(("direction", "mark"), [("LONG", "1"), ("SHORT", "200")])
def test_liquidation_uses_executable_exit_side_and_handles_gap(direction, mark):
    r = assess_margin(
        cash=D("1000"),
        direction=direction,
        quantity=D("40"),
        entry_price=D("100"),
        exit_quote=D(mark),
        leverage=D("5"),
        maintenance_rate=D("0.005"),
    )
    assert r.liquidation_required


def test_invalid_price_fails_closed():
    with pytest.raises(ValueError):
        assess_margin(
            cash=D("1000"),
            direction="LONG",
            quantity=D("10"),
            entry_price=D("100"),
            exit_quote=D("0"),
            leverage=D("5"),
            maintenance_rate=D("0.005"),
        )
