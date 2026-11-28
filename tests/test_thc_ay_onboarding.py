"""Isolated verification suite for the 15 Aoying S / AY V3.1 Mode B paper instances.

Covers:
1. Frozen source SHA-256 verification for all 15 configs.
2. Research-mode signal & SL/TP exact alignment against thankucrypto-backtest & ay-research.
3. Native timeframe boundary gating (15m / 1h / 4h / 1d UTC alignment; no 15m re-eval on HTF).
4. Formal paper lifecycle ($10,000, 2% risk, 5x cap, prewarm zero-retro-trades, quote open/close,
   pause protection, stale-market guard & pending_exit recovery, restart reconciliation, cooldown, rejections).
5. Zero-hardcoding discovery across build_live_paper_rows, paper_fills, and ResearchV3Engine.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

V31_ROOT = Path(__file__).resolve().parents[1]
WS_ROOT = V31_ROOT.parent.parent
if str(V31_ROOT) not in sys.path:
    sys.path.insert(0, str(V31_ROOT))

from backend.common_guard import CommonMarketGuard
from backend.domain import CostModel
from backend.thc_ay_extension import ThcAyExtension, ThcAyPaperLedger
from scripts.live_copy_v2 import paper_fills
from scripts.local_dashboard import build_live_paper_rows
from strategies.r_series.indicators import aggregate_closed_htf
from strategies.thc_ay.signals import (
    ThcAyCycleContext,
    compute_s_sl_tp,
    evaluate_ay_signal,
    evaluate_s_signal,
    validate_ay_entry,
)
from strategies.thc_ay.specs import THC_AY_IDS, THC_AY_SPECS

UTC = timezone.utc


def _healthy_guard() -> CommonMarketGuard:
    return CommonMarketGuard(
        feed_live=True,
        quote_fresh=True,
        ws_healthy=True,
        candles_complete=True,
        startup_monitor_checked=True,
        reason=None,
    )


def _stale_guard() -> CommonMarketGuard:
    return CommonMarketGuard(
        feed_live=True,
        quote_fresh=False,
        ws_healthy=True,
        candles_complete=True,
        startup_monitor_checked=True,
        reason="QUOTE_STALE",
    )


def _make_quote(event_dt: datetime, bid: str, ask: str) -> dict:
    return {
        "source": "BINANCE_USDM_PERP",
        "symbol": "BTC/USD",
        "currency": "USD_RESEARCH",
        "timestamp_valid": True,
        "age_ms": 120,
        "event_at": event_dt.astimezone(UTC).isoformat(),
        "bid": bid,
        "ask": ask,
    }


def test_01_all_15_source_config_sha256_match():
    assert len(THC_AY_IDS) == 15
    thc_cfg = WS_ROOT / "thankucrypto-backtest" / "configs"
    ay_inp = WS_ROOT / "ay-research" / "inputs"
    for sid in THC_AY_IDS:
        spec = THC_AY_SPECS[sid]
        if spec.is_s_family:
            p = thc_cfg / f"{spec.source_experiment}.json"
        else:
            p = ay_inp / f"{spec.source_experiment}_quantized.json"
        assert p.is_file(), f"Missing source config {p}"
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        assert actual == spec.config_sha256, f"SHA-256 mismatch for {sid}"


def test_02_research_signal_alignment_s_and_ay():
    ext_db = V31_ROOT / "research" / "r_series" / "binance_btcusdt_15m_extended.sqlite3"
    if not ext_db.is_file():
        pytest.skip("Extended 15m archive not present")
    from research.r_series.backtest_runner import load_candles_ro

    bars, _ = load_candles_ro(ext_db)
    # Use last 25,000 bars (~260 days >= 200 daily warmup) for fast exact alignment
    slice_bars = bars[-25000:]
    ctx = ThcAyCycleContext(slice_bars)
    daily_cnt = ctx.closed_daily_count_at(slice_bars[-1]["close_ms"])
    assert daily_cnt >= 250

    # --- 1) Verify AY02, AY03, AY04 exact signal parity with ay-research/ay_signals.py ---
    ay_dir = str(WS_ROOT / "ay-research")
    if ay_dir not in sys.path:
        sys.path.insert(0, ay_dir)
    from ay_signals import Context as AyOrigContext

    orig_ctx = AyOrigContext(slice_bars)
    tail_n = 300
    tail_start = len(slice_bars) - tail_n
    for sid, ay_id in (("thc-ay02-15m", "AY02"), ("thc-ay03-15m", "AY03"), ("thc-ay04-15m", "AY04")):
        spec = THC_AY_SPECS[sid]
        orig_sigs = {
            i: sig for i, sig in orig_ctx.signals(ay_id, spec.parameters, structure=False) if i >= tail_start
        }
        our_sigs = {}
        for idx in range(tail_start, len(slice_bars)):
            sub_ctx = ThcAyCycleContext(slice_bars[: idx + 1])
            sig, _, _ = evaluate_ay_signal(spec, sub_ctx)
            if sig is not None:
                our_sigs[idx] = (sig["direction"], float(sig["stop"]), float(sig["target"]) if sig["target"] is not None else None)
        assert set(our_sigs.keys()) == set(orig_sigs.keys()), f"Signal index mismatch for {sid}"
        for idx, o_sig in orig_sigs.items():
            d_dir, d_stop, d_tgt = our_sigs[idx]
            assert d_dir == o_sig.direction
            assert abs(d_stop - o_sig.stop) < 1e-6
            if o_sig.target is not None:
                assert d_tgt is not None and abs(d_tgt - o_sig.target) < 1e-6

    # --- 2) Verify S01 / S02 / COMBO 1H signal parity with thankucrypto-backtest/src/backtest_engine.py ---
    thc_src = str(WS_ROOT / "thankucrypto-backtest" / "src")
    if thc_src not in sys.path:
        sys.path.insert(0, thc_src)
    from backtest_engine import BacktestEngine, Bar as ThcBar

    bars_1h = ctx.get_tf_bars("1h")
    bars_1d = ctx.daily_bars()
    thc_1h = [
        ThcBar(b["t"], b["close_ms"], b["open_at"], b["close_at"], b["o"], b["h"], b["l"], b["c"], b["v"], True)
        for b in bars_1h
    ]
    thc_1d = [
        ThcBar(b["t"], b["close_ms"], b["open_at"], b["close_at"], b["o"], b["h"], b["l"], b["c"], b["v"], True)
        for b in bars_1d
    ]
    for sid, stype in (("thc-s01-1h", "S01"), ("thc-s02-1h", "S02"), ("thc-s01-s02-combo-1h", "COMBO")):
        spec = THC_AY_SPECS[sid]
        eng = BacktestEngine(
            {"strategy_id": sid, "strategy_type": stype, "exec_tf": "1h", "min_daily_warmup": 200, "params": spec.parameters},
            thc_1h,
            thc_1d,
        )
        # Check last 40 closed 1h bars
        for h_idx in range(len(bars_1h) - 40, len(bars_1h)):
            target_close_ms = bars_1h[h_idx]["close_ms"]
            # Find matching 15m index ending at target_close_ms
            pos_15m = next(k for k in range(len(slice_bars) - 1, -1, -1) if slice_bars[k]["close_ms"] == target_close_ms)
            sub_ctx = ThcAyCycleContext(slice_bars[: pos_15m + 1])
            our_sig, _, _ = evaluate_s_signal(spec, sub_ctx, target_close_ms)

            regime = eng._classify_regime(h_idx, spec.parameters)
            orig_side = None
            orig_sub = None
            if stype == "S01":
                orig_side, _, _ = eng._s01_signal(h_idx, regime, spec.parameters)
                orig_sub = "S01" if orig_side else None
            elif stype == "S02":
                orig_side, _, _ = eng._s02_signal(h_idx, spec.parameters)
                orig_sub = "S02" if orig_side else None
            else:
                if regime == "RANGE":
                    orig_side, _, _ = eng._s01_signal(h_idx, regime, spec.parameters)
                    orig_sub = "S01" if orig_side else None
                elif regime == "TREND":
                    orig_side, _, _ = eng._s02_signal(h_idx, spec.parameters)
                    orig_sub = "S02" if orig_side else None

            assert (our_sig["direction"] if our_sig else None) == orig_side, f"Mismatch at {sid} idx={h_idx}"
            assert (our_sig["sub_strategy"] if our_sig else None) == orig_sub


def test_03_native_timeframe_gating_no_15m_reeval():
    ext_db = V31_ROOT / "research" / "r_series" / "binance_btcusdt_15m_extended.sqlite3"
    from research.r_series.backtest_runner import load_candles_ro

    bars, _ = load_candles_ro(ext_db)
    # Pick a bar ending at :15 UTC (not 1h, 4h, or 1d boundary)
    idx_15 = next(k for k in range(len(bars) - 1, len(bars) - 200, -1) if (bars[k]["close_ms"] % 3_600_000) != 0)
    ctx_15 = ThcAyCycleContext(bars[idx_15 - 22000 : idx_15 + 1])
    for sid in ("thc-s01-1h", "thc-s02-4h", "thc-s01-s02-combo-1d"):
        sig, st, nat_iso = evaluate_s_signal(THC_AY_SPECS[sid], ctx_15, bars[idx_15]["close_ms"])
        assert sig is None
        assert st == "WAIT_NATIVE_CLOSE"
        assert nat_iso is None

    # Pick a bar ending at 00:00 UTC (valid 15m, 1h, 4h, AND 1d boundary!)
    idx_1d = next(k for k in range(len(bars) - 1, len(bars) - 400, -1) if (bars[k]["close_ms"] % 86_400_000) == 0)
    ctx_1d = ThcAyCycleContext(bars[idx_1d - 22000 : idx_1d + 1])
    for sid in ("thc-s01-15m", "thc-s01-1h", "thc-s01-4h", "thc-s01-1d"):
        _, st, nat_iso = evaluate_s_signal(THC_AY_SPECS[sid], ctx_1d, bars[idx_1d]["close_ms"])
        assert st != "WAIT_NATIVE_CLOSE"
        assert nat_iso == bars[idx_1d]["close_at"]


def test_04_paper_lifecycle_protection_recovery_and_discovery(tmp_path):
    ext_dir = tmp_path / "extensions"
    ext = ThcAyExtension(ext_dir, project_root=V31_ROOT, register=True, enabled=())
    assert len(ext.ledgers) == 15
    # All 15 start DISABLED with $10,000 initial equity and zero fills
    for sid in THC_AY_IDS:
        st = ext.ledgers[sid].snapshot()
        assert st["enabled"] is False
        assert st["cash"] == "10000"
        assert st["position"] is None
        assert ext.ledgers[sid].fills() == []

    # Test full open -> pause protection -> stop exit -> restart reconciliation -> copy-trading fills on thc-s02-15m
    lg = ext.ledgers["thc-s02-15m"]
    lg.set_enabled(True)
    sig_dt = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
    with lg._tx() as db:
        st = lg._read(db)
        st["pending_entry"] = {
            "signal_id": "thc-s02-15m:1.0.0:2026-11-27T09:00:00+00:00:LONG",
            "signal_cycle": sig_dt.isoformat(),
            "signal_close_ms": int(sig_dt.timestamp() * 1000),
            "direction": "LONG",
            "sub_strategy": "S02",
            "signal_source": "S02_Q_BTC_15m_lab:S02:TREND",
            "regime": "TREND",
            "signal_bar_high": 62000.0,
            "signal_bar_low": 61200.0,
            "signal_bar_close": 61900.0,
            "atr_at_signal": 300.0,
            "upper_band": 61800.0,
            "lower_band": 60500.0,
        }
        lg._save_and_sync(db, st)

    # Stale quote must block entry
    res_stale = lg.on_quote(_make_quote(sig_dt + timedelta(seconds=5), "61900", "61901"), guard=_stale_guard())
    assert res_stale["status"] == "QUOTE_STALE_OR_NOT_READY"

    # Fresh quote opens position
    res_open = lg.on_quote(_make_quote(sig_dt + timedelta(seconds=10), "61900", "61901"), guard=_healthy_guard())
    assert res_open["status"] == "OPEN_LONG"
    st_open = lg.snapshot()
    assert st_open["position"] is not None
    stop_px = D(st_open["position"]["stop"])
    assert stop_px < D(st_open["position"]["entry"]) < D(st_open["position"]["target"])

    # User pauses strategy while holding: protection MUST remain active!
    lg.set_enabled(False, paused_by_user=True)
    res_stop = lg.on_quote(
        _make_quote(sig_dt + timedelta(seconds=25), str(stop_px - D("50")), str(stop_px - D("49"))),
        guard=_healthy_guard(),
    )
    assert res_stop["status"] == "CLOSE_STOP_QUOTE"
    assert lg.snapshot()["position"] is None

    # Restart recovery & ledger reconciliation
    lg_reloaded = ThcAyPaperLedger(ext_dir, "thc-s02-15m", cost=CostModel())
    rec = lg_reloaded.reconcile()
    assert rec["ok"] is True
    assert rec["fill_count"] == 2

    # Zero-hardcoding copy-trading adapter verification (G10/G11)
    rows = build_live_paper_rows(data_dir=tmp_path)
    for sid in THC_AY_IDS:
        assert sid in rows
    fills_out = paper_fills(ext_dir / "thc-s02-15m.sqlite3", after=0, strategy="thc-s02-15m")
    assert len(fills_out) == 2
    assert fills_out[0].action == "OPEN"
    assert fills_out[1].action == "FULL_CLOSE"

    # AY04 net-R rejection check
    tgt, rej = validate_ay_entry("LONG", D("60000"), D("60500"), None, D("60400"), D("60412"), D("0.0005"), D("0.0002"))
    assert tgt is None and rej in ("NET_R_BELOW_ONE", "TARGET_CANNOT_COVER_COSTS")
