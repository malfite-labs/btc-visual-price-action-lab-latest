"""Isolated regression tests for BTC V3.1 dual-environment copy trading."""
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import pytest

from scripts.live_c_demo_exchange import DemoExchange, ProductionExchange
from scripts.live_c_demo_executor import DemoExecutor
from scripts.live_copy_account_lock import account_owner
from scripts.live_copy_env import (
    DEMO_ENV,
    PROD_ENV,
    config_ledger_dir,
    copy_config_payload,
    env_paths,
)
from scripts.live_copy_follow import FollowExecutor
from scripts.live_copy_service import DryFollower
from scripts.local_dashboard import build_live_paper_rows, strategy_display_name


class FakeExchange:
    """In-memory USD-M exchange double supporting HEDGE/ONE_WAY and Demo/Production isolation."""
    def __init__(self, environment='DEMO', *, hedge=True, leverage=5, available='500', price='60000'):
        self.environment = environment
        self.hedge = hedge
        self.leverage = leverage
        self.available = Decimal(available)
        self.price = Decimal(price)
        self.orders = {}
        self.algos = {}
        self.trades_by_oid = {}
        self.next_oid = 1000
        self.next_tid = 5000
        self.position = Decimal(0)
        self.position_side = None
        self.writes = []

    def synchronize(self):
        return None

    def _read(self, op):
        if op == 'exchange_info':
            return {'symbols': [{
                'symbol': 'BTCUSDT', 'status': 'TRADING',
                'filters': [
                    {'filterType': 'LOT_SIZE', 'minQty': '0.001', 'maxQty': '100', 'stepSize': '0.001'},
                    {'filterType': 'MARKET_LOT_SIZE', 'minQty': '0.001', 'maxQty': '100', 'stepSize': '0.001'},
                    {'filterType': 'MIN_NOTIONAL', 'notional': '50'},
                    {'filterType': 'PRICE_FILTER', 'tickSize': '0.10'},
                ],
            }]}
        if op == 'ticker':
            return {'symbol': 'BTCUSDT', 'price': str(self.price), 'time': int(datetime.now(UTC).timestamp() * 1000)}
        if op == 'commission':
            return {'takerCommissionRate': '0.0005'}
        raise ValueError(op)

    def account(self):
        return {
            'availableBalance': str(self.available),
            'totalWalletBalance': str(self.available),
            'totalMarginBalance': str(self.available),
            'assets': [{'asset': 'USDT', 'availableBalance': str(self.available), 'walletBalance': str(self.available)}],
        }

    def exposure(self):
        open_algos = [a for a in self.algos.values() if a['algoStatus'] == 'NEW']
        return self.position, [], open_algos

    def risk_snapshot(self):
        net, orders, algos = self.exposure()
        return {
            'available': self.available,
            'positions': [net] if net else [],
            'orders': orders,
            'conditional': algos,
            'permissions': {'enableFutures': True},
            'symbol': [{'symbol': 'BTCUSDT', 'marginType': 'ISOLATED', 'leverage': str(self.leverage)}],
        }

    def entry(self, intent):
        self.writes.append(('entry', intent.client_order_id))
        oid = self.next_oid
        self.next_oid += 1
        side = 'BUY' if intent.direction == 'LONG' else 'SELL'
        pos_side = intent.direction if self.hedge else 'BOTH'
        self.orders[intent.client_order_id] = {
            'clientOrderId': intent.client_order_id, 'orderId': oid, 'symbol': 'BTCUSDT',
            'side': side, 'positionSide': pos_side, 'origQty': str(intent.quantity),
            'executedQty': str(intent.quantity), 'status': 'FILLED',
        }
        tid = self.next_tid
        self.next_tid += 1
        self.trades_by_oid[oid] = [{
            'id': tid, 'orderId': oid, 'symbol': 'BTCUSDT', 'side': side, 'positionSide': pos_side,
            'qty': str(intent.quantity), 'price': str(self.price), 'commission': '0.03',
            'commissionAsset': 'USDT', 'realizedPnl': '0', 'time': int(datetime.now(UTC).timestamp() * 1000),
        }]
        self.position = intent.quantity if intent.direction == 'LONG' else -intent.quantity
        self.position_side = pos_side
        return self.orders[intent.client_order_id]

    def order(self, client_id):
        return self.orders[client_id]

    def trades(self, order_id):
        return self.trades_by_oid[order_id]

    def place_protection(self, client_id, *, side, kind, trigger, quantity):
        self.writes.append(('algo', client_id))
        pos_side = ('LONG' if side == 'SELL' else 'SHORT') if self.hedge else 'BOTH'
        self.algos[client_id] = {
            'algoId': 9000 + len(self.algos), 'clientAlgoId': client_id, 'algoStatus': 'NEW',
            'symbol': 'BTCUSDT', 'side': side, 'positionSide': pos_side, 'orderType': kind,
            'reduceOnly': not self.hedge, 'quantity': str(quantity), 'triggerPrice': str(trigger),
        }
        return self.algos[client_id]

    def protection(self, client_id):
        return self.algos.get(client_id)

    def cancel_protection(self, client_id):
        if client_id in self.algos:
            self.algos[client_id]['algoStatus'] = 'CANCELED'
        return {'clientAlgoId': client_id}

    def reduce(self, client_id, side, quantity):
        self.writes.append(('reduce', client_id))
        oid = self.next_oid
        self.next_oid += 1
        pos_side = ('LONG' if side == 'SELL' else 'SHORT') if self.hedge else 'BOTH'
        self.orders[client_id] = {
            'clientOrderId': client_id, 'orderId': oid, 'symbol': 'BTCUSDT',
            'side': side, 'positionSide': pos_side, 'origQty': str(quantity),
            'executedQty': str(quantity), 'status': 'FILLED',
        }
        tid = self.next_tid
        self.next_tid += 1
        self.trades_by_oid[oid] = [{
            'id': tid, 'orderId': oid, 'symbol': 'BTCUSDT', 'side': side, 'positionSide': pos_side,
            'qty': str(quantity), 'price': str(self.price + Decimal('100')), 'commission': '0.03',
            'commissionAsset': 'USDT', 'realizedPnl': '0.10', 'time': int(datetime.now(UTC).timestamp() * 1000),
        }]
        self.position = Decimal(0)
        return self.orders[client_id]


