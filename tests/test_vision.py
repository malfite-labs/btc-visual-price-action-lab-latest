import asyncio
from datetime import UTC, datetime, timedelta

from backend.vision import AgentOrchestrator, FakeVisionAdapter, GenericVisionAdapter


class SlowAdapter(FakeVisionAdapter):
    async def decide(self, **kwargs):
        await asyncio.sleep(0.1)
        return await super().decide(**kwargs)


def test_no_model_is_explicit_and_test_fixture_is_labelled_and_deduped(tmp_path):
    store = AgentOrchestrator(str(tmp_path / "decisions.sqlite"))
    close = datetime.now(UTC) - timedelta(minutes=1)
    deadline = close + timedelta(minutes=15)
    strat = {a: ("v1.0.0", f"{a} private md") for a in "ABC"}
    blocked = asyncio.run(
        store.run_cycle(
            adapter=GenericVisionAdapter(),
            image=b"snapshot",
            candle_close=close,
            cycle_deadline=deadline,
            strategy=strat,
        )
    )
    assert all(x["status"] == "MODEL_NOT_CONFIGURED" for x in blocked)
    fake = FakeVisionAdapter()
    results = asyncio.run(
        store.run_cycle(
            adapter=fake,
            image=b"snapshot",
            candle_close=close,
            cycle_deadline=deadline,
            strategy=strat,
        )
    )
    assert len({x["snapshot_sha256"] for x in results}) == 1
    assert all(x["mode"] == "TEST_SIMULATION_ONLY" for x in results)
    assert store.decisions_for("A")[0]["rationale"] == "test fixture"
    assert store.decisions_for("A")[0]["md_version"] == "v1.0.0"
    assert store.decisions_for("B")[0]["agent"] == "B"
    assert store.decisions_for("A")[0]["snapshot_sha256"] == results[0]["snapshot_sha256"]
    again = asyncio.run(
        store.run_cycle(
            adapter=fake,
            image=b"snapshot",
            candle_close=close,
            cycle_deadline=deadline,
            strategy=strat,
        )
    )
    assert all(x["status"] == "DUPLICATE_CYCLE" for x in again)
    changed_md = asyncio.run(
        store.run_cycle(
            adapter=fake,
            image=b"different snapshot bytes",
            candle_close=close,
            cycle_deadline=deadline,
            strategy={**strat, "A": ("v2", "new A MD")},
        )
    )
    assert changed_md[0]["status"] == "DUPLICATE_CYCLE"


def test_missing_strategy_is_blocked_even_if_model_fixture_is_available(tmp_path):
    store = AgentOrchestrator(str(tmp_path / "decisions.sqlite"))
    close = datetime.now(UTC) - timedelta(minutes=1)
    result = asyncio.run(
        store.run_cycle(
            adapter=FakeVisionAdapter(),
            image=b"shared",
            candle_close=close,
            cycle_deadline=close + timedelta(minutes=15),
            strategy={"A": ("a-v1", "A rules"), "B": ("b-v1", "B rules"), "C": None},
        )
    )
    assert result[2]["status"] == "STRATEGY_NOT_CONFIGURED"
    assert store.memory_for("C") == ""


def test_non_object_provider_result_is_durable_and_never_retried(tmp_path):
    class WrongShape:
        configured = True
        model_id = "FIXTURE"
        test_only = True

        async def decide(self, **_kwargs):
            return ["not", "a", "decision object"]

    store = AgentOrchestrator(str(tmp_path / "decisions.sqlite"))
    close = datetime.now(UTC) - timedelta(minutes=1)
    result = asyncio.run(
        store.run_cycle(
            adapter=WrongShape(),
            image=b"shape",
            candle_close=close,
            cycle_deadline=close + timedelta(minutes=15),
            strategy={a: ("v1", "md") for a in "ABC"},
        )
    )
    assert all(item["status"] == "MODEL_RESPONSE_INVALID" for item in result)
    again = asyncio.run(
        store.run_cycle(
            adapter=WrongShape(),
            image=b"shape",
            candle_close=close,
            cycle_deadline=close + timedelta(minutes=15),
            strategy={a: ("v1", "md") for a in "ABC"},
        )
    )
    assert all(item["status"] == "DUPLICATE_CYCLE" for item in again)


def test_provider_diagnostic_status_is_preserved_without_response_body(tmp_path):
    class ProviderError:
        configured = True
        model_id = "FIXTURE"
        test_only = False

        async def decide(self, **_kwargs):
            return {"status": "MODEL_RESPONSE_CONTENT_NOT_JSON", "private_body": "must not persist"}

    store = AgentOrchestrator(str(tmp_path / "provider-diagnostic.sqlite"))
    close = datetime.now(UTC) - timedelta(minutes=1)
    result = asyncio.run(
        store.run_cycle(
            adapter=ProviderError(),
            image=b"diagnostic",
            candle_close=close,
            cycle_deadline=close + timedelta(minutes=15),
            strategy={a: ("v1", "md") for a in "ABC"},
        )
    )
    assert all(item["status"] == "MODEL_RESPONSE_CONTENT_NOT_JSON" for item in result)
    for agent in "ABC":
        persisted = store.decisions_for(agent)[0]
        assert persisted["outcome"] == "MODEL_RESPONSE_CONTENT_NOT_JSON"
        assert persisted["raw_response"] == {"status": "MODEL_RESPONSE_CONTENT_NOT_JSON"}


