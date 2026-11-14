"""Create a new, explicitly incomplete ABC shadow-ledger epoch without importing history."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.ledger import Ledger
from backend.settings import INITIAL_EQUITY, PAPER_CURRENCY

EVOLUTION_THRESHOLD = 40
HISTORY_STATUS = "ARCHIVED_INCOMPLETE"
EPOCH_STATUS = "REBUILT_NEW_EPOCH"

_FORWARD_SCHEMA = """
CREATE TABLE forward_cycles(close_at TEXT PRIMARY KEY,status TEXT NOT NULL,error TEXT);
CREATE TABLE forward_bars(open_at TEXT PRIMARY KEY,close_at TEXT NOT NULL,bar_json TEXT NOT NULL);
CREATE TABLE forward_decisions(agent TEXT NOT NULL,close_at TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,PRIMARY KEY(agent,close_at));
CREATE TABLE forward_positions(agent TEXT PRIMARY KEY,trade_id TEXT NOT NULL,signal_close TEXT NOT NULL,target TEXT NOT NULL,max_hold INTEGER NOT NULL,bars_held INTEGER NOT NULL DEFAULT 0,initial_cash TEXT NOT NULL,exit_due TEXT);
CREATE TABLE forward_trades(trade_id TEXT PRIMARY KEY,agent TEXT NOT NULL,signal_close TEXT NOT NULL,closed_at TEXT NOT NULL,net_pnl TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE forward_reviews(agent TEXT NOT NULL,batch INTEGER NOT NULL,status TEXT NOT NULL,details TEXT,PRIMARY KEY(agent,batch));
CREATE TABLE forward_versions(agent TEXT NOT NULL,batch INTEGER NOT NULL,activation_open TEXT NOT NULL,rule_json TEXT NOT NULL,rule_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(agent,batch));
CREATE TABLE forward_halts(id INTEGER PRIMARY KEY CHECK(id=1),reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE forward_gap_events(close_at TEXT PRIMARY KEY,reason TEXT NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE forward_gap_resolutions(close_at TEXT PRIMARY KEY,resolved_at TEXT NOT NULL,evidence_json TEXT NOT NULL);
CREATE TABLE forward_halt_resolutions(halt_id INTEGER PRIMARY KEY,original_reason TEXT NOT NULL,resolved_at TEXT NOT NULL,resolution_code TEXT NOT NULL CHECK(resolution_code='RESOLVED'),evidence_json TEXT NOT NULL);
CREATE TRIGGER forward_halt_resolutions_no_update BEFORE UPDATE ON forward_halt_resolutions BEGIN SELECT RAISE(ABORT,'HALT_RESOLUTION_APPEND_ONLY'); END;
CREATE TRIGGER forward_halt_resolutions_no_delete BEFORE DELETE ON forward_halt_resolutions BEGIN SELECT RAISE(ABORT,'HALT_RESOLUTION_APPEND_ONLY'); END;
CREATE TABLE forward_version_approvals(agent TEXT NOT NULL,batch INTEGER NOT NULL,approved_at TEXT NOT NULL,approved_by TEXT NOT NULL,note TEXT NOT NULL,PRIMARY KEY(agent,batch));
CREATE TRIGGER forward_version_approvals_no_update BEFORE UPDATE ON forward_version_approvals BEGIN SELECT RAISE(ABORT,'VERSION_APPROVAL_APPEND_ONLY'); END;
CREATE TRIGGER forward_version_approvals_no_delete BEFORE DELETE ON forward_version_approvals BEGIN SELECT RAISE(ABORT,'VERSION_APPROVAL_APPEND_ONLY'); END;
CREATE TABLE shadow_ledger_meta(epoch_id TEXT PRIMARY KEY,epoch_status TEXT NOT NULL,history_status TEXT NOT NULL,created_at_utc TEXT NOT NULL,last_trusted_archive_cycle_utc TEXT NOT NULL,rebuild_authorization TEXT NOT NULL,initial_equity TEXT NOT NULL,currency TEXT NOT NULL,evolution_threshold INTEGER NOT NULL);
CREATE TRIGGER shadow_ledger_meta_no_update BEFORE UPDATE ON shadow_ledger_meta BEGIN SELECT RAISE(ABORT,'SHADOW_EPOCH_METADATA_APPEND_ONLY'); END;
CREATE TRIGGER shadow_ledger_meta_no_delete BEFORE DELETE ON shadow_ledger_meta BEGIN SELECT RAISE(ABORT,'SHADOW_EPOCH_METADATA_APPEND_ONLY'); END;
"""


def initialize_shadow_ledger(
    db_path: str | Path,
    *,
    epoch_id: str,
    created_at_utc: str,
    rebuild_authorization: str,
    last_trusted_archive_cycle_utc: str,
) -> dict:
    """Atomically initialize a new, flat A/B/C epoch; refuse any existing ledger or status."""
    target = Path(db_path).resolve()
    status_path = target.with_name("status.json")
    if target.exists() or status_path.exists():
        raise FileExistsError("SHADOW_EPOCH_TARGET_ALREADY_EXISTS")
    if not epoch_id.strip() or not rebuild_authorization.strip():
        raise ValueError("EPOCH_ID_AND_AUTHORIZATION_REQUIRED")
    created = datetime.fromisoformat(created_at_utc.replace("Z", "+00:00"))
    trusted = datetime.fromisoformat(last_trusted_archive_cycle_utc.replace("Z", "+00:00"))
    if created.tzinfo is None or trusted.tzinfo is None:
        raise ValueError("EPOCH_TIMESTAMPS_MUST_INCLUDE_TIMEZONE")
    if created.astimezone(UTC) < trusted.astimezone(UTC):
        raise ValueError("EPOCH_PRECEDES_LAST_TRUSTED_ARCHIVE")

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".abc-shadow-new-epoch-", suffix=".sqlite3", dir=target.parent)
    os.close(fd)
    temp_db = Path(temp_name)
    status_temp = target.parent / f".status-{uuid4().hex}.tmp"
    created_text = created.astimezone(UTC).isoformat()
    status = {
        "state": EPOCH_STATUS,
        "pid": None,
        "updated_utc": created_text,
        "epoch_id": epoch_id,
        "history_status": HISTORY_STATUS,
        "last_trusted_archive_cycle_utc": trusted.astimezone(UTC).isoformat(),
        "last_bar_close": None,
        "last_quote_at": None,
        "note": "New research epoch; earlier shadow history is incomplete and was not imported.",
    }
    try:
        Ledger(temp_db)
        with closing(sqlite3.connect(temp_db, timeout=10)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            db.executescript(_FORWARD_SCHEMA)
            db.execute(
                "UPDATE evo_counts SET closed_count=0,next_batch=? WHERE agent IN ('A','B','C')",
                (EVOLUTION_THRESHOLD,),
            )
            metadata = (
                epoch_id,
                EPOCH_STATUS,
                HISTORY_STATUS,
                created_text,
                trusted.astimezone(UTC).isoformat(),
                rebuild_authorization,
                INITIAL_EQUITY,
                PAPER_CURRENCY,
                EVOLUTION_THRESHOLD,
            )
            db.execute("INSERT INTO shadow_ledger_meta VALUES(?,?,?,?,?,?,?,?,?)", metadata)
            audit_payload = {
                "epoch_id": epoch_id,
                "epoch_status": EPOCH_STATUS,
                "history_status": HISTORY_STATUS,
                "created_at_utc": created_text,
                "last_trusted_archive_cycle_utc": trusted.astimezone(UTC).isoformat(),
                "rebuild_authorization": rebuild_authorization,
                "initial_equity": INITIAL_EQUITY,
                "currency": PAPER_CURRENCY,
                "evolution_threshold": EVOLUTION_THRESHOLD,
                "imported_old_history": False,
                "imported_formal_positions": False,
            }
            db.execute(
                "INSERT INTO audit_events(event_id,agent,kind,payload,occurred_at) VALUES(?,?,?,?,?)",
                (str(uuid4()), "SYSTEM", "SHADOW_LEDGER_REBUILT_NEW_EPOCH", json.dumps(audit_payload, sort_keys=True), created_text),
            )
            db.commit()
            check = db.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError("SHADOW_EPOCH_INTEGRITY_FAILED:" + str(check))
            accounts = db.execute("SELECT agent,currency,cash,state,position_json,pending_stop_json FROM accounts ORDER BY agent").fetchall()
            expected = [(a, PAPER_CURRENCY, INITIAL_EQUITY, "FLAT", None, None) for a in "ABC"]
            if accounts != expected:
                raise RuntimeError("SHADOW_EPOCH_ACCOUNTS_INVALID")
            if db.execute("SELECT count(*) FROM evo_counts WHERE next_batch=? AND closed_count=0", (EVOLUTION_THRESHOLD,)).fetchone()[0] != 3:
                raise RuntimeError("SHADOW_EPOCH_THRESHOLD_INVALID")
            if any(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("fills", "forward_cycles", "forward_trades", "forward_positions", "forward_reviews", "forward_versions")):
                raise RuntimeError("NEW_EPOCH_CONTAINS_UNEXPECTED_HISTORY")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        status_temp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if target.exists() or status_path.exists():
            raise FileExistsError("SHADOW_EPOCH_TARGET_ALREADY_EXISTS")
        os.rename(temp_db, target)
        try:
            os.rename(status_temp, status_path)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return {"state": EPOCH_STATUS, "history_status": HISTORY_STATUS, "epoch_id": epoch_id, "created_at_utc": created_text, "db_path": str(target), "status_path": str(status_path), "sha256": digest}
    finally:
        temp_db.unlink(missing_ok=True)
        Path(str(temp_db) + "-wal").unlink(missing_ok=True)
        Path(str(temp_db) + "-shm").unlink(missing_ok=True)
        status_temp.unlink(missing_ok=True)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-new-epoch", action="store_true", required=True)
    parser.add_argument("--epoch-id", default=f"abc-shadow-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
    args = parser.parse_args()
    folder = root / "docs" / "reports" / "abc-forward-v26"
    freeze = json.loads((folder / "freeze.json").read_text(encoding="utf-8-sig"))
    expected = freeze["risk_execution_hashes"]["backend/market.py"]
    actual = hashlib.sha256((root / "backend" / "market.py").read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit("FROZEN_MARKET_HASH_MISMATCH")
    if not (root / "data" / "paper.sqlite3").is_file():
        raise SystemExit("FORMAL_LEDGER_MISSING")
    result = initialize_shadow_ledger(
        folder / "shadow-ledger.sqlite3",
        epoch_id=args.epoch_id,
        created_at_utc=datetime.now(UTC).isoformat(),
        rebuild_authorization="User explicitly authorized a new empty ABC shadow epoch; missing history remains ARCHIVED_INCOMPLETE.",
        last_trusted_archive_cycle_utc="2026-11-02T14:45:00+00:00",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
