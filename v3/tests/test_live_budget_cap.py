import sqlite3
from decimal import Decimal

from backend.vision_provider import VisionBudget


def test_lifetime_cap_survives_date_change_and_reopen(tmp_path):
    path = tmp_path / "budget.sqlite"
    a = VisionBudget(str(path))
    assert a.reserve("first", Decimal("2.75"), Decimal("5"), 20, total_limit=Decimal("5"))[0]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE vision_budget_calls SET utc_day='2000-01-01' WHERE request_id='first'")
    b = VisionBudget(str(path))
    assert not b.reserve("second", Decimal("2.26"), Decimal("5"), 20, total_limit=Decimal("5"))[0]
    assert b.reserve("third", Decimal("2.25"), Decimal("5"), 20, total_limit=Decimal("5"))[0]
    assert not b.reserve("fourth", Decimal("0.01"), Decimal("5"), 20, total_limit=Decimal("100"))[0]
    assert not b.reserve("fifth", Decimal("0.01"), Decimal("5"), 20)[0]


def test_budget_reports_known_usage_and_preserves_unknown_reservations(tmp_path):
    budget = VisionBudget(str(tmp_path / "budget.sqlite"))
    budget.bind_total_limit(Decimal("5"))
    assert budget.reserve("known", Decimal("0.10"), Decimal("5"), 20, total_limit=Decimal("5"))[0]
    assert budget.reserve("unknown", Decimal("0.20"), Decimal("5"), 20, total_limit=Decimal("5"))[0]
    budget.mark("known", "RESPONSE_RECEIVED", "0.012")
    result = budget.summary()
    assert Decimal(result["reserved_usd"]) == Decimal("0.30")
    assert Decimal(result["known_usage_estimate_usd"]) == Decimal("0.012")
    assert Decimal(result["unknown_usage_reserve_usd"]) == Decimal("0.20")


def test_two_connections_cannot_reserve_beyond_cumulative_cap(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "parallel.sqlite"
    budgets = [VisionBudget(str(path)), VisionBudget(str(path))]

    def reserve(index):
        return budgets[index].reserve(
            f"request-{index}", Decimal("3"), Decimal("5"), 20, total_limit=Decimal("5")
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, (0, 1)))
    assert sorted(outcomes) == [False, True]
    assert Decimal(budgets[0].summary()["reserved_usd"]) == Decimal("3")


def test_paper_gate_requires_persistent_budget_and_non_thinking_model(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from backend import app
    from backend.vision_provider import VisionProviderConfig

    monkeypatch.setattr(app, "ROOT", tmp_path)
    config = VisionProviderConfig(
        thinking_mode="disabled",
        total_budget_usd=Decimal("5"),
        budget_db_path=str(tmp_path / "data" / "budget.sqlite"),
    )
    assert app._paper_automation_gate(config, SimpleNamespace(configured=True))[0]
    unsafe = VisionProviderConfig(
        thinking_mode=None,
        total_budget_usd=Decimal("5"),
        budget_db_path=str(tmp_path / "data" / "budget.sqlite"),
    )
    assert not app._paper_automation_gate(unsafe, SimpleNamespace(configured=True))[0]
    unsafe = VisionProviderConfig(
        thinking_mode="disabled",
        total_budget_usd=Decimal("5.01"),
        budget_db_path=str(tmp_path / "data" / "budget.sqlite"),
    )
    assert not app._paper_automation_gate(unsafe, SimpleNamespace(configured=True))[0]


def test_auth_failure_latches_and_prevents_later_requests(tmp_path, monkeypatch):
    import asyncio

    import httpx

    from backend.vision_provider import ConfigurableVisionAdapter
    from tests.test_vision_adapter import PNG, config

    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-only")
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(401, json={"error": "unauthorized"})

    adapter = ConfigurableVisionAdapter(
        config(budget_db_path=str(tmp_path / "budget.sqlite")),
        transport=httpx.MockTransport(handler),
    )
    adapter.image_capability_verified = True

    async def run():
        first = await adapter.decide(
            image=PNG,
            strategy_md="rules",
            md_version="v1",
            agent="A",
            bar_close="2026-01-01T00:00:00Z",
        )
        second = await adapter.decide(
            image=PNG,
            strategy_md="rules",
            md_version="v1",
            agent="B",
            bar_close="2026-01-01T00:00:00Z",
        )
        return first, second

    first, second = asyncio.run(run())
    assert first["status"] == "MODEL_HTTP_401"
    assert second["status"] == "MODEL_PROVIDER_AUTH_BLOCKED"
    assert len(sent) == 1
