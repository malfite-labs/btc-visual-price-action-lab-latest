"""One CLI for the existing declarative strategy-pool lifecycle.

Examples:
  python scripts/manage_strategy.py validate strategy.json
  python scripts/manage_strategy.py upload strategy.json
  python scripts/manage_strategy.py list
  python scripts/manage_strategy.py enable STRATEGY_ID 1.0.0
  python scripts/manage_strategy.py pause STRATEGY_ID
  python scripts/manage_strategy.py rollback STRATEGY_ID 1.0.0 --confirm-rollback
  python scripts/manage_strategy.py archive STRATEGY_ID
  python scripts/manage_strategy.py restore STRATEGY_ID 1.0.0

Uploads use the existing local 8011 API and remain disabled. Test the candidate
with scripts/strategy_pool_sandbox.py before explicitly enabling it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.strategy_pool import StrategyValidationError, validate_strategy  # noqa: E402
from scripts.ledger_backup import run_once  # noqa: E402


def _require_prechange_backup() -> None:
    secondary = os.getenv("BTC_LAB_BACKUP_SECONDARY")
    result = run_once(ROOT, ROOT / "logs" / "ledger-backups",
                      Path(secondary).resolve() if secondary else None)
    if result["errors"]:
        raise RuntimeError("PRE_CHANGE_BACKUP_FAILED:" + json.dumps(result["errors"], ensure_ascii=False))


def _request(base: str, path: str, *, method: str = "GET", payload: bytes | None = None):
    req = Request(base.rstrip("/") + path, data=payload, method=method,
                  headers={"Content-Type": "application/json"} if payload is not None else {})
    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP_{exc.code}:{body}") from exc
    except URLError as exc:
        raise RuntimeError(f"BACKEND_UNAVAILABLE:{exc.reason}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("file", type=Path)
    upload = commands.add_parser("upload")
    upload.add_argument("file", type=Path)
    commands.add_parser("list")
    versions = commands.add_parser("versions")
    versions.add_argument("strategy_id")
    enable = commands.add_parser("enable")
    enable.add_argument("strategy_id")
    enable.add_argument("version")
    pause = commands.add_parser("pause")
    pause.add_argument("strategy_id")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("strategy_id")
    rollback.add_argument("version")
    rollback.add_argument("--confirm-rollback", action="store_true")
    archive = commands.add_parser("archive")
    archive.add_argument("strategy_id")
    restore = commands.add_parser("restore")
    restore.add_argument("strategy_id")
    restore.add_argument("version")
    check = commands.add_parser("check")
    check.add_argument("strategy_id")
    check.add_argument("--max-cycle-age-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    try:
        if args.command in {"validate", "upload"}:
            raw = args.file.read_bytes()
            doc, digest = validate_strategy(raw)
            if args.command == "validate":
                result = {"valid": True, "strategy_id": doc["strategy_id"], "version": doc["version"], "sha256": digest, "enabled": False}
            else:
                _require_prechange_backup()
                result = _request(args.base_url, "/api/v1/strategy-pool/upload", method="POST", payload=raw)
                if result.get("enabled") is not False:
                    raise RuntimeError("UPLOAD_DID_NOT_REMAIN_DISABLED")
        elif args.command == "list":
            result = _request(args.base_url, "/api/v1/strategy-pool")
        elif args.command == "versions":
            result = _request(args.base_url, f"/api/v1/strategy-pool/{args.strategy_id}/versions")
        elif args.command in {"enable", "pause", "rollback"}:
            if args.command == "rollback" and not args.confirm_rollback:
                raise RuntimeError("ROLLBACK_REQUIRES_--confirm-rollback")
            _require_prechange_backup()
            enabled = args.command != "pause"
            version = args.version if enabled else None
            result = _request(args.base_url, f"/api/v1/strategy-pool/{args.strategy_id}/enable", method="POST",
                              payload=json.dumps({"version": version, "enabled": enabled}).encode())
        elif args.command == "archive":
            _require_prechange_backup()
            result = _request(args.base_url, f"/api/v1/strategy-pool/{args.strategy_id}/archive", method="POST")
        elif args.command == "restore":
            _require_prechange_backup()
            result = _request(args.base_url, f"/api/v1/strategy-pool/{args.strategy_id}/restore", method="POST",
                              payload=json.dumps({"version": args.version}).encode())
        else:
            status = _request(args.base_url, "/api/v1/strategy-pool")
            system = _request(args.base_url, "/api/v1/system/status")
            quote = system.get("quote") or {}
            item = next((x for x in status.get("items", []) if x.get("id") == args.strategy_id), None)
            if item is None:
                raise RuntimeError("STRATEGY_NOT_REGISTERED")
            versions = _request(args.base_url, f"/api/v1/strategy-pool/{args.strategy_id}/versions").get("items", [])
            active = next((x for x in versions if x.get("version") == item.get("active_version")), None)
            document = json.loads(active["document"]) if active and active.get("document") else {}
            risk = document.get("risk") or {}
            try:
                risk_ok = Decimal(str(risk.get("risk_fraction", "NaN"))) <= Decimal("0.02") and Decimal(str(risk.get("risk_fraction", "NaN"))) > 0 and Decimal(str(risk.get("max_leverage", "NaN"))) <= Decimal("5") and Decimal(str(risk.get("max_leverage", "NaN"))) > 0
            except InvalidOperation:
                risk_ok = False
            position = item.get("position")
            if isinstance(position, str) and position:
                try:
                    position = json.loads(position)
                except ValueError:
                    position = {"_invalid": True}
            stop_target_ok = True
            if position:
                try:
                    if position.get("pending_entry"):
                        stop_target_ok = Decimal(str(position["stop"])) > 0
                    else:
                        entry = Decimal(str(position["entry"]))
                        stop = Decimal(str(position["stop"]))
                        direction = position["direction"]
                        stop_target_ok = entry > 0 and ((direction == "LONG" and stop < entry) or (direction == "SHORT" and stop > entry))
                        target = position.get("target")
                        if target is not None:
                            target = Decimal(str(target))
                            stop_target_ok = stop_target_ok and ((direction == "LONG" and target > entry) or (direction == "SHORT" and target < entry))
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    stop_target_ok = False
            fills = _request(args.base_url, f"/api/v1/strategy-pool/{args.strategy_id}/fills?limit=2000").get("items", [])
            keys = [x.get("idempotency_key") for x in fills]
            age = quote.get("age_ms")
            checks = {
                "enabled": item.get("enabled") is True,
                "quote_fresh": system.get("quote_health") == "FRESH" and isinstance(age, (int, float)) and age <= 60000,
                "natural_cycle_observed": item.get("last_cycle") is not None,
                "cycle_recent": False,
                "account_present": item.get("equity") is not None,
                "active_definition_present": active is not None and bool(active.get("sha256")),
                "risk_limits_within_2pct_and_5x": risk_ok,
                "stop_target_consistent": stop_target_ok,
                "no_duplicate_fill_keys_in_latest_2000": len(keys) == len(set(keys)),
                "status": item.get("status"),
                "last_cycle": item.get("last_cycle"),
                "last_action": item.get("last_action"),
                "open_position": item.get("position_direction"),
            }
            if item.get("last_cycle"):
                from datetime import datetime, timezone
                cycle = datetime.fromisoformat(item["last_cycle"].replace("Z", "+00:00"))
                checks["cycle_recent"] = 0 <= (datetime.now(timezone.utc) - cycle).total_seconds() <= args.max_cycle_age_seconds
            result = {"strategy_id": args.strategy_id, "checks": checks,
                      "passed": all(checks[k] for k in ("enabled", "quote_fresh", "natural_cycle_observed", "cycle_recent", "account_present", "active_definition_present", "risk_limits_within_2pct_and_5x", "stop_target_consistent", "no_duplicate_fill_keys_in_latest_2000"))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, StrategyValidationError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