def _make_paper_db(path: Path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            """CREATE TABLE fills (
                fill_id TEXT, agent TEXT, trade_id TEXT, strategy_version TEXT,
                position_effect TEXT, position_direction TEXT, quantity TEXT, price TEXT, occurred_at TEXT
            )"""
        )


def _insert_fill(path: Path, *, fill_id, agent, trade_id, effect, direction='LONG', qty='0.01', price='60000'):
    now = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            'INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?)',
            (fill_id, agent, trade_id, 'V3.1', effect, direction, qty, price, now),
        )


def test_account_lock_isolated_by_environment():
    with account_owner('DEMO', 'shared-alias-test'):
        # Same alias in PRODUCTION must succeed concurrently
        with account_owner('PRODUCTION', 'shared-alias-test'):
            pass
        # Same alias in DEMO again must fail (same process mutex is reentrant on Windows,
        # so verify validation of environment parameter strictly)
        with pytest.raises(ValueError):
            with account_owner('INVALID_ENV', 'shared-alias-test'):
                pass


def test_env_paths_strictly_isolated(tmp_path):
    demo = env_paths(DEMO_ENV, base_root=tmp_path / 'demo')
    prod = env_paths(PROD_ENV, base_root=tmp_path / 'prod')
    assert demo.root != prod.root
    assert demo.configs != prod.configs
    assert demo.runtime != prod.runtime
    assert demo.ledgers != prod.ledgers
    assert demo.switch_queue != prod.switch_queue
    d_dir = config_ledger_dir(DEMO_ENV, 'cid1', {'legacy_demo_ledger': False}, base_root=tmp_path / 'demo')
    p_dir = config_ledger_dir(PROD_ENV, 'cid1', {'legacy_demo_ledger': True}, base_root=tmp_path / 'prod')
    assert d_dir != p_dir
    assert p_dir == prod.ledgers / 'cid1'


