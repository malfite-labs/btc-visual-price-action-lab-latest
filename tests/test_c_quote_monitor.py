from decimal import Decimal as D
from types import SimpleNamespace

from backend.domain import CostModel
from backend.ledger import Ledger
from backend.scheduler import PaperCycleScheduler


def test_shared_quote_monitor_prioritizes_liquidation_and_is_idempotent(tmp_path):
    ledger = Ledger(tmp_path / "test.sqlite")
    opened = ledger.open_position(
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
    )
    assert opened["accepted"]
    scheduler = PaperCycleScheduler(
        tmp_path / "test.sqlite", SimpleNamespace(ledger=ledger), c_maintenance_rate=D("0.005")
    )
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
    result = scheduler.monitor_stops(event_ids={"C": "q1"}, quote=quote, cost=CostModel())
    assert result["C"]["accepted"]
    assert ledger.account("C")["state"] == "FLAT"
    assert len(ledger.fills("C")) == 2
    assert "LIQUIDATION" in ledger.fills("C")[-1]["rationale"]
    scheduler.monitor_stops(event_ids={"C": "q1"}, quote=quote, cost=CostModel())
    assert len(ledger.fills("C")) == 2


def test_stale_quote_never_triggers_exit_and_fresh_quote_triggers_stop(tmp_path):
    ledger = Ledger(tmp_path / "test.sqlite")
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
    scheduler = PaperCycleScheduler(
        tmp_path / "test.sqlite", SimpleNamespace(ledger=ledger), c_maintenance_rate=D("0.005")
    )
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "age_ms": 60001,
        "timestamp_valid": True,
        "bid": "99.98",
        "ask": "100",
        "bid_qty": "1000",
        "ask_qty": "1000",
        "event_id": "q1",
    }
    scheduler.monitor_stops(event_ids={"C": "q1"}, quote=quote, cost=CostModel())
    assert ledger.account("C")["state"] == "OPEN_LONG"
    quote["age_ms"] = 0
    scheduler.monitor_stops(event_ids={"C": "q2"}, quote=quote, cost=CostModel())
    assert ledger.account("C")["state"] == "FLAT"
    assert len(ledger.fills("C")) == 2


def test_separate_c_pending_stops_have_distinct_recovery_keys(tmp_path):
    ledger = Ledger(tmp_path / "test.sqlite")
    scheduler = PaperCycleScheduler(
        tmp_path / "test.sqlite", SimpleNamespace(ledger=ledger), c_maintenance_rate=D("0.005")
    )
    quote = {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "age_ms": 0,
        "timestamp_valid": True,
        "bid": "100",
        "ask": "100.1",
        "bid_qty": "1000",
        "ask_qty": "1000",
    }
    for i in range(2):
        account = ledger.account("C")
        assert ledger.open_position(
            agent="C",
            direction="LONG",
            quantity=D("1"),
            price=D("100"),
            stop=D("99"),
            fee=D("0.05"),
            slippage=D("0"),
            idempotency_key=f"entry-{i}",
            expected_version=account["version"],
            research_c_5x=True,
        )["accepted"]
        account = ledger.account("C")
        assert ledger.request_stop_exit(
            agent="C",
            trigger_price=D('98'),
            event_key=f"stop-{i}",
            quote_event_id="confirmed-quote",
            expected_version=account["version"],
        )["accepted"]
        assert scheduler.recover_stops(quote=quote, cost=CostModel())["C"]["accepted"]
        assert ledger.account("C")["state"] == "FLAT"
    assert len(ledger.fills("C")) == 4
