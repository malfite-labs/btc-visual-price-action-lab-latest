"""Isolated tests verifying single-source common market guard & recovery across all 16+ strategies."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from backend.common_guard import classify_unified_status, evaluate_common_market_guard
from backend.d_bridge import DBridge
from backend.pa_extension import PAExtension
from backend.strategy_pool import StrategyPool
from scripts.local_dashboard import console_control_state, console_rows, pa_status_label


def _fresh_quote(now=None, *, bid='61000.0', ask='61001.0', age_ms=100):
    t = now or (datetime.now(timezone.utc) - timedelta(milliseconds=100))
    return {
        'symbol': 'BTC/USD', 'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH',
        'bid': bid, 'ask': ask, 'event_at': t.isoformat(), 'received_at': t.isoformat(),
        'timestamp_valid': True, 'age_ms': age_ms,
    }


def _make_bars(count=65, *, end_at=None):
    end = end_at or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(minutes=15 * count)
    bars = []
    px = 60000.0
    for i in range(count):
        oa = start + timedelta(minutes=15 * i)
        ca = oa + timedelta(minutes=15)
        bars.append({
            'symbol': 'BTC/USD', 'interval': '15m', 'source': 'BINANCE_USDM_PERP',
            'currency': 'USD_RESEARCH', 'is_closed': True,
            'open_at': oa.isoformat(), 'close_at': ca.isoformat(),
            'open': px, 'high': px + 40, 'low': px - 40, 'close': px + 10, 'volume': 5.0,
        })
        px += 10
    return bars, end


def test_single_common_market_guard_and_unified_status_rules():
    ok_q = _fresh_quote()
    g_ok = evaluate_common_market_guard(
        feed_health='LIVE', quote=ok_q, ws_recovery_state='HEALTHY',
        gap_count_288=0, candle_count=288, startup_monitor_checked=True,
    )
    assert g_ok.entry_ready is True
    assert g_ok.quote_ready is True

    # Brief WS drop
    g_drop = evaluate_common_market_guard(
        feed_health='STALE', quote=ok_q, ws_recovery_state='RECOVERING',
        gap_count_288=0, candle_count=288, startup_monitor_checked=True,
    )
    assert g_drop.entry_ready is False
    assert g_drop.quote_ready is False

    # Priority: real fault never masked; manual pause never auto-cleared
    assert classify_unified_status(enabled=True, market_ready=True, fault_reason='GAP') == 'RECONCILE_ERROR'
    assert classify_unified_status(enabled=False, paused_by_user=True, market_ready=True) == 'PAUSED'
    assert classify_unified_status(enabled=False, paused_by_user=False, market_ready=True) == 'DISABLED'
    assert classify_unified_status(enabled=True, market_ready=False) == 'MARKET_RECOVERING'
    assert classify_unified_status(enabled=True, market_ready=True, holding=True) == 'RUNNING'
    assert classify_unified_status(enabled=True, market_ready=True, waiting_signal=True) == 'WAITING_SIGNAL'


def test_d_common_guard_recovers_from_brief_network_drop_and_preserves_manual_pause(tmp_path):
    async def scenario():
        net_ok = {'ready': True}
        now_box = {'now': datetime.now(timezone.utc)}
        bridge = DBridge(
            tmp_path / 'd_formal.sqlite3',
            tmp_path / 'd_trading.sqlite3',
            archive=None,
            feed_healthy=lambda: net_ok['ready'],
            candles_complete=lambda: net_ok['ready'],
            clock=lambda: now_box['now'],
            common_guard=True,
        )
        await bridge.start()
        try:
            q1 = _fresh_quote(now_box['now'] - timedelta(seconds=2))
            await bridge.process_common('quote', q1)
            st1 = await bridge.status(q1)
            assert st1['status'] == 'AUTO_PAPER'
            assert st1['integration_version'] == 'D-COMMON-GUARD-V1'

            # Simulate brief network drop / invalid quote -> MARKET_RECOVERING (not permanent lock)
            net_ok['ready'] = False
            await bridge.process_common('quote', None)
            st_drop = await bridge.status(q1)
            assert st_drop['status'] == 'MARKET_RECOVERING'
            assert st_drop['control_state'] == 'MARKET_RECOVERING'

            # Network recovers -> next valid quote automatically restores AUTO_PAPER
            net_ok['ready'] = True
            now_box['now'] += timedelta(seconds=5)
            q2 = _fresh_quote(now_box['now'] - timedelta(seconds=1))
            await bridge.process_common('quote', q2)
            st_rec = await bridge.status(q2)
            assert st_rec['status'] == 'AUTO_PAPER'

            # Manual pause is NEVER auto-resumed by healthy quotes
            bridge.set_enabled(False)
            now_box['now'] += timedelta(seconds=5)
            q3 = _fresh_quote(now_box['now'] - timedelta(seconds=1))
            await bridge.process_common('quote', q3)
            st_paused = await bridge.status(q3)
            assert st_paused['status'] == 'PAUSED'
            assert st_paused['enabled'] is False

            # Explicit user resume rearms common_guard immediately
            bridge.set_enabled(True)
            st_resumed = await bridge.status(q3)
            assert st_resumed['status'] == 'AUTO_PAPER'
            assert st_resumed['enabled'] is True
        finally:
            await bridge.close()

    asyncio.run(scenario())


def test_pa09_pa10_auto_recover_after_network_drop_and_sync_toggles(tmp_path):
    ext = PAExtension(tmp_path / 'pa-ext', register=True, enabled=('PA-09', 'PA-10'))
    now = datetime.now(timezone.utc)
    bar_end = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    bars, close_dt = _make_bars(65, end_at=bar_end)

    # Bar closes during transient network drop -> ENTRY_BLOCKED
    for sid in ('PA-09', 'PA-10'):
        res = ext.ledger_cycle(sid, bars, close_dt, lambda: False)
        assert res == 'ENTRY_BLOCKED'

    # While network is still down -> MARKET_RECOVERING, toggle STILL 'on' (authorized!)
    items_down = ext.list(market_ready=False)
    for item in items_down:
        assert item['status'] == 'MARKET_RECOVERING'
        assert item['enabled'] is True
        assert pa_status_label(item) == '行情恢复中'

    # Network recovers -> fresh quote clears transient ENTRY_BLOCKED back to WAIT -> RUNNING
    q = _fresh_quote(close_dt + timedelta(seconds=10))
    for sid in ('PA-09', 'PA-10'):
        ext.ledger_quote(sid, q, lambda: True)
    items_up = ext.list(market_ready=True)
    for item in items_up:
        assert item['status'] == 'RUNNING'
        assert item['last_status'] == 'WAIT'
        assert pa_status_label(item) == '运行中'

    # Console rows & toggle switch consistency for PA-09, PA-10, G
    g_status = {
        'enabled': True, 'status': 'WAIT_FOR_C_NEXT_OPEN', 'follow_state': 'WAIT_FOR_C_NEXT_OPEN',
        'last_action': 'MIRROR_RISK_EXIT', 'last_error': None, 'position': None,
        'equity': '9758.78', 'realized': '-241.22', 'unrealized': '0', 'fees': '12.00',
    }
    rows = console_rows({}, items_up, g_status=g_status)
    assert rows['G']['status_label'] == '等待C开仓'
    assert rows['G']['action'] == 'WAIT_FOR_C_NEXT_OPEN'
    assert rows['G']['historical_action'] == 'MIRROR_RISK_EXIT'
    assert console_control_state('G', rows['G']) == 'on'
    for sid in ('PA-09', 'PA-10'):
        assert sid in rows
        assert rows[sid]['status_label'] == '运行中'
        assert console_control_state(sid, rows[sid]) == 'on'
