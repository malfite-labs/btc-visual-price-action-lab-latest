import json
import sqlite3
from decimal import Decimal as D

from backend.domain import BookLevel, calculate_position_size, simulate_fill
from backend.ledger import Ledger
from backend.orchestration import TestOnlyPaperCycleRunner
from backend.settings import COST_MODEL, QUOTE_AND_BOOK_MAX_AGE_MS


def test_user_approved_costs_and_60_second_quote_boundary():
    assert COST_MODEL.fee_rate == D("0.0005")
    assert COST_MODEL.adverse_slippage_bps == D("2")
    assert (
        COST_MODEL.max_quote_age_ms
        == COST_MODEL.max_book_age_ms
        == QUOTE_AND_BOOK_MAX_AGE_MS
        == 60000
    )
    quote = {
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "bid": "99",
        "ask": "101",
        "age_ms": 60000,
    }
    book = {"source": "BINANCE_USDM_PERP", "age_ms": 60000}
    assert TestOnlyPaperCycleRunner._fresh(quote, book, COST_MODEL)
    assert not TestOnlyPaperCycleRunner._fresh({**quote, "age_ms": 60001}, book, COST_MODEL)
    assert not TestOnlyPaperCycleRunner._fresh(quote, {**book, "age_ms": 60001}, COST_MODEL)
    assert not TestOnlyPaperCycleRunner._fresh(
        {**quote, "timestamp_valid": False}, book, COST_MODEL
    )
    assert not TestOnlyPaperCycleRunner._fresh(
        quote, {**book, "timestamp_valid": False}, COST_MODEL
    )


def test_status_reports_approved_research_costs_without_arming():
    import asyncio

    from backend import app

    status = asyncio.run(app.system_status())
    assert status["cost_model"]["maker_reference_rate"] == "0.0002"
    assert status["cost_model"]["taker_fee_rate"] == "0.0005"
    assert status["cost_model"]["slippage_per_fill"] == "0.0002"
    assert status["cost_model"]["quote_book_max_age_ms"] == 60000
    assert status["cost_model"]["funding"] == "NOT_INCLUDED"
    assert status["paper_automation"] == "DISABLED_NOT_ARMED"


def test_closed_cycle_market_failure_is_recorded_in_isolated_audit(tmp_path, monkeypatch):
    import asyncio
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from backend import app

    ledger = Ledger(tmp_path / "audit.sqlite")
    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    monkeypatch.setattr(app, "ledger", ledger)
    monkeypatch.setattr(app, "paper_scheduler", SimpleNamespace(enabled=True))
    monkeypatch.setattr(app, "paper_runner", SimpleNamespace(enabled=True))
    monkeypatch.setattr(app, "candles", [{"close_at": close.isoformat(), "is_closed": True}])
    monkeypatch.setattr(app, "market_status", {"feed_health": "STALE", "gap_count_288": 1})
    outcome = asyncio.run(app.process_closed_bar(close, now=close))
    assert outcome["A"]["status"] == "MARKET_DATA_INCOMPLETE"
    with sqlite3.connect(ledger.path) as db:
        kinds = [row[0] for row in db.execute("SELECT kind FROM audit_events")]
    assert "MARKET_DATA_INCOMPLETE" in kinds


def test_buy_sell_fill_keeps_spread_and_applies_adverse_slippage_and_taker_fees():
    bids, asks = [BookLevel(D("99"), D("2"))], [BookLevel(D("101"), D("2"))]
    buy, buy_fee = simulate_fill("BUY", D("1"), bids, asks, COST_MODEL)
    sell, sell_fee = simulate_fill("SELL", D("1"), bids, asks, COST_MODEL)
    assert buy == D("101.03") and sell == D("98.98")
    assert buy_fee == buy * D("0.0005") and sell_fee == sell * D("0.0005")
    assert buy > D("101") > D("99") > sell


