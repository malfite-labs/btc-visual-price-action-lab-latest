from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from backend.evolution import Metrics, ReviewCoordinator


def pair(candidate, incumbent):
    return dict(
        candidate=candidate,
        incumbent=incumbent,
        candidate_initial_equity=D("10000"),
        incumbent_initial_equity=D("10000"),
        candidate_model_id="vision-v1",
        incumbent_model_id="vision-v1",
        candidate_cost_hash="cost-hash-a",
        incumbent_cost_hash="cost-hash-a",
        candidate_market_event_ids=["q1", "q2", "q3"],
        incumbent_market_event_ids=["q1", "q2", "q3"],
    )


def test_shadow_checkpoint_promotion_waits_for_flat_and_new_cycle(tmp_path):
    coord = ReviewCoordinator(tmp_path / "shadow.sqlite")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert coord.start_candidate("A", "candidate-1", start)["accepted"]
    winner = Metrics(D(200), D(2), D("0.04"), 20)
    incumbent = Metrics(D(100), D(1), D("0.05"), 20)
    result = coord.evaluate_candidate_checkpoint(
        "A", 7, now=start + timedelta(days=7), old_position_open=True, **pair(winner, incumbent)
    )
    assert result["outcome"] == "PROMOTION_PENDING"
    assert (
        coord.start_candidate("A", "candidate-2", start + timedelta(days=8))["reason"]
        == "CANDIDATE_SLOT_OCCUPIED"
    )
    assert (
        coord.activate_after_flat("A", old_position_open=True, at_new_cycle=True)["status"]
        == "WAITING_OLD_POSITION"
    )
    assert (
        coord.activate_after_flat("A", old_position_open=False, at_new_cycle=False)["status"]
        == "WAITING_NEW_CYCLE"
    )
    assert (
        coord.activate_after_flat("A", old_position_open=False, at_new_cycle=True)["status"]
        == "PROMOTED"
    )
    assert coord.status("A")["candidate"][1] == "PROMOTED"


def test_shadow_requires_paired_model_cost_market_and_equity(tmp_path):
    coord = ReviewCoordinator(tmp_path / "shadow.sqlite")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    coord.start_candidate("B", "candidate-1", start)
    result = coord.evaluate_candidate_checkpoint(
        "B",
        7,
        now=start + timedelta(days=7),
        old_position_open=False,
        **{
            **pair(Metrics(D(200), D(2), D("0.04"), 20), Metrics(D(100), D(1), D("0.05"), 20)),
            "candidate_model_id": "different-model",
        },
    )
    assert result["outcome"] == "PAIRING_MISMATCH"
    assert coord.status("B")["candidate"][1] == "RUNNING"


def test_shadow_checkpoint_minimum_samples_and_day30_expiry(tmp_path):
    coord = ReviewCoordinator(tmp_path / "shadow.sqlite")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    coord.start_candidate("B", "candidate-1", start)
    low = Metrics(D(100), D(2), D("0.01"), 19)
    old = Metrics(D(50), D(1), D("0.02"), 20)
    result = coord.evaluate_candidate_checkpoint(
        "B", 7, now=start + timedelta(days=7), old_position_open=False, **pair(low, old)
    )
    assert result["outcome"] == "INSUFFICIENT_SAMPLE"
    late = coord.evaluate_candidate_checkpoint(
        "B", 30, now=start + timedelta(days=30), old_position_open=False, **pair(low, old)
    )
    assert late["outcome"] == "EXPIRED"
    assert coord.status("B")["candidate"][1] == "EXPIRED"


def test_shadow_recovery_is_limited_per_fixed_checkpoint(tmp_path):
    coord = ReviewCoordinator(tmp_path / "shadow.sqlite")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    coord.start_candidate("A", "candidate-1", start)
    assert coord.record_checkpoint_recovery("A", 7)["accepted"]
    assert not coord.record_checkpoint_recovery("A", 7)["accepted"]
    assert coord.record_checkpoint_recovery("A", 14)["accepted"]
    assert coord.status("A")["candidate"][2] == start.isoformat()
