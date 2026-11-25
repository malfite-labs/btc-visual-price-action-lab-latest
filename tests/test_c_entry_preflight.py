from decimal import Decimal as D

import pytest

from backend.domain import CostModel
from backend.execution import prepare_entry
from backend.ledger import Ledger


@pytest.mark.parametrize(("direction", "stop"), [("LONG", "99.9"), ("SHORT", "100.1")])
def test_leveraged_preflight_rechecks_post_fill_risk_and_margin(direction, stop):
    quote = {"source": "BINANCE_USDM_PERP", "bid": "100", "ask": "100"}
    book = {"bids": [{"p": "100", "s": "100"}], "asks": [{"p": "100", "s": "100"}]}
    result = prepare_entry(
        equity=D("1000"),
        available_cash=D("1000"),
        direction=direction,
        stop=D(stop),
        quote=quote,
        orderbook=book,
        cost=CostModel(),
        quote_age_ms=0,
        book_age_ms=0,
        leverage=D("5"),
    )
    assert result["accepted"]
    assert D("1000") < D(result["notional"]) <= D("5000")
    assert D(result["planned_loss"]) <= D("20")
    assert D(result["notional"]) / D("5") + D(result["fee"]) <= D("1000")


@pytest.mark.parametrize(("direction", "stop"), [("LONG", "99.9"), ("SHORT", "100.1")])
def test_preflight_fill_commits_to_isolated_c_ledger(tmp_path, direction, stop):
    quote = {"source": "BINANCE_USDM_PERP", "bid": "100", "ask": "100"}
    book = {"bids": [{"p": "100", "s": "1000"}], "asks": [{"p": "100", "s": "1000"}]}
    result = prepare_entry(
        equity=D("10000"),
        available_cash=D("10000"),
        direction=direction,
        stop=D(stop),
        quote=quote,
        orderbook=book,
        cost=CostModel(),
        quote_age_ms=0,
        book_age_ms=0,
        leverage=D("5"),
    )
    assert result["accepted"] and D(result["notional"]) > D("10000")
    ledger = Ledger(tmp_path / "isolation.sqlite")
    opened = ledger.open_position(
        agent="C",
        direction=direction,
        quantity=D(result["quantity"]),
        price=D(result["fill_price"]),
        stop=D(stop),
        fee=D(result["fee"]),
        slippage=D("0"),
        idempotency_key="entry",
        expected_version=0,
        research_c_5x=True,
    )
    assert opened["accepted"]
    assert ledger.account("A")["state"] == ledger.account("B")["state"] == "FLAT"


def test_preflight_rejects_long_stop_crossed_by_actual_fill():
    quote = {"source": "BINANCE_USDM_PERP", "bid": "100", "ask": "100"}
    book = {"bids": [{"p": "99", "s": "100"}], "asks": [{"p": "99", "s": "100"}]}
    result = prepare_entry(
        equity=D("1000"),
        available_cash=D("1000"),
        direction="LONG",
        stop=D("99.9"),
        quote=quote,
        orderbook=book,
        cost=CostModel(max_price_deviation_bps=D("200")),
        quote_age_ms=0,
        book_age_ms=0,
        leverage=D("5"),
    )
    assert not result["accepted"]

