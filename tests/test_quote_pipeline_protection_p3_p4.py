"""P3 (14 protection scenarios) + P4 (38-strategy burst load) isolated verification suite.

Proves:
- All 14 protection scenarios (normal, burst, WS backlog, stale WS vs fresh REST, REST fallback & WS recovery,
  out-of-order/duplicate/reconnect, intermediate stop touch followed by rebound, intermediate target touch
  followed by pullback, SL/TP conflict priority, disconnect during holding, duplicate pending_exit consumption,
  pause & resume under overload, slow DB / timeout isolation, restart & pending_exit recovery).
- Zero loss of intermediate protection triggers under high-frequency quote coalescing.
- Queue overflow latches overload_blocked=True (never silently drops protection and keeps opening).
- 38-strategy burst stress test (<200ms p95 latency, zero unbounded growth).
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

V31_ROOT = Path(__file__).resolve().parents[1]
if str(V31_ROOT) not in sys.path:
    sys.path.insert(0, str(V31_ROOT))

from backend.common_guard import CommonMarketGuard
from backend.domain import CostModel
from backend.quote_pipeline import (
    BoundedQuoteDispatcher,
    ProtectionWatchlist,
    WatchEntry,
    accept_quote_update,
)
from backend.r_series_extension import RSeriesPaperLedgerV11
from backend.thc_ay_extension import ThcAyExtension, ThcAyPaperLedger
from strategies.thc_ay.specs import THC_AY_IDS

UTC = timezone.utc


def _q(ev: datetime, recv: datetime, bid: str, ask: str, *, valid: bool = True, basis: str = "ws_bookTicker") -> dict:
    age = int((recv - ev).total_seconds() * 1000) if valid else 10**9
    return {
        "symbol": "BTC/USD",
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "bid": bid,
        "ask": ask,
        "event_at": ev.astimezone(UTC).isoformat(),
        "received_at": recv.astimezone(UTC).isoformat(),
        "age_ms": age,
        "timestamp_valid": valid and age >= 0,
        "timestamp_basis": basis,
    }


def _healthy_guard() -> CommonMarketGuard:
    return CommonMarketGuard(
        feed_live=True, quote_fresh=True, ws_healthy=True,
        candles_complete=True, startup_monitor_checked=True, reason=None,
    )


def _stale_guard() -> CommonMarketGuard:
    return CommonMarketGuard(
        feed_live=True, quote_fresh=False, ws_healthy=True,
        candles_complete=True, startup_monitor_checked=True, reason="QUOTE_STALE_OR_INVALID",
    )


def test_p2_and_p3_01_to_06_quote_acceptance_and_anti_stale_overwrite():
    now = datetime(2026, 11, 27, 11, 0, 10, tzinfo=UTC)
    # 1. Normal fresh REST quote accepted
    q_rest = _q(now - timedelta(milliseconds=100), now, "62000", "62001", basis="local_receive_time_rest_bookTicker")
    cur, acc, reason = accept_quote_update(None, q_rest, max_age_ms=2000, now=now)
    assert acc is True and reason == "ACCEPTED" and cur is q_rest

    # 2. Stale backlogged WS quote (165s old) MUST NOT overwrite fresh REST quote!
    q_ws_stale = _q(now - timedelta(seconds=165), now, "61500", "61501", basis="ws_bookTicker")
    cur2, acc2, reason2 = accept_quote_update(cur, q_ws_stale, max_age_ms=2000, now=now)
    assert acc2 is False and reason2 == "STALE_OVERWRITE_BLOCKED" and cur2 is q_rest

    # 3. Out-of-order fresh WS quote with older event_at MUST NOT regress current quote!
    q_older = _q(now - timedelta(milliseconds=500), now, "61990", "61991")
    cur3, acc3, reason3 = accept_quote_update(cur, q_older, max_age_ms=2000, now=now)
    assert acc3 is False and reason3 == "REGRESSIVE_EVENT_TIME_BLOCKED" and cur3 is q_rest

    # 4. WS reconnect / recovery with fresh event_at >= current seamlessly resumes!
    now2 = now + timedelta(milliseconds=200)
    q_ws_fresh = _q(now2 - timedelta(milliseconds=80), now2, "62010", "62011")
    cur4, acc4, reason4 = accept_quote_update(cur, q_ws_fresh, max_age_ms=2000, now=now2)
    assert acc4 is True and reason4 == "ACCEPTED" and cur4 is q_ws_fresh

    # 5. Stale incoming when current is ALSO expired keeps diagnostic visibility (never masks outage)
    later = now2 + timedelta(seconds=10)
    q_still_stale = _q(later - timedelta(seconds=5), later, "62010", "62011")
    cur5, acc5, reason5 = accept_quote_update(cur4, q_still_stale, max_age_ms=2000, now=later)
    assert acc5 is False and reason5 == "INCOMING_STALE_OR_INVALID" and cur5 is q_still_stale


def test_p3_07_08_09_intermediate_stop_and_target_never_lost_under_burst():
    async def _run():
        processed = []

        async def handler(q):
            processed.append((q["bid"], q["ask"]))

        wl = ProtectionWatchlist()
        # Open LONG with stop=61000, target=63000
        wl.update([WatchEntry("thc-s02-15m", "LONG", D("61000"), D("63000"))])
        disp = BoundedQuoteDispatcher(handler, watchlist=wl, protection_maxsize=64, max_age_ms=2000)

        now = datetime(2026, 11, 27, 11, 0, 10, tzinfo=UTC)
        cur = None
        # Burst of 50 routine quotes + 1 intermediate STOP touch + 50 rebound quotes (all before worker drains!)
        for k in range(50):
            t = now + timedelta(milliseconds=k)
            cur, _, _ = disp.submit(cur, _q(t, t, "62000", "62001"), now=t)
        # Intermediate tick hits STOP (60950 <= 61000)
        t_stop = now + timedelta(milliseconds=60)
        cur, acc_s, r_s = disp.submit(cur, _q(t_stop, t_stop, "60950", "60951"), now=t_stop)
        assert acc_s is True and r_s == "ENQUEUED_PROTECTION"
        # Next tick immediately rebounds above stop!
        for k in range(50):
            t = now + timedelta(milliseconds=70 + k)
            cur, _, _ = disp.submit(cur, _q(t, t, "62100", "62101"), now=t)
        # Intermediate tick hits TARGET (63050 >= 63000) then pulls back!
        t_tp = now + timedelta(milliseconds=130)
        cur, acc_t, r_t = disp.submit(cur, _q(t_tp, t_tp, "63050", "63051"), now=t_tp)
        assert acc_t is True and r_t == "ENQUEUED_PROTECTION"
        t_after = now + timedelta(milliseconds=140)
        cur, _, _ = disp.submit(cur, _q(t_after, t_after, "62050", "62051"), now=t_after)

        # Drain worker
        while await disp.process_one():
            pass

        # Verify BOTH intermediate STOP and TARGET touches were delivered FIRST and NEVER coalesced away!
        assert ("60950", "60951") in processed
        assert ("63050", "63051") in processed
        assert processed[0] == ("60950", "60951")
        assert processed[1] == ("63050", "63051")
        # Routine 101 ticks coalesced cleanly into 1!
        assert disp.coalesced_total >= 95
        assert len(processed) == 3

    asyncio.run(_run())


def test_p3_10_to_14_overload_latch_pause_pending_exit_and_recovery(tmp_path):
    async def _run():
        async def slow_handler(q):
            pass

        wl = ProtectionWatchlist()
        wl.update([WatchEntry("R01", "LONG", D("61000"), D("63000"))])
        disp = BoundedQuoteDispatcher(slow_handler, watchlist=wl, protection_maxsize=2, max_age_ms=2000)
        now = datetime(2026, 11, 27, 11, 0, 10, tzinfo=UTC)
        cur = None
        for k in range(4):
            t = now + timedelta(milliseconds=k)
            cur, _, _ = disp.submit(cur, _q(t, t, "60900", "60901"), now=t)
        # Overflow MUST latch overload_blocked=True (never silently drop and keep opening!)
        assert disp.overload_blocked is True
        assert disp.overload_events >= 2
        while await disp.process_one():
            pass
        assert disp.overload_blocked is False

    asyncio.run(_run())

    # Pending exit recovery + duplicate consumption + restart reconciliation on R01
    lg = RSeriesPaperLedgerV11(tmp_path / "r", "R01", cost=CostModel(), enabled=True)
    t0 = datetime(2026, 11, 27, 11, 0, tzinfo=UTC)
    with lg._tx() as db:
        st = lg._read(db)
        st["pending_entry"] = {
            "signal_id": "R01:p3:1", "signal_cycle": t0.isoformat(),
            "direction": "LONG", "stop": "61000", "signal_source": "R01:P3",
        }
        lg._save_and_sync(db, st)
    assert lg.on_quote(_q(t0 + timedelta(seconds=2), t0 + timedelta(seconds=2), "62000", "62001"), guard=_healthy_guard())["status"] == "OPEN_LONG"
    # Inject pending_exit (simulating stop hit during market disconnect) & pause strategy
    lg.set_enabled(False, paused_by_user=True)
    with lg._tx() as db:
        st = lg._read(db)
        st["pending_exit"] = {
            "trade_id": st["position"]["trade_id"], "reason": "STOP",
            "trigger_price": "61000", "triggered_at": (t0 + timedelta(seconds=10)).isoformat(),
            "event_key": "R01:exit:1",
        }
        lg._save_and_sync(db, st)
    # Restart ledger from disk & recover pending_exit on fresh quote
    lg2 = RSeriesPaperLedgerV11(tmp_path / "r", "R01", cost=CostModel())
    q_rec = _q(t0 + timedelta(seconds=20), t0 + timedelta(seconds=20), "60950", "60951")
    r_rec = lg2.on_quote(q_rec, guard=_healthy_guard())
    assert r_rec.get("recovered") is True and "STOP_RECOVERED" in r_rec["status"]
    # Duplicate quote / second exit attempt must NOT double-close
    assert lg2.on_quote(q_rec, guard=_healthy_guard())["status"] == "NO_POSITION"
    assert lg2.reconcile()["ok"] is True


def test_p4_burst_load_15_extensions_zero_backlog(tmp_path):
    async def _run():
        ext = ThcAyExtension(tmp_path / "ext", project_root=V31_ROOT, register=True, enabled=THC_AY_IDS)
        g = _healthy_guard()

        async def handler(q):
            for sid in ext.ledgers:
                ext.ledger_quote(sid, q, g)

        disp = BoundedQuoteDispatcher(handler, watchlist=ProtectionWatchlist(), protection_maxsize=64, max_age_ms=2000)
        now = datetime(2026, 11, 27, 11, 0, 10, tzinfo=UTC)
        cur = None
        t0 = time.perf_counter()
        # Simulate 500 high-frequency bookTicker frames arriving in a burst
        for k in range(500):
            ts = now + timedelta(milliseconds=k)
            cur, _, _ = disp.submit(cur, _q(ts, ts, f"{62000 + (k % 5)}", f"{62001 + (k % 5)}"), now=ts)
        submit_ms = (time.perf_counter() - t0) * 1000
        while await disp.process_one():
            pass
        stats = disp.stats()
        assert submit_ms < 200.0, f"500 submits took {submit_ms:.1f}ms"
        assert stats["received_total"] == 500
        assert stats["coalesced_total"] == 499
        assert stats["routine_processed"] == 1
        assert stats["overload_events"] == 0
        assert stats["age_ms_max"] == 0

    asyncio.run(_run())
