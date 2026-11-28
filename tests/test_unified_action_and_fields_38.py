"""Regression tests for unified 13-action contract & 38-strategy public display field alignment."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

V31_ROOT = Path(__file__).resolve().parents[1]
if str(V31_ROOT) not in sys.path:
    sys.path.insert(0, str(V31_ROOT))

from scripts.local_dashboard import (
    action_label,
    action_tone,
    d_control_state,
    money_label,
    strategy_view,
)


@pytest.mark.parametrize("raw_code,expected_cn,expected_tone", [
    ("WAIT", "等待信号", "neutral"),
    ("IDLE", "等待信号", "neutral"),
    ("NO_SETUP", "等待信号", "neutral"),
    ("NO_SIGNAL", "等待信号", "neutral"),
    ("WAIT_FOR_C_NEXT_OPEN", "等待信号", "neutral"),
    ("WAIT_NATIVE_CLOSE", "等待原生周期", "neutral"),
    ("WAIT_COMPLETE_NATIVE_BAR", "等待原生周期", "neutral"),
    ("WARMUP_DAILY:120/200", "等待原生周期", "neutral"),
    ("MARKET_BLOCKED:QUOTE_STALE_OR_INVALID", "行情等待", "amber"),
    ("BLOCKED:QUOTE_UNAVAILABLE_OR_STALE", "行情等待", "amber"),
    ("GAP_NO_ENTRY", "行情等待", "amber"),
    ("PENDING_NEXT_OPEN:LONG", "待开仓", "amber"),
    ("SIGNAL_LONG", "待开仓", "amber"),
    ("OPEN_LONG", "做多开仓", "green"),
    ("OPEN_SHORT", "做空开仓", "green"),
    ("HOLD", "持有", "green"),
    ("CLOSE_TARGET", "止盈平仓", "green"),
    ("CLOSE_TARGET_QUOTE", "止盈平仓", "green"),
    ("CLOSE_STOP", "止损平仓", "red"),
    ("CLOSE_STOP_QUOTE", "止损平仓", "red"),
    ("CLOSE_TIME", "到期平仓", "neutral"),
    ("CLOSE_MAX_HOLD", "到期平仓", "neutral"),
    ("EXIT_PENDING_STOP", "待退出", "amber"),
    ("PAUSED", "暂停", "gray"),
    ("REGISTERED", "暂停", "gray"),
    ("CYCLE_ERROR", "需检查", "red"),
    ("RECONCILE_ERROR", "需检查", "red"),
    ("BLOCKED_GAP_RECONCILIATION_REQUIRED", "需检查", "red"),
    ("UNRECOGNIZED_INTERNAL_FAULT", "需检查", "red"),
])
def test_unified_13_action_contract(raw_code, expected_cn, expected_tone):
    lbl = action_label(raw_code)
    assert lbl == expected_cn
    assert action_tone(lbl) == expected_tone


def test_pa_r_s_ay_identical_under_market_recovering_and_holding():
    # Under 行情恢复中 when flat: PA (WAIT), R (MARKET_BLOCKED:*), S (WAIT), AY (MARKET_BLOCKED:*) ALL show 行情等待!
    pa_v = strategy_view("pa-trend-40-breakout", {"status_label": "行情恢复中", "state": "FLAT", "action": "WAIT"})
    r_v = strategy_view("R01", {"status_label": "行情恢复中", "state": "FLAT", "action": "MARKET_BLOCKED:QUOTE_STALE_OR_INVALID"})
    s_v = strategy_view("thc-s01-15m", {"status_label": "行情恢复中", "state": "FLAT", "action": "WAIT"})
    ay_v = strategy_view("thc-ay02-15m", {"status_label": "行情恢复中", "state": "FLAT", "action": "MARKET_BLOCKED:QUOTE_STALE_OR_INVALID"})
    assert pa_v["action"] == r_v["action"] == s_v["action"] == ay_v["action"] == "行情等待"
    assert pa_v["action_tone"] == r_v["action_tone"] == "amber"
    assert r_v["raw_action"] == "MARKET_BLOCKED:QUOTE_STALE_OR_INVALID"

    # When holding under 行情恢复中: MUST show 持有!
    hold_v = strategy_view("pa-sma20-pullback-confirm", {"status_label": "行情恢复中", "state": "OPEN_LONG", "action": "HOLD"})
    assert hold_v["action"] == "持有"
    assert hold_v["position"] == "做多"

    # Real error under 行情恢复中: MUST NOT be masked -> 需检查!
    err_v = strategy_view("R02", {"status_label": "对账异常", "state": "FLAT", "action": "RECONCILE_ERROR"})
    assert err_v["action"] == "需检查"
    assert err_v["action_tone"] == "red"


def test_public_display_fields_consistency():
    assert money_label("10029.39") == "10,029.39"
    assert money_label("10,349.31") == "10,349.31"
    assert d_control_state({"status": "MARKET_RECOVERING", "enabled": True}) == "MARKET_RECOVERING"
    assert d_control_state({"status": "RUNNING", "enabled": True}) == "RUNNING"
