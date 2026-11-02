"""Evaluator-only plan for the frozen, adjudicated synthetic vision set."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

from backend.vision_prompt import build_decision_prompt
from tests.vision_eval_runner import (
    REPO_ROOT,
    ProviderInput,
    _answer_leak_check,
    _decision_schema_check,
    _read_md,
)


@dataclass(frozen=True)
class AdjudicatedBlindCase:
    layout_variant: str
    fixture_id: str
    agent: str
    repetition: int
    provider_input: ProviderInput
    score_key: dict


def _build_plan(
    root: Path, selections: list[tuple[str, str, str, int]], *, seed: int
) -> list[AdjudicatedBlindCase]:
    fixture_root = root / "tests" / "fixtures" / "vision" / "adjudicated_v1"
    manifest = json.loads((fixture_root / "ground_truth_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("prompt_must_not_include_ground_truth") is not True:
        raise ValueError("ADJUDICATED_SET_NOT_BLINDED")
    strategies = {agent: _read_md(agent, root) for agent in ("A", "B")}
    plan: list[AdjudicatedBlindCase] = []

    def prepare(case_id: str, agent: str, layout: str, repetition: int) -> None:
        case = manifest["cases"][case_id]
        image_path = fixture_root / case["images"][layout]["path"]
        image = image_path.read_bytes()
        image_hash = hashlib.sha256(image).hexdigest()
        expected_hash = case["images"][layout]["sha256"]
        if image_hash != expected_hash:
            raise ValueError(f"ADJUDICATED_IMAGE_HASH_MISMATCH:{case_id}:{layout}")
        md, version, md_hash = strategies[agent]
        prompt = build_decision_prompt(
            agent=agent,
            md_version=version,
            strategy_md=md,
            account_state={"position": "FLAT"},
            agent_memory="",
        )
        score_key = case["score_key"]
        leakage_key = {
            "pattern": score_key["assessment"],
            "global_bias": score_key["global_bias"],
            "local_bias": score_key["local_bias"],
            "assessment": score_key["assessment"],
        }
        if (
            case_id.casefold() in prompt.casefold()
            or "score_key" in prompt
            or _answer_leak_check(prompt, case_id, leakage_key)
            or not _decision_schema_check()
        ):
            raise ValueError("GROUND_TRUTH_LEAK_TO_PROMPT")
        request = ProviderInput(
            agent=agent,
            md_version=version,
            md_sha256=md_hash,
            image=image,
            image_sha256=image_hash,
            prompt=prompt,
        )
        plan.append(
            AdjudicatedBlindCase(
                layout_variant=layout,
                fixture_id=case_id,
                agent=agent,
                repetition=repetition,
                provider_input=request,
                score_key=score_key,
            )
        )

    for case_id, agent, layout, repetition in selections:
        prepare(case_id, agent, layout, repetition)
    random.Random(seed).shuffle(plan)
    return plan


def build_adjudicated_blind_plan(root: Path = REPO_ROOT) -> list[AdjudicatedBlindCase]:
    selections = [
        (case_id, agent, layout, 1)
        for layout in ("legacy", "v2")
        for case_id in ("case_03", "case_05")
        for agent in ("A", "B")
    ]
    selections.extend(("case_05", agent, "v2", 2) for agent in ("A", "B"))
    return _build_plan(root, selections, seed=20261028)


def build_adjudicated_eight_case_plan(root: Path = REPO_ROOT) -> list[AdjudicatedBlindCase]:
    """Prepare exactly one v2 image request for each of four cases and two agents."""
    selections = [
        (case_id, agent, "v2", 1)
        for case_id in ("case_01", "case_02", "case_03", "case_05")
        for agent in ("A", "B")
    ]
    return _build_plan(root, selections, seed=20261028)
