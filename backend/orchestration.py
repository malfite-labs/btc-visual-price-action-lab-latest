from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

from backend.domain import Action, BookLevel, CostModel
from backend.execution import (
    handle_confirmed_stop,
    handle_research_liquidation,
    prepare_entry,
    recover_pending_stop,
)
from backend.ledger import Ledger
from backend.vision import AgentOrchestrator, VisionAdapter


class TestOnlyPaperCycleRunner:
    """Explicitly test-scoped closed-candle → decision → risk → isolated-ledger cycle."""

    __test__ = False

    def __init__(
        self,
        ledger: Ledger,
        decisions: AgentOrchestrator,
        cost: CostModel,
        *,
        require_test_adapter: bool = True,
        enabled: bool = True,
        enable_c_5x: bool = False,
        c_maintenance_rate: Decimal | None = None,
    ):
        self.ledger = ledger
        self.decisions = decisions
        self.cost = cost
        self.require_test_adapter = require_test_adapter
        self.enabled = enabled
        self.enable_c_5x = enable_c_5x
        self.c_maintenance_rate = c_maintenance_rate

    @staticmethod
    def _fresh(quote: dict | None, book: dict | None, cost: CostModel) -> bool:
        return bool(
            quote
            and quote.get("source") == "ALPACA_US_BTCUSD_SPOT"
            and quote.get("currency") == "USD_RESEARCH"
            and quote.get("timestamp_valid", True) is True
            and 0 <= int(quote.get("age_ms", 10**9)) <= cost.max_quote_age_ms
            and Decimal(str(quote.get("bid", 0))) > 0
            and Decimal(str(quote.get("ask", 0))) > 0
        )

    def recover_pending_stops(self, event_ids: dict[str, str], quote: dict | None) -> dict:
        results = {}
        for agent in "ABC":
            account = self.ledger.account(agent)
            if account["state"] == "STOP_EXIT_PENDING":
                results[agent] = recover_pending_stop(
                    ledger=self.ledger,
                    agent=agent,
                    quote=quote,
                    cost=self.cost,
                    event_id=event_ids.get(agent, "recovery-unknown"),
                )
        return results

    async def run_cycle(
        self,
        *,
        adapter: VisionAdapter,
        image: bytes,
        candle_close: datetime,
        cycle_deadline: datetime,
        strategies: dict[str, tuple[str, str] | None],
        quote: dict | None,
        orderbook: dict | None,
        feed_healthy: bool,
        account_states: dict[str, dict] | None = None,
        confirmed_stop_events: dict[str, str] | None = None,
        refresh_market=None,
        now: datetime | None = None,
    ) -> dict[str, dict]:
        if self.require_test_adapter and not getattr(adapter, "test_only", False):
            return {a: {"status": "TEST_ONLY_ADAPTER_REQUIRED"} for a in "ABC"}
        if not self.enabled:
            return {a: {"status": "PAPER_EXECUTION_DISABLED"} for a in "ABC"}
        current_time = now or datetime.now(UTC)
        decision_started = monotonic()
        if (
            current_time.tzinfo is None
            or candle_close.tzinfo is None
            or cycle_deadline.tzinfo is None
        ):
            return {a: {"status": "TIMESTAMP_MUST_BE_TIMEZONE_AWARE"} for a in "ABC"}

        starting_versions = {a: self.ledger.account(a)["version"] for a in "ABC"}
        stop_results: dict[str, dict] = {}
        self.recover_pending_stops({}, quote)
        for agent, event_id in (confirmed_stop_events or {}).items():
            if agent not in "ABC":
                continue
            state = self.ledger.account(agent)["state"]
            if state in {"OPEN_LONG", "OPEN_SHORT"}:
                stop_results[agent] = handle_confirmed_stop(
                    ledger=self.ledger,
                    agent=agent,
                    trigger_event_id=event_id,
                    quote=quote,
                    cost=self.cost,
                )

        if not feed_healthy:
            return {
                a: {
                    "status": "MARKET_UNHEALTHY",
                    "stop": stop_results.get(a),
                    "execution": {"accepted": False, "reason": "MARKET_UNHEALTHY"},
                }
                for a in "ABC"
            }
        if candle_close.astimezone(UTC) > current_time.astimezone(UTC):
            return {
                a: {"status": "CANDLE_NOT_CLOSED", "execution": {"accepted": False}} for a in "ABC"
            }
        if not self._fresh(quote, orderbook, self.cost):
            return {
                a: {
                    "status": "QUOTE_OR_BOOK_STALE",
                    "stop": stop_results.get(a),
                    "execution": {"accepted": False, "reason": "QUOTE_OR_BOOK_STALE"},
                }
                for a in "ABC"
            }

        decisions = await self.decisions.run_cycle(
            adapter=adapter,
            image=image,
            candle_close=candle_close,
            cycle_deadline=cycle_deadline,
            strategy=strategies,
            account_states=account_states,
            now=current_time,
        )
        elapsed_ms = max(0, int((monotonic() - decision_started) * 1000))
        execution_quote = {**quote, "age_ms": int(quote["age_ms"]) + elapsed_ms}
        execution_book = {"age_ms": 0}
        refresh_error = None
        refreshed_at = None
        if refresh_market and any(
            d.get("status") == "VALID" and d.get("action") not in {"WAIT", "HOLD"}
            for d in decisions
        ):
            remaining = (
                cycle_deadline.astimezone(UTC)
                - (current_time.astimezone(UTC) + timedelta(milliseconds=elapsed_ms))
            ).total_seconds()
            if remaining <= 0:
                refresh_error = "CYCLE_EXPIRED"
            else:
                try:
                    execution_quote, execution_book = await asyncio.wait_for(
                        refresh_market(), timeout=remaining
                    )
                    refreshed_at = monotonic()
                    if not self._fresh(execution_quote, execution_book, self.cost):
                        refresh_error = "QUOTE_OR_BOOK_STALE"
                except Exception:
                    refresh_error = "MARKET_REFRESH_FAILED"
        result = {}
        for decision in decisions:
            agent = decision["agent"]
            row = {**decision, "stop": stop_results.get(agent)}
            if decision.get("status") != "VALID":
                result[agent] = row
                continue
            required_mode = "TEST_SIMULATION_ONLY" if self.require_test_adapter else "REAL_PROVIDER"
            if decision.get("mode") != required_mode:
                row["execution"] = {
                    "accepted": False,
                    "reason": "TEST_ONLY_DECISION_REQUIRED"
                    if self.require_test_adapter
                    else "NON_PRODUCTION_DECISION_REJECTED",
                }
                result[agent] = row
                continue
            account = self.ledger.account(agent)
            if account["version"] != starting_versions[agent]:
                row["execution"] = {"accepted": False, "reason": "ACCOUNT_CHANGED_DURING_DECISION"}
                result[agent] = row
                continue
            if account["state"] == "STOP_EXIT_PENDING":
                row["execution"] = {"accepted": False, "reason": "STOP_EXIT_PRIORITY"}
                result[agent] = row
                continue
            if decision.get("action") not in {"WAIT", "HOLD"} and refresh_error:
                row["execution"] = {"accepted": False, "reason": refresh_error}
                result[agent] = row
                continue
            if decision.get("action") not in {"WAIT", "HOLD"} and current_time.astimezone(
                UTC
            ) + timedelta(milliseconds=elapsed_ms) >= cycle_deadline.astimezone(UTC):
                row["execution"] = {"accepted": False, "reason": "CYCLE_EXPIRED"}
                result[agent] = row
                continue
            if refreshed_at is not None:
                elapsed_after_refresh = max(0, int((monotonic() - refreshed_at) * 1000))
                effective_quote = {
                    **execution_quote,
                    "age_ms": int(execution_quote["age_ms"]) + elapsed_after_refresh,
                }
                effective_book = {
                    **execution_book,
                    "age_ms": int(execution_book["age_ms"]) + elapsed_after_refresh,
                }
                if current_time.astimezone(UTC) + timedelta(
                    seconds=max(0, monotonic() - decision_started)
                ) >= cycle_deadline.astimezone(UTC):
                    row["execution"] = {"accepted": False, "reason": "CYCLE_EXPIRED"}
                    result[agent] = row
                    continue
            else:
                effective_quote, effective_book = execution_quote, execution_book
            if decision.get("action") not in {"WAIT", "HOLD"} and not self._fresh(
                effective_quote, effective_book, self.cost
            ):
                row["execution"] = {"accepted": False, "reason": "QUOTE_OR_BOOK_STALE"}
                result[agent] = row
                continue
            row["execution"] = self._execute(
                decision=decision,
                account=account,
                quote=effective_quote,
                orderbook=effective_book,
            )
            result[agent] = row
        return result

    def _execute(self, *, decision: dict, account: dict, quote: dict, orderbook: dict) -> dict:
        agent = decision["agent"]
        action = Action(decision["action"])
        version = decision["md_version"]
        rationale = decision["rationale"]
        key = "test-decision:" + decision["decision_id"]
        if action in {Action.HOLD, Action.WAIT}:
            return {"accepted": True, "no_fill": True, "reason": action.value}
        if action in {Action.OPEN_LONG, Action.OPEN_SHORT}:
            if account["state"] != "FLAT":
                return {"accepted": False, "reason": "POSITION_NOT_FLAT"}
            try:
                stop = Decimal(str(decision["stop_loss"]))
            except (KeyError, ValueError, TypeError):
                return {"accepted": False, "reason": "OPEN_REQUIRES_STOP"}
            metrics = self.ledger.account_metrics(agent, quote, self.cost)
            if metrics["equity"] is None:
                return {"accepted": False, "reason": "ACCOUNT_UNVALUED"}
            preflight = prepare_entry(
                equity=Decimal(metrics["equity"]),
                available_cash=Decimal(metrics["available_cash"]),
                direction="LONG" if action == Action.OPEN_LONG else "SHORT",
                stop=stop,
                quote=quote,
                orderbook=orderbook,
                cost=self.cost,
                quote_age_ms=int(quote["age_ms"]),
                book_age_ms=0,
                leverage=Decimal("5") if agent == "C" and self.enable_c_5x else Decimal("1"),
            )
            if not preflight["accepted"]:
                return preflight
            quantity = Decimal(preflight["quantity"])
            level = Decimal(str(quote["ask" if action == Action.OPEN_LONG else "bid"]))
            return self.ledger.open_position(
                agent=agent,
                direction="LONG" if action == Action.OPEN_LONG else "SHORT",
                quantity=quantity,
                price=Decimal(preflight["fill_price"]),
                stop=stop,
                fee=Decimal(preflight["fee"]),
                slippage=abs(Decimal(preflight["fill_price"]) - level) * quantity,
                idempotency_key=key,
                expected_version=account["version"],
                decision_id=decision["decision_id"],
                strategy_version=version,
                rationale=rationale,
                quote_id=preflight.get("quote_id"),
                cost_model=self.cost,
                research_c_5x=agent == "C" and self.enable_c_5x,
            )
        if action in {Action.CLOSE_LONG, Action.CLOSE_SHORT}:
            if agent == "C" and self.enable_c_5x:
                if self.c_maintenance_rate is None:
                    return {"accepted": False, "reason": "C_MAINTENANCE_RATE_NOT_CONFIGURED"}
                event_id = str(quote.get("event_id") or decision["decision_id"])
                protective = handle_research_liquidation(
                    ledger=self.ledger,
                    agent=agent,
                    quote=quote,
                    cost=self.cost,
                    event_id=event_id,
                    maintenance_rate=self.c_maintenance_rate,
                )
                if protective.get("reason") != "LIQUIDATION_NOT_TRIGGERED":
                    return protective
                stop = handle_confirmed_stop(
                    ledger=self.ledger,
                    agent=agent,
                    trigger_event_id=event_id,
                    quote=quote,
                    cost=self.cost,
                )
                if stop.get("reason") != "STOP_NOT_TRIGGERED":
                    return stop
            expected_direction = "LONG" if action == Action.CLOSE_LONG else "SHORT"
            if account["state"] != "OPEN_" + expected_direction or not account.get("position_json"):
                return {"accepted": False, "reason": "CLOSE_DIRECTION_OR_STATE_MISMATCH"}
            position = json.loads(account["position_json"])
            quantity = Decimal(position["quantity"])
            bids = [BookLevel(Decimal(str(quote['bid'])), Decimal('0'))]
            asks = [BookLevel(Decimal(str(quote['ask'])), Decimal('0'))]
            side = "SELL" if expected_direction == "LONG" else "BUY"
            from backend.domain import simulate_fill

            fill = simulate_fill(side, quantity, bids, asks, self.cost)
            if fill is None:
                return {"accepted": False, "reason": "EXIT_DEPTH_OR_PRICE_PROTECTION"}
            price, fee = fill
            best = bids[0].price if expected_direction == "LONG" else asks[0].price
            return self.ledger.close_position(
                agent=agent,
                price=price,
                fee=fee,
                slippage=abs(price - best) * quantity,
                idempotency_key=key,
                expected_version=account["version"],
                reason="MODEL_CLOSE",
                decision_id=decision["decision_id"],
                strategy_version=version,
                quote_id=quote.get("event_id"),
                rationale=rationale,
                cost_model=self.cost,
            )
        return {"accepted": False, "reason": "ACTION_NOT_EXECUTABLE"}


class PaperCycleRunner(TestOnlyPaperCycleRunner):
    """Production provider-to-internal-ledger path, inert until cost approval and explicit arming."""

    def __init__(
        self,
        ledger: Ledger,
        decisions: AgentOrchestrator,
        cost: CostModel,
        *,
        enabled: bool = False,
        cost_model_approved: bool = False,
        enable_c_5x: bool = False,
        c_maintenance_rate: Decimal | None = None,
    ):
        if enabled and not cost_model_approved:
            raise ValueError("PAPER_COST_MODEL_APPROVAL_REQUIRED")
        if enable_c_5x and (
            not isinstance(c_maintenance_rate, Decimal)
            or not c_maintenance_rate.is_finite()
            or not Decimal("0") < c_maintenance_rate < Decimal("1")
        ):
            raise ValueError("C_MAINTENANCE_RATE_REQUIRED")
        super().__init__(
            ledger,
            decisions,
            cost,
            require_test_adapter=False,
            enabled=enabled,
            enable_c_5x=enable_c_5x,
            c_maintenance_rate=c_maintenance_rate,
        )