def test_model_timeout_is_durable_and_not_retried(tmp_path):
    store = AgentOrchestrator(str(tmp_path / "timeout.sqlite"), timeout_seconds=0.01)
    close = datetime.now(UTC) - timedelta(minutes=1)
    deadline = close + timedelta(minutes=15)
    results = asyncio.run(
        store.run_cycle(
            adapter=SlowAdapter(),
            image=b"s",
            candle_close=close,
            cycle_deadline=deadline,
            strategy={a: ("v1.0.0", "private") for a in "ABC"},
        )
    )
    assert all(x["status"] == "MODEL_TIMEOUT" for x in results)
    again = asyncio.run(
        store.run_cycle(
            adapter=SlowAdapter(),
            image=b"s",
            candle_close=close,
            cycle_deadline=deadline,
            strategy={a: ("v1.0.0", "private") for a in "ABC"},
        )
    )
    assert all(x["status"] == "DUPLICATE_CYCLE" for x in again)


def test_provider_metadata_and_cost_estimate_survive_decision_persistence(tmp_path):
    class MetadataAdapter:
        configured = True
        model_id = "fixture-model"
        test_only = True

        async def decide(self, **kwargs):
            import hashlib

            image = kwargs["image"]
            return {
                "mode": "TEST_SIMULATION_ONLY",
                "agent": kwargs["agent"],
                "action": "WAIT",
                "rationale": "local metadata persistence fixture",
                "stop_loss": None,
                "model_id": self.model_id,
                "snapshot_sha256": hashlib.sha256(image).hexdigest(),
                "bar_close_utc": kwargs["bar_close"].isoformat(),
                "md_version": kwargs["md_version"],
                "provider_fingerprint": "fp_local_test_only",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "estimated_cost_usd": "0.000009",
            }

    store = AgentOrchestrator(str(tmp_path / "metadata.sqlite"))
    close = datetime.now(UTC) - timedelta(minutes=1)
    result = asyncio.run(
        store.run_cycle(
            adapter=MetadataAdapter(),
            image=b"chart",
            candle_close=close,
            cycle_deadline=close + timedelta(minutes=15),
            strategy={"A": ("v1.0.0", "A-only MD"), "B": None, "C": None},
        )
    )
    persisted = store.decisions_for("A")[0]["raw_response"]
    assert result[0]["status"] == "VALID"
    assert persisted["provider_fingerprint"] == "fp_local_test_only"
    assert persisted["usage"]["total_tokens"] == 15
    assert persisted["estimated_cost_usd"] == "0.000009"


def test_orchestrator_passes_only_each_agents_own_account_state(tmp_path):
    class AccountStateAdapter:
        configured = True
        model_id = "fixture-model"
        test_only = True

        def __init__(self):
            self.received = {}

        async def decide(self, **kwargs):
            import hashlib

            agent = kwargs["agent"]
            self.received[agent] = kwargs.get("account_state")
            return {
                "mode": "TEST_SIMULATION_ONLY",
                "agent": agent,
                "action": "WAIT",
                "rationale": "account isolation fixture",
                "stop_loss": None,
                "model_id": self.model_id,
                "snapshot_sha256": hashlib.sha256(kwargs["image"]).hexdigest(),
                "bar_close_utc": kwargs["bar_close"].isoformat(),
                "md_version": kwargs["md_version"],
            }

    adapter = AccountStateAdapter()
    close = datetime.now(UTC) - timedelta(minutes=1)
    asyncio.run(
        AgentOrchestrator(str(tmp_path / "accounts.sqlite")).run_cycle(
            adapter=adapter,
            image=b"same-chart",
            candle_close=close,
            cycle_deadline=close + timedelta(minutes=15),
            strategy={"A": ("A-v1", "A MD"), "B": ("B-v1", "B MD"), "C": None},
            account_states={
                "A": {"position": "FLAT", "equity": "5000 USD"},
                "B": {"position": "OPEN_SHORT", "equity": "5000 USD"},
            },
        )
    )
    assert adapter.received["A"] == {"position": "FLAT", "equity": "5000 USD"}
    assert adapter.received["B"] == {"position": "OPEN_SHORT", "equity": "5000 USD"}
    assert "A" not in adapter.received["B"] and "B" not in adapter.received["A"]
