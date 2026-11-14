"""Discover SQLite ledgers and make verified, transaction-consistent backups.

Run every 15 minutes from Task Scheduler with ``--once``.  The script uses
SQLite's Online Backup API, validates each destination, and never modifies a
source ledger.  Backups are local unless BTC_LAB_BACKUP_SECONDARY points to a
separate trusted storage root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_ROOT = ROOT / "logs" / "ledger-backups"
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "logs", "backups", "snapshots"}
DB_SUFFIXES = (".sqlite", ".sqlite3", ".db")
# A 15-minute cadence yields 192 snapshots in 48 hours.
RETENTION = {"rolling": 192, "daily": 30, "weekly": 12}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def discover_databases(root: Path = ROOT) -> list[Path]:
    """Find SQLite files in project-owned data/config/report roots, excluding archives."""
    found: set[Path] = set()
    for area in ("data", "docs/reports", "knowledge"):
        base = root / area
        if not base.exists():
            continue
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED_DIRS]
            for name in files:
                lower = name.lower()
                if lower.endswith(DB_SUFFIXES):
                    p = (Path(current) / name).resolve()
                    # Reports contain preserved point-in-time backups. Only the
                    # explicitly configured ABC shadow ledger is an active
                    # report-path database; don't recursively back up archives.
                    if area == "docs/reports" and p.relative_to(root.resolve()).as_posix().lower() != "docs/reports/abc-forward-v26/shadow-ledger.sqlite3":
                        continue
                    if p.is_file():
                        found.add(p)
    return sorted(found)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)


def _schema_summary(db: sqlite3.Connection) -> dict:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    counts: dict[str, int | None] = {}
    for table in tables:
        # Table names originate from sqlite_master and are quoted as identifiers.
        escaped = table.replace('"', '""')
        try:
            counts[table] = int(db.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0])
        except sqlite3.Error:
            counts[table] = None
    accounts = []
    if "accounts" in tables:
        columns = [x[1] for x in db.execute('PRAGMA table_info("accounts")')]
        for values in db.execute('SELECT * FROM "accounts"'):
            row = dict(zip(columns, values))
            position_value = row.get("position") or row.get("position_json")
            if isinstance(position_value, str):
                try:
                    position_value = json.loads(position_value)
                except ValueError:
                    position_value = {"unparsed_position": True}
            position_value = position_value if isinstance(position_value, dict) else None
            accounts.append({
                "account_id": row.get("agent") or row.get("strategy_id") or row.get("id"),
                "state": row.get("state"),
                "cash": row.get("cash"),
                "realized": row.get("realized") or row.get("realized_pnl"),
                "position": ({k: position_value.get(k) for k in
                              ("direction", "qty", "quantity", "entry", "entry_price", "stop", "stop_price", "target", "trade_id", "pending_entry")
                              if k in position_value} if position_value else None),
                "pending_stop": bool(row.get("pending_stop_json")),
            })
    return {"user_version": version, "tables": tables, "row_counts": counts,
            "account_position_summary": accounts}


def _integrity(path: Path) -> str:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
    return str(result)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _classify(path: Path, root: Path) -> tuple[str, str, bool]:
    rel = path.relative_to(root).as_posix().lower()
    active = rel.startswith("data/") or rel == "docs/reports/abc-forward-v26/shadow-ledger.sqlite3" or rel.startswith("knowledge/")
    module = "ABC shadow" if "abc-forward-v26/shadow-ledger" in rel else "PA strategy pool" if "strategy-pool" in rel else "market archive" if "market-candles" in rel else "G mirror" if "g/mirror" in rel else "D paper" if "/d/" in rel else "E paper" if "/e/" in rel else "ABC formal" if rel.endswith("/paper.sqlite3") else "strategy/control/research"
    account_type = "market_data" if "market-candles" in rel else "simulation_or_control"
    return module, account_type, active


def _prune(folder: Path, keep: int) -> None:
    files = sorted((p for p in folder.glob("*.sqlite3") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink()
        old.with_suffix(old.suffix + ".json").unlink(missing_ok=True)


def _snapshot(source: Path, dest: Path) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with closing(_connect_readonly(source)) as src, closing(sqlite3.connect(temp, timeout=30)) as dst:
            src.backup(dst, pages=256, sleep=0.05)
            dst.commit()
            schema = _schema_summary(dst)
        result = _integrity(temp)
        if result != "ok":
            raise sqlite3.DatabaseError(f"BACKUP_INTEGRITY_FAILED:{result}")
        os.replace(temp, dest)
        return {"sha256": _sha256(dest), "integrity_check": result, "schema": schema,
                "size_bytes": dest.stat().st_size}
    finally:
        temp.unlink(missing_ok=True)


def run_once(root: Path = ROOT, backup_root: Path = DEFAULT_BACKUP_ROOT,
             secondary_root: Path | None = None, now: datetime | None = None) -> dict:
    now = now or utc_now()
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = discover_databases(root)
    inventory = []
    errors = []
    snapshots = {"rolling": [], "daily": [], "weekly": []}
    day_key = now.astimezone(timezone.utc).strftime("%Y%m%d")
    week_key = now.astimezone(timezone.utc).strftime("%G-W%V")
    for source in paths:
        module, account_type, active = _classify(source, root)
        inventory_item = None
        try:
            with closing(_connect_readonly(source)) as db:
                schema = _schema_summary(db)
            inventory_item = {"path": source.relative_to(root).as_posix(), "module": module,
                              "account_type": account_type, "active": active,
                              "last_updated": datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat(),
                              "schema_version": schema["user_version"], "backup_at": now.isoformat(),
                              "backup_status": "PENDING"}
            inventory.append(inventory_item)
            rel = source.relative_to(root).with_suffix(".sqlite3")
            for tier, key in (("rolling", stamp), ("daily", day_key), ("weekly", week_key)):
                folder = backup_root / tier / rel.parent
                folder.mkdir(parents=True, exist_ok=True)
                dest = folder / f"{rel.stem}-{key}.sqlite3"
                if tier != "rolling" and dest.exists():
                    metadata = dest.with_suffix(dest.suffix + ".json")
                    try:
                        saved = json.loads(metadata.read_text(encoding="utf-8"))
                        if _integrity(dest) == "ok" and _sha256(dest) == saved.get("sha256"):
                            continue
                    except (OSError, ValueError, sqlite3.Error):
                        pass  # replace an unverifiable daily/weekly snapshot safely
                info = _snapshot(source, dest)
                meta = {"source": source.relative_to(root).as_posix(), "module": module,
                        "account_type": account_type, "active": active, "backup_at": now.isoformat(),
                        "tier": tier, **info}
                dest.with_suffix(dest.suffix + ".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
                snapshots[tier].append(meta)
                if secondary_root:
                    second = secondary_root / tier / rel.parent / dest.name
                    second.parent.mkdir(parents=True, exist_ok=True)
                    with closing(sqlite3.connect(dest, timeout=30)) as src2, closing(sqlite3.connect(second, timeout=30)) as dst2:
                        src2.backup(dst2, pages=256, sleep=0.05)
                    if _integrity(second) != "ok" or _sha256(second) != info["sha256"]:
                        raise sqlite3.DatabaseError("SECONDARY_BACKUP_VERIFY_FAILED")
                    second.with_suffix(second.suffix + ".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
                _prune(folder, RETENTION[tier])
            inventory_item["backup_status"] = "VERIFIED"
            inventory_item["backup_sha256"] = info["sha256"]
            inventory_item["schema_version"] = info["schema"]["user_version"]
        except Exception as exc:  # report per-ledger failures without stopping remaining copies
            message = f"{type(exc).__name__}:{exc}"
            if inventory_item is not None:
                inventory_item["backup_status"] = "FAILED"
            errors.append({"path": str(source), "error": message})
    backup_root.mkdir(parents=True, exist_ok=True)
    inventory_doc = {"generated_at": now.isoformat(), "discovered_count": len(paths),
                     "items": inventory, "errors": errors}
    (backup_root / "inventory.json").write_text(json.dumps(inventory_doc, indent=2), encoding="utf-8")
    result = {"generated_at": now.isoformat(), "discovered_count": len(paths),
              "successful_snapshots": {k: len(v) for k, v in snapshots.items()},
              "errors": errors, "secondary_root_configured": bool(secondary_root),
              "local_backup_root": str(backup_root), "snapshots": snapshots}
    with (backup_root / "runs.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({k: v for k, v in result.items() if k != "snapshots"}) + "\n")
    if errors:
        with (backup_root / "alerts.jsonl").open("a", encoding="utf-8") as stream:
            for error in errors:
                stream.write(json.dumps({"at": now.isoformat(), "severity": "RECOVERY_CAPABILITY_DEGRADED", **error}) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="make one verified backup pass")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--secondary-root", type=Path, default=os.getenv("BTC_LAB_BACKUP_SECONDARY"))
    args = parser.parse_args(argv)
    secondary = args.secondary_root.resolve() if args.secondary_root else None
    result = run_once(args.root.resolve(), args.backup_root.resolve(), secondary)
    print(json.dumps({k: v for k, v in result.items() if k != "snapshots"}, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
