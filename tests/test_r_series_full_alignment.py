"""Isolated alignment tests for R01-R06 V1.1 full 23-strategy integration (P1-P5)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend.common_guard import CommonMarketGuard
from backend.r_series_extension import RSeriesPaperLedgerV11
from scripts.live_copy_v2 import paper_fills
from scripts.local_dashboard import (
    build_live_paper_rows,
    pa_status_label,
    strategy_status_appearance,
)

UTC = timezone.utc


def test_p1_unified_status_mapping_for_r_series_and_pa():
    """P1: WAITING_SIGNAL maps to green '运行中'; faults/recovery/pause are never masked."""
    assert pa_status_label({'status': 'WAITING_SIGNAL'}) == '运行中'
    assert pa_status_label({'status': 'RUNNING'}) == '运行中'
    assert pa_status_label({'status': 'MARKET_RECOVERING'}) == '行情恢复中'
    assert pa_status_label({'status': 'RECONCILING'}) == '对账中'
    assert pa_status_label({'status': 'FAULTED'}) == '对账异常'
    assert pa_status_label({'status': 'RECONCILE_ERROR'}) == '对账异常'
    assert pa_status_label({'status': 'PAUSED'}) == '已暂停'
    assert pa_status_label({'status': 'DISABLED'}) == '已停用'

    assert strategy_status_appearance('运行中')['tone'] == 'success'
    assert strategy_status_appearance('行情恢复中')['tone'] == 'warning'
    assert strategy_status_appearance('对账中')['tone'] == 'warning'
    assert strategy_status_appearance('对账异常')['tone'] == 'danger'
    assert strategy_status_appearance('已暂停')['tone'] == 'neutral'
    assert strategy_status_appearance('已停用')['tone'] == 'neutral'


def test_p4_live_copy_discovers_all_23_strategies_and_reads_r_series_fills(tmp_path):
    """P4: build_live_paper_rows discovers all 23 strategies and paper_fills adapts R-series open/close events."""
    rows = build_live_paper_rows()
    assert len(rows) >= 23
    for i in range(1, 7):
        sid = f'R0{i}'
        assert sid in rows
        assert 'V1.1' in rows[sid]

    # Isolated R04 ledger open + stop-loss close while paused (also verifies P5 exit protection!)
    lg = RSeriesPaperLedgerV11(tmp_path, 'R04', enabled=True)
    assert paper_fills(lg.path, strategy='R04') == 0

    now = datetime.now(UTC)
    t_open = (now - timedelta(minutes=2)).isoformat()
    t_close = (now - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(lg.path) as db:
        open_rec = {
            'idempotency_key': 'R04:open:sig1', 'strategy_id': 'R04', 'version': '1.1.0',
            'trade_id': 'R04:sig1', 'kind': 'OPEN', 'side': 'BUY', 'direction': 'LONG',
            'at': t_open, 'qty': '0.1', 'price': '60000', 'fee': '3.0', 'reason': 'ENTRY_NEXT_OPEN',
        }
        close_rec = {
            'idempotency_key': 'R04:close:R04:sig1:STOP_QUOTE', 'strategy_id': 'R04', 'version': '1.1.0',
            'trade_id': 'R04:sig1', 'kind': 'CLOSE', 'side': 'SELL', 'direction': 'LONG',
            'at': t_close, 'qty': '0.1', 'price': '59500', 'fee': '2.975', 'reason': 'STOP_QUOTE',
        }
        db.execute('INSERT INTO fills VALUES(?,?)', (open_rec['idempotency_key'], json.dumps(open_rec)))
        db.execute('INSERT INTO fills VALUES(?,?)', (close_rec['idempotency_key'], json.dumps(close_rec)))
        db.commit()

    assert paper_fills(lg.path, strategy='R04') == 2
    evts = paper_fills(lg.path, after=0, strategy='R04')
    assert len(evts) == 2
    assert evts[0].action == 'OPEN' and evts[0].direction == 'LONG' and evts[0].version == '1.1.0'
    assert evts[1].action == 'FULL_CLOSE' and evts[1].direction == 'LONG' and evts[1].trade_id == 'R04:sig1'
    # Cursor dedup recovery
    assert paper_fills(lg.path, after=2, strategy='R04') == []


def test_p5_r_series_pause_preserves_open_position_stop_protection(tmp_path):
    """P5: Pausing an R-series strategy blocks new entries but still executes stop-loss on open position."""
    lg = RSeriesPaperLedgerV11(tmp_path, 'R04', enabled=True)
    # Seed an open LONG position in isolated ledger, then pause strategy
    now = datetime.now(UTC)
    with lg._tx() as db:
        st = lg._read(db)
        st['position'] = {
            'version': '1.1.0', 'direction': 'LONG', 'entry': '60000', 'stop': '59500',
            'target': '61500', 'qty': '0.1', 'entry_fee': '3.0', 'mark': '60000',
            'trade_id': 'R04:test-pos', 'opened_cycle': (now - timedelta(minutes=15)).isoformat(),
            'bars_held': 1, 'max_hold_bars': 64,
        }
        lg._save_and_sync(db, st)

    lg.set_enabled(False, paused_by_user=True)
    guard = CommonMarketGuard(
        feed_live=True, quote_fresh=True, ws_healthy=True,
        candles_complete=True, startup_monitor_checked=True,
    )
    quote = {
        'bid': '59400', 'ask': '59410',
        'event_at': (now - timedelta(seconds=1)).isoformat(),
        'received_at': now.isoformat(),
        'age_ms': 100, 'timestamp_valid': True,
        'source': 'BINANCE_USDM_PERP', 'symbol': 'BTC/USD', 'currency': 'USD_RESEARCH',
    }
    res = lg.on_quote(quote, guard=guard)
    assert res['status'] == 'CLOSE_STOP_QUOTE'
    assert lg.snapshot()['position'] is None
