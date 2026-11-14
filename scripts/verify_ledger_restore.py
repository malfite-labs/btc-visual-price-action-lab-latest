"""Exercise a SQLite backup by restoring it into an isolated temporary database."""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

try:
    from .ledger_backup import _schema_summary, _connect_readonly, _integrity
except ImportError:  # direct ``python scripts/verify_ledger_restore.py`` execution
    from ledger_backup import _schema_summary, _connect_readonly, _integrity


def verify(source: Path, output_dir: Path) -> dict:
    source = source.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    restored = output_dir / f"restore-test-{source.stem}.sqlite3"
    restored.unlink(missing_ok=True)
    with closing(_connect_readonly(source)) as src, closing(sqlite3.connect(restored, timeout=30)) as dst:
        src_schema = _schema_summary(src)
        src.backup(dst, pages=256, sleep=0.05)
        dst.commit()
        restored_schema = _schema_summary(dst)
    integrity = _integrity(restored)
    passed = integrity == "ok" and src_schema == restored_schema
    result = {"source_backup": str(source), "restored_test_copy": str(restored),
              "integrity_check": integrity, "schema_and_row_counts_match": src_schema == restored_schema,
              "tables": len(restored_schema["tables"]), "restore_test": "PASS" if passed else "FAIL"}
    if not passed:
        raise RuntimeError(json.dumps(result))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="verified backup SQLite file")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    out = args.output_dir or Path(tempfile.mkdtemp(prefix="btc-ledger-restore-test-"))
    print(json.dumps(verify(args.source, out), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