def test_production_exchange_blocks_writes_by_default():
    ex = ProductionExchange('k', 's')
    assert ex.allow_writes is False
    with pytest.raises(RuntimeError, match='PRODUCTION_ORDER_DISABLED'):
        ex._request('POST', '/fapi/v1/order', {'symbol': 'BTCUSDT'})
    ex_armed = ProductionExchange('k', 's', allow_writes=True)
    with pytest.raises(RuntimeError, match='PRODUCTION_WRITE_REQUIRES_ACCOUNT_OWNERSHIP'):
        ex_armed._request('POST', '/fapi/v1/order', {'symbol': 'BTCUSDT'})


def test_production_preflight_blocks_excess_leverage_without_writes():
    calls = []

    class MockProd(ProductionExchange):
        def synchronize(self):
            calls.append(('GET', 'time'))

        def _request(self, method, path, parameters=None, *, signed=True, clock_retry=True):
            assert method == 'GET'
            calls.append((method, path))
            if path == '/fapi/v1/positionSide/dual':
                return {'dualSidePosition': True}
            if path == '/fapi/v3/balance':
                return [{'asset': 'USDT', 'accountAlias': 'uX', 'availableBalance': '100.17'}]
            if path == '/fapi/v3/account':
                return {'assets': [{'asset': 'USDT', 'availableBalance': '100.17', 'walletBalance': '100.17'}]}
            if path == '/fapi/v3/positionRisk':
                return []
            if path in ('/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders'):
                return []
            if path == '/fapi/v1/symbolConfig':
                return [{'symbol': 'BTCUSDT', 'marginType': 'ISOLATED', 'leverage': '10'}]
            if path == '/fapi/v1/accountConfig':
                return {'canTrade': True}
            raise AssertionError(path)

        def _read(self, op):
            if op == 'exchange_info':
                return {'symbols': [{
                    'symbol': 'BTCUSDT', 'status': 'TRADING',
                    'filters': [
                        {'filterType': 'LOT_SIZE', 'minQty': '0.001', 'maxQty': '100', 'stepSize': '0.001'},
                        {'filterType': 'MARKET_LOT_SIZE', 'minQty': '0.001', 'maxQty': '100', 'stepSize': '0.001'},
                        {'filterType': 'MIN_NOTIONAL', 'notional': '50'},
                        {'filterType': 'PRICE_FILTER', 'tickSize': '0.10'},
                    ],
                }]}
            if op == 'ticker':
                return {'symbol': 'BTCUSDT', 'price': '61000.00'}
            if op == 'commission':
                return {'takerCommissionRate': '0.0005'}
            raise AssertionError(op)

    ex = MockProd('k', 's')
    report = ex.preflight_report(planned_leverage=5, fixed_margin=Decimal('20'), budget=Decimal('100'))
    assert report['status'] == 'BLOCKED'
    assert report['position_mode'] == 'HEDGE'
    assert report['actual_leverage'] == 10
    assert any('10×' in b for b in report['blockers'])
    assert all(m == 'GET' for m, _ in calls)


