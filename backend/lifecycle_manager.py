"""Unified Strategy Lifecycle Manager for BTC Paper Lab V3.1.

Covers ALL strategies uniformly (A/B/C/D/E/G, PA-01..10, Chan, R01-R06, S01-S12, AY02-04,
and any future strategy) with minimal persistent metadata (`data/strategy-lifecycle.sqlite3`):
- Soft Pause (unified across all strategies: blocks new entries, preserves open positions & SL/TP)
- Resume
- Archive (gated, unloads runtime resources, preserves cold archive, restorable)
- Delete (8-gate checked under account lock, exports immutable cold archive, dynamically unloads
  scheduler/cache/watchlist/DB handles, writes anti-resurrection tombstone)
- Restore & Explicit Re-register

Never duplicates trading accounts or fills; never auto-closes positions to force deletion;
never modifies Demo or Production copy-trading configurations.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

UTC = timezone.utc

VALID_STATES = ("REGISTERED", "RUNNING", "PAUSED", "ARCHIVED", "DELETED")
EXCLUDED_RUNTIME_STATES = ("ARCHIVED", "DELETED")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class LifecycleRegistry:
    """Minimal SQLite metadata & anti-resurrection tombstone store (`strategy-lifecycle.sqlite3`)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._tx() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_lifecycle (
                    strategy_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    strategy_type TEXT,
                    rule_sha256 TEXT,
                    archive_path TEXT,
                    reason TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lifecycle_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _tx(self):
        with self._lock:
            conn = sqlite3.connect(str(self.path), timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    yield conn
            finally:
                conn.close()

    def get(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM strategy_lifecycle WHERE strategy_id=?",
                (str(strategy_id),),
            ).fetchone()
            return dict(row) if row else None

    def set_state(
        self,
        strategy_id: str,
        state: str,
        *,
        strategy_type: Optional[str] = None,
        rule_sha256: Optional[str] = None,
        archive_path: Optional[str] = None,
        reason: str = "",
        audit_details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if state not in VALID_STATES:
            raise ValueError(f"INVALID_LIFECYCLE_STATE:{state}")
        sid = str(strategy_id)
        now = _utc_now()
        with self._tx() as db:
            prev = db.execute(
                "SELECT strategy_type, rule_sha256, archive_path FROM strategy_lifecycle WHERE strategy_id=?",
                (sid,),
            ).fetchone()
            st_type = strategy_type or (prev["strategy_type"] if prev else None)
            sha = rule_sha256 or (prev["rule_sha256"] if prev else None)
            arch = archive_path if archive_path is not None else (prev["archive_path"] if prev else None)
            db.execute(
                """
                INSERT INTO strategy_lifecycle(strategy_id, state, strategy_type, rule_sha256, archive_path, reason, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    state=excluded.state,
                    strategy_type=COALESCE(excluded.strategy_type, strategy_lifecycle.strategy_type),
                    rule_sha256=COALESCE(excluded.rule_sha256, strategy_lifecycle.rule_sha256),
                    archive_path=COALESCE(excluded.archive_path, strategy_lifecycle.archive_path),
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (sid, state, st_type, sha, arch, reason, now),
            )
            db.execute(
                "INSERT INTO lifecycle_audit(strategy_id, action, details, at) VALUES(?, ?, ?, ?)",
                (sid, state, json.dumps(audit_details or {"reason": reason}, ensure_ascii=False, sort_keys=True), now),
            )
            row = db.execute("SELECT * FROM strategy_lifecycle WHERE strategy_id=?", (sid,)).fetchone()
            return dict(row)

    def excluded_ids(self) -> Set[str]:
        """IDs that must NOT be auto-registered or resurrected on startup (ARCHIVED or DELETED)."""
        with self._tx() as db:
            rows = db.execute(
                "SELECT strategy_id FROM strategy_lifecycle WHERE state IN ('ARCHIVED', 'DELETED')"
            ).fetchall()
            return {str(r["strategy_id"]) for r in rows}

    def deleted_ids(self) -> Set[str]:
        with self._tx() as db:
            rows = db.execute(
                "SELECT strategy_id FROM strategy_lifecycle WHERE state='DELETED'"
            ).fetchall()
            return {str(r["strategy_id"]) for r in rows}

    def archived_ids(self) -> Set[str]:
        with self._tx() as db:
            rows = db.execute(
                "SELECT strategy_id FROM strategy_lifecycle WHERE state='ARCHIVED'"
            ).fetchall()
            return {str(r["strategy_id"]) for r in rows}

    def all_records(self) -> List[Dict[str, Any]]:
        with self._tx() as db:
            rows = db.execute("SELECT * FROM strategy_lifecycle ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]

    def audit_log(self, strategy_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._tx() as db:
            if strategy_id:
                rows = db.execute(
                    "SELECT * FROM lifecycle_audit WHERE strategy_id=? ORDER BY id DESC LIMIT ?",
                    (str(strategy_id), limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM lifecycle_audit ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["details"] = json.loads(d["details"])
                except Exception:
                    pass
                out.append(d)
            return out


def inspect_copy_trading_bindings(
    strategy_id: str,
    *,
    demo_root: Optional[Path] = None,
    prod_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read-only check whether Demo or Production copy-trading has any binding or active process for strategy_id."""
    local_app = Path(os.environ.get("LOCALAPPDATA", ""))
    d_root = demo_root if demo_root is not None else (local_app / "BTC Paper Lab V3 Demo Acceptance")
    p_root = prod_root if prod_root is not None else (local_app / "BTC Paper Lab V3" / "production-copy")

    def _scan(root: Path, runtime_sub: str) -> Dict[str, Any]:
        bound_configs: List[str] = []
        active_runtime = False
        cfg_file = root / "copy-configs.json"
        if cfg_file.is_file():
            try:
                raw = json.loads(cfg_file.read_text(encoding="utf-8-sig"))
                items = raw.values() if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    cfg_sid = str(item.get("strategy") or item.get("strategy_id") or "")
                    if cfg_sid.lower() == strategy_id.lower():
                        if item.get("enabled", True) is not False or item.get("active", False) or item.get("state") not in ("DISABLED", "DELETED", "ARCHIVED"):
                            bound_configs.append(str(item.get("id") or item.get("name") or cfg_sid))
            except (OSError, ValueError, TypeError):
                # Conservative safety: unreadable copy config blocks deletion
                bound_configs.append("CONFIG_UNREADABLE")
        st_file = root / runtime_sub / "status.json"
        if st_file.is_file():
            try:
                st = json.loads(st_file.read_text(encoding="utf-8-sig"))
                if str(st.get("strategy") or "").lower() == strategy_id.lower():
                    if str(st.get("state") or "").upper() not in ("STOPPED", "IDLE", ""):
                        active_runtime = True
            except (OSError, ValueError, TypeError):
                pass
        return {
            "bound": bool(bound_configs or active_runtime),
            "bound_configs": bound_configs,
            "active_runtime": active_runtime,
        }

    demo_info = _scan(d_root, "dry-v2-service")
    prod_info = _scan(p_root, "runtime")
    return {"demo": demo_info, "production": prod_info}


def _checkpoint_sqlite(db_path: Path) -> None:
    """Safely flush WAL & close handle before archiving/moving a standalone SQLite ledger."""
    if not db_path.is_file():
        return
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


class StrategyLifecycleManager:
    """Orchestrates unified lifecycle operations across all strategy adapters with account-level locking."""

    def __init__(
        self,
        data_root: Path,
        *,
        account_locks: Any,
        active_index: Any,
        query_cache: Any,
        get_adapters: Callable[[], Dict[str, Any]],
        on_lifecycle_change: Optional[Callable[[str, str], None]] = None,
        demo_copy_root: Optional[Path] = None,
        prod_copy_root: Optional[Path] = None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.registry = LifecycleRegistry(self.data_root / "strategy-lifecycle.sqlite3")
        self.archive_root = self.data_root / "archived-deleted"
        self.account_locks = account_locks
        self.active_index = active_index
        self.query_cache = query_cache
        self._get_adapters = get_adapters
        self._on_lifecycle_change = on_lifecycle_change
        self.demo_copy_root = demo_copy_root
        self.prod_copy_root = prod_copy_root

    def _classify(self, strategy_id: str) -> Tuple[str, str]:
        """Return (canonical_id, adapter_kind)."""
        sid = str(strategy_id).strip()
        up = sid.upper()
        adapters = self._get_adapters()
        if up in ("A", "B", "C"):
            return up, "CORE_ABC"
        if up == "D":
            return "D", "CORE_D"
        if up == "E":
            return "E", "CORE_E"
        if up == "G":
            return "G", "MIRROR_G"
        if up in adapters.get("pa_extension").ledgers or up in ("PA-09", "PA-10"):
            return up, "PA_EXT"
        if up in adapters.get("r_series_ext").ledgers or up in ("R01", "R02", "R03", "R04", "R05", "R06"):
            return up, "R_SERIES"
        if sid in adapters.get("thc_ay_ext").ledgers or sid.lower() in adapters.get("thc_ay_ext").ledgers:
            return sid.lower(), "THC_AY"
        # Check strategy_pool DB
        pool = adapters.get("strategy_pool")
        if pool is not None:
            for it in pool.list():
                if it.get("id") == sid:
                    return sid, "STRATEGY_POOL"
        # Check lifecycle registry (archived/deleted)
        rec = self.registry.get(sid) or self.registry.get(up)
        if rec:
            return rec["strategy_id"], rec.get("strategy_type") or "UNKNOWN"
        raise KeyError(f"STRATEGY_NOT_FOUND:{strategy_id}")

    def inspect_runtime_state(self, strategy_id: str) -> Dict[str, Any]:
        """Inspect live enabled/position/pending/reconcile/db_path state of any strategy."""
        sid, kind = self._classify(strategy_id)
        adapters = self._get_adapters()
        enabled = False
        has_position = False
        has_pending = False
        reconcile_ok = True
        reconcile_issues: List[str] = []
        db_path: Optional[Path] = None
        rule_sha256: Optional[str] = None
        version: Optional[str] = None
        equity: Optional[str] = None
        fill_count: int = 0
        mem_cache_kb: float = 0.0

        if kind == "CORE_ABC":
            ctrl = adapters["strategy_control"]()
            st = ctrl.status(sid)
            enabled = st.get("state") == "RUNNING"
            acct = adapters["ledger"].account(sid)
            has_position = acct.get("state") not in (None, "FLAT")
            has_pending = acct.get("state") in ("EXIT_PENDING", "PENDING_CLOSE", "STOP_PENDING")
            db_path = Path(adapters["ledger"].path)
            equity = str(acct.get("cash") or "10000")
            fill_count = len(adapters["ledger"].fills(sid))
            mem_cache_kb = 12.0
        elif kind == "CORE_D":
            d_bridge = adapters.get("d_bridge")
            enabled = bool(d_bridge is not None and getattr(d_bridge, "enabled", True))
            db_path = self.data_root / "d" / "trading.sqlite3"
            if db_path.is_file():
                try:
                    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
                        row = db.execute("SELECT state FROM d_account LIMIT 1").fetchone()
                        if row and row[0] not in (None, "FLAT"):
                            has_position = True
                except sqlite3.Error:
                    pass
            mem_cache_kb = 16.0
        elif kind == "CORE_E":
            ctrl = adapters["strategy_control"]()
            enabled = ctrl.status("E").get("state") == "RUNNING"
            db_path = self.data_root / "e" / "formal.sqlite3"
            if db_path.is_file():
                from backend.e_paper_store import read_e_store
                e_st = read_e_store(db_path)
                for acct in (e_st.get("accounts") or {}).values():
                    if isinstance(acct, dict) and acct.get("state") not in (None, "FLAT"):
                        has_position = True
                        break
            mem_cache_kb = 16.0
        elif kind == "MIRROR_G":
            gm = adapters["g_mirror"]
            st = gm.status({})
            rec = self.registry.get("G")
            enabled = not (rec and rec.get("state") in ("PAUSED", "ARCHIVED", "DELETED"))
            has_position = bool(st.get("position"))
            db_path = Path(gm.path)
            fill_count = len(gm.fills())
            mem_cache_kb = 12.0
        elif kind == "PA_EXT":
            ext = adapters["pa_extension"]
            lg = ext.ledgers.get(sid)
            if lg is not None:
                st = lg.snapshot()
                enabled = bool(st.get("enabled") or sid in ext.enabled)
                has_position = bool(st.get("position"))
                has_pending = bool(st.get("pending"))
                db_path = Path(lg.path)
                equity = str(st.get("cash") or "10000")
                fill_count = len(lg.fills())
                version = "1.0.0"
                mem_cache_kb = 20.0
        elif kind == "R_SERIES":
            ext = adapters["r_series_ext"]
            lg = ext.ledgers.get(sid)
            if lg is not None:
                st = lg.snapshot()
                enabled = bool(st.get("enabled"))
                has_position = bool(st.get("position"))
                has_pending = bool(st.get("pending_entry") or st.get("pending_exit") or st.get("reconciling"))
                rec = lg.reconcile()
                reconcile_ok = bool(rec.get("ok"))
                reconcile_issues = list(rec.get("issues") or [])
                db_path = Path(lg.path)
                version = lg.spec.version
                equity = str(st.get("cash") or "10000")
                fill_count = int(rec.get("fill_count", 0))
                rule_sha256 = hashlib.sha256(json.dumps(lg.doc, sort_keys=True).encode()).hexdigest()
                mem_cache_kb = 32.0
        elif kind == "THC_AY":
            ext = adapters["thc_ay_ext"]
            lg = ext.ledgers.get(sid)
            if lg is not None:
                st = lg.snapshot()
                enabled = bool(st.get("enabled"))
                has_position = bool(st.get("position"))
                has_pending = bool(st.get("pending_entry") or st.get("pending_exit") or st.get("reconciling"))
                rec = lg.reconcile()
                reconcile_ok = bool(rec.get("ok"))
                reconcile_issues = list(rec.get("issues") or [])
                db_path = Path(lg.path)
                version = lg.spec.version
                rule_sha256 = lg.spec.config_sha256
                equity = str(st.get("cash") or "10000")
                fill_count = int(rec.get("fill_count", 0))
                mem_cache_kb = 28.0
        elif kind == "STRATEGY_POOL":
            pool = adapters["strategy_pool"]
            item = next((it for it in pool.list() if it.get("id") == sid), None)
            if item is not None:
                enabled = bool(item.get("enabled"))
                pos = item.get("position")
                if isinstance(pos, str):
                    pos = json.loads(pos)
                has_position = bool(pos and not pos.get("pending_entry"))
                has_pending = bool(pos and (pos.get("pending_entry") or pos.get("pending_exit")))
                db_path = Path(pool.path)
                version = item.get("active_version")
                equity = str(item.get("equity") or item.get("cash") or "10000")
                fill_count = int(item.get("fill_count") or 0)
                vers = pool.versions(sid)
                if vers:
                    rule_sha256 = vers[-1].get("sha256")
                mem_cache_kb = 24.0

        db_kb = round(db_path.stat().st_size / 1024.0, 1) if (db_path and db_path.is_file()) else 0.0
        reg_rec = self.registry.get(sid)
        lifecycle_state = reg_rec["state"] if reg_rec else ("RUNNING" if enabled else "PAUSED")
        return {
            "strategy_id": sid,
            "kind": kind,
            "lifecycle_state": lifecycle_state,
            "enabled": enabled,
            "has_position": has_position,
            "has_pending": has_pending,
            "reconcile_ok": reconcile_ok,
            "reconcile_issues": reconcile_issues,
            "version": version,
            "rule_sha256": rule_sha256,
            "equity": equity,
            "fill_count": fill_count,
            "db_path": str(db_path) if db_path else None,
            "db_size_kb": db_kb,
            "estimated_mem_kb": mem_cache_kb,
            "in_active_index": self.active_index.has_strategy(sid),
        }

    def check_delete_gates(
        self,
        strategy_id: str,
        *,
        confirm_strategy_id: Optional[str] = None,
        require_confirmation: bool = True,
    ) -> Dict[str, Any]:
        """Evaluate all 8 mandatory safety gates before Archive or Delete."""
        info = self.inspect_runtime_state(strategy_id)
        sid = info["strategy_id"]
        copy_info = inspect_copy_trading_bindings(
            sid, demo_root=self.demo_copy_root, prod_root=self.prod_copy_root
        )
        # Gate 5: C -> G mirror dependency check
        dep_blocked = False
        if sid == "C":
            g_rec = self.registry.get("G")
            if not (g_rec and g_rec.get("state") in ("ARCHIVED", "DELETED")):
                dep_blocked = True

        checks = [
            {
                "gate": "SG-1_PAUSED",
                "passed": not info["enabled"],
                "code": "LIFECYCLE_MUST_PAUSE_FIRST",
                "message": "策略已停止新开仓" if not info["enabled"] else "策略仍在运行中，请先执行柔和暂停",
            },
            {
                "gate": "SG-2_NO_POSITION",
                "passed": not info["has_position"],
                "code": "LIFECYCLE_OPEN_POSITION_BLOCKED",
                "message": "当前账户无持仓" if not info["has_position"] else "当前账户存在未平持仓，禁止归档或删除",
            },
            {
                "gate": "SG-3_NO_PENDING",
                "passed": not info["has_pending"],
                "code": "LIFECYCLE_PENDING_OR_FAULT_BLOCKED",
                "message": "无待开仓/待退出及异常事务" if not info["has_pending"] else "存在待执行订单或待恢复状态，禁止删除",
            },
            {
                "gate": "SG-4_NO_DEMO_COPY",
                "passed": not copy_info["demo"]["bound"],
                "code": "LIFECYCLE_DEMO_COPY_BOUND",
                "message": "未绑定 Demo 跟单" if not copy_info["demo"]["bound"] else f"已绑定 Demo 跟单配置：{copy_info['demo']['bound_configs']}",
            },
            {
                "gate": "SG-5_NO_PROD_COPY",
                "passed": not copy_info["production"]["bound"],
                "code": "LIFECYCLE_PROD_COPY_BOUND",
                "message": "未绑定正式实盘跟单" if not copy_info["production"]["bound"] else f"已绑定正式实盘跟单配置：{copy_info['production']['bound_configs']}",
            },
            {
                "gate": "SG-6_NO_STRATEGY_DEPENDENCY",
                "passed": not dep_blocked,
                "code": "LIFECYCLE_MIRROR_DEPENDENCY_ACTIVE",
                "message": "无其他策略依赖" if not dep_blocked else "镜像策略 G 仍依赖策略 C，请先归档或卸载 G",
            },
            {
                "gate": "SG-7_RECONCILE_OK",
                "passed": info["reconcile_ok"],
                "code": "LIFECYCLE_RECONCILE_FAILED",
                "message": "账本对账正常" if info["reconcile_ok"] else f"账本对账异常：{info['reconcile_issues']}",
            },
        ]
        if require_confirmation:
            confirmed = str(confirm_strategy_id or "").strip().lower() == sid.lower()
            checks.append({
                "gate": "SG-8_DOUBLE_CONFIRM",
                "passed": confirmed,
                "code": "LIFECYCLE_CONFIRMATION_REQUIRED",
                "message": "已完成二次确认" if confirmed else f"需要输入策略ID（{sid}）完成二次确认",
            })

        blocking = [c for c in checks if not c["passed"]]
        return {
            "strategy_id": sid,
            "allowed": len(blocking) == 0,
            "blocking_codes": [c["code"] for c in blocking],
            "blocking_messages": [c["message"] for c in blocking],
            "checks": checks,
            "runtime": info,
            "copy_bindings": copy_info,
        }

    def soft_pause(self, strategy_id: str, *, reason: str = "USER_SOFT_PAUSE") -> Dict[str, Any]:
        """Unified Soft Pause: stops new entries while keeping existing open positions & SL/TP active."""
        sid, kind = self._classify(strategy_id)
        adapters = self._get_adapters()
        with self.account_locks.lock_for(sid):
            if kind in ("CORE_ABC", "CORE_E"):
                ctrl = adapters["strategy_control"]()
                ctrl.soft_pause(sid)
            elif kind == "CORE_D":
                if adapters.get("set_d_enabled"):
                    adapters["set_d_enabled"](False)
            elif kind == "MIRROR_G":
                # G mirror stops taking new mirrored entries when marked PAUSED in registry
                pass
            elif kind == "PA_EXT":
                ext = adapters["pa_extension"]
                if sid in ext.ledgers:
                    ext.set_enabled(sid, False, datetime.now(UTC), lambda: True)
            elif kind == "R_SERIES":
                ext = adapters["r_series_ext"]
                if sid in ext.ledgers:
                    ext.set_enabled(sid, False)
            elif kind == "THC_AY":
                ext = adapters["thc_ay_ext"]
                if sid in ext.ledgers:
                    ext.set_enabled(sid, False)
            elif kind == "STRATEGY_POOL":
                pool = adapters["strategy_pool"]
                item = next((it for it in pool.list() if it.get("id") == sid), None)
                ver = (item or {}).get("active_version") or "1.0.0"
                pool.enable(sid, ver, False)

            rec = self.registry.set_state(sid, "PAUSED", strategy_type=kind, reason=reason, audit_details={"mode": "SOFT_PAUSE", "reason": reason})
            self.query_cache.invalidate()
            if self._on_lifecycle_change:
                self._on_lifecycle_change(sid, "PAUSED")
            return {"ok": True, "strategy_id": sid, "state": "PAUSED", "mode": "SOFT_PAUSE", "record": rec}

    def resume(self, strategy_id: str, *, reason: str = "USER_RESUME") -> Dict[str, Any]:
        """Unified Resume from PAUSED (or REGISTERED) back to RUNNING."""
        sid, kind = self._classify(strategy_id)
        reg_rec = self.registry.get(sid)
        if reg_rec and reg_rec.get("state") in ("ARCHIVED", "DELETED"):
            raise ValueError(f"CANNOT_RESUME_FROM_{reg_rec['state']}:USE_RESTORE_OR_REREGISTER")
        adapters = self._get_adapters()
        with self.account_locks.lock_for(sid):
            if kind in ("CORE_ABC", "CORE_E"):
                ctrl = adapters["strategy_control"]()
                ctrl.resume(sid)
            elif kind == "CORE_D":
                if adapters.get("set_d_enabled"):
                    adapters["set_d_enabled"](True)
            elif kind == "PA_EXT":
                ext = adapters["pa_extension"]
                if sid in ext.ledgers:
                    ext.set_enabled(sid, True, datetime.now(UTC), lambda: True)
            elif kind == "R_SERIES":
                ext = adapters["r_series_ext"]
                if sid in ext.ledgers:
                    ext.set_enabled(sid, True)
            elif kind == "THC_AY":
                ext = adapters["thc_ay_ext"]
                if sid in ext.ledgers:
                    ext.set_enabled(sid, True)
            elif kind == "STRATEGY_POOL":
                pool = adapters["strategy_pool"]
                item = next((it for it in pool.list() if it.get("id") == sid), None)
                ver = (item or {}).get("active_version") or (item or {}).get("latest_version")
                pool.enable(sid, ver, True)

            rec = self.registry.set_state(sid, "RUNNING", strategy_type=kind, reason=reason)
            self.query_cache.invalidate()
            if self._on_lifecycle_change:
                self._on_lifecycle_change(sid, "RUNNING")
            return {"ok": True, "strategy_id": sid, "state": "RUNNING", "record": rec}

    def _export_cold_archive(self, info: Dict[str, Any], *, action: str, reason: str) -> Path:
        sid = info["strategy_id"]
        kind = info["kind"]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest_dir = self.archive_root / f"{sid}_{stamp}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        adapters = self._get_adapters()
        archived_files: Dict[str, str] = {}
        db_path_str = info.get("db_path")
        standalone_kinds = ("PA_EXT", "R_SERIES", "THC_AY", "MIRROR_G", "CORE_D", "CORE_E")
        if db_path_str:
            src_db = Path(db_path_str)
            if src_db.is_file():
                _checkpoint_sqlite(src_db)
                if kind in standalone_kinds:
                    dst_db = dest_dir / src_db.name
                    shutil.copy2(src_db, dst_db)
                    archived_files[src_db.name] = _sha256_file(dst_db)
                else:
                    # Shared DB (paper.sqlite3 / strategy-pool.sqlite3): export strategy-scoped JSON ledger snapshot
                    export_payload: Dict[str, Any] = {"strategy_id": sid, "kind": kind, "exported_at": _utc_now()}
                    if kind == "CORE_ABC":
                        export_payload["account"] = adapters["ledger"].account(sid)
                        export_payload["fills"] = adapters["ledger"].fills(sid)
                    elif kind == "STRATEGY_POOL":
                        pool = adapters["strategy_pool"]
                        export_payload["versions"] = pool.versions(sid)
                        export_payload["fills"] = pool.fills(sid, limit=10000)
                        export_payload["lifecycle_events"] = pool.lifecycle_events(sid)
                    snap_file = dest_dir / f"{sid}_ledger_export.json"
                    snap_file.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                    archived_files[snap_file.name] = _sha256_file(snap_file)

        manifest = {
            "schema": "BTC_PAPER_LAB_COLD_ARCHIVE_V1",
            "strategy_id": sid,
            "kind": kind,
            "action": action,
            "reason": reason,
            "archived_at_utc": _utc_now(),
            "version": info.get("version"),
            "rule_sha256": info.get("rule_sha256"),
            "equity": info.get("equity"),
            "fill_count": info.get("fill_count"),
            "files": archived_files,
            "lifecycle_audit": self.registry.audit_log(sid, limit=50),
        }
        manifest_path = dest_dir / "archive_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # Verify cold archive is immediately readable
        verified = json.loads(manifest_path.read_text(encoding="utf-8"))
        if verified.get("strategy_id") != sid:
            raise RuntimeError("COLD_ARCHIVE_VERIFICATION_FAILED")
        return dest_dir

    def _unload_runtime_resources(self, sid: str, kind: str, *, move_standalone_db: bool, archive_dir: Path) -> Dict[str, Any]:
        """Release scheduler registration, active index, exclusive caches, and standalone DB files."""
        adapters = self._get_adapters()
        unloaded_from: List[str] = []
        moved_db: Optional[str] = None

        if kind == "R_SERIES":
            ext = adapters["r_series_ext"]
            db_path = ext.root / f"{sid}.sqlite3"
            if ext.unload(sid):
                unloaded_from.append("r_series_ext")
            if move_standalone_db and db_path.is_file():
                _checkpoint_sqlite(db_path)
                for suffix in ("", "-wal", "-shm"):
                    p = Path(str(db_path) + suffix)
                    if p.is_file():
                        p.unlink()
                moved_db = str(db_path)
        elif kind == "THC_AY":
            ext = adapters["thc_ay_ext"]
            db_path = ext.root / f"{sid}.sqlite3"
            if ext.unload(sid):
                unloaded_from.append("thc_ay_ext")
            if move_standalone_db and db_path.is_file():
                _checkpoint_sqlite(db_path)
                for suffix in ("", "-wal", "-shm"):
                    p = Path(str(db_path) + suffix)
                    if p.is_file():
                        p.unlink()
                moved_db = str(db_path)
        elif kind == "PA_EXT":
            ext = adapters["pa_extension"]
            db_path = self.data_root / "pa-v1-extensions" / f"{sid}.sqlite3"
            if ext.unload(sid):
                unloaded_from.append("pa_extension")
            if move_standalone_db and db_path.is_file():
                _checkpoint_sqlite(db_path)
                for suffix in ("", "-wal", "-shm"):
                    p = Path(str(db_path) + suffix)
                    if p.is_file():
                        p.unlink()
                moved_db = str(db_path)
        elif kind == "STRATEGY_POOL":
            pool = adapters["strategy_pool"]
            try:
                pool.archive(sid)
                unloaded_from.append("strategy_pool")
            except Exception:
                pass
        elif kind == "CORE_D":
            if adapters.get("set_d_enabled"):
                adapters["set_d_enabled"](False)
            unloaded_from.append("d_bridge")

        # Remove from ActivePositionIndex
        self.active_index.remove(sid)
        unloaded_from.append("active_position_index")
        return {"unloaded_from": unloaded_from, "removed_active_db": moved_db, "archive_dir": str(archive_dir)}

    def archive(self, strategy_id: str, *, reason: str = "USER_ARCHIVE") -> Dict[str, Any]:
        """Gated Archive: unloads from active scheduling, preserves cold archive, restorable."""
        sid, kind = self._classify(strategy_id)
        with self.account_locks.lock_for(sid):
            gate = self.check_delete_gates(sid, require_confirmation=False)
            if not gate["allowed"]:
                raise PermissionError(";".join(gate["blocking_codes"]))
            archive_dir = self._export_cold_archive(gate["runtime"], action="ARCHIVE", reason=reason)
            unload_res = self._unload_runtime_resources(sid, kind, move_standalone_db=False, archive_dir=archive_dir)
            rec = self.registry.set_state(
                sid,
                "ARCHIVED",
                strategy_type=kind,
                rule_sha256=gate["runtime"].get("rule_sha256"),
                archive_path=str(archive_dir),
                reason=reason,
                audit_details={"action": "ARCHIVE", "unload": unload_res},
            )
        self.account_locks.remove(sid)
        self.query_cache.invalidate()
        if self._on_lifecycle_change:
            self._on_lifecycle_change(sid, "ARCHIVED")
        return {"ok": True, "strategy_id": sid, "state": "ARCHIVED", "archive_path": str(archive_dir), "unload": unload_res, "record": rec}

    def delete(self, strategy_id: str, *, confirm_strategy_id: str, reason: str = "USER_DELETE") -> Dict[str, Any]:
        """8-Gate Safe Delete under account lock: cold archive -> dynamic unload -> anti-resurrection tombstone."""
        sid, kind = self._classify(strategy_id)
        # Idempotent: already deleted
        existing = self.registry.get(sid)
        if existing and existing.get("state") == "DELETED":
            return {"ok": True, "strategy_id": sid, "state": "DELETED", "idempotent": True, "record": existing}

        with self.account_locks.lock_for(sid):
            gate = self.check_delete_gates(sid, confirm_strategy_id=confirm_strategy_id, require_confirmation=True)
            if not gate["allowed"]:
                raise PermissionError(";".join(gate["blocking_codes"]))
            archive_dir = self._export_cold_archive(gate["runtime"], action="DELETE", reason=reason)
            unload_res = self._unload_runtime_resources(sid, kind, move_standalone_db=True, archive_dir=archive_dir)
            rec = self.registry.set_state(
                sid,
                "DELETED",
                strategy_type=kind,
                rule_sha256=gate["runtime"].get("rule_sha256"),
                archive_path=str(archive_dir),
                reason=reason,
                audit_details={"action": "DELETE", "unload": unload_res, "confirmed_id": confirm_strategy_id},
            )
        # Release account lock AFTER exiting lock context
        lock_removed = self.account_locks.remove(sid)
        self.query_cache.invalidate()
        if self._on_lifecycle_change:
            self._on_lifecycle_change(sid, "DELETED")
        return {
            "ok": True,
            "strategy_id": sid,
            "state": "DELETED",
            "archive_path": str(archive_dir),
            "lock_removed": lock_removed,
            "unload": unload_res,
            "record": rec,
        }

    def restore(self, strategy_id: str, *, reason: str = "USER_RESTORE") -> Dict[str, Any]:
        """Restore an ARCHIVED strategy back to PAUSED (verified archive manifest, never auto-opens)."""
        sid = str(strategy_id).strip()
        rec = self.registry.get(sid) or self.registry.get(sid.upper())
        if not rec or rec.get("state") != "ARCHIVED":
            raise ValueError("STRATEGY_NOT_ARCHIVED")
        sid = rec["strategy_id"]
        arch_path = Path(rec["archive_path"]) if rec.get("archive_path") else None
        if arch_path and (arch_path / "archive_manifest.json").is_file():
            manifest = json.loads((arch_path / "archive_manifest.json").read_text(encoding="utf-8"))
            if manifest.get("strategy_id") != sid:
                raise ValueError("ARCHIVE_MANIFEST_MISMATCH")
        adapters = self._get_adapters()
        if rec.get("strategy_type") == "STRATEGY_POOL":
            pool = adapters["strategy_pool"]
            vers = pool.versions(sid)
            if vers:
                pool.restore(sid, vers[-1]["version"])
        updated = self.registry.set_state(sid, "PAUSED", reason=reason, audit_details={"action": "RESTORE_FROM_ARCHIVE"})
        self.query_cache.invalidate()
        if self._on_lifecycle_change:
            self._on_lifecycle_change(sid, "PAUSED")
        return {"ok": True, "strategy_id": sid, "state": "PAUSED", "record": updated}

    def re_register(self, strategy_id: str, *, reason: str = "USER_REREGISTER") -> Dict[str, Any]:
        """Explicitly clear a DELETED tombstone so a strategy can be re-registered cleanly (starts DISABLED/PAUSED)."""
        sid = str(strategy_id).strip()
        rec = self.registry.get(sid) or self.registry.get(sid.upper())
        if not rec or rec.get("state") != "DELETED":
            raise ValueError("STRATEGY_NOT_DELETED")
        sid = rec["strategy_id"]
        updated = self.registry.set_state(
            sid,
            "REGISTERED",
            reason=reason,
            audit_details={"action": "RE_REGISTER", "previous_archive": rec.get("archive_path"), "live_copy_disabled": True},
        )
        self.query_cache.invalidate()
        if self._on_lifecycle_change:
            self._on_lifecycle_change(sid, "REGISTERED")
        return {"ok": True, "strategy_id": sid, "state": "REGISTERED", "record": updated}

    def summary(self) -> Dict[str, Any]:
        records = self.registry.all_records()
        by_state: Dict[str, List[str]] = {s: [] for s in VALID_STATES}
        for r in records:
            st = r.get("state")
            if st in by_state:
                by_state[st].append(r["strategy_id"])
        return {
            "excluded_ids": sorted(self.registry.excluded_ids()),
            "archived_ids": sorted(by_state["ARCHIVED"]),
            "deleted_ids": sorted(by_state["DELETED"]),
            "paused_ids": sorted(by_state["PAUSED"]),
            "records": records,
            "counts": {k: len(v) for k, v in by_state.items()},
        }
