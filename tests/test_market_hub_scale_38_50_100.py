"""Scale (38 / 50 / 100 strategies), per-account concurrency safety, shared HTF hub,
and read-only query isolation stress tests for BTC Paper Lab V3.1."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

V31_ROOT = Path(__file__).resolve().parents[1]
if str(V31_ROOT) not in sys.path:
    sys.path.insert(0, str(V31_ROOT))

from backend.market_hub import (
    AccountLockRegistry,
    BoundedCycleExecutor,
    ReadOnlySnapshotCache,
    SharedMarketHub,
)
from backend.quote_pipeline import BoundedQuoteDispatcher, ProtectionWatchlist, WatchEntry

UTC = timezone.utc


def _q(ts: datetime, bid: str, ask: str) -> dict:
    return {
        "symbol": "BTC/USD",
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "bid": bid,
        "ask": ask,
        "event_at": ts.astimezone(UTC).isoformat(),
        "received_at": ts.astimezone(UTC).isoformat(),
        "age_ms": 0,
        "timestamp_valid": True,
    }


def _make_15m_bars(n: int = 384) -> list[dict]:
    start = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    bars = []
    px = 62000.0
    for i in range(n):
        o = start + timedelta(minutes=15 * i)
        c = o + timedelta(minutes=15)
        bars.append({
            "t": int(o.timestamp() * 1000),
            "close_ms": int(c.timestamp() * 1000),
            "open_at": o.isoformat(),
            "close_at": c.isoformat(),
            "o": px, "h": px + 50, "l": px - 50, "c": px + 10, "v": 100.0,
            "open": str(px), "high": str(px + 50), "low": str(px - 50), "close": str(px + 10), "volume": "100",
            "is_closed": True,
        })
        px += 1.0
    return bars


@pytest.mark.parametrize("n_strategies", [38, 50, 100])
def test_scale_38_50_100_shared_hub_concurrent_cycle_protection_and_query_isolation(n_strategies):
    hub = SharedMarketHub()
    hub.update_15m_bars(_make_15m_bars(384))  # 4 full days of 15m bars
    locks = AccountLockRegistry()
    executor = BoundedCycleExecutor(locks, max_workers=6)
    cache = ReadOnlySnapshotCache(ttl_seconds=1.0)

    # 1. Shared HTF aggregation across N strategies: each HTF (1h/4h/1d) built ONCE!
    eval_counter = {"ok": 0}
    eval_lock = threading.Lock()

    def _eval_one(idx: int):
        tf = ("15m", "1h", "4h", "1d")[idx % 4]
        bars = hub.get_bars(tf)
        assert len(bars) > 0
        with eval_lock:
            eval_counter["ok"] += 1
        return {"tf": tf, "bars": len(bars)}

    # Inject 1 faulted strategy to verify isolation
    tasks = [(f"S-{i:03d}", (lambda: (_ for _ in ()).throw(RuntimeError("SIM_FAULT"))) if i == 0 else (lambda k=i: _eval_one(k))) for i in range(n_strategies)]
    results = executor.run_batch(tasks)
    assert len(results) == n_strategies
    assert results["S-000"]["status"] == "ERROR"
    assert eval_counter["ok"] == n_strategies - 1
    hs = hub.stats()
    # 1h, 4h, 1d built at most once each across ALL 38/50/100 strategies!
    assert hs["htf_build_count"] <= 3
    assert hs["htf_cache_hits"] >= n_strategies - 4

    # 2. High-frequency quote burst (1,000 frames) with 20% strategies holding open positions + intermediate stop hits
    async def _burst():
        triggered_ticks = []

        async def handler(q):
            triggered_ticks.append(q["bid"])

        wl = ProtectionWatchlist()
        open_cnt = max(4, n_strategies // 5)
        wl.update([WatchEntry(f"S-{i:03d}", "LONG", D("61000"), D("63000")) for i in range(open_cnt)])
        disp = BoundedQuoteDispatcher(handler, watchlist=wl, protection_maxsize=64, max_age_ms=2000)

        now = datetime(2026, 11, 27, 11, 10, 0, tzinfo=UTC)
        cur = None
        t0 = time.perf_counter()
        for k in range(1000):
            ts = now + timedelta(milliseconds=k)
            # Inject 3 intermediate stop/target touches inside 1,000-frame burst
            if k in (250, 750):
                q = _q(ts, "60950", "60951")  # STOP hit!
            elif k == 500:
                q = _q(ts, "63050", "63051")  # TARGET hit!
            else:
                q = _q(ts, "62000", "62001")
            cur, _, _ = disp.submit(cur, q, now=ts)
        submit_ms = (time.perf_counter() - t0) * 1000
        while await disp.process_one():
            pass
        st = disp.stats()
        assert submit_ms < 500.0
        assert triggered_ticks.count("60950") == 2
        assert triggered_ticks.count("63050") == 1
        assert st["overload_events"] == 0
        assert st["age_ms_max"] == 0
        return submit_ms, st

    submit_ms, q_stats = asyncio.run(_burst())

    # 3. Concurrent read-only query storm (Desktop Console + Research V3 + Live K-line + GrokBot = 60 concurrent polls)
    build_calls = {"n": 0}

    def _expensive_db_read():
        build_calls["n"] += 1
        time.sleep(0.01)
        return {"strategies": n_strategies}

    t_q0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=12) as qpool:
        q_res = list(qpool.map(lambda _: cache.get_or_build("pool_snap", _expensive_db_read), range(60)))
    query_ms = (time.perf_counter() - t_q0) * 1000
    assert len(q_res) == 60
    assert build_calls["n"] <= 12  # Heavily cached after first build
    assert cache.hits >= 48
    print(
        f"SCALE_BENCH n={n_strategies:3d} | fanout_ms={executor.last_fanout_ms:5.1f} | "
        f"htf_builds={hs['htf_build_count']} hits={hs['htf_cache_hits']} | "
        f"1000q_submit_ms={submit_ms:5.1f} coal={q_stats['coalesced_total']} | "
        f"60_queries_ms={query_ms:5.1f} cache_hits={cache.hits}"
    )


def test_per_account_lock_prevents_concurrent_double_fill():
    locks = AccountLockRegistry()
    state = {"balance": 10000, "position": 0, "fills": 0}

    def _attempt_open():
        with locks.lock_for("thc-s01-15m"):
            if state["position"] == 0:
                time.sleep(0.005)
                state["position"] = 1
                state["balance"] -= 10
                state["fills"] += 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: _attempt_open(), range(16)))
    assert state["position"] == 1
    assert state["fills"] == 1
    assert state["balance"] == 9990
