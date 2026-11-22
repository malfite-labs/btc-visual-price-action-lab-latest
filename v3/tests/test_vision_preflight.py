import json
from importlib.util import find_spec
from pathlib import Path

import pytest

from backend import vision_prompt


def test_decision_prompt_builder_is_shared_and_available():
    assert callable(getattr(vision_prompt, "build_decision_prompt", None))


def test_reproducible_blinded_preflight_runner_exists():
    assert find_spec("tests.vision_eval_runner") is not None


def test_all_twelve_blinded_requests_pass_offline_preflight():
    from tests.vision_eval_runner import build_preflight_plan

    prepared, evidence = build_preflight_plan()
    assert len(prepared) == 12
    assert len(evidence) == 12
    assert all(all(row["checks"].values()) for row in evidence)
    assert all(row["provider_called"] is False for row in evidence)
    assert all(row["errors"] == [] for row in evidence)


def test_score_key_is_separate_and_prompts_contain_no_answer_labels():
    from tests.vision_eval_runner import build_preflight_plan

    prepared, _ = build_preflight_plan()
    for item in prepared:
        request = item.provider_input
        strategy_file = Path("strategies") / request.agent / "v1.0.0.md"
        original_md = strategy_file.read_text(encoding="utf-8").strip()
        assert "case_id" not in request.__dataclass_fields__
        assert "score_key" not in request.__dataclass_fields__
        assert original_md in request.prompt
        assert item.fixture_id not in request.prompt
        assert "downtrend_then_reversal" not in request.prompt
        assert "rising_reversal" not in request.prompt
        assert item.score_key


def test_answer_leak_detector_blocks_explicit_answer_but_allows_strategy_vocabulary():
    from tests.vision_eval_runner import _answer_leak_check

    truth = {
        "pattern": "downtrend_then_reversal",
        "global_bias": "falling",
        "local_bias": "rising_reversal",
        "assessment": "reversal_structure",
    }
    safe_strategy = (
        "Prefer trend continuation; consider a reversal only at a key level. "
        "A rising market can still reverse."
    )
    assert _answer_leak_check(safe_strategy, "case_04", truth) == []

    leaked = "Answer key case_04: global_bias=falling; local_bias=rising_reversal"
    codes = _answer_leak_check(leaked, "case_04", truth)
    assert {"LEAK-CASE-ID", "LEAK-ANSWER-PAIR", "LEAK-ANSWER-LABEL"}.issubset(codes)


def test_prompt_preserves_arbitrarily_long_multilingual_md_without_truncation():
    rule = "COMPLETE_RULE_MARKER_"
    md = ("# COMPLETE_STRATEGY\n" + rule + "Keep all position sizing and stop rules.\n") * 1200
    prompt = vision_prompt.build_decision_prompt(agent="A", md_version="v1", strategy_md=md)
    assert f"Agent MD:\n{md}\n" in prompt
    assert len(prompt.encode("utf-8")) > 2048
    assert prompt.count("COMPLETE_RULE_MARKER_") == 1200


def test_prompt_contains_compact_complete_json_example_without_changing_strategy_md():
    md = "# Full strategy\n" + ("Retain this complete structural rule.\n" * 80)
    prompt = vision_prompt.build_decision_prompt(agent="A", md_version="v1", strategy_md=md)

    assert f"Agent MD:\n{md}\n" in prompt
    for field in (
        '"action"',
        '"global_trend"',
        '"local_trend"',
        '"structure"',
        '"key_highs"',
        '"key_lows"',
        '"rationale"',
        '"stop_loss"',
    ):
        assert field in prompt
    assert "Do not provide chain-of-thought" in prompt


def test_prompt_assesses_global_then_local_then_relationship_without_labels():
    prompt = vision_prompt.build_decision_prompt(
        agent="A", md_version="v1", strategy_md="# complete A strategy\nKeep every rule."
    )
    assert "First assess the upper GLOBAL panel independently" in prompt
    assert "Then assess the lower LOCAL panel independently" in prompt
    assert "Finally describe how the two structures relate" in prompt
    assert all(label not in prompt for label in ("downtrend_then_reversal", "range_or_wait"))


def test_prompt_keeps_account_context_allow_list_and_secret_checks():
    with pytest.raises(ValueError, match="allow-list"):
        vision_prompt.build_decision_prompt(
            agent="A", md_version="v1", strategy_md="rules", account_state={"fixture_id": "case_01"}
        )
    with pytest.raises(ValueError, match="credential-shaped"):
        vision_prompt.build_decision_prompt(
            agent="A",
            md_version="v1",
            strategy_md="rules",
            account_state={"equity": "sk-" + "x" * 24},
        )


def test_local_decision_json_contract_accepts_valid_and_rejects_bad_payloads():
    payload = {
        "action": "WAIT",
        "global_trend": "unclear",
        "local_trend": "unclear",
        "structure": "range",
        "key_highs": [],
        "key_lows": [],
        "rationale": "No setup.",
        "stop_loss": None,
    }
    assert vision_prompt.validate_decision_json(json.dumps(payload)) == payload
    payload["action"] = "OPEN_LONG"
    with pytest.raises(ValueError, match="require a stop_loss"):
        vision_prompt.validate_decision_json(payload)
    payload["stop_loss"] = 0
    with pytest.raises(ValueError, match="positive finite"):
        vision_prompt.validate_decision_json(payload)
    payload["stop_loss"] = None
    payload["key_highs"] = [float("inf")]
    with pytest.raises(ValueError, match="finite numeric array"):
        vision_prompt.validate_decision_json(payload)


def test_preflight_evidence_has_trace_hashes_and_nullable_provider_fields():
    from tests.vision_eval_runner import build_preflight_plan

    prepared, evidence = build_preflight_plan()
    assert all(len(item.provider_input.image_sha256) == 64 for item in prepared)
    assert all(len(item.provider_input.md_sha256) == 64 for item in prepared)
    assert all(item.provider_input.md_version == "v1.0.0" for item in prepared)
    expected_fields = {
        "model_id",
        "model_fingerprint",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_cost_usd",
    }
    assert all(set(row["provider_evidence"]) == expected_fields for row in evidence)
    assert all(row["provider_evidence"]["estimated_cost_usd"] is None for row in evidence)
    assert all(row["checks"]["PROMPT-01"] for row in evidence)
