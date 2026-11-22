from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from backend.evolution import (
    Metrics,
    ReviewCoordinator,
    candidate_age_expired,
    checkpoint_days,
    compare,
    performance_metrics,
    recovery_allowed,
)
from backend.strategy import StrategyRegistry


def test_metrics_and_checkpoints():
    m = performance_metrics(
        D(1000), [D(1000), D(1050), D(980), D(1030)], [D(40), D(-20), D(10), D(-10)]
    )
    assert m.net_return == D(30) and m.avg_win_loss_ratio == D(25) / D(15)
    assert m.max_drawdown_fraction == D(70) / D(1050)
    t = datetime(2026, 1, 1, tzinfo=UTC)
    assert checkpoint_days(t, t + timedelta(days=21)) == [7, 14, 21]
    assert candidate_age_expired(t, t + timedelta(days=30))


def test_evolution_requires_samples_and_all_metrics():
    candidate = Metrics(D(50), D(2), D("0.05"), 20)
    old = Metrics(D(20), D("1.5"), D("0.05"), 20)
    assert compare(candidate, old)[0]
    assert compare(Metrics(D(60), D(3), D("0.01"), 19), old)[1] == "INSUFFICIENT_SAMPLE"
    assert compare(Metrics(D(60), None, D("0.01"), 20), old)[1] == "WIN_LOSS_RATIO_UNDEFINED"


def test_c_not_configured_until_user_md_import(tmp_path):
    r = StrategyRegistry(tmp_path / "strategies")
    assert r.list()[2]["status"] == "C_NOT_CONFIGURED"
    assert not r.import_c("# My rules")["accepted"]
    content = "# C用户策略\n\n## 核心操盘理念\n" + ("用户原始理念与边界。" * 30)
    result = r.import_c(content)
    assert result["accepted"] and r.list()[2]["core_lock"] == "PROTECTED_PENDING_USER_APPROVAL"


def test_50_trade_queue_waits_for_single_candidate_slot(tmp_path):
    coord = ReviewCoordinator(tmp_path / "evo.sqlite")
    for n in range(49):
        assert not coord.record_completed_trade("B", f"t-{n}")["queued"]
    coord.start_candidate("B", "candidate-1")
    result = coord.record_completed_trade("B", "t-49")
    assert result["queued"] and coord.status("B")["reviews"][0][1] == "QUEUED"
    assert not coord.start_candidate("B", "candidate-2")["accepted"]
    coord.mark_candidate_finished("B", "REJECTED")
    assert coord.status("B")["reviews"][0][1] == "READY_FOR_REVIEW"
    assert recovery_allowed(0) and not recovery_allowed(1)
