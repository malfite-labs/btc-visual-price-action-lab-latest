import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.vision_prompt import build_decision_prompt


def test_neutral_prompt_preserves_full_strategy_and_output_contract():
    md = "FULL STRATEGY\n" * 500
    state = {
        "position": "FLAT",
        "equity": "10000",
        "available_cash": "9000",
        "bid": "100",
        "ask": "101",
        "quote_time_utc": "2026-01-01T00:00:00+00:00",
        "candle_close_utc": "2026-01-01T00:00:00+00:00",
        "open_allowed": True,
    }
    a = build_decision_prompt(
        agent="A",
        md_version="v1",
        strategy_md=md,
        account_state=state,
        prompt_variant="wait_example",
    )
    b = build_decision_prompt(
        agent="A", md_version="v1", strategy_md=md, account_state=state, prompt_variant="neutral"
    )
    assert md in a and md in b
    assert '"action":"WAIT"' in a and '"action":"WAIT"' not in b
    for action in ("OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "HOLD", "WAIT"):
        assert action in a and action in b
    assert '"available_cash": "9000"' in a and '"available_cash": "9000"' in b


def test_unknown_prompt_variant_fails_closed():
    with pytest.raises(ValueError):
        build_decision_prompt(
            agent="A", md_version="v1", strategy_md="rules", prompt_variant="unknown"
        )


def test_context_has_fresh_quote_currency_account_and_explicit_gate():
    from backend import app

    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    account = {"state": "FLAT", "position_json": None, "version": 3}
    metrics = {"equity": "10000", "available_cash": "9000"}
    quote = {
        "bid": "100",
        "ask": "102",
        "event_at": close.isoformat(),
        "age_ms": 20,
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
    }
    context = app._cycle_decision_context(
        account, metrics, quote, close, "a" * 64, "b" * 64, "TEST_SNAPSHOT"
    )
    assert context["mid_price"] == "101" and context["quote_time_utc"] == close.isoformat()
    assert context["available_cash"] == "9000" and context["open_allowed"] is True
    assert context["data_mode"] == "TEST_SNAPSHOT" and context["image_sha256"] == "a" * 64
    assert context["candle_close_utc"] == close.isoformat()
    context = app._cycle_decision_context(
        {**account, "state": "STOP_EXIT_PENDING"},
        metrics,
        quote,
        close,
        "a" * 64,
        "b" * 64,
        "TEST_SNAPSHOT",
    )
    assert context["open_allowed"] is False and context["stop_state"] == "STOP_EXIT_PENDING"


def test_chart_window_excludes_future_candles_and_rejects_gaps():
    from backend import app

    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    candles = [
        {"close_at": (close - timedelta(minutes=15 * n)).isoformat(), "is_closed": True}
        for n in range(-1, 289)
    ]
    selected = app._closed_cycle_candles(candles, close)
    assert len(selected) == 288 and selected[-1]["close_at"] == close.isoformat()
    assert all(datetime.fromisoformat(x["close_at"]) <= close for x in selected)
    with pytest.raises(ValueError):
        app._closed_cycle_candles(candles[:5] + candles[6:], close)


def test_candle_market_and_interval_must_match_chart_context():
    from backend import app

    row = {
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "symbol": "BTC/USD",
        "interval": "15Min",
        "open_at": "2026-01-01T12:00:00+00:00",
        "close_at": "2026-01-01T12:15:00+00:00",
    }
    assert app._valid_cycle_candle(row)
    assert not app._valid_cycle_candle({**row, "symbol": "ETH/USD"})
    assert not app._valid_cycle_candle({**row, "open_at": "2026-01-01T12:05:00+00:00"})
    assert not app._valid_cycle_candle({**row, "source": "OTHER"})


def test_quote_expiring_during_model_inference_cannot_fill(tmp_path, monkeypatch):
    import backend.orchestration as orchestration
    from backend.domain import CostModel
    from backend.ledger import Ledger
    from backend.vision import AgentOrchestrator
    from tests.test_orchestration_e2e import ScriptedAdapter, run_cycle

    ticks = iter([10.0, 71.0])
    monkeypatch.setattr(orchestration, "monotonic", lambda: next(ticks), raising=False)
    ledger = Ledger(tmp_path / "isolated.sqlite")
    runner = orchestration.TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    adapter = ScriptedAdapter(
        {"A": {"action": "OPEN_LONG", "stop_loss": "99000", "rationale": "synthetic"}}
    )
    result = run_cycle(runner, adapter, datetime.now(UTC) - timedelta(minutes=1))
    assert result["A"]["execution"]["reason"] == "QUOTE_OR_BOOK_STALE"
    assert ledger.fills() == []


def test_context_reports_open_position_and_rejects_old_quote():
    from backend import app

    close = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    account = {
        "state": "OPEN_LONG",
        "position_json": json.dumps({"entry_price": "99", "stop_price": "90"}),
        "version": 4,
    }
    metrics = {"equity": "10000", "available_cash": "8000"}
    quote = {
        "bid": "100",
        "ask": "102",
        "event_at": close.isoformat(),
        "age_ms": 20,
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
    }
    ctx = app._cycle_decision_context(
        account, metrics, quote, close, "a" * 64, "b" * 64, "TEST_SNAPSHOT"
    )
    assert ctx["entry_price"] == "99" and ctx["stop_loss"] == "90"
    assert ctx["open_allowed"] is False and ctx["open_reason"] == "POSITION_NOT_FLAT"
    quote["event_at"] = (close - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        app._cycle_decision_context(
            account, metrics, quote, close, "a" * 64, "b" * 64, "TEST_SNAPSHOT"
        )


def test_frozen_comparison_plan_hashes_and_prompt_pairs():
    root = Path(__file__).resolve().parents[1]
    plan = json.loads(
        (root / "docs/reports/AB_CONTEXT_COMPARISON_PLAN_V1.json").read_text(encoding="utf8")
    )
    assert len(plan["rows"]) == 8 and plan["run_limit"] == 8
    assert (
        hashlib.sha256((root / plan["snapshot_path"]).read_bytes()).hexdigest()
        == plan["snapshot_sha256"]
    )
    assert (
        hashlib.sha256((root / plan["image_path"]).read_bytes()).hexdigest() == plan["image_sha256"]
    )
    combos = set()
    for row in plan["rows"]:
        md_path = root / "strategies" / row["agent"] / "v1.0.0.md"
        assert hashlib.sha256(md_path.read_bytes()).hexdigest() == row["md_sha256"]
        prompt = build_decision_prompt(
            agent=row["agent"],
            md_version=row["md_version"],
            strategy_md=md_path.read_text(encoding="utf8"),
            account_state=row["context"],
            prompt_variant=row["prompt_variant"],
        )
        assert hashlib.sha256(prompt.encode("utf8")).hexdigest() == row["prompt_sha256"]
        assert row["image_sha256"] == plan["image_sha256"]
        assert "case_01" not in prompt and "score_key" not in prompt
        combos.add((row["agent"], row["context_variant"], row["prompt_variant"]))
    assert len(combos) == 8


def test_decision_expiring_during_inference_is_rejected(tmp_path):
    import asyncio

    from backend.vision import AgentOrchestrator, FakeVisionAdapter

    class SlowAdapter(FakeVisionAdapter):
        async def decide(self, **kwargs):
            await asyncio.sleep(0.03)
            return await super().decide(**kwargs)

    now = datetime.now(UTC)
    output = asyncio.run(
        AgentOrchestrator(str(tmp_path / "offline.sqlite")).run_cycle(
            adapter=SlowAdapter(),
            image=b"test",
            candle_close=now,
            cycle_deadline=now + timedelta(milliseconds=5),
            strategy={"A": ("v1", "rules")},
            now=now,
        )
    )
    assert output[0]["status"] == "CYCLE_EXPIRED"


def test_mock_vision_provider_preserves_snapshot_through_decision_gate(tmp_path, monkeypatch):
    import asyncio

    import httpx

    from backend.vision import AgentOrchestrator, GenericVisionAdapter
    from tests.test_vision_adapter import PNG, config, provider_reply, valid_wait_decision

    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    adapter = GenericVisionAdapter(
        config(prompt_variant="neutral"),
        transport=httpx.MockTransport(lambda _: provider_reply(valid_wait_decision())),
    )
    adapter.image_capability_verified = True
    now = datetime.now(UTC)
    output = asyncio.run(
        AgentOrchestrator(str(tmp_path / "offline.sqlite")).run_cycle(
            adapter=adapter,
            image=PNG,
            candle_close=now,
            cycle_deadline=now + timedelta(minutes=15),
            strategy={"A": ("v1", "full A strategy")},
            now=now,
            account_states={
                "A": {"position": "FLAT", "available_cash": "10000", "data_mode": "TEST_SNAPSHOT"}
            },
        )
    )
    assert output[0]["status"] == "VALID"
    assert output[0]["snapshot_sha256"] == hashlib.sha256(PNG).hexdigest()
    assert output[0]["mode"] == "TEST_TRANSPORT_ONLY"

