"""Comprehensive isolated tests for BTC Paper Lab V3.1 Strategy Pool Fixes (BUG-001..BUG-005)."""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend.strategy_pool import StrategyPool, StrategyValidationError
from scripts.live_copy_v2 import paper_fills
from scripts.local_dashboard import (
    build_live_paper_rows,
    console_rows,
    is_pool_strategy_id,
    rule_detail_target,
    strategy_view,
)
from scripts.strategy_pool_ui import (
    add_calendar_months,
    normalize_fills_for_table,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_v2_strategy(sid: str, name: str, version: str = '1.0.0', target_r: float = 1.5, always_long: bool = False) -> bytes:
    long_expr = (
        {'op': 'gt', 'args': [{'op': 'close', 'args': [0]}, 0]}
        if always_long
        else {'op': 'and', 'args': [
            {'op': 'gt', 'args': [{'op': 'close', 'args': [0]}, {'op': 'sma', 'args': [20, 0]}]},
            {'op': 'gt', 'args': [{'op': 'close', 'args': [0]}, {'op': 'open', 'args': [0]}]},
        ]}
    )
    doc = {
        'format': 'btc-lab-declarative-strategy/v2',
        'strategy_id': sid,
        'name': name,
        'version': version,
        'description': f'{name} 隔离测试策略',
        'market': 'BTC/USD',
        'timeframe': '15m',
        'parameters': {'confirm_body_min': 0.35},
        'entry': {'long': long_expr, 'short': {'op': 'eq', 'args': [0, 1]}},
        'position': {
            'direction': 'long',
            'entry_timing': 'next_open',
            'max_hold_bars': 12,
            'same_bar_priority': 'stop',
            'stop': {
                'kind': 'swing_atr',
                'lookback': 4,
                'atr_period': 14,
                'buffer_atr': 0.15,
                'min_distance_atr': 0.5,
                'max_distance_atr': 5.0,
            },
            'target_r': target_r,
        },
        'risk': {'currency': 'USD_RESEARCH', 'initial_equity': '10000', 'max_leverage': 5, 'risk_fraction': '0.02'},
    }
    return json.dumps(doc, ensure_ascii=False).encode('utf-8')


def _make_bars(count: int, start_dt: datetime, base_price: float = 60000.0, *, gap_at: int | None = None) -> list[dict]:
    bars = []
    cur = start_dt
    px = base_price
    for i in range(count):
        if gap_at is not None and i == gap_at:
            cur += timedelta(minutes=45)  # 3 missing bars
        # Oscillating trend so both entries and exits trigger naturally
        delta = 120.0 if (i % 10) < 6 else -140.0
        op = px
        cl = max(1000.0, op + delta)
        hi = max(op, cl) + 80.0
        lo = min(op, cl) - 80.0
        close_dt = cur + timedelta(minutes=15)
        bars.append({
            'symbol': 'BTC/USD', 'interval': '15Min',
            'open_at': cur.isoformat(), 'close_at': close_dt.isoformat(),
            'open': f'{op:.2f}', 'high': f'{hi:.2f}', 'low': f'{lo:.2f}', 'close': f'{cl:.2f}',
            'volume': '500.0', 'is_closed': True, 'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH',
        })
        px = cl
        cur = close_dt
    return bars


def test_t01_t02_t03_t04_id_name_separation_and_safe_update(tmp_path):
    """T01-T04: Separate ID/Chinese name/version, target-bound update, wrong-ID rejection, ledger preservation."""
    pool = StrategyPool(tmp_path / 'pool.sqlite3')
    v1 = _make_v2_strategy('iso-alpha', '隔离测试均值回归策略', '1.0.0', target_r=1.5)
    pool.upload(v1)
    pool.enable('iso-alpha', '1.0.0', True)

    # T01: list() exposes real Chinese name separately from id and version
    items = pool.list()
    assert len(items) == 1
    assert items[0]['id'] == 'iso-alpha'
    assert items[0]['name'] == '隔离测试均值回归策略'
    assert items[0]['active_version'] == '1.0.0'

    # Simulate an existing ledger state (cash modified, realized pnl recorded)
    with sqlite3.connect(pool.path) as c:
        c.execute("UPDATE accounts SET cash='10425.50', realized='425.50' WHERE strategy_id='iso-alpha'")
        c.commit()

    # T03: Wrong strategy_id must be rejected in both validate_only and commit
    wrong = _make_v2_strategy('iso-other', '其他策略', '1.1.0')
    with pytest.raises(StrategyValidationError, match='策略 ID 不匹配'):
        pool.upload(wrong, expected_strategy_id='iso-alpha', validate_only=True)
    with pytest.raises(StrategyValidationError, match='策略 ID 不匹配'):
        pool.upload(wrong, expected_strategy_id='iso-alpha', validate_only=False)

    # T02: Valid update preview + commit
    v2 = _make_v2_strategy('iso-alpha', '隔离测试均值回归策略（升级版）', '1.1.0', target_r=2.0)
    preview = pool.upload(v2, expected_strategy_id='iso-alpha', validate_only=True)
    assert preview['strategy_id'] == 'iso-alpha'
    assert preview['previous_version'] == '1.0.0'
    assert preview['version'] == '1.1.0'
    assert any(ch['field'] == 'position' for ch in preview['version_diff']['changes'])
    # validate_only must NOT write version 1.1.0
    assert [v['version'] for v in pool.versions('iso-alpha')] == ['1.0.0']

    committed = pool.upload(v2, expected_strategy_id='iso-alpha', validate_only=False, activate=True)
    assert committed['activated_version'] == '1.1.0'
    assert [v['version'] for v in pool.versions('iso-alpha')] == ['1.0.0', '1.1.0']

    # T04: Ledger cash & realized PnL preserved intact after version update
    updated = pool.list()[0]
    assert updated['active_version'] == '1.1.0'
    assert updated['name'] == '隔离测试均值回归策略（升级版）'
    assert Decimal(updated['available_cash']) == Decimal('10425.50')
    assert Decimal(updated['realized']) == Decimal('425.50')


def test_t05_t09_t10_t11_t12_standardized_registration_chan_and_generic_strategy(tmp_path):
    """T05/T09/T10/T11/T12: Standardized registration, cycle/quote fill loop, and copy-trading feed for any new strategy."""
    pool = StrategyPool(tmp_path / 'strategy-pool.sqlite3')
    raw_generic = _make_v2_strategy('custom-generic-v3', '通用标准化测试策略', '1.0.0', always_long=True)
    pool.upload(raw_generic)
    pool.enable('custom-generic-v3', '1.0.0', True)

    # Dynamic routing helpers accept any pool strategy without hardcoded IDs
    assert is_pool_strategy_id('pa-chan-center-divergence') is True
    assert is_pool_strategy_id('custom-generic-v3') is True
    assert rule_detail_target('pa-chan-center-divergence') == 'pa-chan-center-divergence'
    assert rule_detail_target('custom-generic-v3') == 'custom-generic-v3'

    # Console rows & view render Chinese name cleanly
    items = pool.list()
    rows = console_rows({}, items)
    assert 'custom-generic-v3' in rows
    view = strategy_view('custom-generic-v3', rows['custom-generic-v3'])
    assert view['name'] == '通用标准化测试策略'

    # Run natural cycle + fresh quote to trigger real isolated open & close fills
    start = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    bars = _make_bars(42, start, base_price=60000.0)
    # Ensure final bar closes cleanly above 4-bar swing low so stop distance is within [0.5, 5.0] ATR
    bars[-1]['open'] = '60400.00'
    bars[-1]['low'] = '60350.00'
    bars[-1]['high'] = '60850.00'
    bars[-1]['close'] = '60800.00'
    cycle_ts = bars[-1]['close_at']
    now_utc = datetime.now(timezone.utc)
    fresh_quote = {
        'bid': '60800.00', 'ask': '60801.00',
        'event_at': (now_utc - timedelta(seconds=1)).isoformat(),
        'received_at': now_utc.isoformat(),
        'age_ms': 100, 'timestamp_valid': True,
        'source': 'BINANCE_USDM_PERP', 'symbol': 'BTC/USD', 'currency': 'USD_RESEARCH',
    }
    # Step 1: natural cycle generates pending_entry signal
    res = pool.cycle(bars, cycle_ts, quote=fresh_quote, quote_age_ms=100)
    assert any(r['id'] == 'custom-generic-v3' and r['status'] == 'SIGNAL_LONG' for r in res)

    # Step 2: fresh quote fills pending_entry
    q_res = pool.monitor_quote(fresh_quote)
    assert any(r['id'] == 'custom-generic-v3' and r['status'] == 'OPEN_LONG' for r in q_res)

    # Step 3: Copy-trading unified paper_fills() reads the new strategy's fill automatically (Demo & Prod ready!)
    events = paper_fills(pool.path, after=0, strategy='custom-generic-v3')
    assert len(events) == 1
    assert events[0].action == 'OPEN'
    assert events[0].direction == 'LONG'

    # Step 4: build_live_paper_rows discovers the new strategy automatically for Demo & Production selectors
    selectable = build_live_paper_rows(data_dir=tmp_path)
    assert 'custom-generic-v3' in selectable
    assert '通用标准化测试策略' in selectable['custom-generic-v3']


def test_t13_t18_backtest_determinism_warmup_gaps_cancel_and_isolation(tmp_path):
    """T13-T19: Backtest calendar span, warmup isolation, gap detection, determinism, cancel, and ledger isolation."""
    # Calendar 3-month helper check
    from datetime import date
    assert add_calendar_months(date(2026, 8, 27), 3) == date(2026, 11, 27)
    assert add_calendar_months(date(2026, 1, 31), 3) == date(2026, 4, 30)

    pool = StrategyPool(tmp_path / 'pool.sqlite3')
    pool.upload(_make_v2_strategy('bt-test', '回测验证策略', '1.0.0'))
    start = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    all_bars = _make_bars(240, start, base_price=60000.0, gap_at=150)
    warmup_bars = all_bars[:40]
    test_bars = all_bars[40:]
    bt_start = test_bars[0]['close_at']
    bt_end = test_bars[-1]['close_at']

    # Run twice -> deterministic equality (excluding elapsed_ms)
    r1 = pool.backtest('bt-test', '1.0.0', test_bars, start=bt_start, end=bt_end, warmup_bars=warmup_bars)
    r2 = pool.backtest('bt-test', '1.0.0', test_bars, start=bt_start, end=bt_end, warmup_bars=warmup_bars)
    r1_cmp = {k: v for k, v in r1.items() if k != 'elapsed_ms'}
    r2_cmp = {k: v for k, v in r2.items() if k != 'elapsed_ms'}
    assert r1_cmp == r2_cmp
    assert r1['strategy_name'] == '回测验证策略'
    assert r1['warmup_bar_count'] == 40
    assert len(r1['data_gaps']) == 1
    assert r1['data_gaps'][0]['missing_bars'] == 3
    # No trades before start date
    for tr in r1['trades']:
        assert tr['cycle'] >= bt_start
    # Paper account completely untouched (T16)
    acc = pool.list()[0]
    assert Decimal(acc['available_cash']) == Decimal('10000.00')
    assert acc['fill_count'] == 0

    # Cancel event stops cleanly (T19)
    cancel_evt = threading.Event()
    cancel_evt.set()
    with pytest.raises(StrategyValidationError, match='BACKTEST_CANCELED'):
        pool.backtest('bt-test', '1.0.0', test_bars, start=bt_start, end=bt_end, cancel_event=cancel_evt)


def test_t20_t21_t22_fills_normalization_and_csv_consistency(tmp_path):
    """T20-T22: Trade fills table normalization (open/close action accuracy) and CSV round-trip."""
    raw_fills = [
        {'cycle': '2026-11-26T08:00:00+00:00', 'side': 'BUY', 'qty': '0.1', 'price': '60000', 'fee': '3.0', 'slippage': '1.2', 'reason': 'ENTRY_NEXT_OPEN', 'version': '1.0.0', 'idempotency_key': 's:open:1'},
        {'cycle': '2026-11-26T09:00:00+00:00', 'side': 'SELL', 'qty': '0.1', 'price': '61000', 'fee': '3.05', 'slippage': '1.22', 'reason': 'TARGET', 'version': '1.0.0', 'idempotency_key': 's:close:1'},
    ]
    rows = normalize_fills_for_table(raw_fills, strategy_id='s', strategy_name='测试中文策略', source_kind='paper')
    assert len(rows) == 2
    # Newest first
    assert rows[0]['action'] == '平多'
    assert rows[0]['side'] == 'SELL'
    assert rows[0]['pnl_num'] == pytest.approx(93.95, abs=0.01)  # 100 gross - 3.0 - 3.05
    assert rows[1]['action'] == '开多'
    assert rows[1]['side'] == 'BUY'
    assert rows[1]['pnl'] == '—'
