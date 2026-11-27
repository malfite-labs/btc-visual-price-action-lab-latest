"""Comprehensive test suite for Quant Strategy Research Center V3 (R01-R32)."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend.strategy_pool import StrategyPool
from scripts.research_v3_engine import (
    CALCULATION_VERSION,
    ResearchV3Engine,
    build_equity_series,
    compute_attribution,
    compute_metrics,
    compute_multi_compare,
    compute_version_breakdown,
    export_research_report,
    pair_ledger_fills_v3,
    pair_pool_fills_v3,
)

UTC = timezone.utc


class _FailingStubClient:
    """Simulates partial endpoint failure & timeout (R25)."""
    def __init__(self, fail_paths=None):
        self.fail_paths = set(fail_paths or ())

    def get(self, path: str):
        if path in self.fail_paths:
            raise TimeoutError(f'TIMEOUT:{path}')
        if path == '/api/v1/agents':
            return []
        if path == '/api/v1/statistics/abc':
            return {'agents': {}}
        if path == '/api/v1/fills':
            return {'items': []}
        if path == '/api/v1/strategies':
            return {'items': []}
        if path == '/api/v1/strategy-pool':
            return {'items': []}
        return {}


def _make_sample_trades():
    base = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)  # 10:00 Beijing
    return [
        {
            'trade_id': 't1', 'strategy_id': 'S1', 'account_id': 'pool:S1',
            'opening_strategy_version': '1.0.0', 'execution_strategy_version': '1.0.0',
            'entry_time': base.isoformat(), 'exit_time': (base + timedelta(hours=2)).isoformat(),
            'side': 'LONG', 'entry_price': '60000', 'exit_price': '61000', 'quantity': '0.1',
            'gross_pnl': '100', 'open_fee': '3', 'close_fee': '3.05', 'fees': '6.05', 'slippage': '2.4',
            'net_pnl': '93.95', 'exit_reason': 'TARGET', 'exit_reason_category': '止盈',
            'holding_duration_sec': 7200.0, 'close_evidence_status': 'VERIFIED_LIFECYCLE',
        },
        {
            'trade_id': 't2', 'strategy_id': 'S1', 'account_id': 'pool:S1',
            'opening_strategy_version': '1.0.0', 'execution_strategy_version': '1.0.0',
            'entry_time': (base + timedelta(days=5)).isoformat(), 'exit_time': (base + timedelta(days=5, hours=6)).isoformat(),
            'side': 'SHORT', 'entry_price': '61000', 'exit_price': '61500', 'quantity': '0.1',
            'gross_pnl': '-50', 'open_fee': '3.05', 'close_fee': '3.075', 'fees': '6.125', 'slippage': '2.4',
            'net_pnl': '-56.125', 'exit_reason': 'STOP', 'exit_reason_category': '止损',
            'holding_duration_sec': 21600.0, 'close_evidence_status': 'VERIFIED_LIFECYCLE',
        },
        {
            'trade_id': 't3', 'strategy_id': 'S1', 'account_id': 'pool:S1',
            'opening_strategy_version': '1.1.0', 'execution_strategy_version': '1.1.0',
            'entry_time': (base + timedelta(days=10)).isoformat(), 'exit_time': (base + timedelta(days=10, minutes=30)).isoformat(),
            'side': 'LONG', 'entry_price': '62000', 'exit_price': '63000', 'quantity': '0.1',
            'gross_pnl': '100', 'open_fee': '3.1', 'close_fee': '3.15', 'fees': '6.25', 'slippage': '2.5',
            'net_pnl': '93.75', 'exit_reason': 'TARGET', 'exit_reason_category': '止盈',
            'holding_duration_sec': 1800.0, 'close_evidence_status': 'VERIFIED_LIFECYCLE',
        },
    ]


def test_r10_r14_metrics_interval_return_pf_dd_and_no_double_fee():
    """R10-R14: Exact interval start equity return %, win rate, PF, zero-loss PF, max drawdown, no double fee."""
    trades = _make_sample_trades()
    init_cap = Decimal('10000')
    eq_pts, dd_sum = build_equity_series(
        trades, initial_capital=init_cap, current_equity=Decimal('10131.575'),
        current_realized=Decimal('131.575'), current_unrealized=Decimal('0'),
        running_since='2026-10-31T00:00:00+00:00', snapshot_time='2026-11-15T00:00:00+00:00',
    )
    m_full = compute_metrics(trades, initial_capital=init_cap, current_equity=Decimal('10131.575'), all_closed_trades=trades, equity_points=eq_pts)
    assert m_full['closed_count'] == 3
    assert m_full['win_count'] == 2 and m_full['loss_count'] == 1
    assert m_full['win_rate'] == pytest.approx(66.6667, abs=0.01)
    # PF = (93.95 + 93.75) / 56.125 = 3.3443
    assert m_full['profit_factor'] == pytest.approx(3.3443, abs=0.01)
    assert Decimal(m_full['fees']) == Decimal('18.425')
    # Max DD after t1 peak 10093.95 -> t2 10037.825 = 56.125 / 10093.95 = 0.556%
    assert m_full['max_drawdown_pct'] == pytest.approx(0.556, abs=0.01)
    assert dd_sum['in_drawdown'] is False
    assert dd_sum['dd_recovery'] is not None

    # R10: Sub-interval starting after t1 uses interval_start_equity = 10093.95 (NOT 10000!)
    sub_start = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    m_sub = compute_metrics(trades, initial_capital=init_cap, current_equity=Decimal('10131.575'), start_dt=sub_start, all_closed_trades=trades, equity_points=eq_pts)
    assert Decimal(m_sub['interval_start_equity']) == Decimal('10093.95')
    expected_ret = float((Decimal('10131.575') - Decimal('10093.95')) / Decimal('10093.95') * 100)
    assert m_sub['net_return_pct'] == pytest.approx(expected_ret, abs=0.001)

    # Zero-loss profit factor never returns fake 999
    m_win_only = compute_metrics([trades[0]], initial_capital=init_cap, current_equity=Decimal('10093.95'))
    assert m_win_only['profit_factor'] is None
    assert '∞' in m_win_only['profit_factor_label']


def test_r16_r22_attribution_versions_and_non_comparable_compare():
    """R16-R22: Attribution, cross-version attribution, multi-strategy compare & capital model warning."""
    trades = _make_sample_trades()
    attr = compute_attribution(trades)
    assert attr['direction']['LONG']['count'] == 2
    assert attr['direction']['SHORT']['count'] == 1
    assert attr['exit_reason']['止盈']['count'] == 2
    assert attr['exit_reason']['止损']['count'] == 1
    assert '1小时以内' in attr['holding_duration']
    assert '1至4小时' in attr['holding_duration']
    assert '4至12小时' in attr['holding_duration']

    # R22: Cross-version breakdown keeps v1.0.0 and v1.1.0 separate
    vers = compute_version_breakdown(trades)
    vmap = {v['version']: v for v in vers}
    assert vmap['1.0.0']['closed_count'] == 2
    assert vmap['1.1.0']['closed_count'] == 1

    # R17: Multi-strategy compare flags different capital models
    r1 = {
        'base': {'strategy_id': 'S1', 'strategy_name': '策略一', 'strategy_version': '1.0.0', 'running_since': '2026-11-01T00:00:00+00:00'},
        'account': {'capital_model': 'PAPER_10000_RISK_2PCT_5X', 'initial_capital': '10000', 'current_equity': '10131.575'},
        'closed_trades': trades,
        'equity_series': build_equity_series(trades, initial_capital=Decimal('10000'), current_equity=Decimal('10131.575'), current_realized=Decimal('131.575'), current_unrealized=Decimal('0'), running_since='2026-11-01T00:00:00+00:00', snapshot_time='2026-11-15T00:00:00+00:00')[0],
    }
    r2 = {
        'base': {'strategy_id': 'G', 'strategy_name': '镜像策略', 'strategy_version': 'MIRROR-V1', 'running_since': '2026-11-05T00:00:00+00:00'},
        'account': {'capital_model': 'MIRROR_C_EVENT_10000', 'initial_capital': '10000', 'current_equity': '9900'},
        'closed_trades': [],
        'equity_series': [],
    }
    cmp_res = compute_multi_compare([r1, r2], common_window=True)
    assert cmp_res['comparable'] is False
    assert '不同资金模型' in (cmp_res['warning'] or '')
    assert cmp_res['common_start'] == '2026-11-05T00:00:00+00:00'


def test_r06_r07_r24_r25_r26_r29_dynamic_discovery_fault_isolation_export_and_100_strategy_scale(tmp_path):
    """R06/R07/R24/R25/R26/R29: New strategy auto-discovery, partial failure isolation, CSV/MD export, and 50/100 strategy scale benchmark."""
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    pool = StrategyPool(data_dir / 'strategy-pool.sqlite3')

    # Populate 100 distinct strategies in isolated strategy-pool.sqlite3 (R29)
    now_iso = datetime.now(UTC).isoformat()
    with sqlite3.connect(pool.path) as c:
        for i in range(100):
            sid = f'scale-strat-{i:03d}'
            doc = json.dumps({'format': 'btc-lab-declarative-strategy/v2', 'strategy_id': sid, 'name': f'规模测试策略 {i:03d}', 'version': '1.0.0', 'risk': {'initial_equity': '10000'}}, ensure_ascii=False)
            c.execute('INSERT INTO strategies(id,active_version,enabled) VALUES(?,?,1)', (sid, '1.0.0'))
            c.execute('INSERT INTO versions(strategy_id,version,sha256,document,uploaded_at) VALUES(?,?,?,?,?)', (sid, '1.0.0', f'hash_{i}', doc, now_iso))
            c.execute("INSERT INTO accounts(strategy_id,cash,realized,last_status) VALUES(?,'10000.00','0','OK')", (sid,))
            # Give first 50 strategies 20 closed round-trips (40 fills each = 2,000 fills total)
            if i < 50:
                base = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
                for j in range(20):
                    t_open = (base + timedelta(hours=j * 4)).isoformat()
                    t_close = (base + timedelta(hours=j * 4 + 2)).isoformat()
                    c.execute(
                        'INSERT INTO fills(strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (sid, '1.0.0', t_open, 'BUY', '0.1', '60000', '3.0', '1.2', 'ENTRY_NEXT_OPEN', f'{sid}:open:{j}', t_open),
                    )
                    c.execute(
                        'INSERT INTO fills(strategy_id,version,cycle,side,qty,price,fee,slippage,reason,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (sid, '1.0.0', t_close, 'SELL', '0.1', '60200' if j % 2 == 0 else '59900', '3.0', '1.2', 'TARGET' if j % 2 == 0 else 'STOP', f'{sid}:close:{j}', t_close),
                    )
        c.commit()

    # R25: Simulate partial HTTP timeout on /api/v1/g-mirror/status while loading all 100 strategies
    eng = ResearchV3Engine(root=tmp_path, client=_FailingStubClient(fail_paths={'/api/v1/g-mirror/status'}))
    t0 = time.perf_counter()
    snap = eng.load_snapshot(force=True)
    ms_100 = (time.perf_counter() - t0) * 1000

    pool_recs = [r for r in snap['strategies'] if r['base']['strategy_id'].startswith('scale-strat-')]
    assert len(pool_recs) == 100
    assert any('/api/v1/g-mirror/status' in e for e in snap['errors'])
    # R07: Zero-trade strategies appear cleanly with '暂无成交'
    zero_rec = next(r for r in pool_recs if r['base']['strategy_id'] == 'scale-strat-099')
    assert zero_rec['data_quality']['completeness'] == '暂无成交'
    assert zero_rec['metrics']['closed_count'] == 0
    # R29: 100 strategies + 2,000 fills must load well under 3 seconds
    assert ms_100 < 3000, f'100-strategy load took {ms_100:.1f}ms'
    print(f'100-STRATEGY SCALE BENCHMARK: {ms_100:.1f} ms (2,000 fills)')

    # R24: Export CSV + MD report cleanly
    out_dir = tmp_path / 'exports'
    paths = export_research_report(output_dir=out_dir, records=pool_recs[:5], mode='scale_test', snapshot_time=snap['snapshot_time'])
    assert len(paths) == 2
    assert paths[0].suffix == '.csv' and paths[0].stat().st_size > 100
    assert paths[1].suffix == '.md' and CALCULATION_VERSION in paths[1].read_text(encoding='utf-8')
