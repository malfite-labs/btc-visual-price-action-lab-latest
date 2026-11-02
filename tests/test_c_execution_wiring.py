from decimal import Decimal as D

import pytest

from backend.domain import CostModel
from backend.ledger import Ledger
from backend.orchestration import PaperCycleRunner, TestOnlyPaperCycleRunner


@pytest.mark.parametrize(("action", "stop"), [("OPEN_LONG", "99.9"), ("OPEN_SHORT", "100.1")])
def test_c_decision_preflight_and_ledger_use_guarded_leverage(tmp_path, action, stop):
    ledger = Ledger(tmp_path / "isolated.sqlite")
    runner = TestOnlyPaperCycleRunner(
        ledger, None, CostModel(), enable_c_5x=True, c_maintenance_rate=D("0.005")
    )
    decision = {
        "agent": "C",
        "action": action,
        "md_version": "v1.0.0",
        "rationale": "test only",
        "decision_id": "test-1",
        "stop_loss": stop,
    }
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "currency": "USD_RESEARCH",
        "bid": "100",
        "ask": "100",
        "age_ms": 0,
        "timestamp_valid": True,
        "event_id": "q1",
    }
    book = {"bids": [{"p": "100", "s": "1000"}], "asks": [{"p": "100", "s": "1000"}], "age_ms": 0}
    result = runner._execute(
        decision=decision, account=ledger.account("C"), quote=quote, orderbook=book
    )
    assert result["accepted"]
    assert D(ledger.fills("C")[0]["quantity"]) * D(ledger.fills("C")[0]["price"]) > D("10000")
    assert ledger.account("A")["state"] == ledger.account("B")["state"] == "FLAT"


@pytest.mark.parametrize(("bid", "expected"), [("70", "LIQUIDATION"), ("99.98", "STOP")])
def test_protective_exit_precedes_model_close(tmp_path, bid, expected):
    ledger = Ledger(tmp_path / "isolated.sqlite")
    assert ledger.open_position(
        agent="C",
        direction="LONG",
        quantity=D("400"),
        price=D("100"),
        stop=D("99.99"),
        fee=D("20"),
        slippage=D("0"),
        idempotency_key="entry",
        expected_version=0,
        research_c_5x=True,
    )["accepted"]
    runner = TestOnlyPaperCycleRunner(
        ledger, None, CostModel(), enable_c_5x=True, c_maintenance_rate=D("0.005")
    )
    decision = {
        "agent": "C",
        "action": "CLOSE_LONG",
        "md_version": "v1.0.0",
        "rationale": "model close",
        "decision_id": "model",
    }
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "currency": "USD_RESEARCH",
        "bid": bid,
        "ask": "100",
        "bid_qty": "1000",
        "ask_qty": "1000",
        "age_ms": 0,
        "timestamp_valid": True,
        "event_id": "q1",
    }
    book = {"bids": [{"p": bid, "s": "1000"}], "asks": [{"p": "100", "s": "1000"}], "age_ms": 0}
    result = runner._execute(
        decision=decision, account=ledger.account("C"), quote=quote, orderbook=book
    )
    assert result["accepted"] and ledger.account("C")["state"] == "FLAT"
    assert expected in ledger.fills("C")[-1]["rationale"]


def test_production_runner_can_be_explicitly_configured_but_is_disabled_by_default(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite")
    default = PaperCycleRunner(ledger, None, CostModel())
    assert not default.enable_c_5x
    with pytest.raises(ValueError, match="C_MAINTENANCE_RATE"):
        PaperCycleRunner(ledger, None, CostModel(), enable_c_5x=True)
    configured = PaperCycleRunner(
        ledger, None, CostModel(), enable_c_5x=True, c_maintenance_rate=D("0.005")
    )
    assert configured.enable_c_5x and configured.c_maintenance_rate == D("0.005")
