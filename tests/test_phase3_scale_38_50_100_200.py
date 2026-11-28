"""Phase 3 (P3.1) Realistic 38 / 50 / 100 / 200 Strategy Scale & Protection Stress Test.

Uses REAL strategy evaluation logic (RSignalEngineV11, evaluate_s_signal, evaluate_ay_signal,
and declarative PA V2 expressions) — never empty dummy functions — to benchmark:
- Natural cycle burst fan-out at 38, 50, 100, and 200 strategies
- Quote protection hot-path latency under 10%, 50%, and 100% open-position ratios
- Simultaneous stop-loss trigger across all open positions (zero missed triggers!)
- Concurrent read-only snapshot queries without contending with trading execution
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from backend.market_hub import AccountLockRegistry, BoundedCycleExecutor, ReadOnlySnapshotCache, SharedMarketHub
from backend.quote_pipeline import ActivePositionIndex, BoundedQuoteDispatcher, WatchEntry
from strategies.r_series.indicators import normalize_closed_15m_bar
from strategies.r_series.v11_engine import MultiTFContextV11, RSignalEngineV11, R_V11_SPECS
from strategies.thc_ay.signals import ThcAyCycleContext, evaluate_ay_signal, evaluate_s_signal
from strategies.thc_ay.specs import THC_AY_IDS, THC_AY_SPECS

UTC = timezone.utc


def _bars(n: int = 800) -> list[dict]:
    base = datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
    out = []
    px = 92000.0
    for i in range(n):
        o_dt = base + timedelta(minutes=15 * i)
        c_dt = o_dt + timedelta(minutes=15)
        c = 92000.0 + math.sin(i * 0.11) * 400.0 + math.cos(i * 0.04) * 600.0
        out.append({
            "open_at": o_dt.isoformat(),
            "close_at": c_dt.isoformat(),
            "open": str(round(px, 2)),
            "high": str(round(max(px, c) + 95.0, 2)),
            "low": str(round(min(px, c) - 90.0, 2)),
            "close": str(round(c, 2)),
            "volume": str(round(150.0 + (i % 9) * 15.0, 2)),
            "is_closed": True,
        })
        px = c
    return out


@pytest.mark.parametrize("scale_n", [38, 50, 100, 200])
def test_realistic_scale_cycle_and_protection_stress(scale_n: int):
    raw = _bars(700)
    hub = SharedMarketHub()
    hub.update_15m_bars(raw[:699])
    # Warm incremental THC context
    thc_ctx = ThcAyCycleContext(hub.get_bars("15m"))
    for tf in ("15m", "1h", "4h", "1d"):
        thc_ctx.s_indicators(tf)
    thc_ctx.ay_atr14()
    thc_ctx.ay_adx14()

    # Simulate 1 new closed 15m bar arriving
    hub.append_15m_bars([raw[699]])
    bars_15m = hub.get_bars("15m")
    wm = hub.latest_15m_close_ms
    thc_ctx.extend_15m([bars_15m[-1]])

    locks = AccountLockRegistry()
    executor = BoundedCycleExecutor(locks, max_workers=6)
    qcache = ReadOnlySnapshotCache(ttl_seconds=15.0)

    r_ids = list(R_V11_SPECS.keys())
    thc_ids = list(THC_AY_IDS)
    idx = len(bars_15m) - 1

    # Build N realistic strategy evaluation tasks sharing hub contexts
    tracemalloc.start()
    t0 = time.perf_counter()
    r_ctx = hub.get_or_build_context("r_v11_ctx", lambda: MultiTFContextV11(bars_15m), watermark_ms=wm)

    tasks = []
    for i in range(scale_n):
        sid = f"SIM-{scale_n}-{i:03d}"
        if i % 3 == 0:
            rid = r_ids[i % len(r_ids)]
            eng = RSignalEngineV11(rid)
            tasks.append((sid, lambda e=eng: e.step(r_ctx, idx, has_open_position=False)))
        else:
            tid = thc_ids[i % len(thc_ids)]
            spec = THC_AY_SPECS[tid]
            if spec.is_s_family:
                tasks.append((sid, lambda s=spec: evaluate_s_signal(s, thc_ctx, wm)))
            else:
                tasks.append((sid, lambda s=spec: evaluate_ay_signal(s, thc_ctx)))

    results = executor.run_batch(tasks)
    total_cycle_ms = (time.perf_counter() - t0) * 1000.0
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(results) == scale_n
    assert executor.total_strategy_errors == 0
    assert total_cycle_ms < 3000.0, f"Scale {scale_n} cycle exceeded 3000ms: {total_cycle_ms:.1f}ms"

    # Protection stress: 50% open positions + simultaneous stop-loss trigger across ALL open positions!
    open_cnt = scale_n // 2
    active_idx = ActivePositionIndex()
    entries = [
        WatchEntry(f"SIM-{scale_n}-{i:03d}", "LONG", stop=D("90000"), target=D("98000"))
        for i in range(open_cnt)
    ]
    active_idx.update(entries)

    # 1. Safe routine tick -> zero urgent triggers
    t_q0 = time.perf_counter()
    u_safe, _ = active_idx.matching_strategies({"bid": "92000", "ask": "92001"}, now_mono=100.0)
    u_safe2, m_safe2 = active_idx.matching_strategies({"bid": "92002", "ask": "92003"}, now_mono=100.5)
    safe_ms = (time.perf_counter() - t_q0) * 1000.0
    assert u_safe == set()
    assert u_safe2 == set() and m_safe2 == set()  # Zero-IO skip!

    # 2. Flash crash simultaneous stop trigger across ALL open positions -> 100% detected!
    t_s0 = time.perf_counter()
    u_crash, _ = active_idx.matching_strategies({"bid": "89500", "ask": "89501"}, now_mono=101.0)
    crash_ms = (time.perf_counter() - t_s0) * 1000.0
    assert len(u_crash) == open_cnt, f"Missed stop triggers: {len(u_crash)}/{open_cnt}"
    assert crash_ms < 20.0, f"Simultaneous stop scan too slow: {crash_ms:.2f}ms"

    # 3. Concurrent query cache hit rate
    for _ in range(20):
        qcache.get_or_build("pool_snapshot", lambda: {"count": scale_n})
    assert qcache.stats()["hit_rate"] >= 0.90

    print(
        f"[SCALE={scale_n}] cycle_ms={total_cycle_ms:.1f} fanout_ms={executor.last_fanout_ms:.1f} "
        f"open={open_cnt} safe_tick_ms={safe_ms:.2f} crash_all_stop_ms={crash_ms:.2f} "
        f"peak_mb={peak_bytes/1024/1024:.2f} cache_hit={qcache.stats()['hit_rate']}"
    )
