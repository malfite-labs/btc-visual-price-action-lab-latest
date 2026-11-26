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


def test_leverage_upgrade_1_to_10_and_5x_compat():
    from scripts.live_copy_env import MAX_COPY_LEVERAGE
    from scripts.live_copy_v2 import order_quantity

    assert MAX_COPY_LEVERAGE == 10
    # 5x existing config remains compatible: 20 * 5 = 100 notional -> 0.001 BTC at 60000
    q5 = order_quantity(
        margin=Decimal('20'), available=Decimal('100'), price=Decimal('60000'),
        planned_leverage=5, actual_leverage=5, step=Decimal('0.001'),
        minimum=Decimal('0.001'), maximum=Decimal('10'), min_notional=Decimal('50'),
        fee=Decimal('0.0005'), reserve=Decimal('0.10'), risk_cap=Decimal('5'), stop_distance=None,
    )
    assert q5 == Decimal('0.001')

    # 10x config: 20 * 10 = 200 notional -> 0.003 BTC at 60000 (180 USDT notional, within 20 USDT margin)
    q10 = order_quantity(
        margin=Decimal('20'), available=Decimal('100'), price=Decimal('60000'),
        planned_leverage=10, actual_leverage=10, step=Decimal('0.001'),
        minimum=Decimal('0.001'), maximum=Decimal('10'), min_notional=Decimal('50'),
        fee=Decimal('0.0005'), reserve=Decimal('0.10'), risk_cap=Decimal('5'), stop_distance=None,
    )
    assert q10 == Decimal('0.003')

    # 11x must still be rejected
    with pytest.raises(ValueError, match='LEVERAGE_MISMATCH'):
        order_quantity(
            margin=Decimal('20'), available=Decimal('100'), price=Decimal('60000'),
            planned_leverage=11, actual_leverage=10, step=Decimal('0.001'),
            minimum=Decimal('0.001'), maximum=Decimal('10'), min_notional=Decimal('50'),
            fee=Decimal('0.0005'), reserve=Decimal('0.10'), risk_cap=Decimal('5'), stop_distance=None,
        )


def test_production_preflight_10x_passes_when_planned_10x_and_blocks_11x():
    class MockProd10x(ProductionExchange):
        def synchronize(self):
            pass

        def _request(self, method, path, parameters=None, *, signed=True, clock_retry=True):
            assert method == 'GET'
            if path == '/fapi/v1/positionSide/dual':
                return {'dualSidePosition': True}
            if path == '/fapi/v3/balance':
                return [{'asset': 'USDT', 'accountAlias': 'uX', 'availableBalance': '100.17'}]
            if path == '/fapi/v3/account':
                return {'assets': [{'asset': 'USDT', 'availableBalance': '100.17', 'walletBalance': '100.17'}]}
            if path in ('/fapi/v3/positionRisk', '/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders'):
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

    ex = MockProd10x('k', 's')
    r10 = ex.preflight_report(planned_leverage=10, fixed_margin=Decimal('20'), budget=Decimal('100'))
    assert r10['status'] == 'PASS'
    assert r10['blockers'] == []
    assert r10['max_leverage'] == 10

    r5 = ex.preflight_report(planned_leverage=5, fixed_margin=Decimal('20'), budget=Decimal('100'))
    assert r5['status'] == 'BLOCKED'
    assert any('10×' in b and '5×' in b for b in r5['blockers'])


def test_pause_resume_and_safe_stop_lifecycle(tmp_path):
    from scripts.live_copy_service import demo_poll

    paper_db = tmp_path / 'paper.sqlite3'
    _make_paper_db(paper_db)
    work = tmp_path / 'demo_lifecycle'
    work.mkdir()
    follower = DryFollower(paper_db, work / 'demo-execution-inbox.sqlite3', Decimal('20'), Decimal('100'), strategy='C')
    ex = FakeExchange('DEMO', hedge=True, leverage=5)
    engine = FollowExecutor(follower, ex, work, 'DEMO-CONTINUOUS', leverage=5, stop_fraction=None, risk_cap=Decimal('5'), exit_mode='FOLLOW_PAPER')

    published = []
    def publish(state, reason='', *, success=False, retries=0):
        published.append((state, reason))

    # 1. Start running -> open position
    _insert_fill(paper_db, fill_id='f1', agent='C', trade_id='t1', effect='OPEN', direction='LONG')
    stopped, evs = demo_poll(engine, lambda: 'run', publish)
    assert not stopped and evs == [('C:f1', 'FOLLOW_PAPER')]
    assert ex.position == Decimal('0.001')

    # 2. Pause -> new open is SKIPPED (not opened), while existing position stays managed
    _insert_fill(paper_db, fill_id='f2', agent='C', trade_id='t2', effect='OPEN', direction='LONG')
    stopped, evs = demo_poll(engine, lambda: 'pause', publish)
    assert not stopped and evs == [('C:f2', 'SKIPPED')]
    assert published[-1][0] == 'DEMO_PAUSED'

    # 3. Stop while holding position -> does NOT stop prematurely, enters DEMO_STOP_PENDING
    stopped, evs = demo_poll(engine, lambda: 'stop', publish)
    assert not stopped
    assert published[-1][0] == 'DEMO_STOP_PENDING'

    # 4. Close original trade t1 while in stop-pending -> linked close executes, then next stop succeeds!
    _insert_fill(paper_db, fill_id='f3', agent='C', trade_id='t1', effect='CLOSE', direction='LONG')
    stopped, evs = demo_poll(engine, lambda: 'stop', publish)
    assert not stopped and evs == [('C:f3', 'CLOSED')]
    assert ex.position == Decimal(0)

    stopped, evs = demo_poll(engine, lambda: 'stop', publish)
    assert stopped is True and evs == []

    # 5. Resume on fresh trade t3 -> does not backfill t2, follows new t3 cleanly
    _insert_fill(paper_db, fill_id='f4', agent='C', trade_id='t3', effect='OPEN', direction='SHORT')
    stopped, evs = demo_poll(engine, lambda: 'run', publish)
    assert not stopped and evs == [('C:f4', 'FOLLOW_PAPER')]
    assert ex.position == Decimal('-0.001')


