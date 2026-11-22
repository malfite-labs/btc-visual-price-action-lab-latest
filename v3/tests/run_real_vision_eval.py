"""Explicitly invoked, bounded DeepSeek visual evaluation; never imported by pytest."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from backend.vision_prompt import DECISION_SCHEMA, validate_decision_json
from backend.vision_provider import ConfigurableVisionAdapter, VisionProviderConfig
from tests.vision_eval_runner import ANSWER_FIELDS, build_preflight_plan

ROOT = Path(__file__).resolve().parents[1]
RATE_INPUT = Decimal("0.30")
RATE_OUTPUT = Decimal("1.20")
MAX_CALLS = 12
DAILY_LIMIT = Decimal("0.10")
MAX_CALL_LIMIT = Decimal("0.05")
MAX_OUTPUT_TOKENS = 1000
SECRET_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")
CAPABILITY_PROBE_ID = "deepseek-capability-probe-20261027"


def trend_bucket(value: str) -> str:
    text = value.casefold()
    positive = any(word in text for word in ("rising", "uptrend", "bullish", "upward", "上涨"))
    negative = any(word in text for word in ("falling", "downtrend", "bearish", "downward", "下跌"))
    if positive and not negative:
        return "rising"
    if negative and not positive:
        return "falling"
    if positive and negative:
        # Explicit bias terms take precedence over incidental mentions of the opposite structure.
        first_positive = min(
            text.find(word) for word in ("rising", "uptrend", "bullish", "上涨") if word in text
        )
        first_negative = min(
            text.find(word) for word in ("falling", "downtrend", "bearish", "下跌") if word in text
        )
        return "rising" if first_positive < first_negative else "falling"
    if any(word in text for word in ("sideways", "range", "ranging", "consolidat", "横盘", "震荡")):
        return "sideways"
    if "higher highs" in text and "higher lows" in text:
        return "rising"
    if "lower highs" in text and "lower lows" in text:
        return "falling"
    return "uncertain"


def trend_matches(expected: str, actual: str) -> bool:
    bucket = trend_bucket(actual)
    if expected == "rising_reversal":
        text = actual.casefold()
        has_rising_signal = any(
            w in text for w in ("rising", "uptrend", "bullish", "upward", "上涨")
        )
        has_reversal_signal = any(w in text for w in ("reversal", "turn", "recover", "反转"))
        return has_rising_signal and has_reversal_signal
    return bucket == expected


def estimate_reservation(prompt: str) -> Decimal:
    input_ceiling = len(prompt.encode("utf-8")) + 1280
    return (
        Decimal(input_ceiling) * RATE_INPUT + Decimal(MAX_OUTPUT_TOKENS) * RATE_OUTPUT
    ) / Decimal(1_000_000)


def reserve_authorized_call(
    db_path: Path, request_id: str, amount: Decimal, *, max_calls: int = MAX_CALLS
) -> bool:
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        count = db.execute("SELECT COUNT(*) FROM vision_eval_authorized_calls").fetchone()[0]
        total = sum(
            (
                Decimal(row[0])
                for row in db.execute(
                    "SELECT reserved_usd FROM vision_eval_authorized_calls"
                ).fetchall()
            ),
            Decimal(0),
        )
        if count >= max_calls or total + amount > DAILY_LIMIT:
            db.rollback()
            return False
        try:
            db.execute(
                "INSERT INTO vision_eval_authorized_calls VALUES(?,?,?)",
                (request_id, str(amount), "RESERVED"),
            )
        except sqlite3.IntegrityError:
            db.rollback()
            return False
        db.commit()
    return True


def seed_previous_call_budgets(budget_db_path: Path) -> None:
    """Carry old paid-call reservations into this evaluation's lifetime hard cap."""
    with sqlite3.connect(budget_db_path, timeout=10) as target:
        target.execute(
            "CREATE TABLE IF NOT EXISTS vision_budget_calls (request_id TEXT PRIMARY KEY, utc_day TEXT NOT NULL, reserved_usd TEXT NOT NULL, status TEXT NOT NULL)"
        )
        target.execute(
            "CREATE TABLE IF NOT EXISTS vision_eval_authorized_calls(request_id TEXT PRIMARY KEY, reserved_usd TEXT NOT NULL, status TEXT NOT NULL)"
        )
        for source_path in (ROOT / "data").glob("vision-eval-*.sqlite3"):
            if source_path.resolve() == budget_db_path.resolve():
                continue
            try:
                with sqlite3.connect(source_path) as source:
                    rows = source.execute(
                        "SELECT request_id,utc_day,reserved_usd,status FROM vision_budget_calls"
                    ).fetchall()
            except sqlite3.Error:
                continue
            for request_id, utc_day, amount, status in rows:
                target.execute(
                    "INSERT OR IGNORE INTO vision_budget_calls(request_id,utc_day,reserved_usd,status) VALUES(?,?,?,?)",
                    (request_id, utc_day, amount, status),
                )
                target.execute(
                    "INSERT OR IGNORE INTO vision_eval_authorized_calls VALUES(?,?,?)",
                    (request_id, amount, status),
                )
        target.commit()


