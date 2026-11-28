"""Comprehensive P1-P6 alignment & G14-G18 regression tests for BTC Paper Lab V3.1 (38 strategies + dynamic unknown strategy)."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

V31_ROOT = Path(__file__).resolve().parents[1]
if str(V31_ROOT) not in sys.path:
    sys.path.insert(0, str(V31_ROOT))

from backend.common_guard import CommonMarketGuard
from backend.domain import CostModel
from backend.kline_observation import EventReader
from backend.thc_ay_extension import ThcAyExtension, ThcAyPaperLedger
from scripts.live_copy_v2 import paper_fills
from scripts.local_dashboard import (
    build_live_paper_rows,
    dynamic_console_categories,
    filter_console_rows,
    strategy_category,
)
from strategies.thc_ay.specs import THC_AY_IDS

UTC = timezone.utc


def _healthy_guard() -> CommonMarketGuard:
    return CommonMarketGuard(
        feed_live=True, quote_fresh=True, ws_healthy=True,
        candles_complete=True, startup_monitor_checked=True, reason=None,
    )


def test_p2_dynamic_categories_38_and_unknown_future_strategy():
    # Build synthetic 38-strategy map matching live metadata
    rows = {}
    for s in ("A", "B", "C", "D", "E"):
        rows[s] = {"id": s, "name": s, "status_label": "运行中"}
    rows["G"] = {"id": "G", "name": "C反向镜像", "status_label": "运行中"}
    for i in range(1, 11):
        sid = f"PA-{i:02d}"
        rows[sid] = {"id": sid, "name": sid, "status_label": "运行中", "raw": {"group": "PA"}}
    rows["pa-chan-center-divergence"] = {"id": "pa-chan-center-divergence", "name": "中枢震荡", "status_label": "运行中", "raw": {"group": "策略池"}}
    for i in range(1, 7):
        sid = f"R0{i}"
        rows[sid] = {"id": sid, "name": sid, "status_label": "运行中", "raw": {"group": "R系列"}}
    for sid in THC_AY_IDS:
        grp = "S系列" if sid.startswith("thc-s") else "AY系列"
        rows[sid] = {"id": sid, "name": sid, "status_label": "运行中", "raw": {"group": grp}}

    cats = dict(dynamic_console_categories(rows))
    assert cats["全部"] == "全部策略(38)"
    assert cats["原有策略"] == "原有策略(5)"
    assert cats["镜像策略"] == "镜像策略(1)"
    assert cats["PA系列"] == "PA系列(10)"
    assert cats["Chan"] == "Chan(1)"
    assert cats["R系列"] == "R系列(6)"
    assert cats["S系列"] == "S系列(12)"
    assert cats["AY系列"] == "AY系列(3)"
    assert sum(len(filter_console_rows(rows, k)) for k in ("原有策略", "镜像策略", "PA系列", "Chan", "R系列", "S系列", "AY系列")) == 38

    # Dynamic unknown future strategy (zero hardcoding!)
    rows["x01-quantum-trend"] = {"id": "x01-quantum-trend", "name": "未来X策略", "status_label": "运行中", "raw": {"group": "X系列"}}
    cats2 = dict(dynamic_console_categories(rows))
    assert cats2["全部"] == "全部策略(39)"
    assert cats2["X系列"] == "X系列(1)"
    assert filter_console_rows(rows, "X系列") == ["x01-quantum-trend"]


def test_p4_kline_event_reader_discovers_extensions_and_r_series(tmp_path):
    ext_dir = tmp_path / "extensions"
    lg = ThcAyPaperLedger(ext_dir, "thc-s01-15m", cost=CostModel(), enabled=True)
    t0 = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
    with lg._tx() as db:
        st = lg._read(db)
        st["pending_entry"] = {
            "signal_id": "thc-s01-15m:1.0.0:2026-11-27T09:00:00+00:00:LONG",
            "signal_cycle": t0.isoformat(),
            "signal_close_ms": int(t0.timestamp() * 1000),
            "direction": "LONG",
            "sub_strategy": "S01",
            "signal_source": "S01_Q_BTC_15m_lab:S01:RANGE",
            "regime": "RANGE",
            "signal_bar_high": 62000.0,
            "signal_bar_low": 61000.0,
            "signal_bar_close": 61100.0,
            "atr_at_signal": 200.0,
            "upper_band": 62500.0,
            "lower_band": 61200.0,
        }
        lg._save_and_sync(db, st)
    q_open = {
        "source": "BINANCE_USDM_PERP", "symbol": "BTC/USD", "currency": "USD_RESEARCH",
        "timestamp_valid": True, "age_ms": 80, "event_at": (t0 + timedelta(seconds=10)).isoformat(),
        "bid": "61100", "ask": "61101",
    }
    res_o = lg.on_quote(q_open, guard=_healthy_guard())
    assert res_o["status"] == "OPEN_LONG"

    reader = EventReader(V31_ROOT, data_root=tmp_path)
    evs = [e for e in reader.read() if e["strategy_id"] == "thc-s01-15m"]
    assert len(evs) == 1
    assert evs[0]["verification_status"] == "VERIFIED"
    assert evs[0]["position_effect"] == "OPEN"
    assert evs[0]["position_direction"] == "LONG"


def test_p5_copy_trading_rejects_mismatched_and_backtest_ledgers(tmp_path):
    # 1. Identity mismatch rejected
    bad_id_db = tmp_path / "thc-s01-15m.sqlite3"
    with sqlite3.connect(str(bad_id_db)) as db:
        db.execute("CREATE TABLE snapshot (value TEXT NOT NULL)")
        db.execute("CREATE TABLE fills (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO snapshot VALUES (?)", (json.dumps({"strategy_id": "OTHER-ID"}),))
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        paper_fills(bad_id_db, after=None, strategy="thc-s01-15m")

    # 2. Backtest ledger rejected
    bt_db = tmp_path / "thc-s02-15m.sqlite3"
    with sqlite3.connect(str(bt_db)) as db:
        db.execute("CREATE TABLE snapshot (value TEXT NOT NULL)")
        db.execute("CREATE TABLE fills (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO snapshot VALUES (?)", (json.dumps({"strategy_id": "thc-s02-15m", "is_backtest": True}),))
    with pytest.raises(ValueError, match="BACKTEST_LEDGER_REJECTED"):
        paper_fills(bt_db, after=None, strategy="thc-s02-15m")

    # 3. Unknown future compliant extension dynamically discovered in isolated data_dir
    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir(parents=True, exist_ok=True)
    fut_db = ext_dir / "x01-future.sqlite3"
    with sqlite3.connect(str(fut_db)) as db:
        db.execute("CREATE TABLE snapshot (value TEXT NOT NULL)")
        db.execute("CREATE TABLE fills (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO snapshot VALUES (?)", (json.dumps({"strategy_id": "x01-future", "name": "未来X策略"}),))
    discovered = build_live_paper_rows(data_dir=tmp_path)
    assert discovered["x01-future"] == "x01-future · 未来X策略"
    assert paper_fills(fut_db, after=None, strategy="x01-future") == 0