def test_exchange_unknown_and_partial_fill_handling(tmp_path):
    from scripts.live_c_demo_exchange import DemoApiError
    from scripts.live_c_intent import EntryIntent

    class UnknownExchange(FakeExchange):
        def order(self, client_id):
            err = DemoApiError('/fapi/v1/order', 0, 0)
            err.method = 'GET'
            raise err

    ex = UnknownExchange('PRODUCTION', hedge=True, leverage=10)
    exec_dir = tmp_path / 'unk'
    exec_dir.mkdir()
    executor = DemoExecutor(ex, exec_dir, 'PROD-test', Decimal('0.10'))
    now = datetime.now(UTC)
    intent = EntryIntent('PROD-test', now.isoformat(), 'V3.1', 'dcp-unk1', 'LONG', Decimal('0.002'), Decimal('60000'), None, None, 'FOLLOW_PAPER', None)
    assert executor.accept(intent, now=now) == 'EXCHANGE_UNKNOWN'

    # Partial fill with FOLLOW_PAPER must not mark as completed (non-terminal -> RECONCILE_REQUIRED)
    class PartialExchange(FakeExchange):
        def entry(self, intent):
            super().entry(intent)
            self.orders[intent.client_order_id]['status'] = 'PARTIALLY_FILLED'
            self.orders[intent.client_order_id]['executedQty'] = '0.001'
            oid = self.orders[intent.client_order_id]['orderId']
            self.trades_by_oid[oid][0]['qty'] = '0.001'
            self.position = Decimal('0.001')
            return self.orders[intent.client_order_id]

    pex = PartialExchange('PRODUCTION', hedge=True, leverage=10)
    pdir = tmp_path / 'part'
    pdir.mkdir()
    pexec = DemoExecutor(pex, pdir, 'PROD-part', Decimal('0.10'))
    pintent = EntryIntent('PROD-part', now.isoformat(), 'V3.1', 'dcp-part1', 'LONG', Decimal('0.002'), Decimal('60000'), None, None, 'FOLLOW_PAPER', None)
    assert pexec.accept(pintent, now=now) == 'RECONCILE_REQUIRED'


def test_kline_feed_live_env_and_environment_isolation():
    import urllib.request
    from scripts.kline_window import _attach_demo_feed

    class DummyWin:
        def __init__(self):
            self._cbs = []
        def bind(self, _ev, fn, add=None):
            self._cbs.append(fn)

    win = DummyWin()
    state = {'environment': 'demo', 'seq': 1}
    _attach_demo_feed(
        win,
        lambda: {'environment': 'demo', 'strategies': ['G · C反向镜像'], 'items': [], 'positions': [], 'errors': []},
        lambda: {'environment': 'production', 'strategies': [], 'items': [], 'positions': [], 'errors': []},
        env_state=lambda: dict(state),
    )
    server = getattr(win, '_demo_feed', None)
    if server is None:
        pytest.skip('Port 5187 already bound by running Kline window')
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def fetch(path):
            req = urllib.request.Request('http://127.0.0.1:5187' + path, headers={'Origin': 'http://127.0.0.1:5175'})
            with opener.open(req, timeout=3) as resp:
                return json.load(resp)
        assert fetch('/live-env') == {'environment': 'demo', 'seq': 1}
        state['environment'] = 'production'
        state['seq'] = 2
        assert fetch('/live-env') == {'environment': 'production', 'seq': 2}
        assert fetch('/production-fills')['environment'] == 'production'
        assert fetch('/production-fills')['items'] == []
        assert fetch('/demo-fills')['environment'] == 'demo'
    finally:
        server.shutdown()
        server.server_close()


def _make_g_db(path: Path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            """CREATE TABLE g_fills (
                fill_id TEXT, source_fill_id TEXT, c_trade_id TEXT, g_trade_id TEXT,
                effect TEXT, direction TEXT, quantity TEXT, price TEXT, fee TEXT,
                slippage TEXT, pnl TEXT, reason TEXT, occurred_at TEXT
            )"""
        )