def redact(value):
    if isinstance(value, str):
        return SECRET_RE.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): redact(item)
            for key, item in value.items()
            if not any(
                sensitive in str(key).casefold()
                for sensitive in ("api_key", "secret", "authorization", "password")
            )
        }
    return value


def persist_run_evidence(path: Path, payload: dict) -> None:
    """Atomically persist a secret-filtered snapshot after each paid attempt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def count_prior_eval_attempts(data_dir: Path, budget_db_path: Path | None = None) -> int:
    """Count durable attempts and evidence without double-counting overlapping records."""
    total = 0
    for path in data_dir.glob("vision-eval-run*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            attempts = int(payload.get("calls_attempted", 0))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError("PRIOR_EVALUATION_EVIDENCE_INVALID") from exc
        if attempts < 0:
            raise ValueError("PRIOR_EVALUATION_EVIDENCE_INVALID")
        total += attempts
    if budget_db_path is None or not budget_db_path.is_file():
        return total
    try:
        with sqlite3.connect(budget_db_path) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vision_eval_authorized_calls'"
            ).fetchone()
            if not table:
                return total
            reserved_attempts = int(
                db.execute("SELECT COUNT(*) FROM vision_eval_authorized_calls").fetchone()[0]
            )
    except sqlite3.Error as exc:
        raise ValueError("PRIOR_EVALUATION_EVIDENCE_INVALID") from exc
    return max(total, reserved_attempts)


def add_external_call_reservation(db_path: Path, request_id: str, amount: Decimal) -> bool:
    """Durably count an externally evidenced provider call exactly once."""
    if amount <= 0:
        raise ValueError("EXTERNAL_CALL_COST_INVALID")
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS vision_eval_authorized_calls(request_id TEXT PRIMARY KEY, reserved_usd TEXT NOT NULL, status TEXT NOT NULL)"
        )
        try:
            db.execute(
                "INSERT INTO vision_eval_authorized_calls VALUES(?,?,?)",
                (request_id, str(amount), "EXTERNAL_CALL_EVIDENCED"),
            )
        except sqlite3.IntegrityError:
            return False
        db.commit()
    return True


def completed_evaluation_keys(data_dir: Path, prepared) -> set[tuple[str, str, int]]:
    """Only skip successful fixture/agent/repetition tuples; failures remain eligible once."""
    completed: set[tuple[str, str, int]] = set()
    fixture_by_hash = {item.provider_input.image_sha256: item.fixture_id for item in prepared}
    for path in data_dir.glob("vision-eval-run*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload.get("records", [])
            if not isinstance(records, list):
                raise ValueError
            for record in records:
                if not isinstance(record, dict) or record.get("json_valid") is not True:
                    continue
                fixture_id = record.get("fixture_id")
                if not isinstance(fixture_id, str):
                    fixture_id = fixture_by_hash.get(record.get("fixture_sha256"))
                agent = record.get("agent")
                repetition = record.get("repetition", 1)
                if (
                    isinstance(fixture_id, str)
                    and agent in {"A", "B"}
                    and isinstance(repetition, int)
                ):
                    completed.add((fixture_id, agent, repetition))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError("PRIOR_EVALUATION_EVIDENCE_INVALID") from exc
    return completed


def select_pending_evaluations(prepared, data_dir: Path, limit: int):
    completed = completed_evaluation_keys(data_dir, prepared)
    pending = [
        item
        for item in prepared
        if (item.fixture_id, item.provider_input.agent, item.repetition) not in completed
    ]
    return pending[:limit]


def structure_bucket(value: str) -> str:
    text = re.split(r"[.!?;]", value.casefold(), maxsplit=1)[0]
    affirmative_text = re.sub(
        r"\b(?:no|not|without|rather than|lack of|absence of)\b[^.;,]{0,100}\b(?:reversal|反转)\b",
        "",
        text,
    )
    if any(term in affirmative_text for term in ("reversal", "反转")):
        return "reversal_structure"
    if any(term in text for term in ("continuation", "trend", "趋势")):
        return "trend_structure"
    if any(term in text for term in ("range", "sideways", "震荡", "横盘")):
        return "range_or_wait"
    return "uncertain"


async def run() -> dict:
    evidence_path = (
        ROOT
        / "data"
        / ("vision-eval-run-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json")
    )
    budget_path = ROOT / "data" / "vision-eval-budget.sqlite3"
    seed_previous_call_budgets(budget_path)
    probe_report = ROOT / "docs" / "reports" / "DEEPSEEK_REAL_VISION_TEST_2026-10-27.json"
    if probe_report.is_file():
        try:
            probe = json.loads(probe_report.read_text(encoding="utf-8"))
            probe_count = int(probe.get("actual_api_calls", 0))
            probe_cost = Decimal(str(probe.get("actual_cost_upper_estimate_usd", "0")))
            if probe_count < 0 or (probe_count and probe_cost <= 0):
                raise ValueError
            if probe_count and not add_external_call_reservation(
                budget_path, CAPABILITY_PROBE_ID, probe_cost
            ):
                pass
        except (OSError, ValueError, TypeError, AttributeError):
            return {"status": "BLOCKED_EXTERNAL_CALL_EVIDENCE_INVALID", "calls": 0}
    try:
        prior_attempts = count_prior_eval_attempts(ROOT / "data", budget_path)
    except ValueError:
        return {"status": "BLOCKED_PRIOR_EVIDENCE_INVALID", "calls": 0}
    remaining_calls = max(0, MAX_CALLS - prior_attempts)
    if remaining_calls == 0:
        return {"status": "BLOCKED_AUTHORIZED_CALL_LIMIT_REACHED", "calls": 0}
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        for line in (
            (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines()
            if (ROOT / ".env").is_file()
            else []
        ):
            name, sep, value = line.partition("=")
            if sep and name.strip() == "DEEPSEEK_API_KEY":
                os.environ.setdefault("DEEPSEEK_API_KEY", value.strip().strip("\"'"))
                break
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        return {"status": "BLOCKED_NO_CREDENTIAL", "calls": 0}
    prepared, preflight = build_preflight_plan(ROOT)
    if len(prepared) != MAX_CALLS or any(not all(row["checks"].values()) for row in preflight):
        return {
            "status": "BLOCKED_PREFLIGHT",
            "calls": 0,
            "preflight_errors": sum(bool(row["errors"]) for row in preflight),
        }
    if any(
        any(field in item.provider_input.prompt for field in ANSWER_FIELDS) for item in prepared
    ):
        # The field names are checked only as a narrow backstop; actual semantic leakage
        # inspection remains the dedicated fixture preflight predicate.
        if any(row["checks"].get("LEAK-01") is False for row in preflight):
            return {"status": "BLOCKED_LABEL_LEAK", "calls": 0}

    selected = select_pending_evaluations(prepared, ROOT / "data", remaining_calls)
    if len(selected) < remaining_calls:
        remaining_calls = len(selected)
    with sqlite3.connect(budget_path) as db:
        reserved_before = sum(
            (
                Decimal(row[0])
                for row in db.execute(
                    "SELECT reserved_usd FROM vision_eval_authorized_calls"
                ).fetchall()
            ),
            Decimal(0),
        )
    worst_case = reserved_before + sum(
        (estimate_reservation(item.provider_input.prompt) for item in selected), Decimal(0)
    )
    if (
        not selected
        or worst_case > DAILY_LIMIT
        or any(estimate_reservation(x.provider_input.prompt) > MAX_CALL_LIMIT for x in selected)
    ):
        return {"status": "BLOCKED_BUDGET_PREFLIGHT", "calls": 0, "worst_case_usd": str(worst_case)}

    with sqlite3.connect(budget_path) as db:
        prior_calls = db.execute("SELECT COUNT(*) FROM vision_eval_authorized_calls").fetchone()[0]
    per_run_call_limit = prior_calls + remaining_calls

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
        thinking_mode="disabled",
        max_requests_per_day=per_run_call_limit,
        max_call_cost_usd=MAX_CALL_LIMIT,
        daily_budget_usd=DAILY_LIMIT,
        input_price_per_million_usd=RATE_INPUT,
        output_price_per_million_usd=RATE_OUTPUT,
        budget_db_path=str(budget_path),
    )
    adapter = ConfigurableVisionAdapter(config)
    manifest = json.loads(
        (ROOT / "tests/fixtures/vision/ground_truth_manifest.json").read_text(encoding="utf-8")
    )
    if (
        manifest.get("market_data") is not False
        or manifest.get("bars_per_image") != {"global_3d": 288, "local_1d": 96}
        or len(manifest.get("cases", {})) != 4
    ):
        return {"status": "BLOCKED_FIXTURE_MANIFEST_INVALID", "calls": 0}
    records = []
    stopped = None
    actual_total = Decimal(0)
    cost_unknown = False
    attempted = 0
    for item in selected:
        request = item.provider_input
        request_id = "vision-eval:" + str(uuid.uuid4())
        reservation = estimate_reservation(request.prompt)
        if not reserve_authorized_call(
            Path(config.budget_db_path), request_id, reservation, max_calls=per_run_call_limit
        ):
            stopped = "AUTHORIZED_CALL_OR_BUDGET_LIMIT"
            break
        response = await adapter._send_image_request(
            request.image,
            request.prompt,
            request_id=request_id,
        )
        attempted += 1
        if response.get("status"):
            stopped = response["status"]
            diagnostics = response.get("provider_diagnostics") or {}
            known_cost = diagnostics.get("estimated_cost_usd")
            if known_cost is None:
                cost_unknown = True
            else:
                actual_total += Decimal(str(known_cost))
            with sqlite3.connect(config.budget_db_path) as db:
                db.execute(
                    "UPDATE vision_eval_authorized_calls SET status=? WHERE request_id=?",
                    (stopped, request_id),
                )
                db.commit()
            records.append(
                {
                    "request_id": request_id,
                    "agent": request.agent,
                    "fixture_id": item.fixture_id,
                    "repetition": item.repetition,
                    "fixture_sha256": request.image_sha256,
                    "md_version": request.md_version,
                    "md_sha256": request.md_sha256,
                    "requested_max_tokens": MAX_OUTPUT_TOKENS,
                    "requested_thinking_mode": "disabled",
                    "status": stopped,
                    "provider_diagnostics": diagnostics,
                    "known_cost_usd": known_cost,
                    "actual_cost_status": "UNKNOWN" if cost_unknown else "ESTIMATED_FROM_USAGE",
                    "json_valid": False,
                }
            )
            persist_run_evidence(
                evidence_path,
                {
                    "status": stopped,
                    "requested_model": "deepseek-flash",
                    "calls_attempted": attempted,
                    "actual_cost_usd": "UNKNOWN" if cost_unknown else str(actual_total),
                    "records": records,
                },
            )
            break
        decision = {key: response.get(key) for key in DECISION_SCHEMA["required"]}
        try:
            decision = validate_decision_json(decision)
            schema_valid = True
            schema_error = None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            schema_valid = False
            schema_error = type(exc).__name__
        if schema_valid and response.get("model_id") == "deepseek-flash":
            marker_path = ROOT / "data" / "vision-capability.json"
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker = {
                "verified": True,
                "model": "deepseek-flash",
                "endpoint": "https://api.deepseek.com/chat/completions",
                "verified_at_utc": datetime.now(UTC).isoformat(),
                "image_sha256": request.image_sha256,
                "evidence": "successful real image request with valid decision JSON",
            }
            temporary_marker = marker_path.with_suffix(".json.tmp")
            temporary_marker.write_text(json.dumps(marker, indent=2), encoding="utf-8")
            temporary_marker.replace(marker_path)
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        cost_text, cost_basis = adapter._estimate_usage_cost(usage)
        if cost_text is None:
            stopped = "USAGE_OR_COST_UNVERIFIABLE"
            with sqlite3.connect(config.budget_db_path) as db:
                db.execute(
                    "UPDATE vision_eval_authorized_calls SET status=? WHERE request_id=?",
                    (stopped, request_id),
                )
                db.commit()
            records.append(
                {"agent": request.agent, "fixture_sha256": request.image_sha256, "status": stopped}
            )
            break
        actual_cost = Decimal(cost_text)
        actual_total += actual_cost
        with sqlite3.connect(config.budget_db_path) as db:
            db.execute(
                "UPDATE vision_eval_authorized_calls SET status=? WHERE request_id=?",
                ("RESPONSE_RECEIVED", request_id),
            )
            db.commit()
        if actual_total > DAILY_LIMIT:
            stopped = "ACTUAL_COST_CAP_EXCEEDED"
            break
        truth = item.score_key
        observation = {
            "agent": request.agent,
            "fixture_id": item.fixture_id,
            "fixture_sha256": request.image_sha256,
            "md_version": request.md_version,
            "md_sha256": request.md_sha256,
            "requested_max_tokens": MAX_OUTPUT_TOKENS,
            "requested_thinking_mode": "disabled",
            "model_id": response.get("model_id"),
            "model_fingerprint": response.get("provider_fingerprint"),
            "usage": usage,
            "actual_cost_usd": cost_text,
            "cost_basis": cost_basis,
            "json_valid": schema_valid,
            "json_error": schema_error,
            "decision": redact(decision),
            "global_trend_match": trend_matches(
                truth["global_bias"], decision.get("global_trend", "")
            )
            if schema_valid
            else False,
            "local_trend_match": trend_matches(truth["local_bias"], decision.get("local_trend", ""))
            if schema_valid
            else False,
            "structure_report": redact(decision.get("structure")) if schema_valid else None,
            "structure_bucket": structure_bucket(decision.get("structure", ""))
            if schema_valid
            else "uncertain",
            "structure_match": structure_bucket(decision.get("structure", ""))
            == truth["assessment"]
            if schema_valid
            else False,
            "assessment_reference": truth["assessment"],
            "repetition": item.repetition,
            "request_id": request_id,
            "provider_diagnostics": response.get("provider_diagnostics"),
        }
        records.append(observation)
        persist_run_evidence(
            evidence_path,
            {
                "status": "IN_PROGRESS",
                "requested_model": "deepseek-flash",
                "calls_attempted": attempted,
                "actual_cost_usd": "UNKNOWN" if cost_unknown else str(actual_total),
                "records": records,
            },
        )
        if not schema_valid:
            # An invalid structured response is an evaluation failure; no retry is issued.
            stopped = "STRUCTURED_DECISION_INVALID"
            break
        if response.get("model_id") != "deepseek-flash":
            stopped = "MODEL_ID_MISMATCH"
            break

    result = {
        "status": "COMPLETED"
        if len(records) == MAX_CALLS and prior_attempts == 0 and stopped is None
        else (stopped or "PARTIAL"),
        "started_at_utc": datetime.now(UTC).isoformat(),
        "requested_model": "deepseek-flash",
        "calls_attempted": attempted,
        "records": records,
        "actual_cost_usd": "UNKNOWN" if cost_unknown else str(actual_total),
        "known_cost_subtotal_usd": str(actual_total),
        "worst_case_reserved_per_call_usd": [
            str(estimate_reservation(x.provider_input.prompt)) for x in prepared
        ],
        "worst_case_reservation_total_usd": str(worst_case),
        "hard_budget_usd": str(DAILY_LIMIT),
        "max_calls": MAX_CALLS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "thinking_mode": "disabled",
        "prior_attempts_counted_toward_authorization": prior_attempts,
        "remaining_authorized_calls": remaining_calls,
        "status_reason": stopped,
    }
    persist_run_evidence(evidence_path, result)
    return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
