from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from backend.settings import (
    EVOLUTION_BATCH_SIZE,
    EVOLUTION_CHECKPOINT_DAYS,
    EVOLUTION_MAX_DAYS,
    EVOLUTION_MIN_TRADES_PER_SIDE,
    EVOLUTION_RECOVERY_ATTEMPTS_PER_CHECKPOINT,
)


@dataclass(frozen=True)
class Metrics:
    net_return: Decimal
    avg_win_loss_ratio: Decimal | None
    max_drawdown_fraction: Decimal
    closed_trades: int


def performance_metrics(
    start_equity: Decimal, equity_curve: list[Decimal], closed_trade_pnls: list[Decimal]
) -> Metrics:
    wins = [x for x in closed_trade_pnls if x > 0]
    losses = [-x for x in closed_trade_pnls if x < 0]
    ratio = (
        None if not wins or not losses else (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    )
    peak, max_dd = start_equity, Decimal(0)
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    final = equity_curve[-1] if equity_curve else start_equity
    return Metrics(final - start_equity, ratio, max_dd, len(closed_trade_pnls))


def compare(candidate: Metrics, incumbent: Metrics) -> tuple[bool, str]:
    if (
        candidate.closed_trades < EVOLUTION_MIN_TRADES_PER_SIDE
        or incumbent.closed_trades < EVOLUTION_MIN_TRADES_PER_SIDE
    ):
        return False, "INSUFFICIENT_SAMPLE"
    if candidate.avg_win_loss_ratio is None or incumbent.avg_win_loss_ratio is None:
        return False, "WIN_LOSS_RATIO_UNDEFINED"
    if incumbent.max_drawdown_fraction == 0:
        dd_ok = candidate.max_drawdown_fraction == 0
    else:
        dd_ok = candidate.max_drawdown_fraction <= incumbent.max_drawdown_fraction * Decimal("1.1")
    dd_ok = dd_ok and candidate.max_drawdown_fraction - incumbent.max_drawdown_fraction <= Decimal(
        "0.02"
    )
    ok = (
        candidate.net_return > incumbent.net_return
        and candidate.avg_win_loss_ratio > incumbent.avg_win_loss_ratio
        and dd_ok
    )
    return ok, "ALL_GATES_PASSED" if ok else "PERFORMANCE_GATE_FAILED"


def checkpoint_days(started_at: datetime, now: datetime) -> list[int]:
    elapsed = (now.astimezone(UTC) - started_at.astimezone(UTC)).total_seconds() / 86400
    return [day for day in EVOLUTION_CHECKPOINT_DAYS if elapsed >= day]


def candidate_age_expired(started_at: datetime, now: datetime) -> bool:
    return now.astimezone(UTC) >= started_at.astimezone(UTC) + timedelta(days=EVOLUTION_MAX_DAYS)


def recovery_allowed(attempts_at_checkpoint: int) -> bool:
    return attempts_at_checkpoint < EVOLUTION_RECOVERY_ATTEMPTS_PER_CHECKPOINT


class ReviewCoordinator:
    """Durable 50-complete-trade counter and single-candidate/queued-review control."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evo_counts(agent TEXT PRIMARY KEY, closed_count INTEGER NOT NULL DEFAULT 0, next_batch INTEGER NOT NULL DEFAULT 50);
                CREATE TABLE IF NOT EXISTS evo_trades(trade_id TEXT PRIMARY KEY, agent TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evo_reviews(review_id TEXT PRIMARY KEY, agent TEXT NOT NULL, batch_end INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evo_candidates(agent TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evo_checkpoints(agent TEXT NOT NULL, checkpoint_day INTEGER NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(agent,checkpoint_day));
                CREATE TABLE IF NOT EXISTS evo_checkpoint_recovery(agent TEXT NOT NULL, checkpoint_day INTEGER NOT NULL, attempts INTEGER NOT NULL, PRIMARY KEY(agent,checkpoint_day));
            """)
            for agent in "ABC":
                db.execute("INSERT OR IGNORE INTO evo_counts(agent) VALUES(?)", (agent,))

    def record_completed_trade(self, agent: str, trade_id: str) -> dict:
        if agent not in "ABC":
            raise ValueError("unknown agent")
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO evo_trades VALUES(?,?)", (trade_id, agent)
            ).rowcount
            if not inserted:
                count = db.execute(
                    "SELECT closed_count FROM evo_counts WHERE agent=?", (agent,)
                ).fetchone()[0]
                db.commit()
                return {"duplicate": True, "closed_count": count, "queued": False}
            count, next_batch = db.execute(
                "SELECT closed_count,next_batch FROM evo_counts WHERE agent=?", (agent,)
            ).fetchone()
            count += 1
            queued = False
            if count >= next_batch:
                candidate = db.execute(
                    "SELECT status FROM evo_candidates WHERE agent=?", (agent,)
                ).fetchone()
                status = (
                    "QUEUED"
                    if candidate and candidate[0] in {"RUNNING", "PROMOTION_PENDING"}
                    else "READY_FOR_REVIEW"
                )
                exists = db.execute(
                    "SELECT 1 FROM evo_reviews WHERE agent=? AND batch_end=?", (agent, next_batch)
                ).fetchone()
                if not exists:
                    db.execute(
                        "INSERT INTO evo_reviews VALUES(?,?,?,?,?)",
                        (str(uuid.uuid4()), agent, next_batch, status, now),
                    )
                    queued = True
                next_batch += EVOLUTION_BATCH_SIZE
            db.execute(
                "UPDATE evo_counts SET closed_count=?,next_batch=? WHERE agent=?",
                (count, next_batch, agent),
            )
            db.commit()
        return {"duplicate": False, "closed_count": count, "queued": queued}

    def mark_candidate_finished(self, agent: str, status: str) -> dict:
        if status not in {"PROMOTION_PENDING", "REJECTED", "ARCHIVED", "EXPIRED", "PROMOTED"}:
            raise ValueError("invalid terminal candidate status")
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE evo_candidates SET status=? WHERE agent=? AND status='RUNNING'",
                (status, agent),
            ).rowcount
            if changed:
                # Any already queued 50-trade review remains durable and can start only after this slot frees.
                db.execute(
                    "UPDATE evo_reviews SET status='READY_FOR_REVIEW' WHERE agent=? AND status='QUEUED'",
                    (agent,),
                )
            db.commit()
        return {"updated": bool(changed), "status": status if changed else "NO_RUNNING_CANDIDATE"}

    def start_candidate(self, agent: str, candidate_id: str, now: datetime | None = None) -> dict:
        now = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            if agent not in {"A", "B", "C"}:
                return {"accepted": False, "reason": "AGENT_INVALID"}
            active = db.execute(
                "SELECT candidate_id FROM evo_candidates WHERE agent=? AND status IN ('RUNNING','PROMOTION_PENDING')",
                (agent,),
            ).fetchone()
            if active:
                db.rollback()
                return {"accepted": False, "reason": "CANDIDATE_SLOT_OCCUPIED"}
            db.execute(
                "INSERT INTO evo_candidates(agent,candidate_id,status,started_at) VALUES(?,?,'RUNNING',?) ON CONFLICT(agent) DO UPDATE SET candidate_id=excluded.candidate_id,status='RUNNING',started_at=excluded.started_at",
                (agent, candidate_id, now),
            )
            db.commit()
        return {"accepted": True, "started_at": now, "deadline_days": 30}

    def status(self, agent: str) -> dict:
        if agent not in {"A", "B", "C"}:
            raise ValueError("unknown agent")
        with sqlite3.connect(self.path) as db:
            count, next_batch = db.execute(
                "SELECT closed_count,next_batch FROM evo_counts WHERE agent=?", (agent,)
            ).fetchone()
            reviews = db.execute(
                "SELECT batch_end,status,created_at FROM evo_reviews WHERE agent=? ORDER BY batch_end",
                (agent,),
            ).fetchall()
            candidate = db.execute(
                "SELECT candidate_id,status,started_at FROM evo_candidates WHERE agent=?", (agent,)
            ).fetchone()
            checkpoints = db.execute(
                "SELECT checkpoint_day,result_json FROM evo_checkpoints WHERE agent=? ORDER BY checkpoint_day",
                (agent,),
            ).fetchall()
        return {
            "agent": agent,
            "closed_trades": count,
            "next_review_at": next_batch,
            "progress": count % 50,
            "reviews": reviews,
            "candidate": candidate,
            "candidate_checkpoints_days": [7, 14, 21, 30],
            "checkpoints": [(day, json.loads(payload)) for day, payload in checkpoints],
        }

    def record_checkpoint_recovery(self, agent: str, checkpoint_day: int) -> dict:
        if agent not in {"A", "B", "C"} or checkpoint_day not in EVOLUTION_CHECKPOINT_DAYS:
            return {"accepted": False, "reason": "CHECKPOINT_INVALID"}
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            candidate = db.execute(
                "SELECT status FROM evo_candidates WHERE agent=?", (agent,)
            ).fetchone()
            if not candidate or candidate[0] != "RUNNING":
                db.rollback()
                return {"accepted": False, "reason": "NO_RUNNING_CANDIDATE"}
            old = db.execute(
                "SELECT attempts FROM evo_checkpoint_recovery WHERE agent=? AND checkpoint_day=?",
                (agent, checkpoint_day),
            ).fetchone()
            attempts = old[0] if old else 0
            if not recovery_allowed(attempts):
                db.rollback()
                return {"accepted": False, "reason": "RECOVERY_LIMIT_REACHED"}
            db.execute(
                "INSERT INTO evo_checkpoint_recovery VALUES(?,?,1) ON CONFLICT(agent,checkpoint_day) DO UPDATE SET attempts=attempts+1",
                (agent, checkpoint_day),
            )
            db.commit()
        return {"accepted": True, "attempts": attempts + 1}

    def evaluate_candidate_checkpoint(
        self,
        agent: str,
        checkpoint_day: int,
        *,
        now: datetime,
        old_position_open: bool,
        candidate: Metrics,
        incumbent: Metrics,
        candidate_initial_equity: Decimal,
        incumbent_initial_equity: Decimal,
        candidate_model_id: str,
        incumbent_model_id: str,
        candidate_cost_hash: str,
        incumbent_cost_hash: str,
        candidate_market_event_ids: list[str],
        incumbent_market_event_ids: list[str],
    ) -> dict:
        if agent not in {"A", "B", "C"} or checkpoint_day not in EVOLUTION_CHECKPOINT_DAYS:
            return {"outcome": "CHECKPOINT_INVALID"}
        if now.tzinfo is None:
            return {"outcome": "TIMESTAMP_MUST_BE_TIMEZONE_AWARE"}
        current = now.astimezone(UTC)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT status,started_at FROM evo_candidates WHERE agent=?", (agent,)
            ).fetchone()
            if not active or active[0] != "RUNNING":
                db.rollback()
                return {"outcome": "NO_RUNNING_CANDIDATE"}
            started = datetime.fromisoformat(active[1]).astimezone(UTC)
            checkpoint_at = started + timedelta(days=checkpoint_day)
            deadline = started + timedelta(days=EVOLUTION_MAX_DAYS)
            if current < checkpoint_at:
                db.rollback()
                return {"outcome": "CHECKPOINT_NOT_DUE"}
            if current >= deadline and checkpoint_day != EVOLUTION_MAX_DAYS:
                db.rollback()
                return {"outcome": "CANDIDATE_DEADLINE_PASSED"}
            existing = db.execute(
                "SELECT result_json FROM evo_checkpoints WHERE agent=? AND checkpoint_day=?",
                (agent, checkpoint_day),
            ).fetchone()
            if existing:
                db.rollback()
                return {"outcome": "DUPLICATE_CHECKPOINT", **json.loads(existing[0])}
            paired = bool(
                candidate_initial_equity == incumbent_initial_equity
                and candidate_model_id
                and candidate_model_id == incumbent_model_id
                and candidate_cost_hash
                and candidate_cost_hash == incumbent_cost_hash
                and candidate_market_event_ids
                and candidate_market_event_ids == incumbent_market_event_ids
            )
            if not paired:
                promoted, reason = False, "PAIRING_MISMATCH"
            else:
                promoted, reason = compare(candidate, incumbent)
            outcome = "PROMOTION_PENDING" if promoted else reason
            if promoted:
                db.execute(
                    "UPDATE evo_candidates SET status='PROMOTION_PENDING' WHERE agent=?",
                    (agent,),
                )
            elif checkpoint_day == EVOLUTION_MAX_DAYS:
                outcome = "EXPIRED"
                db.execute(
                    "UPDATE evo_candidates SET status='EXPIRED' WHERE agent=?",
                    (agent,),
                )
            details = {
                "outcome": outcome,
                "old_position_open": bool(old_position_open),
                "candidate_closed_trades": candidate.closed_trades,
                "incumbent_closed_trades": incumbent.closed_trades,
                "candidate_net_return": str(candidate.net_return),
                "incumbent_net_return": str(incumbent.net_return),
                "candidate_avg_win_loss_ratio": str(candidate.avg_win_loss_ratio)
                if candidate.avg_win_loss_ratio is not None
                else None,
                "incumbent_avg_win_loss_ratio": str(incumbent.avg_win_loss_ratio)
                if incumbent.avg_win_loss_ratio is not None
                else None,
                "candidate_max_drawdown": str(candidate.max_drawdown_fraction),
                "incumbent_max_drawdown": str(incumbent.max_drawdown_fraction),
            }
            db.execute(
                "INSERT INTO evo_checkpoints VALUES(?,?,?)",
                (agent, checkpoint_day, json.dumps(details, sort_keys=True)),
            )
            if outcome == "EXPIRED":
                db.execute(
                    "UPDATE evo_reviews SET status='READY_FOR_REVIEW' WHERE agent=? AND status='QUEUED'",
                    (agent,),
                )
            db.commit()
        return details

    def activate_after_flat(
        self, agent: str, *, old_position_open: bool, at_new_cycle: bool
    ) -> dict:
        if agent not in {"A", "B", "C"}:
            return {"status": "AGENT_INVALID"}
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM evo_candidates WHERE agent=?", (agent,)).fetchone()
            if not row or row[0] != "PROMOTION_PENDING":
                db.rollback()
                return {"status": "NO_PROMOTION_PENDING"}
            if old_position_open:
                db.rollback()
                return {"status": "WAITING_OLD_POSITION"}
            if not at_new_cycle:
                db.rollback()
                return {"status": "WAITING_NEW_CYCLE"}
            db.execute("UPDATE evo_candidates SET status='PROMOTED' WHERE agent=?", (agent,))
            db.execute(
                "UPDATE evo_reviews SET status='READY_FOR_REVIEW' WHERE agent=? AND status='QUEUED'",
                (agent,),
            )
            db.commit()
        return {"status": "PROMOTED"}
