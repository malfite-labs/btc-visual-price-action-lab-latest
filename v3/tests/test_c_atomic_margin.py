import json
from decimal import Decimal as D

import pytest

from backend.ledger import Ledger


@pytest.mark.parametrize(
    ("direction", "stop", "exit_price"),
    [
        ("LONG", "99.99", "101"),
        ("SHORT", "100.01", "99"),
    ],
)
def test_c_margin_transaction_round_trip_and_restart(tmp_path, direction, stop, exit_price):
    db = tmp_path / "isolated.sqlite3"
    ledger = Ledger(db)
    before_a = ledger.account("A")
    result = ledger.open_position(
        agent="C",
        direction=direction,
        quantity=D("400"),
        price=D("100"),
        stop=D(stop),
        fee=D("20"),
        slippage=D("0"),
        idempotency_key="open-c",
        expected_version=0,
        research_c_5x=True,
    )
    assert result["accepted"]
    restored = Ledger(db)
    account = restored.account("C")
    pos = json.loads(account["position_json"])
    assert pos["initial_margin"] == "8000" and pos["leverage"] == "5"
    assert D(account["cash"]) == D("9980")
    quote = {
        "source": "BINANCE_USDM_PERP",
        "age_ms": 0,
        "bid": "100",
        "ask": "100",
        "timestamp_valid": True,
    }
    metrics = restored.account_metrics("C", quote)
    assert D(metrics["used_margin"]) == D("8000")
    assert D(metrics["available_cash"]) > D("1900")
    assert restored.account("A") == before_a
    fee = (D("400") * D(exit_price) * D("0.0005")).quantize(D("0.01"), rounding="ROUND_UP")
    close = restored.close_position(
        agent="C",
        price=D(exit_price),
        fee=fee,
        slippage=D("0"),
        idempotency_key="close-c",
        expected_version=account["version"],
    )
    assert close["accepted"]
    assert D(restored.account("C")["cash"]) == D("9980") + D("400") - fee
    duplicate = restored.close_position(
        agent="C",
        price=D(exit_price),
        fee=fee,
        slippage=D("0"),
        idempotency_key="close-c",
        expected_version=account["version"],
    )
    assert duplicate["duplicate"] and len(restored.fills("C")) == 2


def test_leverage_not_available_to_a_or_without_explicit_research_gate(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite3")
    args = dict(
        direction="LONG",
        quantity=D("200"),
        price=D("100"),
        stop=D("99.9"),
        fee=D("10"),
        slippage=D("0"),
        idempotency_key="x",
        expected_version=0,
    )
    assert not ledger.open_position(agent="A", **args)["accepted"]
    with pytest.raises(ValueError):
        ledger.open_position(agent="A", research_c_5x=True, **args)
    assert not ledger.open_position(agent="C", **args)["accepted"]
    with pytest.raises(ValueError, match="C_RISK_FRACTION"):
        ledger.open_position(agent="C", research_c_5x=True, risk_fraction=D("0.03"), **args)


def test_c_open_rolls_back_on_ledger_event_failure(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "isolated.sqlite3")

    def fail_event(*args, **kwargs):
        raise RuntimeError("injected ledger event failure")

    monkeypatch.setattr(ledger, "_ledger_insert", fail_event)
    with pytest.raises(RuntimeError, match="injected"):
        ledger.open_position(
            agent="C",
            direction="LONG",
            quantity=D("400"),
            price=D("100"),
            stop=D("99.99"),
            fee=D("20"),
            slippage=D("0"),
            idempotency_key="rollback",
            expected_version=0,
            research_c_5x=True,
        )
    assert ledger.account("C")["state"] == "FLAT"
    assert D(ledger.account("C")["cash"]) == D("10000")
    assert ledger.fills("C") == []

