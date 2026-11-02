"""Explicit, one-round DeepSeek blind evaluation for frozen adjudicated fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from backend.market import Candle
from backend.vision_prompt import DECISION_SCHEMA, validate_decision_json
from backend.vision_provider import ConfigurableVisionAdapter, VisionProviderConfig
from tests.vision_adjudicated_plan import build_adjudicated_eight_case_plan
from tests.vision_ground_truth import score_windows

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "vision" / "adjudicated_v1"
EVIDENCE_PATH = ROOT / "docs" / "reports" / "DEEPSEEK_VISION_BLIND_TEST_2026-10-27.json"
BUDGET_DB = ROOT / "data" / "vision-adjudicated-20261027.sqlite3"
MAX_CALLS = 8
MAX_OUTPUT_TOKENS = 1000
INPUT_RATE = Decimal("0.30")
OUTPUT_RATE = Decimal("3.96")
CALL_LIMIT = Decimal("0.015")
ROUND_LIMIT = Decimal("0.10")
THINKING_MODE = "disabled"
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
SECRET_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")


def _redact(value):
    if isinstance(value, str):
        return SECRET_RE.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _redact(item)
            for key, item in value.items()
            if not any(
                token in str(key).casefold() for token in ("api_key", "secret", "authorization")
            )
        }
    return value


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(_redact(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(path)


def _load_project_key() -> bool:
    if os.getenv("DEEPSEEK_API_KEY", "").strip():
        return True
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return False
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "DEEPSEEK_API_KEY":
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ["DEEPSEEK_API_KEY"] = value
                return True
            return False
    return False


def _verify_frozen_dataset() -> tuple[dict, list]:
    manifest_path = FIXTURE_ROOT / "ground_truth_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sums = {}
    for line in (FIXTURE_ROOT / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        target = FIXTURE_ROOT / relative
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("FROZEN_DATASET_CHECKSUM_MISMATCH")
        sums[relative] = digest
    if (
        manifest.get("dataset_version") != "adjudicated-v1"
        or manifest.get("market_data") is not False
        or manifest.get("prompt_must_not_include_ground_truth") is not True
        or set(manifest.get("cases", {})) != {"case_01", "case_02", "case_03", "case_05"}
        or not (ROOT / manifest["scoring_rules"]).is_file()
    ):
        raise ValueError("FROZEN_DATASET_MANIFEST_INVALID")
    for case_id, case in manifest["cases"].items():
        raw_path = FIXTURE_ROOT / case["ohlc_file"]
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(canonical).hexdigest() != case["ohlc_sha256"]:
            raise ValueError(f"FROZEN_OHLC_HASH_MISMATCH:{case_id}")
        scored = score_windows([Candle(**item) for item in raw])
        expected = {
            "global_bias": scored["global"]["structure_direction"],
            "local_bias": scored["local"]["structure_direction"],
            "local_event": scored["local"]["event"],
            "assessment": scored["assessment"],
        }
        if expected != case["score_key"] or scored != case["metrics"]:
            raise ValueError(f"FROZEN_LABEL_RECOMPUTE_MISMATCH:{case_id}")
        for layout in ("legacy", "v2"):
            image_info = case["images"][layout]
            image = FIXTURE_ROOT / image_info["path"]
            if hashlib.sha256(image.read_bytes()).hexdigest() != image_info["sha256"]:
                raise ValueError(f"FROZEN_IMAGE_HASH_MISMATCH:{case_id}:{layout}")
            if image_info["candle_sha256"] != case["chart_candle_sha256"]:
                raise ValueError(f"IMAGE_OHLC_PAIR_MISMATCH:{case_id}:{layout}")
    plan = build_adjudicated_eight_case_plan(ROOT)
    if len(plan) != MAX_CALLS or {
        (item.fixture_id, item.provider_input.agent) for item in plan
    } != {(case_id, agent) for case_id in manifest["cases"] for agent in ("A", "B")}:
        raise ValueError("EIGHT_CASE_PLAN_INVALID")
    return manifest, plan


def _reservation(prompt: str) -> Decimal:
    # Provider documentation caps an image at 1024 tokens; add 256 tokens for protocol overhead.
    input_ceiling = len(prompt.encode("utf-8")) + 1024 + 256
    return (
        Decimal(input_ceiling) * INPUT_RATE + Decimal(MAX_OUTPUT_TOKENS) * OUTPUT_RATE
    ) / Decimal(1_000_000)


def _model_id_is_deepseek_flash(model_id: object) -> bool:
    if not isinstance(model_id, str):
        return False
    normalized = model_id.casefold()
    return normalized == "deepseek-flash" or normalized.startswith("deepseek-v4.1-flash")


def _scored_structure_bucket(value: str) -> str:
    text = value.casefold()
    negated_reversal = re.search(
        r"\b(?:no|not|without|unconfirmed|hasn't|has not|doesn't|does not|failed to|not yet)\b"
        r"[^.;,]{0,100}\b(?:reversal|反转)\b",
        text,
    )
    if negated_reversal:
        return _structural_trend_or_range(text)
    if re.search(r"\b(?:reversal|反转)\b", text) and re.search(
        r"\b(?:confirmed|confirm|confirms|confirmation)\b|确认", text
    ):
        return "reversal_structure"
    return _structural_trend_or_range(text)


def _structural_trend_or_range(text: str) -> str:
    if any(term in text for term in ("range", "ranging", "sideways", "震荡", "横盘")):
        return "range_or_wait"
    if any(
        term in text
        for term in (
            "trend",
            "continuation",
            "channel",
            "higher highs",
            "higher lows",
            "lower highs",
            "lower lows",
            "rising structure",
            "falling structure",
            "趋势",
        )
    ):
        return "trend_structure"
    return "uncertain"


def _direction_bucket(value: str) -> str:
    text = value.casefold()
    positive = bool(
        re.search(r"\b(?:rising|uptrend|bullish|upward|up)\b|上涨", text)
        or ("higher highs" in text and "higher lows" in text)
    )
    negative = bool(
        re.search(r"\b(?:falling|downtrend|bearish|downward|down)\b|下跌", text)
        or ("lower highs" in text and "lower lows" in text)
    )
    if positive and negative:
        return "mixed"
    if positive:
        return "rising"
    if negative:
        return "falling"
    if any(term in text for term in ("sideways", "range", "ranging", "consolidat", "横盘", "震荡")):
        return "sideways"
    return "uncertain"


def _confirmed_reversal_recognized(value: str) -> bool:
    text = value.casefold()
    if not re.search(r"\b(?:reversal|反转)\b", text):
        return False
    if re.search(
        r"\b(?:no|not|without|unconfirmed|hasn't|has not|doesn't|does not|failed to|not yet)\b"
        r"[^.;,]{0,100}\b(?:reversal|反转)\b",
        text,
    ):
        return False
    if re.search(
        r"\b(?:reversal|反转)\b[^.;,]{0,60}\b(?:not|never|unconfirmed|yet|failed)\b",
        text,
    ):
        return False
    return bool(re.search(r"\b(?:confirmed|confirm|confirms|confirmation)\b|确认", text))


def _load_budget_rows() -> tuple[int, Decimal]:
    if not BUDGET_DB.exists():
        return 0, Decimal(0)
    with sqlite3.connect(BUDGET_DB) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vision_budget_calls'"
        ).fetchone()
        if not table:
            return 0, Decimal(0)
        rows = db.execute("SELECT reserved_usd FROM vision_budget_calls").fetchall()
    return len(rows), sum((Decimal(row[0]) for row in rows), Decimal(0))


def _score_record(record: dict, item, manifest: dict) -> dict:
    valid = record["evaluation_valid"] is True
    decision = record.get("decision") if valid else {}
    expected = item.score_key
    actual_global = _direction_bucket(decision.get("global_trend", "")) if valid else "uncertain"
    actual_local = _direction_bucket(decision.get("local_trend", "")) if valid else "uncertain"
    if expected["global_bias"] == "range":
        global_match = actual_global == "sideways"
    else:
        global_match = actual_global == expected["global_bias"]
    if expected["local_bias"] == "range":
        local_match = actual_local == "sideways"
    else:
        local_match = actual_local == expected["local_bias"]
    actual_structure = (
        _scored_structure_bucket(decision.get("structure", "")) if valid else "uncertain"
    )
    structure_match = actual_structure == expected["assessment"]
    reversal_match = (
        actual_structure == "reversal_structure"
        and actual_local == "rising"
        and _confirmed_reversal_recognized(decision.get("structure", ""))
    )
    record.update(
        {
            "global_direction_match": bool(valid and global_match),
            "local_direction_match": bool(valid and local_match),
            "structure_bucket": actual_structure,
            "structure_match": bool(valid and structure_match),
        }
    )
    if item.fixture_id == "case_05":
        record["reversal_recognition_match"] = bool(valid and reversal_match)
    if valid and decision.get("action") in {"OPEN_LONG", "OPEN_SHORT"}:
        metrics = manifest["cases"][item.fixture_id]["metrics"]["local"]
        close = Decimal(metrics["end_close"])
        local_low = Decimal(metrics["low"]["price"])
        local_high = Decimal(metrics["high"]["price"])
        stop = Decimal(str(decision["stop_loss"]))
        action = decision["action"]
        side_valid = stop < close if action == "OPEN_LONG" else stop > close
        within_structure = (
            local_low <= stop <= close if action == "OPEN_LONG" else close <= stop <= local_high
        )
        record["stop_loss_valid"] = bool(
            stop.is_finite() and stop > 0 and side_valid and within_structure
        )
        record["stop_loss_check"] = "inside_local_extreme_range_and_correct_side_of_final_close"
    else:
        record["stop_loss_valid"] = None
        record["stop_loss_check"] = "not_applicable_no_open_action"
    return record


def _summary(records: list[dict], manifest: dict) -> dict:
    valid = [record for record in records if record.get("evaluation_valid") is True]

    def ratio(key: str, rows: list[dict]) -> dict:
        return {
            "correct": sum(row.get(key) is True for row in rows),
            "valid_denominator": len(rows),
        }

    by_case_agent = {
        (
            record.get("case_id_evaluator_only", record.get("fixture_id")),
            record.get("agent"),
        ): record
        for record in records
    }
    differences = {}
    for case_id in manifest["cases"]:
        a = by_case_agent.get((case_id, "A"), {})
        b = by_case_agent.get((case_id, "B"), {})
        differences[case_id] = {
            "A_action": a.get("decision", {}).get("action") if a.get("evaluation_valid") else None,
            "B_action": b.get("decision", {}).get("action") if b.get("evaluation_valid") else None,
            "actions_differ": bool(
                a.get("evaluation_valid")
                and b.get("evaluation_valid")
                and a.get("decision", {}).get("action") != b.get("decision", {}).get("action")
            ),
            "comparable": bool(a.get("evaluation_valid") and b.get("evaluation_valid")),
        }
    reversal_rows = [
        record
        for record in records
        if record.get("case_id_evaluator_only", record.get("fixture_id")) == "case_05"
    ]
    stop_rows = [
        record
        for record in valid
        if record.get("decision", {}).get("action") in {"OPEN_LONG", "OPEN_SHORT"}
    ]
    return {
        "json_validity": {
            "valid": sum(record.get("json_valid") is True for record in records),
            "total_calls": len(records),
        },
        "evaluation_valid_responses": len(valid),
        "global_direction": ratio("global_direction_match", valid),
        "local_direction": ratio("local_direction_match", valid),
        "structure_classification": ratio("structure_match", valid),
        "reversal_recognition": {
            "correct": sum(
                record.get("reversal_recognition_match") is True for record in reversal_rows
            ),
            "related_samples": len(reversal_rows),
            "valid_related_responses": sum(
                record.get("evaluation_valid") is True for record in reversal_rows
            ),
        },
        "stop_loss_validity": {
            "valid": sum(record.get("stop_loss_valid") is True for record in stop_rows),
            "open_decisions_requiring_stop": len(stop_rows),
            "no_open_decisions": len(stop_rows) == 0,
        },
        "a_b_action_difference": {
            "different_cases": sum(value["actions_differ"] for value in differences.values()),
            "paired_cases": sum(value["comparable"] for value in differences.values()),
            "total_cases": len(differences),
            "by_case": differences,
        },
    }


async def run() -> dict:
    attempted = 0
    cumulative_cost = Decimal(0)
    records: list[dict] = []
    adapter = None
    old_env_value = os.environ.get("DEEPSEEK_API_KEY")
    base = {
        "run_id": "adjudicated-blind-20261027",
        "requested_model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "thinking_mode": THINKING_MODE,
        "response_format": "json_object",
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "call_limit": MAX_CALLS,
        "hard_budget_usd": str(ROUND_LIMIT),
        "cost_rate_basis": "DeepSeek official peak uncached-input/output rates; all input tokens billed at uncached upper rate",
        "cost_rate_source": PRICE_SOURCE,
        "rate_input_per_million_usd": str(INPUT_RATE),
        "rate_output_per_million_usd": str(OUTPUT_RATE),
        "dataset_version": "adjudicated-v1",
        "real_trading_orders": 0,
        "paper_orders": 0,
    }
    try:
        if EVIDENCE_PATH.exists():
            return {**base, "status": "BLOCKED_EVIDENCE_ALREADY_EXISTS", "calls_attempted": 0}
        manifest, plan = _verify_frozen_dataset()
        prior_count, prior_reserved = _load_budget_rows()
        call_reservations = [_reservation(item.provider_input.prompt) for item in plan]
        worst_case_total = sum(call_reservations, Decimal(0)) + prior_reserved
        if (
            prior_count != 0
            or worst_case_total > ROUND_LIMIT
            or any(x > CALL_LIMIT for x in call_reservations)
        ):
            return {
                **base,
                "status": "BLOCKED_BUDGET_PREFLIGHT",
                "calls_attempted": 0,
                "prior_round_calls": prior_count,
                "worst_case_reserved_usd": str(worst_case_total),
            }
        if not _load_project_key():
            return {**base, "status": "BLOCKED_NO_LOCAL_KEY", "calls_attempted": 0}
        config = VisionProviderConfig(
            enabled=True,
            calls_authorized=True,
            base_url="https://api.deepseek.com",
            endpoint_path="/chat/completions",
            model="deepseek-flash",
            api_key_env="DEEPSEEK_API_KEY",
            timeout_seconds=60,
            max_concurrency=1,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_mode=THINKING_MODE,
            max_requests_per_day=MAX_CALLS,
            max_call_cost_usd=CALL_LIMIT,
            daily_budget_usd=ROUND_LIMIT,
            input_price_per_million_usd=INPUT_RATE,
            output_price_per_million_usd=OUTPUT_RATE,
            budget_db_path=str(BUDGET_DB),
        )
        adapter = ConfigurableVisionAdapter(config)
        if not adapter._api_key:
            return {**base, "status": "BLOCKED_NO_LOCAL_KEY", "calls_attempted": 0}
        for item, reserved in zip(plan, call_reservations, strict=True):
            request = item.provider_input
            request_id = "adjudicated-blind:" + str(uuid.uuid4())
            attempted += 1  # Count before I/O; a timeout may still have reached the provider.
            response = await adapter._send_image_request(
                request.image, request.prompt, request_id=request_id
            )
            diag = response.get("provider_diagnostics") or {}
            usage = response.get("usage")
            prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
            completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
            cost_known = (
                isinstance(prompt_tokens, int)
                and not isinstance(prompt_tokens, bool)
                and isinstance(completion_tokens, int)
                and not isinstance(completion_tokens, bool)
                and prompt_tokens >= 0
                and completion_tokens >= 0
            )
            estimated_cost = None
            if cost_known:
                estimated_cost = (
                    Decimal(prompt_tokens) * INPUT_RATE + Decimal(completion_tokens) * OUTPUT_RATE
                ) / Decimal(1_000_000)
                cumulative_cost += estimated_cost
            raw_decision = {field: response.get(field) for field in DECISION_SCHEMA["required"]}
            schema_error = None
            try:
                decision = validate_decision_json(raw_decision)
                schema_ok = True
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                decision = raw_decision
                schema_ok = False
                schema_error = type(exc).__name__
            model_id = response.get("model_id") or diag.get("model_id")
            model_ok = _model_id_is_deepseek_flash(model_id)
            valid_json = schema_ok and diag.get("finish_reason") != "length"
            evaluation_valid = valid_json and model_ok
            if schema_ok and not valid_json and schema_error is None:
                schema_error = "FINISH_REASON_LENGTH"
            record = {
                "request_id": request_id,
                "case_id_evaluator_only": item.fixture_id,
                "agent": request.agent,
                "layout": "v2",
                "md_version": request.md_version,
                "md_sha256": request.md_sha256,
                "image_sha256": request.image_sha256,
                "model_id": model_id,
                "model_id_match": model_ok,
                "model_fingerprint": response.get("provider_fingerprint")
                or diag.get("system_fingerprint"),
                "http_status": diag.get("http_status"),
                "finish_reason": diag.get("finish_reason"),
                "usage": usage,
                "reserved_cost_upper_usd": str(reserved),
                "usage_cost_upper_estimate_usd": str(estimated_cost)
                if estimated_cost is not None
                else None,
                "cost_basis": "all prompt tokens at peak cache-miss rate; all completion tokens at peak output rate",
                "json_valid": valid_json,
                "evaluation_valid": evaluation_valid,
                "json_error": schema_error or response.get("status"),
                "decision": decision,
                "provider_diagnostics": diag,
            }
            if response.get("status") is None and evaluation_valid:
                record = _score_record(record, item, manifest)
            else:
                record.update(
                    {
                        "global_direction_match": False,
                        "local_direction_match": False,
                        "structure_match": False,
                        "structure_bucket": "uncertain",
                    }
                )
                if item.fixture_id == "case_05":
                    record["reversal_recognition_match"] = False
                record.update(
                    {"stop_loss_valid": None, "stop_loss_check": "not_applicable_invalid_response"}
                )
            records.append(_redact(record))
            _atomic_json(
                EVIDENCE_PATH,
                {
                    **base,
                    "status": "IN_PROGRESS",
                    "calls_attempted": attempted,
                    "cumulative_cost_upper_estimate_usd": str(cumulative_cost),
                    "worst_case_reserved_usd": str(worst_case_total),
                    "records": records,
                },
            )
            if (
                response.get("status") is not None
                or not cost_known
                or prompt_tokens > len(request.prompt.encode("utf-8")) + 1280
                or completion_tokens > MAX_OUTPUT_TOKENS
                or cumulative_cost > ROUND_LIMIT
                or not model_ok
                or diag.get("finish_reason") == "length"
            ):
                break
            # A completed response with a wrong classification is evidence, not a retry signal.
        summary = _summary(records, manifest)
        final_status = (
            "COMPLETED" if attempted == MAX_CALLS and len(records) == MAX_CALLS else "PARTIAL"
        )
        result = {
            **base,
            "status": final_status,
            "started_at_utc": datetime.now(UTC).isoformat(),
            "calls_attempted": attempted,
            "completed_records": len(records),
            "cumulative_cost_upper_estimate_usd": str(cumulative_cost),
            "worst_case_reserved_usd": str(worst_case_total),
            "summary": summary,
            "records": records,
        }
        _atomic_json(EVIDENCE_PATH, result)
        return result
    except Exception as exc:
        result = {
            **base,
            "status": "FAILED_REDACTED_EXCEPTION",
            "error_class": type(exc).__name__,
            "calls_attempted": attempted,
            "cumulative_cost_upper_estimate_usd": str(cumulative_cost),
            "records": records,
        }
        _atomic_json(EVIDENCE_PATH, result)
        return result
    finally:
        if adapter is not None:
            adapter._api_key = ""
            adapter._budget._db.close()
        if old_env_value is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = old_env_value


if __name__ == "__main__":
    result = asyncio.run(run())
    print(
        json.dumps(
            {
                "status": result.get("status"),
                "calls_attempted": result.get("calls_attempted", 0),
                "completed_records": result.get("completed_records", 0),
                "cumulative_cost_upper_estimate_usd": result.get(
                    "cumulative_cost_upper_estimate_usd"
                ),
                "worst_case_reserved_usd": result.get("worst_case_reserved_usd"),
                "evidence_path": str(EVIDENCE_PATH) if EVIDENCE_PATH.is_file() else None,
                "summary": result.get("summary"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