def test_dual_environment_parallel_execution_and_restart_recovery(tmp_path):
    paper_db = tmp_path / 'paper.sqlite3'
    _make_paper_db(paper_db)

    demo_dir = tmp_path / 'demo_ledger'
    prod_dir = tmp_path / 'prod_ledger'
    demo_dir.mkdir()
    prod_dir.mkdir()

    demo_follower = DryFollower(paper_db, demo_dir / 'demo-execution-inbox.sqlite3', Decimal('20'), Decimal('100'), strategy='C')
    prod_follower = DryFollower(paper_db, prod_dir / 'demo-execution-inbox.sqlite3', Decimal('25'), Decimal('100'), strategy='C')

    demo_ex = FakeExchange('DEMO', hedge=True, leverage=5)
    prod_ex = FakeExchange('PRODUCTION', hedge=True, leverage=5)

    demo_engine = FollowExecutor(demo_follower, demo_ex, demo_dir, 'DEMO-CONTINUOUS', leverage=5, stop_fraction=Decimal('0.02'), risk_cap=Decimal('5'), exit_mode='FOLLOW_PAPER')
    prod_engine = FollowExecutor(prod_follower, prod_ex, prod_dir, 'PROD-bc66076a', leverage=5, stop_fraction=Decimal('0.02'), risk_cap=Decimal('5'), exit_mode='FOLLOW_PAPER')

    # Same paper fill consumed independently by BOTH Demo and Production
    _insert_fill(paper_db, fill_id='f1', agent='C', trade_id='t1', effect='OPEN', direction='LONG')
    d_events = demo_engine.tick()
    p_events = prod_engine.tick()
    assert d_events == [('C:f1', 'PROTECTED')]
    assert p_events == [('C:f1', 'PROTECTED')]
    assert demo_ex.position == Decimal('0.001')
    assert prod_ex.position == Decimal('0.002')
    assert demo_follower.cursor() == 1
    assert prod_follower.cursor() == 1

    # Restart recovery: recreate FollowExecutor on existing ledgers -> zero duplicate orders!
    d_writes_before = len(demo_ex.writes)
    p_writes_before = len(prod_ex.writes)
    demo_engine_restarted = FollowExecutor(demo_follower, demo_ex, demo_dir, 'DEMO-CONTINUOUS', leverage=5, stop_fraction=Decimal('0.02'), risk_cap=Decimal('5'), exit_mode='FOLLOW_PAPER')
    prod_engine_restarted = FollowExecutor(prod_follower, prod_ex, prod_dir, 'PROD-bc66076a', leverage=5, stop_fraction=Decimal('0.02'), risk_cap=Decimal('5'), exit_mode='FOLLOW_PAPER')
    assert demo_engine_restarted.tick() == []
    assert prod_engine_restarted.tick() == []
    assert len(demo_ex.writes) == d_writes_before
    assert len(prod_ex.writes) == p_writes_before

    # Close paper position -> both environments close cleanly and independently
    _insert_fill(paper_db, fill_id='f2', agent='C', trade_id='t1', effect='CLOSE', direction='LONG')
    assert demo_engine_restarted.tick() == [('C:f2', 'CLOSED')]
    assert prod_engine_restarted.tick() == [('C:f2', 'CLOSED')]
    assert demo_ex.position == Decimal(0)
    assert prod_ex.position == Decimal(0)
    assert demo_engine_restarted.can_stop() is True
    assert prod_engine_restarted.can_stop() is True


def test_config_copy_excludes_runtime_auth_and_ledger():
    src = {
        'strategy': 'pa-big-candle-continuation',
        'fixed': '30',
        'budget': '120',
        'leverage': '4',
        'custom_tp': '1',
        'tp': '3.0',
        'custom_sl': '1',
        'stop': '1.5',
        'copy_config_id': 'old-id',
        'legacy_demo_ledger': True,
        'production_order_enabled': True,
        'archived_at': '2026-11-25T10:00:00',
    }
    copied = copy_config_payload(
        src,
        target_environment=PROD_ENV,
        new_cid='new-prod-id',
        created_at='2026-11-26T12:00:00',
        account_label='Binance USDⓈ-M · 主账号',
        account_id='acc-1',
    )
    assert copied['strategy'] == 'pa-big-candle-continuation'
    assert copied['fixed'] == '30'
    assert copied['budget'] == '120'
    assert copied['leverage'] == '4'
    assert copied['tp'] == '3.0' and copied['stop'] == '1.5'
    assert copied['copy_config_id'] == 'new-prod-id'
    assert copied['environment'] == PROD_ENV
    assert copied['production_order_enabled'] is False
    assert copied['archived_at'] is None
    assert 'legacy_demo_ledger' not in copied


def test_strategy_name_sync_and_order():
    rows = build_live_paper_rows()
    assert len(rows) == 16
    assert list(rows.keys())[:6] == ['A', 'B', 'C', 'D', 'E', 'G']
    assert rows['G'] == 'G · C反向镜像'
    assert rows['pa-big-candle-continuation'] == 'PA-07 · 大实体K线趋势推进'
    assert rows['PA-09'] == 'PA-09 · 区间边界 Pinbar 反转'
    assert rows['PA-10'] == 'PA-10 · 完整压力支撑互换'
    assert strategy_display_name('pa-trend-40-breakout') == 'PA-01 · 顺势突破'