def test_new_fill_stores_cost_provenance_without_recomputing_legacy_fill(tmp_path):
    path = tmp_path / "isolated.sqlite"
    ledger = Ledger(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO fills(fill_id,agent,side,quantity,price,fee,slippage,occurred_at,idempotency_key,trade_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("legacy", "B", "BUY", "1", "100", "0.10", "0.5", "2020", "legacy-key", "legacy-trade"),
        )
        db.execute("ALTER TABLE fills DROP COLUMN cost_model_json")
    ledger = Ledger(path)  # Upgrade a pre-provenance schema in an isolated database.
    result = ledger.open_position(
        agent="A",
        direction="LONG",
        quantity=D("0.01"),
        price=D("100"),
        stop=D("90"),
        fee=D("0.0005"),
        slippage=D("0.01"),
        idempotency_key="new",
        expected_version=0,
    )
    assert result["accepted"]
    fills = {row["fill_id"]: row for row in ledger.fills()}
    assert fills["legacy"]["fee"] == "0.10" and fills["legacy"]["cost_model_json"] is None
    model = json.loads(fills[result["fill_id"]]["cost_model_json"])
    assert model["taker_fee_rate"] == "0.0005" and model["slippage_per_fill"] == "0.0002"
    assert model["funding"] == "NOT_INCLUDED" and model["reference"] == "BINANCE_USDT_M_FUTURES"


def test_fee_rounding_cannot_push_planned_loss_over_two_percent(tmp_path):
    ledger = Ledger(tmp_path / "rounding.sqlite")
    result = ledger.open_position(
        agent="A",
        direction="LONG",
        quantity=D("0.03893682"),
        price=D("100000"),
        stop=D("95000"),
        fee=D("1.946841"),
        slippage=D("0"),
        idempotency_key="rounding-risk",
        expected_version=0,
    )
    assert not result["accepted"] and result["reason"] == "ONE_X_OR_RISK_LIMIT"
    assert ledger.fills() == []


def test_ledger_rejects_fee_not_equal_to_approved_rate(tmp_path):
    ledger = Ledger(tmp_path / "fee-mismatch.sqlite")
    result = ledger.open_position(
        agent="A",
        direction="LONG",
        quantity=D("0.01"),
        price=D("100000"),
        stop=D("99000"),
        fee=D("2"),
        slippage=D("0"),
        idempotency_key="mismatch",
        expected_version=0,
    )
    assert not result["accepted"] and result["reason"] == "FEE_AMOUNT_MISMATCH"
    assert ledger.fills() == []
    opened = ledger.open_position(
        agent="A",
        direction="LONG",
        quantity=D("0.01"),
        price=D("100000"),
        stop=D("99000"),
        fee=D("0.50"),
        slippage=D("0"),
        idempotency_key="valid",
        expected_version=0,
    )
    assert opened["accepted"]
    close = ledger.close_position(
        agent="A",
        price=D("101000"),
        fee=D("2"),
        slippage=D("0"),
        idempotency_key="bad-close",
        expected_version=opened["account_version"],
    )
    assert not close["accepted"] and close["reason"] == "FEE_AMOUNT_MISMATCH"
    assert ledger.account("A")["state"] == "OPEN_LONG" and len(ledger.fills()) == 1
    close = ledger.close_position(
        agent="A",
        price=D("101000"),
        fee=D("0.51"),
        slippage=D("0"),
        idempotency_key="valid-close",
        expected_version=opened["account_version"],
    )
    assert close["accepted"]
    with sqlite3.connect(ledger.path) as db:
        amounts = [
            D(row[0]) for row in db.execute("SELECT amount FROM ledger_events WHERE agent='A'")
        ]
    assert D("10000") + sum(amounts, D("0")) == D(ledger.account("A")["cash"])


def test_refreshed_quote_is_used_after_inference_and_failures_block_entry(tmp_path):
    from datetime import UTC, datetime, timedelta

    from backend.vision import AgentOrchestrator
    from tests.test_orchestration_e2e import ScriptedAdapter, books, quote, run_cycle

    ledger = Ledger(tmp_path / "latest.sqlite")
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), COST_MODEL
    )
    adapter = ScriptedAdapter(
        {"A": {"action": "OPEN_LONG", "stop_loss": "99000", "rationale": "test"}}
    )

    async def newer():
        return quote("100999", "101001"), {
            **books(),
            "bids": [{"p": "100999", "s": "1"}],
            "asks": [{"p": "101001", "s": "1"}],
        }

    start = datetime.now(UTC) - timedelta(minutes=1)
    result = run_cycle(runner, adapter, start, refresh_market=newer)
    assert result["A"]["execution"]["accepted"]
    assert D(ledger.fills("A")[0]["price"]) > D("101001")
    assert ledger.fills("A")[0]["quote_id"] == "quote-100999-101001"
    position = json.loads(ledger.account("A")["position_json"])
    initial = calculate_position_size(
        equity=D("10000"),
        available_cash=D("10000"),
        entry_price=D("101001"),
        stop_price=D("99000"),
        direction="LONG",
        bids=[BookLevel(D("100999"), D("1"))],
        asks=[BookLevel(D("101001"), D("1"))],
        cost=COST_MODEL,
    )
    assert D(position["quantity"]) < initial.quantity
    assert D(position["planned_loss"]) <= D("200")

    async def failed():
        raise RuntimeError("offline test")

    ledger2 = Ledger(tmp_path / "failed.sqlite")
    runner2 = TestOnlyPaperCycleRunner(
        ledger2, AgentOrchestrator(str(tmp_path / "failed-decisions.sqlite")), COST_MODEL
    )
    blocked = run_cycle(runner2, adapter, start, refresh_market=failed)
    assert blocked["A"]["execution"]["reason"] == "MARKET_REFRESH_FAILED"
    assert ledger2.fills() == []

