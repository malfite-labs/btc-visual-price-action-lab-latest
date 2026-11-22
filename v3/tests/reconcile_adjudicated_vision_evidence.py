"""Offline scoring correction for saved evidence; never imports or calls a provider."""

from __future__ import annotations

import hashlib
import json
import shutil

from tests.run_adjudicated_vision_eval import (
    EVIDENCE_PATH,
    _score_record,
    _summary,
    _verify_frozen_dataset,
)
from tests.vision_adjudicated_plan import build_adjudicated_eight_case_plan

RAW_COPY = EVIDENCE_PATH.with_name("DEEPSEEK_VISION_BLIND_TEST_2026-10-27_RAW_INITIAL.json")


def _model_evidence_projection(records: list[dict]) -> list[dict]:
    fields = (
        "request_id",
        "case_id_evaluator_only",
        "agent",
        "layout",
        "md_version",
        "md_sha256",
        "image_sha256",
        "model_id",
        "model_id_match",
        "model_fingerprint",
        "http_status",
        "finish_reason",
        "usage",
        "reserved_cost_upper_usd",
        "usage_cost_upper_estimate_usd",
        "decision",
        "provider_diagnostics",
    )
    return [{field: record.get(field) for field in fields} for record in records]


def reconcile() -> dict:
    if not EVIDENCE_PATH.is_file():
        raise ValueError("EVIDENCE_MISSING")
    current = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    if RAW_COPY.exists():
        initial_bytes = RAW_COPY.read_bytes()
        expected_raw_hash = current.get("summary_correction", {}).get("raw_initial_sha256")
        if hashlib.sha256(initial_bytes).hexdigest() != expected_raw_hash:
            raise ValueError("RAW_INITIAL_EVIDENCE_HASH_MISMATCH")
    else:
        initial_bytes = EVIDENCE_PATH.read_bytes()
    initial = json.loads(initial_bytes)
    manifest, _ = _verify_frozen_dataset()
    plan = build_adjudicated_eight_case_plan()
    plan_by_key = {(item.fixture_id, item.agent): item for item in plan}
    records = initial.get("records")
    if initial.get("calls_attempted") != 8 or not isinstance(records, list) or len(records) != 8:
        raise ValueError("EIGHT_CALL_EVIDENCE_INCOMPLETE")
    corrected = []
    for record in records:
        case_id = record.get("case_id_evaluator_only")
        agent = record.get("agent")
        item = plan_by_key.get((case_id, agent))
        if item is None:
            raise ValueError("UNEXPECTED_EVALUATION_RECORD")
        if (
            record.get("image_sha256") != item.provider_input.image_sha256
            or record.get("md_sha256") != item.provider_input.md_sha256
        ):
            raise ValueError("REQUEST_INPUT_HASH_MISMATCH")
        scored = _score_record(dict(record), item, manifest)
        scored["prompt_sha256"] = hashlib.sha256(
            item.provider_input.prompt.encode("utf-8")
        ).hexdigest()
        scored["prompt_utf8_bytes"] = len(item.provider_input.prompt.encode("utf-8"))
        scored["image_size_bytes"] = len(item.provider_input.image)
        corrected.append(scored)
    if _model_evidence_projection(records) != _model_evidence_projection(corrected):
        raise ValueError("MODEL_EVIDENCE_CHANGED_DURING_RESCORING")
    raw_hash = hashlib.sha256(initial_bytes).hexdigest()
    if not RAW_COPY.exists():
        shutil.copy2(EVIDENCE_PATH, RAW_COPY)
    payload = {
        **initial,
        "summary_version": "adjudicated-scoring-v2",
        "summary_correction": {
            "reason": "Initial summary joined on fixture_id while records stored case_id_evaluator_only; direction parsing also failed to distinguish plain up/down and composite directions.",
            "offline_only": True,
            "api_calls_added": 0,
            "labels_changed": False,
            "raw_initial_file": RAW_COPY.name,
            "raw_initial_sha256": raw_hash,
            "decision_and_provider_evidence_unchanged": True,
        },
        "records": corrected,
        "summary": _summary(corrected, manifest),
    }
    temporary = EVIDENCE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(EVIDENCE_PATH)
    return {
        "raw_initial_sha256": raw_hash,
        "corrected_evidence_sha256": hashlib.sha256(EVIDENCE_PATH.read_bytes()).hexdigest(),
        "summary": payload["summary"],
        "api_calls_added": 0,
    }


if __name__ == "__main__":
    print(json.dumps(reconcile(), ensure_ascii=False, indent=2))
