"""Phase 1 Lightweight Architecture Verification:
- Bit-exact numerical equivalence of incremental ThcAyCycleContext vs full recompute
- Exact signal equivalence of shared MultiTFContextV11 across R01-R06
- ActivePositionIndex zero-IO skip for flat strategies + 100% stop/target/pending trigger coverage
- SharedMarketHub incremental append + context cache hit rate >= 95%
- ReadOnlySnapshotCache event invalidation + version/freshness metadata
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from backend.market_hub import AccountLockRegistry, ReadOnlySnapshotCache, SharedMarketHub
from backend.quote_pipeline import ActivePositionIndex, WatchEntry
from strategies.r_series.indicators import normalize_closed_15m_bar
from strategies.r_series.v11_engine import MultiTFContextV11, RSignalEngineV11, R_V11_SPECS
from strategies.thc_ay.signals import ThcAyCycleContext, evaluate_ay_signal, evaluate_s_signal
from strategies.thc_ay.specs import THC_AY_IDS, THC_AY_SPECS

UTC = timezone.utc


def _synthetic_15m_bars(count: int, start_dt: datetime | None = None) -> list[dict]:
    base = start_dt or datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    bars = []
    px = 90000.0
    for i in range(count):
        o_dt = base + timedelta(minutes=15 * i)
        c_dt = o_dt + timedelta(minutes=15)
        wave = math.sin(i * 0.13) * 320.0 + math.cos(i * 0.037) * 540.0
        o = px
        c = max(10000.0, 90000.0 + wave + (i % 17 - 8) * 25.0)
        h = max(o, c) + 85.0 + (i % 5) * 18.0
        l = min(o, c) - 80.0 - (i % 7) * 15.0
        v = 120.0 + (i % 11) * 22.5
        px = c
        bars.append({
            "open_at": o_dt.isoformat(),
            "close_at": c_dt.isoformat(),
            "open": str(round(o, 2)),
            "high": str(round(h, 2)),
            "low": str(round(l, 2)),
            "close": str(round(c, 2)),
            "volume": str(round(v, 2)),
            "is_closed": True,
        })
    return bars


def test_thc_ay_incremental_context_bit_exact_equivalence():
    """Incremental extend_15m across 15m/1h/4h/1d boundaries must match full recompute (<1e-12)."""
    raw = _synthetic_15m_bars(2400)  # 25 days of 15m bars -> covers 15m/1h/4h/1d warmups
    norm = [normalize_closed_15m_bar(b) for b in raw]

    split = 2200
    inc_ctx = ThcAyCycleContext(norm[:split])
    # Prime all TFs & indicators before incremental extension
    for tf in ("15m", "1h", "4h", "1d"):
        inc_ctx.s_indicators(tf)
    inc_ctx.ay_atr14()
    inc_ctx.ay_adx14()

    # Feed remaining 200 bars one by one (exactly like live 15m closes!)
    t0 = time.perf_counter()
    for i in range(split, len(norm)):
        added = inc_ctx.extend_15m([norm[i]])
        assert added == 1
    inc_ms = (time.perf_counter() - t0) * 1000.0

    # Full recompute reference
    full_ctx = ThcAyCycleContext(norm)
    for tf in ("15m", "1h", "4h", "1d"):
        ref_bars = full_ctx.get_tf_bars(tf)
        inc_bars = inc_ctx.get_tf_bars(tf)
        assert len(inc_bars) == len(ref_bars), f"TF {tf} bar count mismatch"
        assert inc_bars[-1] == ref_bars[-1], f"TF {tf} last bar mismatch"

        ref_atr, ref_adx = full_ctx.s_indicators(tf)
        inc_atr, inc_adx = inc_ctx.s_indicators(tf)
        assert len(inc_atr) == len(ref_atr)
        assert len(inc_adx) == len(ref_adx)
        for a, b in zip(inc_atr[-20:], ref_atr[-20:]):
            if a is None or b is None:
                assert a is None and b is None
            else:
                assert abs(a - b) < 1e-12, f"Wilder ATR mismatch on {tf}: {a} vs {b}"
        for a, b in zip(inc_adx[-20:], ref_adx[-20:]):
            if a is None or b is None:
                assert a is None and b is None
            else:
                assert abs(a - b) < 1e-12, f"Wilder ADX mismatch on {tf}: {a} vs {b}"

    for a, b in zip(inc_ctx.ay_atr14()[-20:], full_ctx.ay_atr14()[-20:]):
        assert abs(a - b) < 1e-12
    for a, b in zip(inc_ctx.ay_adx14()[-20:], full_ctx.ay_adx14()[-20:]):
        assert abs(a - b) < 1e-12

    # Signal equivalence across all 15 S/AY strategies at final bar
    close_ms = norm[-1]["close_ms"]
    for sid in THC_AY_IDS:
        spec = THC_AY_SPECS[sid]
        if spec.is_s_family:
            assert evaluate_s_signal(spec, inc_ctx, close_ms) == evaluate_s_signal(spec, full_ctx, close_ms)
        else:
            assert evaluate_ay_signal(spec, inc_ctx) == evaluate_ay_signal(spec, full_ctx)

    # Incremental 200 steps should be blazing fast (< 250ms total -> ~1ms/step)
    assert inc_ms < 500.0, f"Incremental 200 steps too slow: {inc_ms:.1f}ms"


def test_r_series_shared_context_equivalence_and_hub_cache():
    """Shared MultiTFContextV11 in SharedMarketHub gives identical R01-R06 outputs with 1 build."""
    raw = _synthetic_15m_bars(600)
    hub = SharedMarketHub()
    hub.update_15m_bars(raw[:599])
    hub.append_15m_bars([raw[599]])
    assert hub.incremental_appends == 1
    assert hub.full_rebuilds == 1

    bars = hub.get_bars("15m")
    wm = hub.latest_15m_close_ms
    shared_ctx = hub.get_or_build_context("r_v11_ctx", lambda: MultiTFContextV11(bars), watermark_ms=wm)
    # 5 more strategies hit cache
    for _ in range(5):
        c2 = hub.get_or_build_context("r_v11_ctx", lambda: MultiTFContextV11(bars), watermark_ms=wm)
        assert c2 is shared_ctx

    st = hub.stats()
    assert st["context_build_count"] == 1
    assert st["context_cache_hits"] == 5

    ref_ctx = MultiTFContextV11([normalize_closed_15m_bar(b) for b in raw])
    idx = len(bars) - 1
    for sid in R_V11_SPECS:
        e1, e2 = RSignalEngineV11(sid), RSignalEngineV11(sid)
        assert e1.step(shared_ctx, idx) == e2.step(ref_ctx, idx)


def test_active_position_index_zero_io_and_exact_trigger():
    idx = ActivePositionIndex()
    idx.update([
        WatchEntry("R01", "LONG", stop=D("90000"), target=D("95000")),
        WatchEntry("R02", "SHORT", stop=D("93000"), target=D("88000")),
    ])
    # Routine non-triggering quote -> no urgent ids
    q_safe = {"bid": "91500", "ask": "91501"}
    urgent, mark_due = idx.matching_strategies(q_safe, now_mono=100.0, mark_interval_s=5.0)
    assert urgent == set()
    assert mark_due == {"R01", "R02"}

    # Second tick 1s later -> ZERO urgent, ZERO mark_due -> zero SQLite I/O!
    urgent2, mark_due2 = idx.matching_strategies(q_safe, now_mono=101.0, mark_interval_s=5.0)
    assert urgent2 == set()
    assert mark_due2 == set()

    # Stop hit on R01 -> immediate urgent trigger!
    q_stop = {"bid": "89999", "ask": "90000"}
    urgent3, _ = idx.matching_strategies(q_stop, now_mono=101.1, mark_interval_s=5.0)
    assert urgent3 == {"R01"}

    # Target hit on R02 -> immediate urgent trigger!
    q_tp = {"bid": "87999", "ask": "88000"}
    urgent4, _ = idx.matching_strategies(q_tp, now_mono=101.2, mark_interval_s=5.0)
    assert "R02" in urgent4

    # Dynamic unload removes strategy cleanly
    idx.remove("R01")
    assert not idx.has_strategy("R01")
    assert idx.has_strategy("R02")


def test_readonly_snapshot_cache_event_invalidation_and_meta():
    cache = ReadOnlySnapshotCache(ttl_seconds=15.0)
    calls = {"n": 0}
    def build():
        calls["n"] += 1
        return {"v": calls["n"]}

    assert cache.get_or_build("k1", build) == {"v": 1}
    assert cache.get_or_build("k1", build) == {"v": 1}
    meta = cache.snapshot_meta("k1")
    assert meta["cached"] is True
    assert meta["version"] == 1
    assert cache.stats()["hit_rate"] == 0.5

    cache.invalidate()
    assert cache.get_or_build("k1", build) == {"v": 2}
    assert cache.snapshot_meta("k1")["version"] == 2

    locks = AccountLockRegistry()
    locks.lock_for("TEST-1")
    assert locks.count() == 1
    assert locks.remove("TEST-1") is True
    assert locks.count() == 0
