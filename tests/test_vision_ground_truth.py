import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from tests.vision_adjudicated_plan import (
    build_adjudicated_blind_plan,
    build_adjudicated_eight_case_plan,
)
from tests.vision_fixture_factory import (
    candles_for,
    clean_reversal_candles,
    generate_adjudicated_fixtures,
)
from tests.vision_ground_truth import _direction, score_windows


def test_frozen_rules_separate_endpoint_change_structure_and_range():
    expected = {
        "trend_up": ("rising", "rising", "trend_structure"),
        "trend_down": ("falling", "falling", "trend_structure"),
        "range": ("range", "range", "range_or_wait"),
        "downtrend_then_reversal": ("mixed", "rising", "mixed_transition"),
    }
    for pattern, labels in expected.items():
        score = score_windows(candles_for(pattern))
        assert (
            score["global"]["structure_direction"],
            score["local"]["structure_direction"],
            score["assessment"],
        ) == labels

    old_reversal = score_windows(candles_for("downtrend_then_reversal"))
    assert Decimal(old_reversal["global"]["endpoint_change_pct"]) > 0
    assert old_reversal["global"]["structure_direction"] != "falling"
    assert old_reversal["local"]["event"] == "rally_not_confirmed_reversal"


def test_clean_reversal_meets_predeclared_ohlc_rules():
    score = score_windows(clean_reversal_candles())
    assert Decimal(score["global"]["endpoint_change_pct"]) <= -1.0
    assert score["global"]["structure_direction"] == "falling"
    assert Decimal(score["local"]["endpoint_change_pct"]) >= 1.0
    assert score["local"]["structure_direction"] == "rising"
    assert score["local"]["event"] == "confirmed_reversal"
    assert score["assessment"] == "reversal_structure"


def test_rule_threshold_boundaries_are_explicit_and_deterministic():
    assert (
        _direction(
            Decimal("1.0"),
            [Decimal("0.25"), Decimal("0.25"), Decimal("0")],
            segment_floor=Decimal("0.25"),
            endpoint_floor=Decimal("1.0"),
        )
        == "rising"
    )
    assert (
        _direction(
            Decimal("-1.0"),
            [Decimal("-0.25"), Decimal("-0.25"), Decimal("0")],
            segment_floor=Decimal("0.25"),
            endpoint_floor=Decimal("1.0"),
        )
        == "falling"
    )
    assert (
        _direction(
            Decimal("0.999"),
            [Decimal("1.499"), Decimal("-1.499"), Decimal("0")],
            segment_floor=Decimal("0.25"),
            endpoint_floor=Decimal("1.0"),
        )
        == "range"
    )
    assert (
        _direction(
            Decimal("0.999"),
            [Decimal("1.5"), Decimal("-1.499"), Decimal("0")],
            segment_floor=Decimal("0.25"),
            endpoint_floor=Decimal("1.0"),
        )
        == "mixed"
    )


def test_invalid_ohlc_or_nonconsecutive_timestamp_is_unscorable():
    bars = candles_for("trend_up")
    invalid_ohlc = list(bars)
    invalid_ohlc[96] = replace(invalid_ohlc[96], high="1")
    with pytest.raises(ValueError, match="INCOHERENT_OHLC"):
        score_windows(invalid_ohlc)

    duplicated_time = list(bars)
    duplicated_time[97] = replace(duplicated_time[97], open_at=duplicated_time[96].open_at)
    with pytest.raises(ValueError, match="NONCONTIGUOUS_15M_BARS"):
        score_windows(duplicated_time)

    with pytest.raises(ValueError, match="EXACTLY_384_BARS"):
        score_windows(bars[-288:])


def test_adjudicated_manifest_is_reproducible_and_uses_paired_ohlc(tmp_path):
    committed = Path("tests/fixtures/vision/adjudicated_v1/ground_truth_manifest.json")
    manifest = json.loads(committed.read_text(encoding="utf-8"))
    generated = generate_adjudicated_fixtures(tmp_path / "adjudicated_v1")

    assert generated == manifest
    assert set(manifest["cases"]) == {"case_01", "case_02", "case_03", "case_05"}
    assert manifest["excluded_cases"]["case_04"]["scoring_status"] == "excluded_reclassified"
    for case in manifest["cases"].values():
        assert case["global_count"] == 288
        assert case["local_count"] == 96
        assert len(case["ohlc_sha256"]) == 64
        assert case["images"]["legacy"]["candle_sha256"] == case["chart_candle_sha256"]
        assert case["images"]["v2"]["candle_sha256"] == case["chart_candle_sha256"]

    fixture_root = committed.parent
    checksums = {
        line.split("  ", 1)[1]: line.split("  ", 1)[0]
        for line in (fixture_root / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    }
    for relative_path, expected_hash in checksums.items():
        payload = fixture_root / relative_path
        assert hashlib.sha256(payload.read_bytes()).hexdigest() == expected_hash
    for case in manifest["cases"].values():
        raw = json.loads((fixture_root / case["ohlc_file"]).read_text(encoding="utf-8"))
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(canonical).hexdigest() == case["ohlc_sha256"]
        for image_info in case["images"].values():
            path = fixture_root / image_info["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == image_info["sha256"]
            with Image.open(path) as image:
                assert image.format == "PNG"
                image.verify()


def test_adjudicated_blind_plan_keeps_answers_out_and_pairs_repeats():
    plan = build_adjudicated_blind_plan()
    assert len(plan) == 10
    assert {case.fixture_id for case in plan} == {"case_03", "case_05"}
    assert all("score_key" not in case.provider_input.__dataclass_fields__ for case in plan)
    for case in plan:
        assert case.fixture_id not in case.provider_input.prompt
        assert case.score_key["assessment"] not in case.provider_input.prompt

    for case_id in ("case_03", "case_05"):
        for agent in ("A", "B"):
            legacy = next(
                case
                for case in plan
                if case.fixture_id == case_id
                and case.agent == agent
                and case.layout_variant == "legacy"
            )
            v2 = next(
                case
                for case in plan
                if case.fixture_id == case_id
                and case.agent == agent
                and case.layout_variant == "v2"
                and case.repetition == 1
            )
            assert legacy.score_key == v2.score_key
            assert legacy.provider_input.md_sha256 == v2.provider_input.md_sha256
            assert legacy.provider_input.image_sha256 != v2.provider_input.image_sha256

    for agent in ("A", "B"):
        first = next(
            case
            for case in plan
            if case.fixture_id == "case_05"
            and case.agent == agent
            and case.layout_variant == "v2"
            and case.repetition == 1
        )
        repeat = next(
            case
            for case in plan
            if case.fixture_id == "case_05"
            and case.agent == agent
            and case.layout_variant == "v2"
            and case.repetition == 2
        )
        assert first.provider_input.image_sha256 == repeat.provider_input.image_sha256
        assert first.provider_input.prompt == repeat.provider_input.prompt
        assert first.provider_input.md_sha256 == repeat.provider_input.md_sha256


def test_eight_call_plan_is_one_v2_request_per_case_and_agent():
    plan = build_adjudicated_eight_case_plan()
    assert len(plan) == 8
    assert {case.fixture_id for case in plan} == {
        "case_01",
        "case_02",
        "case_03",
        "case_05",
    }
    assert {case.agent for case in plan} == {"A", "B"}
    assert all(case.layout_variant == "v2" and case.repetition == 1 for case in plan)
    assert len({(case.fixture_id, case.agent) for case in plan}) == 8
    assert all("score_key" not in case.provider_input.__dataclass_fields__ for case in plan)
