import sqlite3
from decimal import Decimal

from tests.run_real_vision_eval import (
    add_external_call_reservation,
    count_prior_eval_attempts,
    persist_run_evidence,
    redact,
    reserve_authorized_call,
    select_pending_evaluations,
    structure_bucket,
    trend_bucket,
    trend_matches,
)


def _database(path):
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE vision_eval_authorized_calls(request_id TEXT PRIMARY KEY, reserved_usd TEXT NOT NULL, status TEXT NOT NULL)"
        )


def test_authorized_vision_evaluation_budget_caps_lifetime_call_count(tmp_path):
    path = tmp_path / "eval-budget.sqlite3"
    _database(path)
    for n in range(12):
        assert reserve_authorized_call(path, f"request-{n}", Decimal("0.005"))
    assert not reserve_authorized_call(path, "request-13", Decimal("0.001"))


def test_authorized_vision_evaluation_budget_caps_total_reserved_cost(tmp_path):
    path = tmp_path / "eval-budget.sqlite3"
    _database(path)
    assert reserve_authorized_call(path, "request-1", Decimal("0.06"))
    assert not reserve_authorized_call(path, "request-2", Decimal("0.05"))
    assert reserve_authorized_call(path, "request-3", Decimal("0.04"))


def test_result_redaction_preserves_price_structure_fields_but_removes_credentials():
    result = redact(
        {
            "decision": {"key_highs": [101.0], "key_lows": [99.0]},
            "api_key": "test-secret",
            "secret_key": "test-secret",
        }
    )
    assert result["decision"] == {"key_highs": [101.0], "key_lows": [99.0]}
    assert "api_key" not in result and "secret_key" not in result


def test_paid_call_evidence_is_atomically_persisted_and_secret_filtered(tmp_path):
    path = tmp_path / "vision-eval-run.json"
    persist_run_evidence(
        path,
        {
            "calls_attempted": 1,
            "records": [{"request_id": "r1", "provider_diagnostics": {"http_status": 200}}],
            "debug_api_key": "never-store",
            "body": "sk-" + "0123456789abcdefghijklmnopqrst",
        },
    )
    saved = path.read_text(encoding="utf-8")
    assert '"http_status": 200' in saved
    assert "never-store" not in saved
    assert ("sk-" + "0123456789abcdefghijklmnopqrst") not in saved
    assert not path.with_suffix(".json.tmp").exists()


def test_restarted_runner_counts_attempts_across_all_evidence_files(tmp_path):
    (tmp_path / "vision-eval-run.json").write_text('{"calls_attempted": 1}', encoding="utf-8")
    (tmp_path / "vision-eval-run-20261027T050000Z.json").write_text(
        '{"calls_attempted": 5}', encoding="utf-8"
    )
    assert count_prior_eval_attempts(tmp_path) == 6


def test_prior_attempt_count_uses_budget_ledger_without_double_counting_run_files(tmp_path):
    (tmp_path / "vision-eval-run.json").write_text('{"calls_attempted": 6}', encoding="utf-8")
    ledger = tmp_path / "vision-eval-budget.sqlite3"
    _database(ledger)
    for n in range(7):
        reserve_authorized_call(ledger, f"recorded-{n}", Decimal("0.001"))

    assert count_prior_eval_attempts(tmp_path, ledger) == 7


def test_external_capability_probe_is_counted_once_and_reserves_its_cost(tmp_path):
    ledger = tmp_path / "vision-eval-budget.sqlite3"
    _database(ledger)
    amount = Decimal("0.0002133")

    assert add_external_call_reservation(ledger, "capability-probe-20261027", amount)
    assert not add_external_call_reservation(ledger, "capability-probe-20261027", amount)
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT COUNT(*) FROM vision_eval_authorized_calls").fetchone()[0] == 1
        assert (
            Decimal(
                db.execute("SELECT reserved_usd FROM vision_eval_authorized_calls").fetchone()[0]
            )
            == amount
        )


def test_pending_selection_maps_legacy_success_by_image_hash(tmp_path):
    from tests.vision_eval_runner import build_preflight_plan

    prepared, _ = build_preflight_plan()
    first = prepared[0]
    (tmp_path / "vision-eval-run-legacy.json").write_text(
        '{"calls_attempted":1,"records":[{"agent":"A","repetition":1,'
        '"fixture_sha256":"' + first.provider_input.image_sha256 + '","json_valid":true}]}',
        encoding="utf-8",
    )

    pending = select_pending_evaluations(prepared, tmp_path, 4)

    assert all(
        (item.fixture_id, item.provider_input.agent, item.repetition) != ("case_01", "A", 1)
        for item in pending
    )
    assert [(item.fixture_id, item.provider_input.agent) for item in pending] == [
        ("case_01", "B"),
        ("case_02", "A"),
        ("case_02", "B"),
        ("case_03", "A"),
    ]


def test_restarted_runner_fails_closed_on_unreadable_attempt_history(tmp_path):
    (tmp_path / "vision-eval-run-damaged.json").write_text("not json", encoding="utf-8")
    try:
        count_prior_eval_attempts(tmp_path)
    except ValueError as exc:
        assert str(exc) == "PRIOR_EVALUATION_EVIDENCE_INVALID"
    else:
        raise AssertionError("corrupt prior evidence must block further paid calls")


def test_structure_bucket_is_explicit_and_unknown_is_not_scored_as_correct():
    assert structure_bucket("clear downtrend with lower highs") == "trend_structure"
    assert structure_bucket("range-bound sideways candles") == "range_or_wait"
    assert structure_bucket("bullish reversal") == "reversal_structure"
    assert structure_bucket("structure unclear") == "uncertain"


def test_trend_scorer_prioritizes_explicit_bias_over_incidental_range_words():
    assert (
        trend_bucket("Strong uptrend; highs near the top of the range, no lower-high structure.")
        == "rising"
    )
    assert (
        trend_bucket("Bearish sustained lower highs; at the bottom of the visible range.")
        == "falling"
    )
    assert trend_matches("rising_reversal", "Rising reversal after a prolonged decline.")


def test_structure_scorer_does_not_mistake_negated_reversal_for_reversal():
    assert (
        structure_bucket("Trend continuation with no completed reversal signature.")
        == "trend_structure"
    )
    assert structure_bucket("Bullish reversal structure after a decline.") == "reversal_structure"
