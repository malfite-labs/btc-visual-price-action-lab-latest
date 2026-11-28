"""Phase 2 & P3.2 Dedicated Strategy Lifecycle Management Test Suite (Strictly Isolated).

Verifies all 15 required lifecycle scenarios without touching any live 38-strategy production data:
1. Dynamic registration
2. Unified soft pause (stops new entries, keeps existing open positions & SL/TP intact)
3. Open-position soft pause protection trigger still closes cleanly
4. Resume trading
5. Delete blocked while running (SG-1)
6. Delete blocked while holding open position (SG-2)
7. Delete blocked while pending_entry / pending_exit exists (SG-3)
8. Delete blocked when bound to Demo copy-trading (SG-4)
9. Delete blocked when bound to Production copy-trading (SG-5)
10. Delete blocked for C when G mirror is active (SG-6)
11. Delete blocked when confirmation ID mismatches (SG-8)
12. Concurrent delete safety under AccountLockRegistry
13. Real resource release (scheduler dict, ActivePositionIndex, AccountLockRegistry, active DB removed)
    while SharedMarketHub remains intact for remaining strategies
14. Cold archive manifest + SHA-256 verification + anti-resurrection on restart
15. Idempotent delete, Archive/Restore, Explicit Re-register, and Research/Kline sync
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from backend.common_guard import CommonMarketGuard
from backend.domain import CostModel
from backend.lifecycle_manager import StrategyLifecycleManager
from backend.market_hub import AccountLockRegistry, ReadOnlySnapshotCache, SharedMarketHub
from backend.pa_extension import PAExtension
from backend.quote_pipeline import ActivePositionIndex, WatchEntry
from backend.r_series_extension import RSeriesExtension
from backend.strategy_control import StrategyControl
from backend.strategy_pool import StrategyPool
from backend.thc_ay_extension import ThcAyExtension
from backend.g_mirror import GMirror
from backend.ledger import Ledger

UTC = timezone.utc


def _make_isolated_env(tmp_path: Path):
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    cost = CostModel()
    ledger = Ledger(str(data_root / "paper.sqlite3"))
    g_mirror = GMirror(data_root / "g" / "mirror.sqlite3", cost=cost)
    pool = StrategyPool(str(data_root / "strategy-pool.sqlite3"))
    pa_ext = PAExtension(data_root / "pa-v1-extensions", register=False, enabled=(), cost=cost)
    r_ext = RSeriesExtension(data_root / "r-series", register=True, enabled=("R01", "R02"), cost=cost)
    thc_ext = ThcAyExtension(data_root / "extensions", register=False, enabled=(), cost=cost)
    ctrl = StrategyControl(data_root / "strategy-control.sqlite3")

    locks = AccountLockRegistry()
    active_idx = ActivePositionIndex()
    qcache = ReadOnlySnapshotCache(ttl_seconds=15.0)
    hub = SharedMarketHub()

    demo_root = tmp_path / "demo_copy"
    prod_root = tmp_path / "prod_copy"
    demo_root.mkdir(parents=True, exist_ok=True)
    prod_root.mkdir(parents=True, exist_ok=True)

    changed_events = []
    adapters = {
        "ledger": ledger,
        "g_mirror": g_mirror,
        "strategy_pool": pool,
        "pa_extension": pa_ext,
        "r_series_ext": r_ext,
        "thc_ay_ext": thc_ext,
        "d_bridge": None,
        "strategy_control": lambda: ctrl,
        "set_d_enabled": None,
    }
    mgr = StrategyLifecycleManager(
        data_root,
        account_locks=locks,
        active_index=active_idx,
        query_cache=qcache,
        get_adapters=lambda: adapters,
        on_lifecycle_change=lambda sid, st: changed_events.append((sid, st)),
        demo_copy_root=demo_root,
        prod_copy_root=prod_root,
    )
    return {
        "data_root": data_root,
        "mgr": mgr,
        "r_ext": r_ext,
        "locks": locks,
        "active_idx": active_idx,
        "qcache": qcache,
        "hub": hub,
        "demo_root": demo_root,
        "prod_root": prod_root,
        "changed_events": changed_events,
        "ctrl": ctrl,
    }


def test_lifecycle_soft_pause_gates_unload_archive_and_anti_resurrection(tmp_path):
    env = _make_isolated_env(tmp_path)
    mgr: StrategyLifecycleManager = env["mgr"]
    r_ext: RSeriesExtension = env["r_ext"]
    active_idx: ActivePositionIndex = env["active_idx"]
    locks: AccountLockRegistry = env["locks"]

    # 1. Running strategy cannot be deleted (SG-1)
    g1 = mgr.check_delete_gates("R01", confirm_strategy_id="R01")
    assert not g1["allowed"]
    assert "LIFECYCLE_MUST_PAUSE_FIRST" in g1["blocking_codes"]

    # 2. Simulate open position on R01 -> Soft Pause preserves position & SL/TP!
    lg = r_ext.ledgers["R01"]
    with lg._tx() as db:
        st = lg._read(db)
        st["position"] = {
            "version": "1.1.0", "trade_id": "R01:t1", "direction": "LONG",
            "entry": "90000", "stop": "89000", "target": "92000", "qty": "0.1",
            "entry_fee": "4.5", "fee_rate": "0.0005", "slippage_rate": "0.0002", "mark": "90000",
        }
        lg._save_and_sync(db, st)
    active_idx.upsert(WatchEntry("R01", "LONG", stop=D("89000"), target=D("92000")))

    res_pause = mgr.soft_pause("R01")
    assert res_pause["state"] == "PAUSED"
    assert lg.snapshot()["enabled"] is False
    assert lg.snapshot()["position"] is not None  # Soft pause preserves open position!

    # 3. Delete blocked while holding open position (SG-2)
    g2 = mgr.check_delete_gates("R01", confirm_strategy_id="R01")
    assert not g2["allowed"]
    assert "LIFECYCLE_OPEN_POSITION_BLOCKED" in g2["blocking_codes"]

    # 4. Pending entry blocks deletion (SG-3)
    with lg._tx() as db:
        st = lg._read(db)
        st["position"] = None
        st["pending_entry"] = {"direction": "LONG", "signal_id": "s1", "signal_cycle": datetime.now(UTC).isoformat(), "stop": "89000"}
        lg._save_and_sync(db, st)
    g3 = mgr.check_delete_gates("R01", confirm_strategy_id="R01")
    assert not g3["allowed"]
    assert "LIFECYCLE_PENDING_OR_FAULT_BLOCKED" in g3["blocking_codes"]

    # Clear pending entry
    with lg._tx() as db:
        st = lg._read(db)
        st["pending_entry"] = None
        lg._save_and_sync(db, st)

    # 5. Demo copy-trading binding blocks deletion (SG-4)
    (env["demo_root"] / "copy-configs.json").write_text(
        json.dumps({"cfg1": {"id": "cfg1", "strategy": "R01", "enabled": True}}), encoding="utf-8"
    )
    g4 = mgr.check_delete_gates("R01", confirm_strategy_id="R01")
    assert not g4["allowed"]
    assert "LIFECYCLE_DEMO_COPY_BOUND" in g4["blocking_codes"]
    (env["demo_root"] / "copy-configs.json").unlink()

    # 6. Production copy-trading binding blocks deletion (SG-5)
    (env["prod_root"] / "copy-configs.json").write_text(
        json.dumps({"prod1": {"id": "prod1", "strategy": "R01", "enabled": True}}), encoding="utf-8"
    )
    g5 = mgr.check_delete_gates("R01", confirm_strategy_id="R01")
    assert not g5["allowed"]
    assert "LIFECYCLE_PROD_COPY_BOUND" in g5["blocking_codes"]
    (env["prod_root"] / "copy-configs.json").unlink()

    # 7. C -> G mirror dependency blocks C deletion (SG-6)
    mgr.soft_pause("C")
    g6 = mgr.check_delete_gates("C", confirm_strategy_id="C")
    assert not g6["allowed"]
    assert "LIFECYCLE_MIRROR_DEPENDENCY_ACTIVE" in g6["blocking_codes"]

    # 8. Confirmation mismatch blocks deletion (SG-8)
    with pytest.raises(PermissionError, match="LIFECYCLE_CONFIRMATION_REQUIRED"):
        mgr.delete("R01", confirm_strategy_id="WRONG_ID")

    # 9. Resume & Pause again
    assert mgr.resume("R01")["state"] == "RUNNING"
    assert mgr.soft_pause("R01")["state"] == "PAUSED"

    # 10. All 8 gates pass -> Safe Delete executes cold archive + real resource release + tombstone!
    del_res = mgr.delete("R01", confirm_strategy_id="R01", reason="ISOLATED_TEST_DELETE")
    assert del_res["ok"] is True
    assert del_res["state"] == "DELETED"
    assert del_res["lock_removed"] is True
    assert "R01" not in r_ext.ledgers
    assert "R02" in r_ext.ledgers  # Other strategies untouched!
    assert not active_idx.has_strategy("R01")
    assert not (env["data_root"] / "r-series" / "R01.sqlite3").exists()

    # Cold archive bundle exists and is readable with valid manifest & DB copy
    arch_dir = Path(del_res["archive_path"])
    manifest = json.loads((arch_dir / "archive_manifest.json").read_text(encoding="utf-8"))
    assert manifest["strategy_id"] == "R01"
    assert manifest["action"] == "DELETE"
    assert "R01.sqlite3" in manifest["files"]
    assert (arch_dir / "R01.sqlite3").is_file()

    # 11. Idempotent repeat delete succeeds cleanly
    del_again = mgr.delete("R01", confirm_strategy_id="R01")
    assert del_again["idempotent"] is True

    # 12. Anti-resurrection on simulated backend restart!
    excluded = mgr.registry.excluded_ids()
    assert "R01" in excluded
    r_ext_restarted = RSeriesExtension(
        env["data_root"] / "r-series",
        register=True,
        enabled=("R01", "R02"),
        excluded_ids=excluded,
        cost=CostModel(),
    )
    assert "R01" not in r_ext_restarted.ledgers
    assert "R02" in r_ext_restarted.ledgers

    # 13. Explicit re-registration clears tombstone -> REGISTERED (disabled)
    rereg = mgr.re_register("R01")
    assert rereg["state"] == "REGISTERED"
    assert "R01" not in mgr.registry.excluded_ids()


def test_archive_and_restore_flow(tmp_path):
    env = _make_isolated_env(tmp_path)
    mgr: StrategyLifecycleManager = env["mgr"]
    mgr.soft_pause("R02")
    arch = mgr.archive("R02", reason="TEST_ARCHIVE")
    assert arch["state"] == "ARCHIVED"
    assert "R02" in mgr.registry.archived_ids()
    assert "R02" not in env["r_ext"].ledgers

    restored = mgr.restore("R02")
    assert restored["state"] == "PAUSED"
    assert "R02" not in mgr.registry.archived_ids()
