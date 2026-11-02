from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Protocol

from backend.domain import Action
from backend.settings import MODEL_TIMEOUT_SECONDS
from backend.vision_provider import (
    GenericVisionAdapter as GenericVisionAdapter,
)
from backend.vision_provider import (
    VisionProviderConfig as VisionProviderConfig,
)
from backend.vision_provider import (
    public_vision_status as public_vision_status,
)

__all__ = [
    "AgentOrchestrator",
    "Decision",
    "FakeVisionAdapter",
    "GenericVisionAdapter",
    "VisionAdapter",
    "VisionProviderConfig",
    "public_vision_status",
    "validate_decision",
]


@dataclass(frozen=True)
class Decision:
    agent: str
    bar_close_utc: datetime
    snapshot_sha256: str
    md_version: str
    model_id: str
    action: Action
    rationale: str
    stop_loss: float | None
    received_at_utc: datetime
    mode: str


class VisionAdapter(Protocol):
    @property
    def configured(self) -> bool: ...
    async def decide(
        self,
        *,
        image: bytes,
        strategy_md: str,
        md_version: str,
        agent: str,
        bar_close: datetime,
        request_id: str | None = None,
        agent_memory: str = "",
        account_state: dict | None = None,
    ) -> dict: ...


class FakeVisionAdapter:
    """Deterministic test fixture only; it is impossible to label its output as a real decision."""

    configured = True
    model_id = "FAKE_TEST_ONLY"
    test_only = True

    async def decide(
        self,
        *,
        image: bytes,
        strategy_md: str,
        md_version: str,
        agent: str,
        bar_close: datetime,
        request_id: str | None = None,
        agent_memory: str = "",
        account_state: dict | None = None,
    ) -> dict:
        return {
            "accepted": True,
            "mode": "TEST_SIMULATION_ONLY",
            "agent": agent,
            "action": "WAIT",
            "rationale": "test fixture",
            "stop_loss": None,
            "model_id": self.model_id,
            "snapshot_sha256": __import__("hashlib").sha256(image).hexdigest(),
            "bar_close_utc": bar_close.isoformat(),
            "md_version": md_version,
        }


