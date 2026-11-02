import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from backend.charts import render_ai_snapshot
from backend.domain import CostModel
from backend.ledger import Ledger
from backend.market import Candle
from backend.orchestration import TestOnlyPaperCycleRunner
from backend.vision import AgentOrchestrator, FakeVisionAdapter


class ScriptedAdapter(FakeVisionAdapter):
    def __init__(self, actions):
        self.actions = actions
        self.seen = []

    async def decide(
        self,
        *,
        image,
        strategy_md,
        md_version,
        agent,
        bar_close,
        request_id=None,
        agent_memory="",
        account_state=None,
    ):
        self.seen.append((agent, strategy_md, agent_memory, hashlib.sha256(image).hexdigest()))
        result = await super().decide(
            image=image,
            strategy_md=strategy_md,
            md_version=md_version,
            agent=agent,
            bar_close=bar_close,
            account_state=account_state,
        )
        result.update(self.actions.get(agent, {"action": "WAIT", "rationale": "no setup"}))
        return result


class SlowParallelAdapter(FakeVisionAdapter):
    def __init__(self):
        self.active = 0
        self.peak = 0

    async def decide(
        self,
        *,
        image,
        strategy_md,
        md_version,
        agent,
        bar_close,
        request_id=None,
        agent_memory="",
        account_state=None,
    ):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return await super().decide(
            image=image,
            strategy_md=strategy_md,
            md_version=md_version,
            agent=agent,
            bar_close=bar_close,
            account_state=account_state,
        )


def books():
    return {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "age_ms": 20,
        "bids": [{"p": "99999", "s": "1"}],
        "asks": [{"p": "100001", "s": "1"}],
    }


def quote(bid="99999", ask="100001", age=20):
    return {
        "source": "ALPACA_US_BTCUSD_SPOT",
        "currency": "USD_RESEARCH",
        "bid": bid,
        "ask": ask,
        "bid_qty": "1",
        "ask_qty": "1",
        "age_ms": age,
        "event_id": f"quote-{bid}-{ask}",
    }


def closed_market_image(tmp_path, close):
    first_open = close - timedelta(minutes=15 * 288)
    bars = []
    for index in range(288):
        opened = first_open + timedelta(minutes=15 * index)
        base = D("100000") + D(index % 7)
        bars.append(
            Candle(
                "BTC/USD",
                "15Min",
                opened.isoformat(),
                (opened + timedelta(minutes=15)).isoformat(),
                str(base),
                str(base + 20),
                str(base - 20),
                str(base + 5),
                "0",
                0,
                str(base + 1),
                True,
            )
        )
    output = render_ai_snapshot(bars, tmp_path / f"{close.timestamp()}.png")
    assert output["global_count"] == 288 and output["local_count"] == 96
    return (tmp_path / f"{close.timestamp()}.png").read_bytes()


def run_cycle(runner, adapter, close, **kw):
    kw.setdefault("quote", quote())
    kw.setdefault("orderbook", books())
    kw.setdefault("feed_healthy", True)
    kw.setdefault("now", close + timedelta(seconds=1))
    image = kw.pop("image", b"\x89PNG\r\n\x1a\nchart fixture")
    return asyncio.run(
        runner.run_cycle(
            adapter=adapter,
            image=image,
            candle_close=close,
            cycle_deadline=close + timedelta(minutes=15),
            strategies={"A": ("a-v1", "A private rules"), "B": ("b-v1", "B private rules")},
            **kw,
        )
    )


