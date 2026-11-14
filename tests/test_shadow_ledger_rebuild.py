import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from backend.domain import CostModel
from backend.evolution import ReviewCoordinator
from backend.ledger import Ledger
from scripts.rebuild_abc_shadow_ledger import initialize_shadow_ledger


CREATED = "2026-11-14T10:00:00+00:00"


def new_epoch(path):
    return initialize_shadow_ledger(
        path,
        epoch_id="shadow-epoch-test-001",
        created_at_utc=CREATED,
        rebuild_authorization="user-approved-rebuild",
        last_trusted_archive_cycle_utc="2026-11-02T14:45:00+00:00",
    )


def test_new_epoch_uses_current_account_config_and_marks_history_incomplete(tmp_path):
    db_path = tmp_path / "abc-shadow" / "shadow-ledger.sqlite3"

    result = new_epoch(db_path)

    assert result["state"] == "REBUILT_NEW_EPOCH"
    assert result["history_status"] == "ARCHIVED_INCOMPLETE"
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        accounts = db.execute(
            "SELECT agent,currency,cash,state,position_json,pending_stop_json FROM accounts ORDER BY agent"
        ).fetchall()
        assert accounts == [
            ("A", "USD_RESEARCH", "10000.00", "FLAT", None, None),
            ("B", "USD_RESEARCH", "10000.00", "FLAT", None, None),
            ("C", "USD_RESEARCH", "10000.00", "FLAT", None, None),
        ]
        assert db.execute("SELECT agent,closed_count,next_batch FROM evo_counts ORDER BY agent").fetchall() == [
            ("A", 0, 40), ("B", 0, 40), ("C", 0, 40)
        ]
        assert db.execute("SELECT count(*) FROM forward_cycles").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM forward_trades").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM forward_positions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM forward_reviews").fetchone()[0] == 0
        metadata = db.execute("SELECT epoch_id,history_status,last_trusted_archive_cycle_utc FROM shadow_ledger_meta").fetchone()
        assert metadata == ("shadow-epoch-test-001", "ARCHIVED_INCOMPLETE", "2026-11-02T14:45:00+00:00")
        audit = json.loads(db.execute("SELECT payload FROM audit_events WHERE kind='SHADOW_LEDGER_REBUILT_NEW_EPOCH'").fetchone()[0])
        assert audit["epoch_id"] == "shadow-epoch-test-001"
    status = json.loads(db_path.with_name("status.json").read_text(encoding="utf-8"))
    assert status["state"] == "REBUILT_NEW_EPOCH"
    assert status["last_bar_close"] is None
    assert status["last_quote_at"] is None


def test_shadow_epoch_records_isolated_cycle_and_paper_fill_then_reopens(tmp_path):
    db_path = tmp_path / "shadow-ledger.sqlite3"
    new_epoch(db_path)
    cycle = "2026-11-14T10:15:00+00:00"
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO forward_cycles(close_at,status,error) VALUES(?,?,NULL)", (cycle, "COMPLETE"))
    ledger = Ledger(db_path)
    fill = ledger.open_position(
        agent="A", direction="LONG", quantity=Decimal("0.1"), price=Decimal("10000"),
        stop=Decimal("9900"), fee=Decimal("0.50"), slippage=Decimal("0.20"),
        idempotency_key="isolated-new-epoch-fill", expected_version=0, cost_model=CostModel(),
    )
    assert fill["accepted"] is True
    reopened = Ledger(db_path)
    assert reopened.account("A")["state"] == "OPEN_LONG"
    assert len(reopened.fills("A")) == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM forward_cycles WHERE close_at=?", (cycle,)).fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_new_epoch_review_coordinator_keeps_forty_trade_spacing(tmp_path):
    db_path = tmp_path / "shadow-ledger.sqlite3"
    new_epoch(db_path)
    coordinator = ReviewCoordinator(db_path, batch_size=40)
    for n in range(1, 40):
        assert coordinator.record_completed_trade("A", f"trade-{n}")["queued"] is False
    assert coordinator.record_completed_trade("A", "trade-40")["queued"] is True
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT next_batch FROM evo_counts WHERE agent='A'").fetchone()[0] == 80


def test_rebuild_refuses_to_overwrite_existing_ledger_or_status(tmp_path):
    db_path = tmp_path / "shadow-ledger.sqlite3"
    db_path.write_bytes(b"existing-ledger")

    with pytest.raises(FileExistsError):
        new_epoch(db_path)
    assert db_path.read_bytes() == b"existing-ledger"

    db_path.unlink()
    status = db_path.with_name("status.json")
    status.write_text("existing-status", encoding="utf-8")
    with pytest.raises(FileExistsError):
        new_epoch(db_path)
    assert not db_path.exists()
    assert status.read_text(encoding="utf-8") == "existing-status"