def validate_decision(
    raw: dict,
    *,
    now: datetime,
    candle_close: datetime,
    current_cycle_close: datetime,
    snapshot_sha256: str,
    md_version: str,
    expected_model_id: str,
) -> tuple[bool, str]:
    if now.tzinfo is None or candle_close.tzinfo is None or current_cycle_close.tzinfo is None:
        return False, "TIMESTAMP_MUST_BE_TIMEZONE_AWARE"
    if now.astimezone(UTC) >= current_cycle_close.astimezone(UTC):
        return False, "CYCLE_EXPIRED"
    try:
        action = Action(raw["action"])
    except (KeyError, ValueError):
        return False, "ACTION_INVALID"
    if raw.get("snapshot_sha256") != snapshot_sha256:
        return False, "SNAPSHOT_MISMATCH"
    if raw.get("md_version") != md_version:
        return False, "MD_VERSION_MISMATCH"
    if raw.get("model_id") != expected_model_id:
        return False, "MODEL_ID_MISMATCH"
    try:
        response_close = datetime.fromisoformat(str(raw["bar_close_utc"]).replace("Z", "+00:00"))
        if response_close.tzinfo is None:
            return False, "BAR_CLOSE_INVALID"
        response_close = response_close.astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        return False, "BAR_CLOSE_INVALID"
    if response_close != candle_close.astimezone(UTC):
        return False, "BAR_CLOSE_MISMATCH"
    if not isinstance(raw.get("rationale"), str) or not raw["rationale"].strip():
        return False, "RATIONALE_REQUIRED"
    if action in {Action.OPEN_LONG, Action.OPEN_SHORT}:
        try:
            stop = Decimal(str(raw["stop_loss"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return False, "OPEN_REQUIRES_STOP"
        if not stop.is_finite() or stop <= 0 or not raw.get("rationale"):
            return False, "OPEN_FIELDS_INVALID"
    return True, "VALID"


class AgentOrchestrator:
    """Same-cycle image evidence, isolated MD inputs, durable per-agent deduplication."""

    def __init__(self, db_path: str, timeout_seconds: float = MODEL_TIMEOUT_SECONDS):
        self.db_path = db_path
        self.timeout_seconds = timeout_seconds
        with sqlite3.connect(db_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY, agent TEXT NOT NULL, candle_close TEXT NOT NULL, md_version TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, model_id TEXT, mode TEXT NOT NULL, action TEXT, raw_response TEXT NOT NULL, outcome TEXT NOT NULL, UNIQUE(agent,candle_close,md_version))"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_decisions_one_per_agent_cycle ON decisions(agent,candle_close)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS agent_memories(agent TEXT NOT NULL, candle_close TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(agent,candle_close))"
            )

    def memory_for(self, agent: str, limit: int = 20) -> str:
        if agent not in "ABC" or len(agent) != 1:
            raise ValueError("invalid agent")
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT content FROM agent_memories WHERE agent=? ORDER BY candle_close DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
        return "\n".join(row[0] for row in reversed(rows))

    def _record_memory(self, agent: str, cycle_key: str, content: str) -> None:
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.execute(
                "INSERT OR IGNORE INTO agent_memories VALUES(?,?,?)",
                (agent, cycle_key, content[:2000]),
            )

    def decisions_for(self, agent: str, limit: int = 100) -> list[dict]:
        if agent not in {"A", "B", "C"} or not 1 <= limit <= 500:
            raise ValueError("invalid agent or decision limit")
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT decision_id,agent,candle_close,md_version,snapshot_sha256,model_id,mode,action,raw_response,outcome FROM decisions WHERE agent=? ORDER BY candle_close DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
        result = []
        for row in rows:
            try:
                raw = json.loads(row["raw_response"])
            except (TypeError, json.JSONDecodeError):
                raw = {}
            result.append(
                {
                    "decision_id": row["decision_id"],
                    "agent": row["agent"],
                    "candle_close": row["candle_close"],
                    "md_version": row["md_version"],
                    "snapshot_sha256": row["snapshot_sha256"],
                    "model_id": row["model_id"],
                    "mode": row["mode"],
                    "action": row["action"],
                    "outcome": row["outcome"],
                    "rationale": raw.get("rationale"),
                    "stop_loss": raw.get("stop_loss"),
                    "raw_response": raw,
                }
            )
        return result

    async def run_cycle(
        self,
        *,
        adapter: VisionAdapter,
        image: bytes,
        candle_close: datetime,
        cycle_deadline: datetime,
        strategy: dict[str, tuple[str, str] | None],
        account_states: dict[str, dict] | None = None,
        now: datetime | None = None,
    ) -> list[dict]:
        digest = hashlib.sha256(image).hexdigest()
        started = monotonic()
        if not adapter.configured:
            return [
                {
                    "agent": agent,
                    "snapshot_sha256": digest,
                    "status": "STRATEGY_NOT_CONFIGURED"
                    if not strategy.get(agent)
                    else "MODEL_NOT_CONFIGURED",
                    "action": None,
                    "mode": "PRODUCTION_BLOCKED",
                }
                for agent in "ABC"
            ]

        async def one(agent: str):
            if not strategy.get(agent):
                return {
                    "agent": agent,
                    "status": "STRATEGY_NOT_CONFIGURED",
                    "mode": "PRODUCTION_BLOCKED",
                    "action": None,
                }
            version, md = strategy[agent]
            cycle_key = candle_close.astimezone(UTC).isoformat()
            key = (agent, cycle_key, version)
            decision_id = hashlib.sha256((agent + cycle_key + digest).encode()).hexdigest()
            with sqlite3.connect(self.db_path) as db:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        agent,
                        key[1],
                        version,
                        digest,
                        getattr(adapter, "model_id", None),
                        "PENDING",
                        None,
                        "{}",
                        "PENDING",
                    ),
                ).rowcount
            if not inserted:
                with sqlite3.connect(self.db_path) as db:
                    old = db.execute(
                        "SELECT decision_id,outcome,action FROM decisions WHERE agent=? AND candle_close=?",
                        (agent, cycle_key),
                    ).fetchone()
                if old is None:
                    return {"agent": agent, "status": "DECISION_RESERVATION_CONFLICT"}
                return {
                    "agent": agent,
                    "status": "DUPLICATE_CYCLE",
                    "decision_id": old[0],
                    "action": old[2],
                }
            try:
                raw = await asyncio.wait_for(
                    adapter.decide(
                        image=image,
                        strategy_md=md,
                        md_version=version,
                        agent=agent,
                        bar_close=candle_close,
                        request_id=decision_id,
                        agent_memory=self.memory_for(agent),
                        account_state=dict((account_states or {}).get(agent, {})),
                    ),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError:
                with sqlite3.connect(self.db_path) as db:
                    db.execute(
                        "UPDATE decisions SET mode='PRODUCTION_BLOCKED',outcome='MODEL_TIMEOUT',raw_response='{}' WHERE decision_id=?",
                        (decision_id,),
                    )
                return {
                    "agent": agent,
                    "decision_id": decision_id,
                    "status": "MODEL_TIMEOUT",
                    "mode": "PRODUCTION_BLOCKED",
                }
            except Exception as exc:
                with sqlite3.connect(self.db_path) as db:
                    db.execute(
                        "UPDATE decisions SET mode='PRODUCTION_BLOCKED',outcome='MODEL_ERROR',raw_response=? WHERE decision_id=?",
                        (json.dumps({"error_class": type(exc).__name__}), decision_id),
                    )
                return {
                    "agent": agent,
                    "decision_id": decision_id,
                    "status": "MODEL_ERROR",
                    "error_class": type(exc).__name__,
                    "mode": "PRODUCTION_BLOCKED",
                }
            if not isinstance(raw, dict):
                with sqlite3.connect(self.db_path) as db:
                    db.execute(
                        "UPDATE decisions SET mode='PRODUCTION_BLOCKED',outcome='MODEL_RESPONSE_INVALID',raw_response='{}' WHERE decision_id=?",
                        (decision_id,),
                    )
                return {
                    "agent": agent,
                    "decision_id": decision_id,
                    "status": "MODEL_RESPONSE_INVALID",
                    "mode": "PRODUCTION_BLOCKED",
                }
            if isinstance(raw.get("status"), str):
                provider_status = raw["status"]
                with sqlite3.connect(self.db_path, timeout=10) as db:
                    db.execute(
                        "UPDATE decisions SET mode='PRODUCTION_BLOCKED',outcome=?,raw_response=? WHERE decision_id=?",
                        (provider_status, json.dumps({"status": provider_status}), decision_id),
                    )
                return {
                    "agent": agent,
                    "decision_id": decision_id,
                    "status": provider_status,
                    "mode": "PRODUCTION_BLOCKED",
                }
            mode = raw.get("mode", "REAL_PROVIDER")
            status, reason = validate_decision(
                raw,
                now=(now + timedelta(seconds=max(0, monotonic() - started)))
                if now
                else datetime.now(UTC),
                candle_close=candle_close,
                current_cycle_close=cycle_deadline,
                snapshot_sha256=digest,
                md_version=version,
                expected_model_id=getattr(adapter, "model_id", "unknown"),
            )
            result = {
                **raw,
                "agent": agent,
                "decision_id": decision_id,
                "snapshot_sha256": digest,
                "md_version": version,
                "status": "VALID" if status else reason,
                "mode": mode,
            }
            outcome = (
                ("TEST_ONLY" if status and mode == "TEST_SIMULATION_ONLY" else "VALID")
                if status
                else reason
            )
            with sqlite3.connect(self.db_path, timeout=10) as db:
                db.execute(
                    "UPDATE decisions SET mode=?,action=?,raw_response=?,outcome=? WHERE decision_id=?",
                    (
                        mode,
                        raw.get("action"),
                        json.dumps(raw, ensure_ascii=False),
                        outcome,
                        decision_id,
                    ),
                )
            if status:
                self._record_memory(
                    agent,
                    cycle_key,
                    f"{cycle_key} {version} {raw.get('action')} {raw.get('rationale', '')}",
                )
            return result

        return await asyncio.gather(*(one(a) for a in "ABC"))
