"""Import the verified PA-01..08 archive rules into isolated paper accounts.

This does not create fills. Existing versions are never overwritten; each archive
document is checked against its archived canonical SHA before an account is armed.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT.parent / "strategy_archive_16"
DB = ROOT / "data" / "strategy-pool.sqlite3"
sys.path.insert(0, str(ROOT))

from backend.strategy_pool import StrategyPool, validate_strategy


def backup_database() -> Path:
    DB.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = ROOT / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"strategy-pool-before-pa-bootstrap-{stamp}.sqlite3"
    if DB.exists():
        source = sqlite3.connect(DB)
        try:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    return target


def main() -> None:
    backup = backup_database()
    pool = StrategyPool(DB)
    results = []
    for archive_id in (f"PA-{n:02d}" for n in range(1, 9)):
        archived = json.loads(
            (ARCHIVE / archive_id / "strategy.json").read_text(encoding="utf-8-sig")
        )
        observation = archived.get("runtime_observation", {})
        library = archived.get("library", {})
        if observation.get("enabled") is not True or observation.get("entry_permitted") is not True:
            raise RuntimeError(f"ARCHIVE_NOT_ARMED:{archive_id}")
        if library.get("canonical_sha256_verified") is not True:
            raise RuntimeError(f"ARCHIVE_HASH_NOT_VERIFIED:{archive_id}")
        document = archived["rule_payload"]["registry_document"]
        validated, digest = validate_strategy(document)
        if digest != library.get("sha256"):
            raise RuntimeError(f"CANONICAL_HASH_MISMATCH:{archive_id}")

        sid, version = validated["strategy_id"], validated["version"]
        with sqlite3.connect(DB) as conn:
            existing = conn.execute(
                "SELECT sha256 FROM versions WHERE strategy_id=? AND version=?",
                (sid, version),
            ).fetchone()
        if existing is None:
            pool.upload(json.dumps(validated, sort_keys=True, separators=(",", ":")))
        elif existing[0] != digest:
            raise RuntimeError(f"EXISTING_VERSION_CONFLICT:{archive_id}:{sid}:{version}")

        pool.enable(sid, version, True)
        results.append({"archive_id": archive_id, "strategy_id": sid, "version": version,
                        "initial_cash": "10000.00", "enabled": True, "sha256": digest})

    print(json.dumps({"backup": str(backup), "registered": results}, indent=2))


if __name__ == "__main__":
    main()
