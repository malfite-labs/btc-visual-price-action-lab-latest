"""Offline blinded fixture preflight; scoring keys never enter provider inputs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from backend.vision_prompt import build_decision_prompt, validate_decision_json

REPO_ROOT = Path(__file__).resolve().parents[1]
CASE_ORDER = (
    ("case_01", "A", 1),
    ("case_01", "B", 1),
    ("case_02", "A", 1),
    ("case_02", "B", 1),
    ("case_03", "A", 1),
    ("case_03", "B", 1),
    ("case_04", "A", 1),
    ("case_04", "B", 1),
    ("case_01", "A", 2),
    ("case_01", "B", 2),
    ("case_04", "A", 2),
    ("case_04", "B", 2),
)
ANSWER_FIELDS = ("pattern", "global_bias", "local_bias", "assessment")
ANSWER_CUES = re.compile(r"\b(expected|ground truth|answer key|correct answer)\b", re.I)
SECRET_SHAPE = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")


@dataclass(frozen=True)
class ProviderInput:
    """Only fields allowed to cross the model boundary for a chart decision."""

    agent: str
    md_version: str
    md_sha256: str
    image: bytes
    image_sha256: str
    prompt: str


@dataclass(frozen=True)
class PreparedEvaluation:
    """Local evaluator view. `score_key` is never part of ProviderInput."""

    fixture_id: str
    repetition: int
    provider_input: ProviderInput
    score_key: dict


def _read_md(agent: str, root: Path) -> tuple[str, str, str]:
    md_path = root / "strategies" / agent / "v1.0.0.md"
    metadata_path = root / "strategies" / agent / "v1.0.0.metadata.json"
    markdown = md_path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    declared = re.search(r"(?m)^version:\s*(\S+)\s*$", markdown)
    if not declared or declared.group(1) != metadata.get("version"):
        raise ValueError(f"MD_VERSION_MISMATCH_{agent}")
    if metadata.get("agent") != agent or metadata.get("immutable") is not True:
        raise ValueError(f"MD_METADATA_INVALID_{agent}")
    return markdown, metadata["version"], hashlib.sha256(md_path.read_bytes()).hexdigest()


def _answer_leak_check(prompt: str, fixture_id: str, key: dict) -> list[str]:
    """Catch explicit answer-key material without banning ordinary strategy vocabulary."""
    text = prompt.casefold()
    leaks: list[str] = []
    if re.search(rf"(?<![a-z0-9]){re.escape(fixture_id.casefold())}(?![a-z0-9])", text):
        leaks.append("LEAK-CASE-ID")

    # Dataset-specific identifiers and composite labels are not normal strategy prose.
    for field in ANSWER_FIELDS:
        value = str(key[field]).casefold()
        if "_" in value and re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", text):
            leaks.append("LEAK-ANSWER-LABEL")
        assignment = re.compile(
            rf"(?:['\"]?{re.escape(field)}['\"]?)\s*[:=]\s*['\"]?{re.escape(value)}\b",
            re.I,
        )
        if assignment.search(prompt):
            leaks.append("LEAK-ANSWER-PAIR")
        for cue in ANSWER_CUES.finditer(prompt):
            context = prompt[cue.start() : cue.start() + 120]
            if re.search(rf"\b{re.escape(value)}\b", context, re.I):
                leaks.append("LEAK-ANSWER-CUE")
                break
    return sorted(set(leaks))


def _load_image(
    image_path: Path, expected_sha256: str, expected_size: tuple[int, int]
) -> tuple[bytes, list[str]]:
    data = image_path.read_bytes()
    checks: list[str] = []
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        checks.append("IMG-HASH-MISMATCH")
    try:
        with Image.open(image_path) as image:
            if image.format != "PNG" or image.size != expected_size:
                checks.append("IMG-FORMAT-OR-DIMENSIONS")
            image.verify()
    except Exception:
        checks.append("IMG-INVALID")
    return data, checks


def _decision_schema_check() -> bool:
    valid = {
        "action": "WAIT",
        "global_trend": "description",
        "local_trend": "description",
        "structure": "description",
        "key_highs": [10.0],
        "key_lows": [9.0],
        "rationale": "reason",
        "stop_loss": None,
    }
    try:
        validate_decision_json(valid)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    invalid = {**valid, "action": "UNSUPPORTED"}
    try:
        validate_decision_json(invalid)
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    return False


def build_preflight_plan(
    root: Path = REPO_ROOT, *, fixture_variant: str = "original"
) -> tuple[list[PreparedEvaluation], list[dict]]:
    if fixture_variant not in {"original", "v2"}:
        raise ValueError("FIXTURE_VARIANT_INVALID")
    base_fixture_dir = root / "tests" / "fixtures" / "vision"
    fixture_dir = (
        base_fixture_dir if fixture_variant == "original" else base_fixture_dir / "layout_v2"
    )
    manifest = json.loads((fixture_dir / "ground_truth_manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("market_data") is not False
        or manifest.get("prompt_must_not_include_ground_truth") is not True
    ):
        raise ValueError("FIXTURE_MANIFEST_NOT_BLINDED")
    expected_size = tuple(manifest.get("image_size", [1280, 950]))

    strategies = {agent: _read_md(agent, root) for agent in ("A", "B")}
    prepared: list[PreparedEvaluation] = []
    evidence: list[dict] = []
    for fixture_id, agent, repetition in CASE_ORDER:
        key = manifest["cases"][fixture_id]
        image, image_errors = _load_image(fixture_dir / key["image"], key["sha256"], expected_size)
        md, md_version, md_sha256 = strategies[agent]
        prompt_errors: list[str] = []
        try:
            prompt = build_decision_prompt(
                agent=agent,
                md_version=md_version,
                strategy_md=md,
                account_state={"position": "FLAT"},
                agent_memory="",
            )
        except ValueError as exc:
            prompt = ""
            prompt_errors.append(str(exc))
        leak_errors = _answer_leak_check(prompt, fixture_id, key)
        if SECRET_SHAPE.search(prompt):
            prompt_errors.append("SECRET_SHAPE_IN_PROMPT")
        prompt_bytes = len(prompt.encode("utf-8"))

        request = ProviderInput(
            agent=agent,
            md_version=md_version,
            md_sha256=md_sha256,
            image=image,
            image_sha256=hashlib.sha256(image).hexdigest(),
            prompt=prompt,
        )
        prepared.append(
            PreparedEvaluation(
                fixture_id=fixture_id,
                repetition=repetition,
                provider_input=request,
                score_key={field: key[field] for field in ANSWER_FIELDS},
            )
        )
        checks = {
            "IMG-01": not image_errors,
            "MD-01": md_version == "v1.0.0" and len(md_sha256) == 64,
            "PROMPT-01": bool(prompt) and not prompt_errors,
            "LEAK-01": not leak_errors,
            "TRACE-01": request.image_sha256 == key["sha256"] and request.md_version == md_version,
            "SCHEMA-01": _decision_schema_check(),
            "SECRET-01": not bool(SECRET_SHAPE.search(prompt)),
        }
        evidence.append(
            {
                "fixture_id": fixture_id,
                "agent": agent,
                "repetition": repetition,
                "image_sha256": request.image_sha256,
                "md_version": md_version,
                "md_sha256": md_sha256,
                "prompt_utf8_bytes": prompt_bytes,
                "checks": checks,
                "errors": sorted(set(image_errors + prompt_errors + leak_errors)),
                "provider_called": False,
                "provider_evidence": {
                    "model_id": None,
                    "model_fingerprint": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                    "estimated_cost_usd": None,
                },
            }
        )
    return prepared, evidence
