import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from backend.scheduler import PaperCycleScheduler


class Runner:
    def __init__(self):
        self.calls = []
        self.ledger = object()

    async def run_cycle(self, **inputs):
        self.calls.append(inputs)
        return {agent: {"status": "TEST_DONE"} for agent in "ABC"}


def inputs(close):
    return {
        "candle_close": close,
        "candle_is_closed": True,
        "cycle_deadline": close + timedelta(minutes=15),
        "now": close + timedelta(seconds=1),
        "image": b"same-composite-image",
        "adapter": object(),
    }


def test_scheduler_is_disabled_by_default_and_does_not_reserve_cycles(tmp_path):
    runner = Runner()
    scheduler = PaperCycleScheduler(tmp_path / "scheduler.sqlite", runner)
    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    result = asyncio.run(scheduler.run_closed_cycle(**inputs(close)))
    assert all(row["status"] == "SCHEDULER_DISABLED" for row in result.values())
    assert runner.calls == [] and scheduler.status(close) is None


def test_scheduler_runs_each_closed_quarter_hour_once_and_persists_dedup(tmp_path):
    runner = Runner()
    path = tmp_path / "scheduler.sqlite"
    scheduler = PaperCycleScheduler(path, runner, enabled=True)
    close1 = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    close2 = close1 + timedelta(minutes=15)
    first = asyncio.run(scheduler.run_closed_cycle(**inputs(close1)))
    duplicate = asyncio.run(scheduler.run_closed_cycle(**inputs(close1)))
    second = asyncio.run(scheduler.run_closed_cycle(**inputs(close2)))
    assert all(row["status"] == "TEST_DONE" for row in first.values())
    assert all(row["status"] == "DUPLICATE_CYCLE" for row in duplicate.values())
    assert all(row["status"] == "TEST_DONE" for row in second.values())
    assert len(runner.calls) == 2
    assert runner.calls[0]["image"] == runner.calls[1]["image"]
    assert scheduler.status(close1) == "DONE" and scheduler.status(close2) == "DONE"
    restored = PaperCycleScheduler(path, runner, enabled=True)
    recovered_duplicate = asyncio.run(restored.run_closed_cycle(**inputs(close2)))
    assert all(row["status"] == "DUPLICATE_CYCLE" for row in recovered_duplicate.values())
    assert len(runner.calls) == 2


def test_scheduler_concurrent_duplicate_events_claim_one_cycle(tmp_path):
    runner = Runner()
    scheduler = PaperCycleScheduler(tmp_path / "concurrent.sqlite", runner, enabled=True)
    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)

    async def run_both():
        return await asyncio.gather(
            scheduler.run_closed_cycle(**inputs(close)),
            scheduler.run_closed_cycle(**inputs(close)),
        )

    first, second = asyncio.run(run_both())
    assert len(runner.calls) == 1
    statuses = {first["A"]["status"], second["A"]["status"]}
    assert statuses == {"TEST_DONE", "DUPLICATE_CYCLE"}
    assert runner.calls[0]["image"] == b"same-composite-image"


def test_scheduler_rejects_unclosed_misaligned_and_expired_periods(tmp_path):
    scheduler = PaperCycleScheduler(tmp_path / "scheduler.sqlite", Runner(), enabled=True)
    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    unclosed = inputs(close)
    unclosed["now"] = close - timedelta(seconds=1)
    assert asyncio.run(scheduler.run_closed_cycle(**unclosed))["A"]["status"] == "CANDLE_NOT_CLOSED"
    misaligned = inputs(close + timedelta(minutes=1))
    assert (
        asyncio.run(scheduler.run_closed_cycle(**misaligned))["A"]["status"] == "INVALID_15M_CLOSE"
    )
    expired = inputs(close)
    expired["now"] = close + timedelta(minutes=16)
    assert asyncio.run(scheduler.run_closed_cycle(**expired))["A"]["status"] == "CYCLE_EXPIRED"
    unconfirmed = inputs(close)
    unconfirmed["candle_is_closed"] = False
    assert (
        asyncio.run(scheduler.run_closed_cycle(**unconfirmed))["A"]["status"]
        == "CANDLE_NOT_CONFIRMED_CLOSED"
    )


def test_scheduler_resumes_expired_in_progress_cycle_after_restart(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    first_runner = Runner()
    PaperCycleScheduler(path, first_runner, enabled=True)
    cycle_input = inputs(close)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO scheduler_cycles VALUES(?,?,?,?,?)",
            (close.isoformat(), "RUNNING", 1, None, (close - timedelta(seconds=180)).isoformat()),
        )
    resumed_runner = Runner()
    resumed = PaperCycleScheduler(path, resumed_runner, enabled=True)
    result = asyncio.run(resumed.run_closed_cycle(**cycle_input))
    assert all(item["status"] == "TEST_DONE" for item in result.values())
    assert len(resumed_runner.calls) == 1
    assert resumed.status(close) == "DONE"


