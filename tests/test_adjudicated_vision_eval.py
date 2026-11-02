from tests.run_adjudicated_vision_eval import (
    CALL_LIMIT,
    ROUND_LIMIT,
    _confirmed_reversal_recognized,
    _direction_bucket,
    _model_id_is_deepseek_flash,
    _reservation,
    _scored_structure_bucket,
    _summary,
)
from tests.vision_adjudicated_plan import build_adjudicated_eight_case_plan


def test_official_flash_alias_and_version_identifiers_are_accepted():
    assert _model_id_is_deepseek_flash("deepseek-flash")
    assert _model_id_is_deepseek_flash("deepseek-v4.1-flash-1017")
    assert not _model_id_is_deepseek_flash("deepseek-v4-pro")
    assert not _model_id_is_deepseek_flash(None)


def test_reversal_scorer_does_not_count_negated_or_unconfirmed_mentions():
    assert (
        _scored_structure_bucket("The local chart shows a confirmed reversal.")
        == "reversal_structure"
    )
    assert _confirmed_reversal_recognized("The local chart shows a confirmed reversal.")
    assert (
        _scored_structure_bucket("Global trend continues; this is not a confirmed reversal.")
        == "trend_structure"
    )
    assert not _confirmed_reversal_recognized(
        "Global trend continues; this is not a confirmed reversal."
    )
    assert not _confirmed_reversal_recognized("A possible reversal is not yet confirmed.")


def test_direction_and_structure_scorers_handle_plain_up_and_composite_moves():
    assert _direction_bucket("up") == "rising"
    assert _direction_bucket("down then up (V-shaped recovery)") == "mixed"
    assert _direction_bucket("ranging") == "sideways"
    assert (
        _scored_structure_bucket("Both panels show steady higher highs and higher lows.")
        == "trend_structure"
    )
    assert _scored_structure_bucket("A clean rising channel forms locally.") == "trend_structure"


def test_eight_worst_case_reservations_fit_both_hard_limits():
    plan = build_adjudicated_eight_case_plan()
    reservations = [_reservation(case.provider_input.prompt) for case in plan]
    assert len(reservations) == 8
    assert all(amount <= CALL_LIMIT for amount in reservations)
    assert sum(reservations) <= ROUND_LIMIT


def test_metrics_keep_json_validity_separate_from_scored_validity():
    records = [
        {
            "fixture_id": "case_01",
            "agent": "A",
            "json_valid": True,
            "evaluation_valid": True,
            "global_direction_match": True,
            "local_direction_match": False,
            "structure_match": True,
            "stop_loss_valid": None,
            "decision": {"action": "WAIT"},
        },
        {
            "fixture_id": "case_02",
            "agent": "A",
            "json_valid": False,
            "evaluation_valid": False,
            "global_direction_match": False,
            "local_direction_match": False,
            "structure_match": False,
            "stop_loss_valid": None,
            "decision": {},
        },
    ]
    manifest = {"cases": {"case_01": {}, "case_02": {}}}
    summary = _summary(records, manifest)
    assert summary["json_validity"] == {"valid": 1, "total_calls": 2}
    assert summary["global_direction"] == {"correct": 1, "valid_denominator": 1}
    assert summary["local_direction"] == {"correct": 0, "valid_denominator": 1}
    assert summary["structure_classification"] == {"correct": 1, "valid_denominator": 1}
    assert summary["a_b_action_difference"]["paired_cases"] == 0
