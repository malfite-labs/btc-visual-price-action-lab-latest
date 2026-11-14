import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scripts.ledger_backup import discover_databases, run_once
from scripts.verify_ledger_restore import verify


def _db(path: Path, value: str = "ok"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS accounts(id TEXT, cash TEXT)")
        db.execute("INSERT INTO accounts VALUES('A', ?)", (value,))


def test_discovers_all_sqlite_but_not_backup_tree(tmp_path):
    _db(tmp_path / "data" / "paper.sqlite3")
    _db(tmp_path / "docs" / "reports" / "abc-forward-v26" / "shadow-ledger.sqlite3")
    _db(tmp_path / "logs" / "old.sqlite3")
    assert len(discover_databases(tmp_path)) == 2


def test_online_backup_validates_and_preserves_source(tmp_path):
    root = tmp_path / "project"
    source = root / "data" / "paper.sqlite3"
    _db(source, "123")
    before = source.read_bytes()
    result = run_once(root, tmp_path / "backups", now=datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc))
    assert result["errors"] == []
    assert result["discovered_count"] == 1
    assert result["successful_snapshots"] == {"rolling": 1, "daily": 1, "weekly": 1}
    assert source.read_bytes() == before
    snap = next((tmp_path / "backups" / "rolling").rglob("*.sqlite3"))
    with sqlite3.connect(snap) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT cash FROM accounts").fetchone()[0] == "123"
    meta = json.loads(snap.with_suffix(".sqlite3.json").read_text(encoding="utf-8"))
    assert len(meta["sha256"]) == 64
    assert meta["schema"]["account_position_summary"][0]["account_id"] == "A"


def test_daily_weekly_snapshots_are_not_overwritten(tmp_path):
    root = tmp_path / "project"
    source = root / "data" / "paper.sqlite3"
    _db(source, "first")
    backups = tmp_path / "backups"
    first = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
    run_once(root, backups, now=first)
    _db(source, "second")
    second = datetime(2026, 11, 14, 10, 15, tzinfo=timezone.utc)
    result = run_once(root, backups, now=second)
    assert result["successful_snapshots"] == {"rolling": 1, "daily": 0, "weekly": 0}
    daily = next((backups / "daily").rglob("*.sqlite3"))
    with sqlite3.connect(daily) as db:
        assert db.execute("SELECT cash FROM accounts").fetchone()[0] == "first"


def test_secondary_snapshot_is_verified(tmp_path):
    root = tmp_path / "project"
    _db(root / "data" / "paper.sqlite3")
    result = run_once(root, tmp_path / "local", tmp_path / "secondary")
    assert result["errors"] == []
    assert next((tmp_path / "secondary").rglob("*.sqlite3")).exists()


def test_restore_verification_uses_isolated_copy(tmp_path):
    root = tmp_path / "project"
    _db(root / "data" / "paper.sqlite3")
    backups = tmp_path / "backups"
    run_once(root, backups, now=datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc))
    backup = next((backups / "rolling").rglob("*.sqlite3"))
    result = verify(backup, tmp_path / "restore-test")
    assert result["restore_test"] == "PASS"
    assert result["integrity_check"] == "ok"
