import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D

import pytest

from backend.domain import CostModel
from backend.execution import handle_research_liquidation, recover_pending_stop
from backend.ledger import Ledger


@pytest.mark.parametrize(
    ("direction", "stop", "bid", "ask"),
    [("LONG", "99.99", "70", "70.1"), ("SHORT", "100.01", "129.9", "130")],
)
def test_liquidation_exit_records_one_actual_fill(tmp_path, direction, stop, bid, ask):
    ledger = Ledger(tmp_path / "isolation.sqlite")
    assert ledger.open_position(
        agent="C",
        direction=direction,
        quantity=D("400"),
        price=D("100"),
        stop=D(stop),
        fee=D("20"),
        slippage=D("0"),
        idempotency_key="entry",
        expected_version=0,
        research_c_5x=True,
    )["accepted"]
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "age_ms": 0,
        "timestamp_valid": True,
        "bid": bid,
        "ask": ask,
        "bid_qty": "1000",
        "ask_qty": "1000",
        "event_id": "q1",
    }
    result = handle_research_liquidation(
        ledger=ledger,
        agent="C",
        quote=quote,
        cost=CostModel(),
        event_id="q1",
        maintenance_rate=D("0.005"),
    )
    assert result["accepted"]
    assert ledger.account("C")["state"] == "FLAT"
    assert len(ledger.fills("C")) == 2
    duplicate = handle_research_liquidation(
        ledger=ledger,
        agent="C",
        quote=quote,
        cost=CostModel(),
        event_id="q1",
        maintenance_rate=D("0.005"),
    )
    assert not duplicate["accepted"] and len(ledger.fills("C")) == 2


def test_liquidation_stale_quote_cannot_trigger_or_fill(tmp_path):
    ledger = Ledger(tmp_path / "isolation.sqlite")
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
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "age_ms": 60001,
        "timestamp_valid": True,
        "bid": "70",
        "ask": "70.1",
        "bid_qty": "1000",
        "ask_qty": "1000",
    }
    result = handle_research_liquidation(
        ledger=ledger,
        agent="C",
        quote=quote,
        cost=CostModel(),
        event_id="stale",
        maintenance_rate=D("0.005"),
    )
    assert not result["accepted"] and ledger.account("C")["state"] == "OPEN_LONG"


def test_liquidation_pending_survives_restart_and_keeps_reason(tmp_path):
    db = tmp_path / "isolation.sqlite"
    ledger = Ledger(db)
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
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "age_ms": 0,
        "timestamp_valid": True,
        "bid": "70",
        "ask": "70.1",
        "bid_qty": "1",
        "ask_qty": "1000",
        "event_id": "q1",
    }
    result = handle_research_liquidation(
        ledger=ledger,
        agent="C",
        quote=quote,
        cost=CostModel(),
        event_id="q1",
        maintenance_rate=D("0.005"),
    )
    assert not result["accepted"] and ledger.account("C")["state"] == "STOP_EXIT_PENDING"
    ledger = Ledger(db)
    quote["bid_qty"] = "1000"
    result = recover_pending_stop(
        ledger=ledger, agent="C", quote=quote, cost=CostModel(), event_id="fresh-q2"
    )
    assert result["accepted"]
    with ledger._session() as conn:
        row = conn.execute(
            "SELECT payload FROM audit_events WHERE kind='POSITION_CLOSED'"
        ).fetchone()
    assert json.loads(row["payload"])["reason"] == "STOP_LIQUIDATION_RECOVERY"


def test_liquidation_and_model_close_cannot_both_create_fills(tmp_path):
    ledger = Ledger(tmp_path / "isolation.sqlite")
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
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "age_ms": 0,
        "timestamp_valid": True,
        "bid": "70",
        "ask": "70.1",
        "bid_qty": "1000",
        "ask_qty": "1000",
        "event_id": "q1",
    }

    def model_close():
        return ledger.close_position(
            agent="C",
            price=D("70"),
            fee=D("14"),
            slippage=D("0"),
            idempotency_key="model",
            expected_version=1,
        )

    def liquidation():
        return handle_research_liquidation(
            ledger=ledger,
            agent="C",
            quote=quote,
            cost=CostModel(),
            event_id="q1",
            maintenance_rate=D("0.005"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(model_close), executor.submit(liquidation)]
        for future in futures:
            future.result()
    assert ledger.account("C")["state"] == "FLAT"
    assert len(ledger.fills("C")) == 2
