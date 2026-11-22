"""Offline scenario and contract checks; scripted actions are not model evidence."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from backend.charts import render_ai_snapshot
from backend.domain import CostModel
from backend.execution import prepare_entry
from backend.ledger import Ledger
from backend.market import Candle
from backend.orchestration import TestOnlyPaperCycleRunner
from backend.vision import AgentOrchestrator
from backend.vision_prompt import validate_decision_json
from tests.test_orchestration_e2e import ScriptedAdapter
from tests.vision_adjudicated_plan import build_adjudicated_eight_case_plan
from tests.vision_fixture_factory import candles_for
from tests.vision_ground_truth import score_windows

ROOT = Path(__file__).resolve().parents[1]
D = Decimal


def _decision(action, stop=None):
    return {
        "action": action,
        "global_trend": "fixture",
        "local_trend": "fixture",
        "structure": "fixture",
        "key_highs": [],
        "key_lows": [],
        "rationale": "Controlled test only",
        "stop_loss": stop,
    }


def _scenario(kind):
    if kind == "continuation":
        bars = candles_for("trend_up")
        prior_high = max(D(bar.high) for bar in bars[-21:-1])
        last = bars[-1]
        close = prior_high + D("20")
        bars[-1] = replace(last, high=str(close + 20), close=str(close))
        measured = score_windows(bars)
        assert measured["global"]["structure_direction"] == "rising"
        assert measured["local"]["structure_direction"] == "rising"
        assert D(bars[-1].close) > prior_high
        stop = min(D(bar.low) for bar in bars[-21:-1]) - D("10")
    elif kind == "rejection":
        bars = candles_for("range")
        previous = bars[-96:-1]
        support = min(D(bar.low) for bar in previous)
        assert sum(D(bar.low) <= support + 50 for bar in previous) >= 2
        last = bars[-1]
        close = D(last.open) + 30
        bars[-1] = replace(last, low=str(support - 50), close=str(close), high=str(close + 20))
        measured = score_windows(bars)
        assert measured["global"]["structure_direction"] == "range"
        assert D(bars[-1].low) < support < D(bars[-1].close)
        assert D(bars[-1].close) > D(bars[-1].open)
        stop = D(bars[-1].low) - 10
    else:
        bars = candles_for("range")
        measured = score_windows(bars)
        assert measured["global"]["structure_direction"] == "range"
        assert measured["local"]["structure_direction"] == "range"
        assert D(bars[-1].low) > min(D(bar.low) for bar in bars[-96:-1])
        assert D(bars[-1].close) < max(D(bar.high) for bar in bars[-21:-1])
        stop = None
    return bars, stop


def _market(price):
    bid, ask = price - 1, price + 1
    quote = {
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "bid": str(bid),
        "ask": str(ask),
        "age_ms": 1,
        "event_id": "synthetic-quote",
    }
    book = {
        "source": "BINANCE_USDM_PERP",
        "age_ms": 1,
        "bids": [{"p": str(bid), "s": "1"}],
        "asks": [{"p": str(ask), "s": "1"}],
    }
    return quote, book


def _run(tmp_path, kind, actions, *, quote_override=None, existing=None):
    bars, stop = _scenario(kind)
    if existing:

        def shifted(value):
            return (
                datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(minutes=15)
            ).isoformat()

        bars = [
            replace(bar, open_at=shifted(bar.open_at), close_at=shifted(bar.close_at))
            for bar in bars
        ]
    image_path = tmp_path / f"{kind}.png"
    rendered = render_ai_snapshot(bars, image_path, layout_version="v2")
    assert (rendered["global_count"], rendered["local_count"]) == (288, 96)
    ledger, decisions = existing or (
        Ledger(tmp_path / "test-only-ledger.sqlite"),
        AgentOrchestrator(str(tmp_path / "test-only-decisions.sqlite")),
    )
    runner = TestOnlyPaperCycleRunner(ledger, decisions, CostModel())
    quote, book = _market(D(bars[-1].close))
    if quote_override is not None:
        quote = quote_override
    close = datetime.fromisoformat(bars[-1].close_at.replace("Z", "+00:00"))
    strategy = {
        agent: ("v1.0.0", (ROOT / "strategies" / agent / "v1.0.0.md").read_text(encoding="utf8"))
        for agent in "AB"
    }
    adapter = ScriptedAdapter(actions)
    result = asyncio.run(
        runner.run_cycle(
            adapter=adapter,
            image=image_path.read_bytes(),
            candle_close=close,
            cycle_deadline=close + timedelta(minutes=15),
            strategies=strategy,
            quote=quote,
            orderbook=book,
            feed_healthy=True,
            now=close + timedelta(seconds=1),
            account_states={
                "A": {"position": ledger.account("A")["state"]},
                "B": {"position": ledger.account("B")["state"]},
            },
        )
    )
    return result, ledger, decisions, adapter, stop


def test_contract_accepts_all_named_actions_and_rejects_missing_open_stop():
    for action in ("OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "HOLD", "WAIT"):
        stop = 90 if action.startswith("OPEN_") else None
        assert validate_decision_json(_decision(action, stop))["action"] == action
    with pytest.raises(ValueError, match="require a stop_loss"):
        validate_decision_json(_decision("OPEN_LONG"))


def test_recorded_requests_contain_full_md_and_flat_only_account():
    evidence = json.loads(
        (ROOT / "docs/reports/DEEPSEEK_VISION_BLIND_TEST_2026-10-27.json").read_text(
            encoding="utf8"
        )
    )
    plan = {(item.fixture_id, item.agent): item for item in build_adjudicated_eight_case_plan()}
    for row in evidence["records"]:
        item = plan[(row["case_id_evaluator_only"], row["agent"])]
        path = ROOT / "strategies" / row["agent"] / "v1.0.0.md"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["md_sha256"]
        assert path.read_text(encoding="utf8") in item.provider_input.prompt
        assert (
            hashlib.sha256(item.provider_input.prompt.encode("utf8")).hexdigest()
            == row["prompt_sha256"]
        )
        assert 'account={"position": "FLAT"}' in item.provider_input.prompt


def test_recorded_provider_content_was_not_rewritten_to_wait():
    evidence = json.loads(
        (ROOT / "docs/reports/DEEPSEEK_VISION_BLIND_TEST_2026-10-27.json").read_text(
            encoding="utf8"
        )
    )
    assert len(evidence["records"]) == 8
    for row in evidence["records"]:
        diagnostic = row["provider_diagnostics"]
        assert diagnostic["response_excerpt_truncated"] is False
        provider = json.loads(diagnostic["response_excerpt_redacted"])
        original = json.loads(provider["choices"][0]["message"]["content"])
        assert original == row["decision"]
        assert original["action"] == "WAIT"


def test_case05_frozen_ohlc_and_raw_responses_separate_price_turn_from_entry():
    root = ROOT / "tests/fixtures/vision/adjudicated_v1"
    manifest = json.loads((root / "ground_truth_manifest.json").read_text(encoding="utf8"))
    case = manifest["cases"]["case_05"]
    bars = [
        Candle(**item) for item in json.loads((root / case["ohlc_file"]).read_text(encoding="utf8"))
    ]
    score = score_windows(bars)
    assert score["global"]["structure_direction"] == "falling"
    assert score["local"]["event"] == "confirmed_reversal"
    assert (
        hashlib.sha256((root / case["images"]["v2"]["path"]).read_bytes()).hexdigest()
        == case["images"]["v2"]["sha256"]
    )
    evidence = json.loads(
        (ROOT / "docs/reports/DEEPSEEK_VISION_BLIND_TEST_2026-10-27.json").read_text(
            encoding="utf8"
        )
    )
    rows = [row for row in evidence["records"] if row["case_id_evaluator_only"] == "case_05"]
    assert len(rows) == 2
    for row in rows:
        assert "down then up" in row["decision"]["global_trend"].lower()
        assert row["decision"]["local_trend"].lower() == "up"
        assert row["decision"]["action"] == "WAIT"


def test_a_continuation_scripted_open_and_b_wait_use_isolated_ledger(tmp_path):
    bars, stop = _scenario("continuation")
    quote, book = _market(D(bars[-1].close))
    risk = prepare_entry(
        equity=D("10000"),
        available_cash=D("10000"),
        direction="LONG",
        stop=stop,
        quote=quote,
        orderbook=book,
        cost=CostModel(),
        quote_age_ms=1,
        book_age_ms=1,
    )
    assert (
        risk["accepted"]
        and D(risk["planned_loss"]) <= D("200")
        and D(risk["notional"]) <= D("10000")
    )
    result, ledger, _, adapter, _ = _run(
        tmp_path,
        "continuation",
        {
            "A": {"action": "OPEN_LONG", "stop_loss": str(stop), "rationale": "test continuation"},
            "B": {"action": "WAIT", "rationale": "test no rejection"},
        },
    )
    assert result["A"]["execution"]["accepted"] and result["B"]["execution"]["no_fill"]
    assert ledger.account("A")["state"] == "OPEN_LONG" and ledger.account("B")["state"] == "FLAT"
    assert len(ledger.fills()) == 1
    assert adapter.seen[0][1] != adapter.seen[1][1]


def test_b_support_rejection_scripted_open_and_a_wait(tmp_path):
    _, stop = _scenario("rejection")
    result, ledger, _, _, _ = _run(
        tmp_path,
        "rejection",
        {
            "A": {"action": "WAIT", "rationale": "test no continuation"},
            "B": {"action": "OPEN_LONG", "stop_loss": str(stop), "rationale": "test rejection"},
        },
    )
    assert result["B"]["execution"]["accepted"] and result["A"]["execution"]["no_fill"]
    assert ledger.account("B")["state"] == "OPEN_LONG" and ledger.account("A")["state"] == "FLAT"
    assert len(ledger.fills()) == 1


def test_no_setup_wait_is_not_forced_to_trade(tmp_path):
    result, ledger, _, _, _ = _run(tmp_path, "range", {})
    assert all(result[agent]["execution"]["reason"] == "WAIT" for agent in "AB")
    assert ledger.fills() == []


def test_position_close_and_wrong_stop_side_rejected(tmp_path):
    _, stop = _scenario("continuation")
    first, ledger, decisions, _, _ = _run(
        tmp_path,
        "continuation",
        {
            "A": {"action": "OPEN_LONG", "stop_loss": str(stop), "rationale": "test entry"},
        },
    )
    assert first["A"]["execution"]["accepted"]
    # A separate candle cycle with independent range input closes the test position.
    result, ledger, _, _, _ = _run(
        tmp_path,
        "range",
        {
            "A": {"action": "CLOSE_LONG", "rationale": "test structural exit"},
            "B": {"action": "OPEN_LONG", "stop_loss": "999999", "rationale": "invalid stop"},
        },
        existing=(ledger, decisions),
    )
    assert result["A"]["execution"]["accepted"]
    assert result["B"]["execution"]["reason"] == "STOP_ON_WRONG_SIDE"
    assert ledger.account("A")["state"] == "FLAT" and ledger.account("B")["state"] == "FLAT"
    assert len(ledger.fills()) == 2


def test_missing_market_or_account_value_rejects_entry(tmp_path):
    quote, _ = _market(D("52600"))
    quote["age_ms"] = 60001
    result, ledger, _, adapter, _ = _run(
        tmp_path,
        "continuation",
        {
            "A": {"action": "OPEN_LONG", "stop_loss": "51000", "rationale": "test"},
        },
        quote_override=quote,
    )
    assert result["A"]["status"] == "QUOTE_OR_BOOK_STALE"
    assert adapter.seen == [] and ledger.fills() == []
    from backend.domain import calculate_position_size

    risk = calculate_position_size(
        equity=D("0"),
        available_cash=D("0"),
        direction="LONG",
        entry_price=D("100"),
        stop_price=D("90"),
        bids=(),
        asks=(),
        cost=CostModel(),
    )
    assert not risk.accepted and risk.reason == "INVALID_INPUT"