def test_scheduler_drives_three_isolated_decisions_into_isolated_ledger(tmp_path):
    from backend.domain import CostModel
    from backend.ledger import Ledger
    from backend.orchestration import TestOnlyPaperCycleRunner
    from backend.vision import AgentOrchestrator, FakeVisionAdapter

    path = tmp_path / "isolated-ledger.sqlite"
    ledger = Ledger(path)
    decisions = AgentOrchestrator(str(path))
    runner = TestOnlyPaperCycleRunner(ledger, decisions, CostModel())
    scheduler = PaperCycleScheduler(path, runner, enabled=True)
    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    common = {
        "adapter": FakeVisionAdapter(),
        "image": b"one-identical-chart-image",
        "candle_close": close,
        "candle_is_closed": True,
        "cycle_deadline": close + timedelta(minutes=15),
        "strategies": {agent: (f"{agent}-v1", f"{agent}-only-MD") for agent in "ABC"},
        "quote": {
            "source": "BINANCE_USDM_PERP",
            "currency": "USD_RESEARCH",
            "bid": "99999",
            "ask": "100001",
            "age_ms": 20,
        },
        "orderbook": {
            "source": "BINANCE_USDM_PERP",
            "age_ms": 20,
            "bids": [{"p": "99999", "s": "1"}],
            "asks": [{"p": "100001", "s": "1"}],
        },
        "feed_healthy": True,
        "now": close + timedelta(seconds=1),
    }
    result = asyncio.run(scheduler.run_closed_cycle(**common))
    assert all(item["status"] == "VALID" for item in result.values())
    assert {item["snapshot_sha256"] for item in result.values()} == {
        __import__("hashlib").sha256(common["image"]).hexdigest()
    }
    assert [len(decisions.decisions_for(agent)) for agent in "ABC"] == [1, 1, 1]
    assert ledger.fills() == []
    assert all(ledger.account(agent)["state"] == "FLAT" for agent in "ABC")


def test_production_paper_runner_is_inert_without_approval_and_rejects_test_mode(tmp_path):
    from backend.domain import CostModel
    from backend.ledger import Ledger
    from backend.orchestration import PaperCycleRunner
    from backend.vision import AgentOrchestrator, FakeVisionAdapter

    path = tmp_path / "production-gate.sqlite"
    ledger = Ledger(path)
    decisions = AgentOrchestrator(str(path))
    disabled = PaperCycleRunner(ledger, decisions, CostModel())
    assert disabled.enabled is False
    with pytest.raises(ValueError, match="PAPER_COST_MODEL_APPROVAL_REQUIRED"):
        PaperCycleRunner(ledger, decisions, CostModel(), enabled=True, cost_model_approved=False)

    armed = PaperCycleRunner(ledger, decisions, CostModel(), enabled=True, cost_model_approved=True)
    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    cycle = {
        "adapter": FakeVisionAdapter(),
        "image": b"test-chart",
        "candle_close": close,
        "cycle_deadline": close + timedelta(minutes=15),
        "strategies": {agent: ("v1", "agent md") for agent in "ABC"},
        "quote": {
            "source": "BINANCE_USDM_PERP",
            "currency": "USD_RESEARCH",
            "bid": "99999",
            "ask": "100001",
            "age_ms": 20,
        },
        "orderbook": {
            "source": "BINANCE_USDM_PERP",
            "age_ms": 20,
            "bids": [{"p": "99999", "s": "1"}],
            "asks": [{"p": "100001", "s": "1"}],
        },
        "feed_healthy": True,
        "now": close + timedelta(seconds=1),
    }
    result = asyncio.run(armed.run_cycle(**cycle))
    assert all(
        item["execution"]["reason"] == "NON_PRODUCTION_DECISION_REJECTED"
        for item in result.values()
    )
    assert ledger.fills() == []


def test_scheduler_does_not_reserve_event_when_production_runner_is_unarmed(tmp_path):
    from backend.domain import CostModel
    from backend.ledger import Ledger
    from backend.orchestration import PaperCycleRunner
    from backend.vision import AgentOrchestrator

    path = tmp_path / "unarmed-scheduler.sqlite"
    ledger = Ledger(path)
    runner = PaperCycleRunner(ledger, AgentOrchestrator(str(path)), CostModel())
    scheduler = PaperCycleScheduler(path, runner, enabled=True)
    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    result = asyncio.run(scheduler.run_closed_cycle(**inputs(close)))
    assert all(item["status"] == "SCHEDULER_EXECUTION_DISABLED" for item in result.values())
    assert scheduler.status(close) is None


def test_stop_monitor_calls_stop_handler_without_running_decision_cycle(tmp_path, monkeypatch):
    import backend.scheduler as scheduler_module

    runner = Runner()
    scheduler = PaperCycleScheduler(tmp_path / "scheduler.sqlite", runner, enabled=True)
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return {"accepted": False, "reason": "STOP_PENDING"}

    monkeypatch.setattr(scheduler_module, "handle_confirmed_stop", handler)
    result = scheduler.monitor_stops(event_ids={"A": "stop-1"}, quote=None, cost=object())
    assert result["A"]["reason"] == "STOP_PENDING"
    assert calls[0]["agent"] == "A" and calls[0]["quote"] is None
    assert runner.calls == []