def test_two_15m_cycles_open_close_independent_accounts_and_dedupe(tmp_path):
    ledger = Ledger(tmp_path / "isolated-test-only.sqlite3")
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    start = datetime.now(UTC) - timedelta(minutes=1)
    opening = ScriptedAdapter(
        {
            "A": {"action": "OPEN_LONG", "stop_loss": "99000", "rationale": "A structure"},
            "B": {"action": "OPEN_SHORT", "stop_loss": "101000", "rationale": "B reversal"},
        }
    )
    first = run_cycle(runner, opening, start, image=closed_market_image(tmp_path, start))
    assert first["A"]["execution"]["accepted"] and first["B"]["execution"]["accepted"]
    assert first["C"]["status"] == "STRATEGY_NOT_CONFIGURED"
    assert (
        ledger.account("A")["state"] == "OPEN_LONG" and ledger.account("B")["state"] == "OPEN_SHORT"
    )
    assert ledger.account("C")["cash"] == "10000.00"
    assert [(a, md, memory) for a, md, memory, _ in opening.seen] == [
        ("A", "A private rules", ""),
        ("B", "B private rules", ""),
    ]
    assert (
        opening.seen[0][3]
        == opening.seen[1][3]
        == first["A"]["snapshot_sha256"]
        == first["B"]["snapshot_sha256"]
    )

    duplicated = run_cycle(runner, opening, start, image=closed_market_image(tmp_path, start))
    assert duplicated["A"]["status"] == duplicated["B"]["status"] == "DUPLICATE_CYCLE"
    assert duplicated["C"]["status"] == "STRATEGY_NOT_CONFIGURED"
    assert len(ledger.fills()) == 2

    next_close = start + timedelta(minutes=15)
    closing = ScriptedAdapter(
        {
            "A": {"action": "CLOSE_LONG", "rationale": "A close"},
            "B": {"action": "CLOSE_SHORT", "rationale": "B close"},
        }
    )
    second = run_cycle(
        runner,
        closing,
        next_close,
        image=closed_market_image(tmp_path, next_close),
        quote=quote("101000", "101001"),
        orderbook={
            **books(),
            "bids": [{"p": "101000", "s": "1"}],
            "asks": [{"p": "101001", "s": "1"}],
        },
    )
    assert second["A"]["execution"]["accepted"] and second["B"]["execution"]["accepted"]
    assert ledger.account("A")["state"] == ledger.account("B")["state"] == "FLAT"
    assert len(ledger.fills()) == 4
    assert all(f["decision_id"] for f in ledger.fills())
    assert len({f["strategy_version"] for f in ledger.fills() if f["agent"] == "A"}) == 1
    for agent in "AB":
        entry, exit_fill = ledger.fills(agent)
        import json
        from decimal import ROUND_UP

        for fill in (entry, exit_fill):
            expected_fee = (D(fill["quantity"]) * D(fill["price"]) * D("0.0005")).quantize(
                D("0.01"), rounding=ROUND_UP
            )
            assert D(fill["fee"]) == expected_fee
            model = json.loads(fill["cost_model_json"])
            assert model["slippage_per_fill"] == "0.0002" and model["taker_fee_rate"] == "0.0005"
        if agent == "A":
            assert entry["side"] == "BUY" and D(entry["price"]) > D("100001")
            assert exit_fill["side"] == "SELL" and D(exit_fill["price"]) < D("101000")
        else:
            assert entry["side"] == "SELL" and D(entry["price"]) < D("99999")
            assert exit_fill["side"] == "BUY" and D(exit_fill["price"]) > D("101001")
        direction_sign = 1 if entry["position_direction"] == "LONG" else -1
        gross = (D(exit_fill["price"]) - D(entry["price"])) * D(entry["quantity"]) * direction_sign
        expected_cash = D("10000.00") + gross - D(entry["fee"]) - D(exit_fill["fee"])
        assert D(ledger.account(agent)["cash"]) == expected_cash.quantize(D("0.01"))
        assert ledger.account_metrics(agent, quote("101000", "101001"), CostModel())[
            "equity"
        ] == str(expected_cash.quantize(D("0.01")))
    a_memory = next(memory for agent, _, memory, _ in closing.seen if agent == "A")
    b_memory = next(memory for agent, _, memory, _ in closing.seen if agent == "B")
    assert "A structure" in a_memory and "B reversal" not in a_memory
    assert "B reversal" in b_memory and "A structure" not in b_memory


