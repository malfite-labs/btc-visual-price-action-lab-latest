"""Isolated tests for BTC V3.1 strategy template bundle, upload validation, short-exit fix, and dynamic registration."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import zipfile

import pytest

from backend.strategy_pool import StrategyPool, StrategyValidationError, validate_strategy
from scripts.live_copy_service import DryFollower
from scripts.live_copy_v2 import paper_fills
from scripts.local_dashboard import build_live_paper_rows, console_rows
from scripts.strategy_template_bundle import (
    TEMPLATE_ZIP_NAME,
    build_strategy_template_files,
    export_strategy_template_zip,
    strategy_schema_dict,
    strategy_template_dict,
)


def _valid_custom_v2_strategy(strategy_id='custom-rsi-breakout', version='1.0.0', direction='both'):
    return {
        'format': 'btc-lab-declarative-strategy/v2',
        'strategy_id': strategy_id,
        'version': version,
        'name': '自定义RSI突破测试',
        'market': 'BTC/USD',
        'timeframe': '15m',
        'description': '隔离测试用声明式v2策略，使用自定义参数。',
        'parameters': {'rsi_high': 55, 'rsi_low': 45, 'min_body': 0.2},
        'risk': {
            'initial_equity': '10000',
            'currency': 'USD_RESEARCH',
            'max_leverage': 5,
            'risk_fraction': '0.02',
        },
        'entry': {
            'long': {
                'op': 'and',
                'args': [
                    {'op': 'gt', 'args': [{'op': 'rsi', 'args': [14]}, {'op': 'param', 'args': ['rsi_high']}]},
                    {'op': 'gte', 'args': [{'op': 'body_fraction', 'args': [0]}, {'op': 'param', 'args': ['min_body']}]},
                ],
            },
            'short': {
                'op': 'and',
                'args': [
                    {'op': 'lt', 'args': [{'op': 'rsi', 'args': [14]}, {'op': 'param', 'args': ['rsi_low']}]},
                    {'op': 'gte', 'args': [{'op': 'body_fraction', 'args': [0]}, {'op': 'param', 'args': ['min_body']}]},
                ],
            },
        },
        'position': {
            'direction': direction,
            'entry_timing': 'next_open',
            'stop': {
                'kind': 'swing_atr',
                'lookback': 5,
                'atr_period': 14,
                'buffer_atr': 0.2,
                'min_distance_atr': 0.3,
                'max_distance_atr': 4.0,
            },
            'target_r': 2.0,
            'max_hold_bars': 24,
            'same_bar_priority': 'stop',
        },
    }


def _make_bars(count=40, *, start_price=90000, step=100):
    t0 = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
    bars = []
    px = start_price
    for i in range(count):
        open_at = t0 + timedelta(minutes=15 * i)
        close_at = open_at + timedelta(minutes=15)
        o = px
        c = px + step
        h = max(o, c) + 50
        l = min(o, c) - 50
        bars.append({
            'symbol': 'BTC/USD', 'interval': '15m', 'source': 'BINANCE_USDM_PERP',
            'currency': 'USD_RESEARCH', 'is_closed': True,
            'open_at': open_at.isoformat(), 'close_at': close_at.isoformat(),
            'open': o, 'high': h, 'low': l, 'close': c, 'volume': 10.0,
        })
        px = c
    return bars


def _fresh_quote(bid, ask, at):
    return {
        'bid': str(bid), 'ask': str(ask), 'source': 'BINANCE_USDM_PERP',
        'symbol': 'BTC/USD', 'currency': 'USD_RESEARCH',
        'event_at': at.isoformat(), 'received_at': at.isoformat(),
        'timestamp_valid': True, 'age_ms': 100,
    }


def test_template_zip_export_contains_all_four_files(tmp_path):
    dest = export_strategy_template_zip(tmp_path / TEMPLATE_ZIP_NAME)
    assert dest.is_file()
    with zipfile.ZipFile(dest, 'r') as zf:
        names = set(zf.namelist())
        assert names == {
            'strategy.template.json',
            'strategy.schema.json',
            '策略开发与上传规范.md',
            'AI策略开发指令.md',
        }
        tpl = json.loads(zf.read('strategy.template.json').decode('utf-8'))
        schema = json.loads(zf.read('strategy.schema.json').decode('utf-8'))
        assert tpl['format'] == 'btc-lab-declarative-strategy/v2'
        assert schema['properties']['format']['const'] == 'btc-lab-declarative-strategy/v2'


def test_blank_template_is_rejected_and_cannot_be_uploaded(tmp_path):
    pool = StrategyPool(tmp_path / 'isolated-pool.sqlite3')
    blank = strategy_template_dict()
    with pytest.raises(StrategyValidationError):
        pool.upload(json.dumps(blank))


def test_validation_rejects_undefined_param_and_unsupported_v2_op():
    # Undefined parameter reference
    doc_missing_param = _valid_custom_v2_strategy()
    doc_missing_param['entry']['long'] = {'op': 'gt', 'args': [{'op': 'close', 'args': [0]}, {'op': 'param', 'args': ['not_defined']}]}
    with pytest.raises(StrategyValidationError, match='参数引用未定义'):
        validate_strategy(doc_missing_param)

    # Unsupported v2 operator crosses_above
    doc_bad_op = _valid_custom_v2_strategy()
    doc_bad_op['entry']['long'] = {'op': 'crosses_above', 'args': [{'op': 'close', 'args': [0]}, {'op': 'sma', 'args': [20]}]}
    with pytest.raises(StrategyValidationError, match='不支持的运算符'):
        validate_strategy(doc_bad_op)

    # Negative bar offset (future lookahead)
    doc_future = _valid_custom_v2_strategy()
    doc_future['entry']['long'] = {'op': 'gt', 'args': [{'op': 'close', 'args': [-1]}, 1]}
    with pytest.raises(StrategyValidationError, match='offset'):
        validate_strategy(doc_future)


def test_isolated_upload_version_update_short_cycle_exit_and_unified_fill_adapter(tmp_path):
    pool_db = tmp_path / 'strategy-pool.sqlite3'
    pool = StrategyPool(pool_db)

    # 1. Upload v1.0.0 (registered as disabled by default)
    doc_v1 = _valid_custom_v2_strategy(strategy_id='custom-short-test', version='1.0.0', direction='short')
    res = pool.upload(json.dumps(doc_v1))
    assert res['enabled'] is False
    assert res['strategy_id'] == 'custom-short-test'

    # Duplicate version rejected
    with pytest.raises(StrategyValidationError, match='版本已存在'):
        pool.upload(json.dumps(doc_v1))

    # Update to v1.1.0 preserves account & versions
    doc_v2 = _valid_custom_v2_strategy(strategy_id='custom-short-test', version='1.1.0', direction='short')
    doc_v2['parameters']['rsi_low'] = 48
    pool.upload(json.dumps(doc_v2))
    assert len(pool.versions('custom-short-test')) == 2
    diff = pool.diff('custom-short-test', '1.0.0', '1.1.0')
    assert any(ch['field'] == 'parameters' for ch in diff['changes'])

    # Enable v1.0.0 & verify dynamic discovery in console_rows & build_live_paper_rows
    pool.enable('custom-short-test', '1.0.0', enabled=True)
    live_rows = build_live_paper_rows(data_dir=tmp_path)
    assert 'custom-short-test' in live_rows
    assert '自定义RSI突破测试' in live_rows['custom-short-test']
    c_rows = console_rows({}, pool.list())
    assert 'custom-short-test' in c_rows

    # 2. Simulate SHORT signal -> next_open fill -> cycle STOP exit (verifying BUY close side & unified paper_fills!)
    follower = DryFollower(pool_db, tmp_path / 'copy-ledger.sqlite3', Decimal('20'), Decimal('100'), strategy='custom-short-test')
    assert follower.cursor() == 0

    bars = _make_bars(30, start_price=95000, step=-150)  # downtrend -> RSI < 45 -> SHORT signal
    sig_cycle = datetime.fromisoformat(bars[-1]['close_at'])
    q_now = datetime.now(timezone.utc) - timedelta(seconds=1)
    # Use recent timestamps so _valid_quote passes received<=now
    t_base = q_now.replace(minute=(q_now.minute // 15) * 15, second=0, microsecond=0) - timedelta(minutes=15 * 30)
    for i, b in enumerate(bars):
        oa = t_base + timedelta(minutes=15 * i)
        ca = oa + timedelta(minutes=15)
        b['open_at'], b['close_at'] = oa.isoformat(), ca.isoformat()
    sig_cycle = datetime.fromisoformat(bars[-1]['close_at'])
    out1 = pool.cycle(bars, sig_cycle, quote=_fresh_quote(90500, 90510, sig_cycle))
    assert out1[0]['status'] == 'SIGNAL_SHORT'

    # Fill next_open via monitor_quote
    fill_t = sig_cycle + timedelta(seconds=10)
    m1 = pool.monitor_quote(_fresh_quote(90500, 90510, fill_t))
    assert m1[0]['status'] == 'OPEN_SHORT'

    # Next bar spikes above stop -> cycle() closes SHORT with side='BUY'
    next_open = sig_cycle
    next_close = sig_cycle + timedelta(minutes=15)
    spike_bar = {
        'symbol': 'BTC/USD', 'interval': '15m', 'source': 'BINANCE_USDM_PERP',
        'currency': 'USD_RESEARCH', 'is_closed': True,
        'open_at': next_open.isoformat(), 'close_at': next_close.isoformat(),
        'open': 90500, 'high': 99000, 'low': 90400, 'close': 98500, 'volume': 15.0,
    }
    bars2 = bars + [spike_bar]
    out2 = pool.cycle(bars2, next_close, quote=_fresh_quote(98500, 98510, min(next_close, datetime.now(timezone.utc) - timedelta(seconds=1))))
    assert out2[0]['status'] == 'CLOSE_STOP'

    # Verify unified paper_fills & DryFollower consume OPEN SHORT + FULL_CLOSE SHORT cleanly!
    fills = paper_fills(pool_db, after=0, strategy='custom-short-test')
    assert [(f.action, f.direction) for f in fills] == [('OPEN', 'SHORT'), ('FULL_CLOSE', 'SHORT')]
    processed = follower.poll()
    assert [r['action'] for _, r in processed] == ['OPEN', 'FULL_CLOSE']
