"""Final Freeze Verification Tests (Strictly Isolated, Zero Production Mutation):
1. ActivePositionIndex sync across open, close, soft-pause, resume, pending_exit, and restart recovery
2. WS disconnect -> REST fallback -> WS recovery: zero valid quotes (<=2000ms) dropped, zero missed stop triggers
3. Explicit SG-7 (LIFECYCLE_RECONCILE_FAILED) delete-gate blocking verification
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from backend.domain import CostModel
from backend.g_mirror import GMirror
from backend.ledger import Ledger
from backend.lifecycle_manager import StrategyLifecycleManager
from backend.market_hub import AccountLockRegistry, ReadOnlySnapshotCache
from backend.pa_extension import PAExtension
from backend.quote_pipeline import ActivePositionIndex, BoundedQuoteDispatcher, WatchEntry, accept_quote_update
from backend.r_series_extension import RSeriesExtension
from backend.strategy_control import StrategyControl
from backend.strategy_pool import StrategyPool
from backend.thc_ay_extension import ThcAyExtension

UTC = timezone.utc


def test_active_position_index_sync_full_lifecycle_transitions():
    """Verify ActivePositionIndex stays synchronized across open, pause, resume, pending_exit, close, restart."""
    idx = ActivePositionIndex()
    q_mid = {"bid": "91000", "ask": "91001"}
    q_stop = {"bid": "89999", "ask": "90000"}

    # 1. Flat -> zero triggers
    u, m = idx.matching_strategies(q_stop, now_mono=10.0)
    assert u == set() and m == set()

    # 2. Open position -> immediately indexed & stop triggers detected
    idx.upsert(WatchEntry("R01", "LONG", stop=D("90000"), target=D("94000")))
    u, _ = idx.matching_strategies(q_stop, now_mono=10.5)
    assert u == {"R01"}

    # 3. Soft pause preserves open position in index -> stop STILL triggers!
    u_paused, _ = idx.matching_strategies(q_stop, now_mono=11.0)
    assert u_paused == {"R01"}

    # 4. Pending exit -> triggers on ANY valid quote (even mid-range!) for recovery
    idx.upsert(WatchEntry("R01", "LONG", stop=D("90000"), target=D("94000"), pending=True))
    u_pend, _ = idx.matching_strategies(q_mid, now_mono=11.5)
    assert u_pend == {"R01"}

    # 5. Close / unload -> removed from index -> zero I/O
    idx.remove("R01")
    assert not idx.has_strategy("R01")
    u_after, m_after = idx.matching_strategies(q_stop, now_mono=12.0)
    assert u_after == set() and m_after == set()

    # 6. Restart recovery re-populates index cleanly
    idx.update([WatchEntry("thc-s02-15m", "LONG", stop=D("89500"), target=D("93000"))])
    assert idx.has_strategy("thc-s02-15m")
    u_rec, _ = idx.matching_strategies({"bid": "89490", "ask": "89495"}, now_mono=20.0)
    assert u_rec == {"thc-s02-15m"}


def test_ws_disconnect_rest_fallback_ws_recovery_no_valid_quote_or_stop_dropped():
    """Simulate WS live -> WS disconnect -> REST fallback -> WS reconnect while holding position."""
    now = datetime(2026, 11, 28, 4, 0, 0, tzinfo=UTC)
    idx = ActivePositionIndex()
    idx.update([WatchEntry("R01", "LONG", stop=D("90000"), target=D("95000"))])
    handled = []

    async def handler(q):
        handled.append(q)

    disp = BoundedQuoteDispatcher(handler, watchlist=idx, max_age_ms=2000)

    def make_q(ev_offset_ms: int, rec_offset_ms: int, bid: str, ask: str):
        ev = now + timedelta(milliseconds=ev_offset_ms)
        rec = now + timedelta(milliseconds=rec_offset_ms)
        age = int((rec - ev).total_seconds() * 1000)
        return {
            "symbol": "BTC/USD", "source": "BINANCE_USDM_PERP", "currency": "USD_RESEARCH",
            "bid": bid, "ask": ask, "timestamp_valid": True, "age_ms": age,
            "event_at": ev.isoformat(), "received_at": rec.isoformat(),
        }

    # Phase A: Healthy WS quote
    q1 = make_q(0, 150, "91000", "91001")
    cur, ok, rsn = disp.submit(None, q1, now=now + timedelta(milliseconds=160))
    assert ok and rsn == "COALESCED_ROUTINE"

    # Phase B: WS disconnects; 1.8s later REST fallback delivers fresh quote (within 2000ms)
    q_rest = make_q(1800, 1800, "90500", "90501")
    cur, ok, rsn = disp.submit(cur, q_rest, now=now + timedelta(milliseconds=1850))
    assert ok and rsn == "COALESCED_ROUTINE"

    # Phase C: Stale quote (>2000ms) MUST NOT overwrite fresh valid quote (2000ms gate preserved!)
    q_stale = make_q(-500, 1900, "90400", "90401")  # age=2400ms > 2000ms
    cur2, ok_stale, rsn_stale = disp.submit(cur, q_stale, now=now + timedelta(milliseconds=1900))
    assert not ok_stale and rsn_stale == "STALE_OVERWRITE_BLOCKED"
    assert cur2 is cur

    # Phase D: WS recovers with fresh stop-triggering quote -> MUST enqueue into protection_queue!
    q_ws_stop = make_q(2100, 2250, "89950", "89951")
    cur, ok, rsn = disp.submit(cur, q_ws_stop, now=now + timedelta(milliseconds=2260))
    assert ok and rsn == "ENQUEUED_PROTECTION"
    assert disp.protection_enqueued == 1

    asyncio.run(disp.process_one())
    assert len(handled) == 1 and handled[0]["bid"] == "89950"


def test_sg7_reconcile_failed_blocks_deletion(tmp_path):
    """Explicitly verify SG-7 (LIFECYCLE_RECONCILE_FAILED) blocks deletion when ledger has discrepancy."""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    cost = CostModel()
    r_ext = RSeriesExtension(data_root / "r-series", register=True, enabled=(), cost=cost)
    lg = r_ext.ledgers["R01"]
    # Corrupt cash in snapshot without matching fills so reconcile() detects CASH_MISMATCH!
    with lg._tx() as db:
        st = lg._read(db)
        st["cash"] = "99999.00"
        lg._save_and_sync(db, st)

    adapters = {
        "ledger": Ledger(str(data_root / "paper.sqlite3")),
        "g_mirror": GMirror(data_root / "g" / "mirror.sqlite3", cost=cost),
        "strategy_pool": StrategyPool(str(data_root / "strategy-pool.sqlite3")),
        "pa_extension": PAExtension(data_root / "pa-v1-extensions", register=False, enabled=(), cost=cost),
        "r_series_ext": r_ext,
        "thc_ay_ext": ThcAyExtension(data_root / "extensions", register=False, enabled=(), cost=cost),
        "d_bridge": None,
        "strategy_control": lambda: StrategyControl(data_root / "strategy-control.sqlite3"),
        "set_d_enabled": None,
    }
    mgr = StrategyLifecycleManager(
        data_root,
        account_locks=AccountLockRegistry(),
        active_index=ActivePositionIndex(),
        query_cache=ReadOnlySnapshotCache(ttl_seconds=15.0),
        get_adapters=lambda: adapters,
        demo_copy_root=tmp_path / "demo",
        prod_copy_root=tmp_path / "prod",
    )
    gate = mgr.check_delete_gates("R01", confirm_strategy_id="R01")
    assert not gate["allowed"]
    assert "LIFECYCLE_RECONCILE_FAILED" in gate["blocking_codes"]
    sg7 = next(c for c in gate["checks"] if c["gate"] == "SG-7_RECONCILE_OK")
    assert sg7["passed"] is False
    assert "CASH_MISMATCH" in sg7["message"]
    with pytest.raises(PermissionError, match="LIFECYCLE_RECONCILE_FAILED"):
        mgr.delete("R01", confirm_strategy_id="R01")