def test_stale_feed_blocks_decisions_and_new_positions(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite3")
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    start = datetime.now(UTC) - timedelta(minutes=1)
    adapter = ScriptedAdapter(
        {"A": {"action": "OPEN_LONG", "stop_loss": "99000", "rationale": "x"}}
    )
    result = run_cycle(runner, adapter, start, feed_healthy=False)
    assert result["A"]["status"] == "MARKET_UNHEALTHY"
    assert adapter.seen == [] and ledger.fills() == []


def test_expired_quote_and_invalid_stop_never_open(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite3")
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    start = datetime.now(UTC) - timedelta(minutes=1)
    bad_quote = quote(age=60001)
    result = run_cycle(
        runner,
        ScriptedAdapter({"A": {"action": "OPEN_LONG", "stop_loss": "100500", "rationale": "bad"}}),
        start,
        quote=bad_quote,
    )
    assert result["A"]["status"] == "QUOTE_OR_BOOK_STALE"
    assert ledger.account("A")["state"] == "FLAT" and not ledger.fills()

    invalid_stop = run_cycle(
        runner,
        ScriptedAdapter(
            {"A": {"action": "OPEN_LONG", "stop_loss": "100500", "rationale": "wrong side"}}
        ),
        start + timedelta(minutes=15),
        quote=quote(),
        orderbook=books(),
    )
    assert invalid_stop["A"]["execution"]["reason"] == "STOP_ON_WRONG_SIDE"
    assert ledger.account("A")["state"] == "FLAT" and not ledger.fills()


def test_triggered_stop_persists_and_restart_recovers_before_model_action(tmp_path):
    path = tmp_path / "isolated.sqlite3"
    ledger = Ledger(path)
    acct = ledger.account("A")
    ledger.open_position(
        agent="A",
        direction="LONG",
        quantity=D("0.01"),
        price=D("100000"),
        stop=D("99000"),
        fee=D("0.50"),
        slippage=D("1"),
        idempotency_key="seed",
        expected_version=acct["version"],
    )
    decisions = AgentOrchestrator(str(tmp_path / "decisions.sqlite"))
    runner = TestOnlyPaperCycleRunner(ledger, decisions, CostModel())
    start = datetime.now(UTC) - timedelta(minutes=1)
    no_quote = run_cycle(
        runner,
        ScriptedAdapter({"A": {"action": "CLOSE_LONG", "rationale": "old close"}}),
        start,
        quote=None,
        orderbook=None,
        confirmed_stop_events={"A": "stop-trigger-1"},
    )
    assert ledger.account("A")["state"] == "STOP_EXIT_PENDING"
    assert no_quote["A"]["execution"]["reason"] == "QUOTE_OR_BOOK_STALE"
    restored = Ledger(path)
    runner2 = TestOnlyPaperCycleRunner(restored, decisions, CostModel())
    recovered = runner2.recover_pending_stops({"A": "fresh-quote-1"}, quote())
    assert recovered["A"]["accepted"] and restored.account("A")["state"] == "FLAT"
    assert len(restored.fills("A")) == 2


def test_fresh_stop_trigger_wins_over_same_cycle_active_close(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite3")
    acct = ledger.account("A")
    ledger.open_position(
        agent="A",
        direction="LONG",
        quantity=D("0.01"),
        price=D("100000"),
        stop=D("99000"),
        fee=D("0.50"),
        slippage=D("1"),
        idempotency_key="seed",
        expected_version=acct["version"],
    )
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    start = datetime.now(UTC) - timedelta(minutes=1)
    stop_quote = quote("98000", "98010")
    book = {**books(), "bids": [{"p": "98000", "s": "1"}], "asks": [{"p": "98010", "s": "1"}]}
    result = run_cycle(
        runner,
        ScriptedAdapter({"A": {"action": "CLOSE_LONG", "rationale": "concurrent model close"}}),
        start,
        quote=stop_quote,
        orderbook=book,
        confirmed_stop_events={"A": "confirmed-stop"},
    )
    fills = ledger.fills("A")
    assert len(fills) == 2 and fills[-1]["rationale"] == "STOP_TRIGGER"
    assert result["A"]["execution"]["reason"] == "ACCOUNT_CHANGED_DURING_DECISION"
    assert ledger.account("A")["state"] == "FLAT"


def test_malformed_decision_and_expired_cycle_never_mutate_ledger(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite3")
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    start = datetime.now(UTC) - timedelta(minutes=1)
    malformed = run_cycle(
        runner, ScriptedAdapter({"A": {"action": "open-and-hope", "rationale": "bad"}}), start
    )
    assert malformed["A"]["status"] == "ACTION_INVALID" and not ledger.fills()
    expired = asyncio.run(
        runner.run_cycle(
            adapter=ScriptedAdapter(
                {"A": {"action": "OPEN_LONG", "stop_loss": "99000", "rationale": "late"}}
            ),
            image=b"\x89PNG\r\n\x1a\nlate cycle",
            candle_close=start - timedelta(minutes=15),
            cycle_deadline=start - timedelta(minutes=1),
            strategies={"A": ("a-v1", "A"), "B": ("b-v1", "B")},
            quote=quote(),
            orderbook=books(),
            feed_healthy=True,
            now=start + timedelta(minutes=1),
        )
    )
    assert expired["A"]["status"] == "CYCLE_EXPIRED" and not ledger.fills()


def test_open_15m_bar_is_never_sent_to_the_model(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite3")
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    close = datetime.now(UTC) + timedelta(minutes=1)
    adapter = ScriptedAdapter(
        {"A": {"action": "OPEN_LONG", "stop_loss": "99000", "rationale": "future"}}
    )
    result = run_cycle(runner, adapter, close, now=datetime.now(UTC))
    assert result["A"]["status"] == "CANDLE_NOT_CLOSED"
    assert adapter.seen == [] and ledger.fills() == []


def test_nonfinite_stop_is_rejected_before_risk_calculation(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite3")
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    start = datetime.now(UTC) - timedelta(minutes=1)
    result = run_cycle(
        runner,
        ScriptedAdapter(
            {"A": {"action": "OPEN_LONG", "stop_loss": "NaN", "rationale": "bad stop"}}
        ),
        start,
    )
    assert result["A"]["status"] == "OPEN_FIELDS_INVALID"
    assert ledger.account("A")["state"] == "FLAT" and ledger.fills() == []


def test_parallel_duplicate_cycles_cannot_double_fill(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite3")
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    start = datetime.now(UTC) - timedelta(minutes=1)
    adapter = ScriptedAdapter(
        {"A": {"action": "OPEN_LONG", "stop_loss": "99000", "rationale": "once"}}
    )

    async def two():
        return await asyncio.gather(
            *(
                runner.run_cycle(
                    adapter=adapter,
                    image=b"\x89PNG\r\n\x1a\nchart",
                    candle_close=start,
                    cycle_deadline=start + timedelta(minutes=15),
                    strategies={"A": ("a-v1", "A"), "B": ("b-v1", "B")},
                    quote=quote(),
                    orderbook=books(),
                    feed_healthy=True,
                )
                for _ in range(2)
            )
        )

    results = asyncio.run(two())
    assert (
        sum(
            1
            for row in results
            for value in row.values()
            if value.get("execution", {}).get("accepted") and not value["execution"].get("no_fill")
        )
        == 1
    )
    assert len(ledger.fills("A")) == 1


def test_two_configured_agents_decide_concurrently_while_c_stays_blocked(tmp_path):
    ledger = Ledger(tmp_path / "isolated.sqlite3")
    runner = TestOnlyPaperCycleRunner(
        ledger, AgentOrchestrator(str(tmp_path / "decisions.sqlite")), CostModel()
    )
    adapter = SlowParallelAdapter()
    start = datetime.now(UTC) - timedelta(minutes=1)
    result = run_cycle(runner, adapter, start)
    assert adapter.peak == 2
    assert result["A"]["status"] == result["B"]["status"] == "VALID"
    assert result["C"]["status"] == "STRATEGY_NOT_CONFIGURED"
    assert ledger.fills() == []
