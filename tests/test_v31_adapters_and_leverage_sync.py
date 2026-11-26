"""Isolated unit tests for all 16 strategy fill adapters and Binance auto-leverage sync."""
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.live_c_demo_exchange import DemoExchange, ProductionExchange
from scripts.live_copy_service import DryFollower
from scripts.live_copy_v2 import paper_fills
from scripts.pa09_pa10_execution import CandidateLedger

PROJECT = Path(__file__).resolve().parents[1]


class FakeResponse(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode('utf-8'))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeBinanceOpener:
    """In-memory Binance USD-M mock for isolated leverage sync & readback tests."""
    def __init__(self, *, actual_leverage=5, positions=None, open_orders=None, open_algos=None,
                 readback_override=None, fail_post=False, alias='TEST-ALIAS-01'):
        self.actual_leverage = actual_leverage
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders if open_orders is not None else []
        self.open_algos = open_algos if open_algos is not None else []
        self.readback_override = readback_override
        self.fail_post = fail_post
        self.alias = alias
        self.calls = []

    def open(self, req, timeout=8):
        parsed = urlparse(req.full_url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        method = req.get_method()
        self.calls.append((method, parsed.path, params))
        if parsed.path == '/fapi/v1/time':
            return FakeResponse({'serverTime': 1700000000000})
        if parsed.path == '/fapi/v3/balance':
            return FakeResponse([{'asset': 'USDT', 'accountAlias': self.alias, 'availableBalance': '500.00'}])
        if parsed.path == '/fapi/v3/account':
            return FakeResponse({'assets': [{'asset': 'USDT', 'availableBalance': '500.00', 'walletBalance': '500.00'}]})
        if parsed.path == '/fapi/v1/positionSide/dual':
            return FakeResponse({'dualSidePosition': True})
        if parsed.path == '/fapi/v3/positionRisk':
            return FakeResponse(self.positions)
        if parsed.path == '/fapi/v1/openOrders':
            return FakeResponse(self.open_orders)
        if parsed.path == '/fapi/v1/openAlgoOrders':
            return FakeResponse(self.open_algos)
        if parsed.path == '/fapi/v1/accountConfig':
            return FakeResponse({'canTrade': True})
        if parsed.path == '/fapi/v1/leverageBracket':
            return FakeResponse([{'symbol': 'BTCUSDT', 'brackets': [{'notionalFloor': 0, 'notionalCap': 50000, 'initialLeverage': 125}]}])
        if parsed.path == '/fapi/v1/exchangeInfo':
            return FakeResponse({'symbols': [{
                'symbol': 'BTCUSDT',
                'filters': [
                    {'filterType': 'LOT_SIZE', 'minQty': '0.001', 'stepSize': '0.001'},
                    {'filterType': 'MARKET_LOT_SIZE', 'minQty': '0.001', 'stepSize': '0.001'},
                    {'filterType': 'PRICE_FILTER', 'tickSize': '0.10'},
                    {'filterType': 'MIN_NOTIONAL', 'notional': '100'},
                ],
            }]})
        if parsed.path in ('/fapi/v1/ticker/price', '/fapi/v2/ticker/price'):
            return FakeResponse({'symbol': 'BTCUSDT', 'price': '90000.00'})
        if parsed.path == '/fapi/v1/commissionRate':
            return FakeResponse({'symbol': 'BTCUSDT', 'takerCommissionRate': '0.0005'})
        if parsed.path == '/fapi/v1/symbolConfig':
            lev = self.readback_override if self.readback_override is not None else self.actual_leverage
            return FakeResponse([{'symbol': 'BTCUSDT', 'marginType': 'ISOLATED', 'leverage': lev}])
        if parsed.path == '/fapi/v1/leverage' and method == 'POST':
            if self.fail_post:
                raise OSError('simulated post failure')
            self.actual_leverage = int(params['leverage'])
            return FakeResponse({'symbol': 'BTCUSDT', 'leverage': self.actual_leverage, 'maxNotionalValue': '50000'})
        raise AssertionError(f'Unexpected request: {method} {parsed.path}')


# --- 1. All 16 strategies unified adapter tests ---

def test_all_16_live_ledgers_pass_unified_paper_fills():
    """Read-only verification against all 16 formal strategy ledgers."""
    from scripts.local_dashboard import build_live_paper_rows
    rows = build_live_paper_rows()
    assert len(rows) == 16
    for sid in rows:
        if sid.startswith('pa-'):
            path = PROJECT / 'data/strategy-pool.sqlite3'
        elif sid in ('PA-09', 'PA-10'):
            path = PROJECT / f'data/pa-v1-extensions/{sid}.sqlite3'
        elif sid == 'G':
            path = PROJECT / 'data/g/mirror.sqlite3'
        elif sid == 'D':
            path = PROJECT / 'data/d/trading.sqlite3'
        elif sid == 'E':
            path = PROJECT / 'data/e/formal.sqlite3'
        else:
            path = PROJECT / 'data/paper.sqlite3'
        if not path.is_file():
            pytest.skip(f'Ledger {path} not present in this checkout')
        watermark = paper_fills(path, strategy=sid)
        assert isinstance(watermark, int) and watermark >= 0, f'{sid} failed watermark check'


def test_pa_pool_historical_mismatch_does_not_block_new_fills(tmp_path):
    """PA-03/07/08 style historical closed mismatch must not block new subscription or live fills."""
    db_path = tmp_path / 'pool.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE accounts(strategy_id TEXT PRIMARY KEY, position TEXT);
            CREATE TABLE fills(id INTEGER PRIMARY KEY, strategy_id TEXT, version TEXT, side TEXT,
                qty TEXT, price TEXT, reason TEXT, idempotency_key TEXT, created_at TEXT, cycle TEXT);
            INSERT INTO accounts VALUES ('pa-full-engulfing-reversal', NULL);
            -- Historical closed trade with version mismatch
            INSERT INTO fills VALUES (1,'pa-full-engulfing-reversal','v1','BUY','0.01','90000','ENTRY','pa-full-engulfing-reversal:open:1','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
            INSERT INTO fills VALUES (2,'pa-full-engulfing-reversal','v2','SELL','0.01','91000','STOP','pa-full-engulfing-reversal:close:2026-01-01T00:15:00+00:00','2026-01-01T00:15:00+00:00','2026-01-01T00:15:00+00:00');
        """)
    wm = paper_fills(db_path, strategy='pa-full-engulfing-reversal')
    assert wm == 2

    # Add clean new open + partial close + full close after watermark
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO fills VALUES (3,'pa-full-engulfing-reversal','v3','SELL','0.02','92000','ENTRY','pa-full-engulfing-reversal:open:3','2026-01-01T01:00:00+00:00','2026-01-01T01:00:00+00:00')")
        db.execute("INSERT INTO fills VALUES (4,'pa-full-engulfing-reversal','v3','BUY','0.01','91500','TARGET','pa-full-engulfing-reversal:close:2026-01-01T01:15:00+00:00','2026-01-01T01:15:00+00:00','2026-01-01T01:15:00+00:00')")
        db.execute("INSERT INTO fills VALUES (5,'pa-full-engulfing-reversal','v3','BUY','0.01','91000','TARGET','pa-full-engulfing-reversal:close:2026-01-01T01:30:00+00:00','2026-01-01T01:30:00+00:00','2026-01-01T01:30:00+00:00')")
    fills = paper_fills(db_path, after=wm, strategy='pa-full-engulfing-reversal')
    assert [(f.action, f.direction, f.quantity) for f in fills] == [
        ('OPEN', 'SHORT', Decimal('0.02')),
        ('PARTIAL_CLOSE', 'SHORT', Decimal('0.01')),
        ('FULL_CLOSE', 'SHORT', Decimal('0.01')),
    ]


def test_d_e_pa09_pa10_unified_fill_parsing_and_dry_follower(tmp_path):
    """D, E, PA-09, PA-10 all parse OPEN/PARTIAL_CLOSE/FULL_CLOSE and run through DryFollower."""
    # D ledger
    d_db = tmp_path / 'd.sqlite3'
    with sqlite3.connect(d_db) as db:
        db.executescript("""
            CREATE TABLE d_fills(fill_id TEXT, trade_id TEXT, effect TEXT, quantity TEXT, price TEXT, fee TEXT, reason TEXT, occurred_at TEXT, idempotency_key TEXT);
            INSERT INTO d_fills VALUES ('f1','t-d-1','OPEN','0.01','90000','0.1','ENTRY','2026-11-26T08:00:00+00:00','k1');
            INSERT INTO d_fills VALUES ('f2','t-d-1','PARTIAL_CLOSE','0.005','91000','0.05','TP1','2026-11-26T08:15:00+00:00','k2');
            INSERT INTO d_fills VALUES ('f3','t-d-1','CLOSE','0.005','92000','0.05','TP2','2026-11-26T08:30:00+00:00','k3');
        """)
    d_fills = paper_fills(d_db, after=0, strategy='D')
    assert [(f.action, f.direction, f.quantity) for f in d_fills] == [
        ('OPEN', 'LONG', Decimal('0.01')),
        ('PARTIAL_CLOSE', 'LONG', Decimal('0.005')),
        ('FULL_CLOSE', 'LONG', Decimal('0.005')),
    ]

    # E ledger
    e_db = tmp_path / 'e.sqlite3'
    with sqlite3.connect(e_db) as db:
        db.executescript("""
            CREATE TABLE fills(fill_id TEXT, agent TEXT, side TEXT, quantity TEXT, price TEXT, fee TEXT, slippage TEXT,
                occurred_at TEXT, idempotency_key TEXT, trade_id TEXT, position_effect TEXT, position_direction TEXT,
                decision_id TEXT, strategy_version TEXT, rationale TEXT);
            INSERT INTO fills VALUES ('ef1','A','SELL','0.02','90000','0.1','0','2026-11-26T08:00:00+00:00','ek1','t-e-1','OPEN','SHORT','d1','E-V1.1','r');
            INSERT INTO fills VALUES ('ef2','A','BUY','0.02','89000','0.1','0','2026-11-26T08:30:00+00:00','ek2','t-e-1','CLOSE','SHORT','d2','E-V1.1','r');
        """)
    e_fills = paper_fills(e_db, after=0, strategy='E')
    assert [(f.action, f.direction, f.quantity) for f in e_fills] == [
        ('OPEN', 'SHORT', Decimal('0.02')),
        ('FULL_CLOSE', 'SHORT', Decimal('0.02')),
    ]

    # PA-09 & PA-10 ledgers with explicit direction & trade_id via CandidateLedger._fill
    for sid in ('PA-09', 'PA-10'):
        ledger = CandidateLedger(tmp_path, sid)
        ext_db = ledger.path
        follower = DryFollower(ext_db, tmp_path / f'{sid}-copy.sqlite3', Decimal('20'), Decimal('100'), strategy=sid)
        assert follower.cursor() == 0
        t1 = datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 11, 26, 8, 15, tzinfo=timezone.utc)
        with sqlite3.connect(ext_db) as db:
            ledger._fill(db, {}, t1, 'OPEN', Decimal('90000'), Decimal('0.1'), Decimal('0.01'), 'ENTRY_NEXT_OPEN',
                         direction='LONG', trade_id=f'{sid}:{t1.isoformat()}')
            ledger._fill(db, {}, t2, 'CLOSE', Decimal('91000'), Decimal('0.1'), Decimal('0.01'), 'TARGET', Decimal('9.8'),
                         direction='LONG', trade_id=f'{sid}:{t1.isoformat()}')
        processed = follower.poll()
        assert [r['action'] for _, r in processed] == ['OPEN', 'FULL_CLOSE']


# --- 2. Binance auto-leverage synchronization safety tests ---

def test_leverage_sync_consistent_sends_no_post():
    opener = FakeBinanceOpener(actual_leverage=5)
    ex = DemoExchange('k', 's', opener=opener, allow_writes=False, allow_leverage_sync=True)
    res = ex.sync_leverage_safe(5)
    assert res['changed'] is False
    assert res['after'] == 5
    assert not any(m == 'POST' for m, _, _ in opener.calls)


def test_leverage_sync_inconsistent_posts_once_and_verifies_readback():
    opener = FakeBinanceOpener(actual_leverage=10)
    ex = ProductionExchange('k', 's', opener=opener, allow_writes=False, allow_leverage_sync=True)
    res = ex.sync_leverage_safe(5, fixed_margin=Decimal('40'), budget=Decimal('100'))
    assert res['changed'] is True
    assert res['before'] == 10
    assert res['after'] == 5
    assert res['verified'] is True
    assert res['preflight']['status'] == 'PASS'
    posts = [p for m, p, _ in opener.calls if m == 'POST']
    assert posts == ['/fapi/v1/leverage']
    # Order write must STILL be blocked when allow_writes=False
    with pytest.raises(RuntimeError, match='PRODUCTION_ORDER_DISABLED'):
        ex.reduce('cid-1', 'SELL', Decimal('0.001'))


def test_leverage_sync_blocked_when_permission_disabled():
    opener = FakeBinanceOpener(actual_leverage=10)
    ex = ProductionExchange('k', 's', opener=opener, allow_writes=False, allow_leverage_sync=False)
    with pytest.raises(RuntimeError, match='PRODUCTION_LEVERAGE_SYNC_DISABLED'):
        ex.sync_leverage_safe(5)
    assert not any(m == 'POST' for m, _, _ in opener.calls)


@pytest.mark.parametrize('kwargs', [
    {'positions': [{'symbol': 'BTCUSDT', 'positionAmt': '0.002'}]},
    {'positions': [{'symbol': 'ETHUSDT', 'positionAmt': '-0.1'}]},
    {'open_orders': [{'orderId': 123}]},
    {'open_algos': [{'algoId': 456}]},
])
def test_leverage_sync_blocked_when_exposure_exists(kwargs):
    opener = FakeBinanceOpener(actual_leverage=10, **kwargs)
    ex = DemoExchange('k', 's', opener=opener, allow_writes=False, allow_leverage_sync=True)
    with pytest.raises(ValueError, match='禁止自动调整杠杆'):
        ex.sync_leverage_safe(5)
    assert not any(m == 'POST' for m, _, _ in opener.calls)


def test_leverage_sync_readback_mismatch_raises_and_blocks():
    opener = FakeBinanceOpener(actual_leverage=10, readback_override=10)
    ex = DemoExchange('k', 's', opener=opener, allow_writes=False, allow_leverage_sync=True)
    with pytest.raises(RuntimeError, match='LEVERAGE_READBACK_MISMATCH'):
        ex.sync_leverage_safe(5)


def test_leverage_sync_rejects_above_max_copy_leverage():
    opener = FakeBinanceOpener(actual_leverage=5)
    ex = DemoExchange('k', 's', opener=opener, allow_writes=False, allow_leverage_sync=True)
    with pytest.raises(ValueError, match='超出系统上限'):
        ex.sync_leverage_safe(15)
    assert not any(m == 'POST' for m, _, _ in opener.calls)