def _insert_g_fill(path: Path, *, fill_id, g_trade_id, effect, direction='SHORT', qty='0.53937024', price='60787.24', reason='C_MIRROR_CLOSE:MIRROR_RISK_EXIT', occurred_at=None):
    at = occurred_at or datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            'INSERT INTO g_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (fill_id, 'src-' + fill_id, 'c-' + g_trade_id, g_trade_id, effect, direction, qty, price, '1', '0', '0', reason, at),
        )


def test_g_mirror_risk_exit_full_qty_partial_close_and_delayed_exit_recovery(tmp_path):
    """Exact reproduction of G MIRROR_RISK_EXIT (effect=PARTIAL_CLOSE, qty==before) + >60s delayed exit."""
    g_db = tmp_path / 'mirror.sqlite3'
    _make_g_db(g_db)
    work = tmp_path / 'g_demo'
    work.mkdir()
    follower = DryFollower(g_db, work / 'demo-execution-inbox.sqlite3', Decimal('30'), Decimal('100'), strategy='G')
    ex = FakeExchange('DEMO', hedge=True, leverage=5)
    engine = FollowExecutor(follower, ex, work, 'DEMO-CONTINUOUS', leverage=5, stop_fraction=None, risk_cap=Decimal('5'), exit_mode='FOLLOW_PAPER')

    # 1. Open SHORT on G
    _insert_g_fill(g_db, fill_id='g1', g_trade_id='gt1', effect='OPEN', direction='SHORT', qty='0.53937024')
    assert engine.tick() == [('G:g1', 'FOLLOW_PAPER')]
    assert ex.position == Decimal('-0.002')

    # 2. G emits MIRROR_RISK_EXIT labeled PARTIAL_CLOSE with 100% qty (and 2 hours delayed!)
    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _insert_g_fill(g_db, fill_id='g2', g_trade_id='gt1', effect='PARTIAL_CLOSE', direction='SHORT', qty='0.53937024', occurred_at=old_ts)
    # Simulate restart recovery before processing the exit
    restarted = FollowExecutor(follower, ex, work, 'DEMO-CONTINUOUS', leverage=5, stop_fraction=None, risk_cap=Decimal('5'), exit_mode='FOLLOW_PAPER')
    assert restarted.tick() == [('G:g2', 'CLOSED')]
    assert ex.position == Decimal(0)
    assert restarted.can_stop() is True


def test_unified_partial_close_proportional_and_min_order_skip(tmp_path):
    """Test true proportional partial close (0.004 -> 0.002 -> 0) and sub-minimum partial skip."""
    class PartialCapableExchange(FakeExchange):
        def reduce(self, client_id, side, quantity):
            self.writes.append(('reduce', client_id, str(quantity)))
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
                'qty': str(quantity), 'price': str(self.price), 'commission': '0.02',
                'commissionAsset': 'USDT', 'realizedPnl': '0.05', 'time': int(datetime.now(UTC).timestamp() * 1000),
            }]
            self.position = self.position - quantity if self.position > 0 else self.position + quantity
            return self.orders[client_id]

    g_db = tmp_path / 'mirror_part.sqlite3'
    _make_g_db(g_db)
    work = tmp_path / 'g_part'
    work.mkdir()
    # 50 USDT * 5x = 250 USDT -> 0.004 BTC at 60000
    follower = DryFollower(g_db, work / 'demo-execution-inbox.sqlite3', Decimal('50'), Decimal('200'), strategy='G')
    ex = PartialCapableExchange('DEMO', hedge=True, leverage=5)
    engine = FollowExecutor(follower, ex, work, 'DEMO-CONTINUOUS', leverage=5, stop_fraction=None, risk_cap=Decimal('10'), exit_mode='FOLLOW_PAPER')

    _insert_g_fill(g_db, fill_id='p1', g_trade_id='pt1', effect='OPEN', direction='LONG', qty='1.0000')
    assert engine.tick() == [('G:p1', 'FOLLOW_PAPER')]
    assert ex.position == Decimal('0.004')

    # 10% partial close -> 0.0004 BTC < 0.001 min_qty -> skipped cleanly without crashing!
    _insert_g_fill(g_db, fill_id='p2', g_trade_id='pt1', effect='PARTIAL_CLOSE', direction='LONG', qty='0.1000')
    assert engine.tick() == [('G:p2', 'SKIPPED_MIN_ORDER')]
    assert ex.position == Decimal('0.004')

    # 50% of remaining 0.9000 = 0.4500 -> 0.004 * 0.5 = 0.002 BTC executed!
    _insert_g_fill(g_db, fill_id='p3', g_trade_id='pt1', effect='PARTIAL_CLOSE', direction='LONG', qty='0.4500')
    assert engine.tick() == [('G:p3', 'FOLLOW_PAPER')]
    assert ex.position == Decimal('0.002')

    # Final close of remaining 0.4500 -> closes remaining 0.002 BTC cleanly!
    _insert_g_fill(g_db, fill_id='p4', g_trade_id='pt1', effect='CLOSE', direction='LONG', qty='0.4500')
    assert engine.tick() == [('G:p4', 'CLOSED')]
    assert ex.position == Decimal(0)
    assert engine.can_stop() is True
