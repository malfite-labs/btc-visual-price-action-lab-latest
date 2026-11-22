from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.execution import (
    handle_confirmed_stop,
    handle_research_liquidation,
    recover_pending_stop,
)


class PaperCycleScheduler:
    """Persistent 15-minute event gate. Construction never starts a background task."""

    def __init__(
        self, db_path: str | Path, runner: Any, *, enabled: bool = False, c_maintenance_rate=None
    ):
        self.db_path = str(db_path)
        self.runner = runner
        self.enabled = enabled
        self.c_maintenance_rate = c_maintenance_rate
        self._cycle_lock = asyncio.Lock()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS scheduler_cycles (cycle_close TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL, result_json TEXT, updated_at TEXT NOT NULL)"
            )

    @staticmethod
    def _valid_close(value: datetime) -> bool:
        if value.tzinfo is None or value.second or value.microsecond:
            return False
        return value.astimezone(UTC).minute in {0, 15, 30, 45}

    async def run_closed_cycle(self, **cycle_inputs: Any) -> dict[str, Any]:
        if not self.enabled:
            return {agent: {"status": "SCHEDULER_DISABLED"} for agent in "ABC"}
        if hasattr(self.runner, "enabled") and not self.runner.enabled:
            return {agent: {"status": "SCHEDULER_EXECUTION_DISABLED"} for agent in "ABC"}
        close = cycle_inputs.get("candle_close")
        now = cycle_inputs.get("now") or datetime.now(UTC)
        if not isinstance(close, datetime) or not self._valid_close(close):
            return {agent: {"status": "INVALID_15M_CLOSE"} for agent in "ABC"}
        if cycle_inputs.get("candle_is_closed") is not True:
            return {agent: {"status": "CANDLE_NOT_CONFIRMED_CLOSED"} for agent in "ABC"}
        if now.tzinfo is None or now.astimezone(UTC) < close.astimezone(UTC):
            return {agent: {"status": "CANDLE_NOT_CLOSED"} for agent in "ABC"}
        if now.astimezone(UTC) >= close.astimezone(UTC) + timedelta(minutes=15):
            return {agent: {"status": "CYCLE_EXPIRED"} for agent in "ABC"}

        cycle_key = close.astimezone(UTC).isoformat()
        async with self._cycle_lock:
            with sqlite3.connect(self.db_path, timeout=10) as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute(
                    "SELECT status,updated_at,attempts FROM scheduler_cycles WHERE cycle_close=?",
                    (cycle_key,),
                ).fetchone()
                if old:
                    status, updated_at, attempts = old
                    if status == "DONE":
                        db.rollback()
                        return {
                            agent: {"status": "DUPLICATE_CYCLE", "cycle_close": cycle_key}
                            for agent in "ABC"
                        }
                    age = now.astimezone(UTC) - datetime.fromisoformat(updated_at)
                    if status == "RUNNING" and age < timedelta(seconds=120):
                        db.rollback()
                        return {
                            agent: {"status": "CYCLE_IN_PROGRESS", "cycle_close": cycle_key}
                            for agent in "ABC"
                        }
                    db.execute(
                        "UPDATE scheduler_cycles SET status='RUNNING',attempts=?,updated_at=? WHERE cycle_close=?",
                        (attempts + 1, now.astimezone(UTC).isoformat(), cycle_key),
                    )
                else:
                    db.execute(
                        "INSERT INTO scheduler_cycles VALUES(?,?,?,?,?)",
                        (cycle_key, "RUNNING", 1, None, now.astimezone(UTC).isoformat()),
                    )
                db.commit()
            try:
                workflow_inputs = {
                    name: value
                    for name, value in cycle_inputs.items()
                    if name != "candle_is_closed"
                }
                result = await self.runner.run_cycle(**workflow_inputs)
                serialized = json.dumps(result, ensure_ascii=False, default=str)
                with sqlite3.connect(self.db_path, timeout=10) as db:
                    db.execute(
                        "UPDATE scheduler_cycles SET status='DONE',result_json=?,updated_at=? WHERE cycle_close=?",
                        (serialized, datetime.now(UTC).isoformat(), cycle_key),
                    )
                return result
            except Exception as exc:
                with sqlite3.connect(self.db_path, timeout=10) as db:
                    db.execute(
                        "UPDATE scheduler_cycles SET status='FAILED',result_json=?,updated_at=? WHERE cycle_close=?",
                        (
                            json.dumps({"error_class": type(exc).__name__}),
                            datetime.now(UTC).isoformat(),
                            cycle_key,
                        ),
                    )
                return {
                    agent: {"status": "CYCLE_FAILED", "error_class": type(exc).__name__}
                    for agent in "ABC"
                }

    def monitor_stops(
        self, *, event_ids: dict[str, str], quote: dict | None, cost
    ) -> dict[str, dict]:
        """Independent quote-tick stop path; never invokes or awaits a model decision."""
        outcomes: dict[str, dict] = {}
        for agent, event_id in event_ids.items():
            if agent in {"A", "B", "C"}:
                if agent == "C" and self.c_maintenance_rate is not None:
                    result = handle_research_liquidation(
                        ledger=self.runner.ledger,
                        agent=agent,
                        quote=quote,
                        cost=cost,
                        event_id=event_id,
                        maintenance_rate=self.c_maintenance_rate,
                    )
                    if result.get("reason") not in {
                        "LIQUIDATION_NOT_TRIGGERED",
                        "NOT_RESEARCH_LEVERAGED_POSITION",
                        "NO_OPEN_POSITION",
                    }:
                        outcomes[agent] = result
                        continue
                outcomes[agent] = handle_confirmed_stop(
                    ledger=self.runner.ledger,
                    agent=agent,
                    trigger_event_id=event_id,
                    quote=quote,
                    cost=cost,
                )
        return outcomes

    def recover_stops(self, *, quote: dict | None, cost, orderbook: dict | None = None) -> dict[str, dict]:
        outcomes: dict[str, dict] = {}
        for agent in "ABC":
            account = self.runner.ledger.account(agent)
            if account["state"] == "STOP_EXIT_PENDING":
                recovery_id = "scheduler-recovery"
                if agent in {"B", "C"}:
                    try:
                        trade_id = json.loads(account["position_json"])["trade_id"]
                    except (TypeError, ValueError, KeyError):
                        outcomes[agent] = {"accepted": False, "reason": "RECOVERY_TRADE_ID_MISSING"}
                        continue
                    recovery_id = "scheduler-recovery:" + trade_id
                outcomes[agent] = recover_pending_stop(ledger=self.runner.ledger,agent=agent,quote=quote,cost=cost,event_id=recovery_id)
        return outcomes

    def status(self, cycle_close: datetime) -> str | None:
        if cycle_close.tzinfo is None:
            return None
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT status FROM scheduler_cycles WHERE cycle_close=?",
                (cycle_close.astimezone(UTC).isoformat(),),
            ).fetchone()
        return row[0] if row else None
