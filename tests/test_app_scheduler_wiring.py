from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from backend.domain import CostModel
from backend.ledger import Ledger
from backend.market import Candle
from backend.orchestration import TestOnlyPaperCycleRunner
from backend.scheduler import PaperCycleScheduler
from backend.vision import AgentOrchestrator, FakeVisionAdapter


class TestModeAdapter(FakeVisionAdapter):
    __test__ = False

    def __init__(self):
        self.seen = {}

    async def decide(self, *, image, strategy_md, md_version, agent, bar_close, **kwargs):
        self.seen[agent] = {
            "image": __import__("hashlib").sha256(image).hexdigest(),
            "account": kwargs.get("account_state"),
            "md": strategy_md,
        }
        return {
            "accepted": True,
            "mode": "TEST_SIMULATION_ONLY",
            "agent": agent,
            "action": "OPEN_LONG" if agent == "A" else "WAIT",
            "rationale": "isolated deterministic integration fixture",
            "stop_loss": 90.0 if agent == "A" else None,
            "model_id": self.model_id,
            "snapshot_sha256": __import__("hashlib").sha256(image).hexdigest(),
            "bar_close_utc": bar_close.isoformat(),
            "md_version": md_version,
        }


def test_app_has_inert_scheduler_by_default():
    from backend import app

    assert app.paper_scheduler is not None
    assert app.paper_scheduler.enabled is False
    assert app.paper_runner.enabled is False


def test_image_capability_marker_is_bound_to_model_and_exact_endpoint(tmp_path, monkeypatch):
    import json

    from backend import app

    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path / "data")
    marker = tmp_path / "data" / "vision-capability.json"
    marker.parent.mkdir()
    marker.write_text(
        json.dumps(
            {
                "verified": True,
                "model": "deepseek-flash",
                "endpoint": "https://api.deepseek.com/chat/completions",
                "verified_at_utc": "2026-10-27T00:00:00+00:00",
                "image_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    assert app._stored_image_capability(
        "deepseek-flash", "https://api.deepseek.com", "/chat/completions"
    )
    assert not app._stored_image_capability(
        "deepseek-v4-pro", "https://api.deepseek.com", "/chat/completions"
    )


def test_live_closed_bar_dispatch_uses_one_image_and_current_market_evidence(tmp_path, monkeypatch):
    from backend import app

    db = tmp_path / "isolated-test.sqlite"
    ledger = Ledger(db)
    decisions = AgentOrchestrator(str(db))
    runner = TestOnlyPaperCycleRunner(ledger, decisions, CostModel())
    scheduler = PaperCycleScheduler(db, runner, enabled=True)
    current = datetime.now(UTC)
    close = current.replace(minute=(current.minute // 15) * 15, second=0, microsecond=0)
    quote_at = datetime.now(UTC)
    candles = []
    for index in range(288):
        opened = close - timedelta(minutes=15 * (287 - index + 1))
        ended = opened + timedelta(minutes=15)
        candles.append(
            Candle(
                "BTC/USD",
                "15Min",
                opened.isoformat(),
                ended.isoformat(),
                "100",
                "101",
                "99",
                "100.5",
                "1",
                1,
                "100.2",
                True,
                source="BINANCE_USDM_PERP",
                currency="USD_RESEARCH",
            ).__dict__
        )

    async def quote():
        return {
            "symbol": "BTC/USD",
            "bid": "100",
            "ask": "100.1",
            "bid_qty": "2",
            "ask_qty": "2",
            "event_at": quote_at.isoformat(),
            "received_at": quote_at.isoformat(),
            "age_ms": 1,
            "source": "BINANCE_USDM_PERP",
            "currency": "USD_RESEARCH",
            "event_id": "quote-1",
        }

    async def orderbook():
        return {
            "symbol": "BTC/USD",
            "bids": [{"p": "100", "s": "2"}],
            "asks": [{"p": "100.1", "s": "2"}],
            "event_at": quote_at.isoformat(),
            "received_at": quote_at.isoformat(),
            "age_ms": 1,
            "source": "BINANCE_USDM_PERP",
        }

    monkeypatch.setattr(app, "paper_runner", runner)
    monkeypatch.setattr(app, "paper_scheduler", scheduler)
    monkeypatch.setattr(app, "formal_mode", "VISUAL")
    adapter = TestModeAdapter()
    monkeypatch.setattr(app, "vision_adapter", adapter)
    monkeypatch.setattr(app, "candles", candles)
    monkeypatch.setattr(app, "market_status", {"feed_health": "LIVE", "gap_count_288": 0})
    monkeypatch.setattr(app.market, "status", {"websocket": "CONNECTED", "websocket_recovery_state": "HEALTHY"})
    monkeypatch.setattr(app, "websocket_quote_health", lambda *args, **kwargs: "FRESH")
    monkeypatch.setattr(app.market, "latest_quote", quote)
    monkeypatch.setattr(app.market, "orderbook", orderbook)
    monkeypatch.setattr(
        app,
        "strategies",
        type(
            "Registry",
            (),
            {
                "current_strategy": lambda self, agent: (
                    None if agent == "C" else ("v1", f"{agent}-md")
                )
            },
        )(),
    )

    result = asyncio.run(
        app.process_closed_bar(
                close, now=datetime.now(UTC), snapshot_path=tmp_path / "chart.png"
        )
    )
    assert result["A"]["status"] == result["B"]["status"] == "VALID", result
    assert result["C"]["status"] == "STRATEGY_NOT_CONFIGURED"
    assert adapter.seen["A"]["image"] == adapter.seen["B"]["image"]
    assert adapter.seen["A"]["md"] != adapter.seen["B"]["md"]
    for agent in "AB":
        context = adapter.seen[agent]["account"]
        assert context["data_mode"] == "TEST_SNAPSHOT" and context["position"] == "FLAT"
        assert context["available_cash"] == "10000.00" and context["mid_price"] == "100.05"
        assert context["candle_close_utc"] == close.isoformat()
        assert context["image_sha256"] == adapter.seen[agent]["image"]
    assert {result[agent]["snapshot_sha256"] for agent in "AB"} == {
        decisions.decisions_for(agent)[0]["snapshot_sha256"] for agent in "AB"
    }
    assert (
        len({d["snapshot_sha256"] for agent in "ABC" for d in decisions.decisions_for(agent)}) == 1
    )
    assert decisions.decisions_for("C") == []
    assert ledger.account("A")["state"] == "OPEN_LONG"
    assert ledger.account("B")["state"] == "FLAT"
    assert ledger.account("C")["state"] == "FLAT"
    assert len(ledger.fills()) == 1
    assert ledger.fills()[0]["agent"] == "A"
    duplicate = asyncio.run(
        app.process_closed_bar(
            close, now=close + timedelta(seconds=3), snapshot_path=tmp_path / "chart.png"
        )
    )
    assert all(item["status"] == "DUPLICATE_CYCLE" for item in duplicate.values())
    assert len(ledger.fills()) == 1


def test_quote_stop_monitor_and_recovery_are_separate_from_decisions(tmp_path, monkeypatch):
    from backend import app

    db = tmp_path / "stop.sqlite"
    runner = TestOnlyPaperCycleRunner(Ledger(db), AgentOrchestrator(str(db)), CostModel())
    scheduler = PaperCycleScheduler(db, runner, enabled=False)
    monkeypatch.setattr(app, "paper_runner", runner)
    monkeypatch.setattr(app, "paper_scheduler", scheduler)
    calls = []
    monkeypatch.setattr(scheduler, "monitor_stops", lambda **kwargs: calls.append(kwargs) or {})
    monkeypatch.setattr(scheduler, "recover_stops", lambda **kwargs: calls.append(kwargs) or {})
    quote = {
        "symbol": "BTC/USD",
        "bid": "100",
        "ask": "100.1",
        "bid_qty": "1",
        "ask_qty": "1",
        "event_at": "2026-01-01T00:00:00+00:00",
        "received_at": "2026-01-01T00:00:00+00:00",
        "age_ms": 1,
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
    }
    asyncio.run(app.process_stop_quote(quote))
    assert len(calls) == 2
    assert calls[0]["quote"]["source"] == "BINANCE_USDM_PERP"
    assert runner.ledger.fills() == []

def test_startup_position_gate_waits_for_fresh_monitored_quote(tmp_path, monkeypatch):
    from decimal import Decimal
    from backend import app
    from backend.rule_cycle import RuleCycleRunner
    from backend.vision import AgentOrchestrator
    db=tmp_path/'startup.sqlite3'; ledger=Ledger(db); AgentOrchestrator(str(db))
    assert ledger.open_position(agent='B',direction='LONG',quantity=Decimal('1'),price=Decimal('100'),stop=Decimal('90'),fee=Decimal('.05'),slippage=Decimal('0'),idempotency_key='isolated',expected_version=0)['accepted']
    runner=RuleCycleRunner(ledger,db); scheduler=PaperCycleScheduler(db,runner,enabled=True)
    monkeypatch.setenv('BTC_REQUIRE_STARTUP_MONITOR','true')
    monkeypatch.setattr(app,'ledger',ledger);monkeypatch.setattr(app,'paper_runner',runner);monkeypatch.setattr(app,'paper_scheduler',scheduler)
    monkeypatch.setattr(app,'formal_mode','RULE_DRIVEN');monkeypatch.setattr(app,'market_status',{'feed_health':'LIVE','gap_count_288':0})
    close=datetime.now(UTC).replace(minute=(datetime.now(UTC).minute//15)*15,second=0,microsecond=0)
    candle={'close_at':close.isoformat(),'is_closed':True};monkeypatch.setattr(app,'candles',[candle])
    monkeypatch.setattr(app,'_closed_cycle_candles',lambda *args:[candle]*288);monkeypatch.setattr(app,'_valid_cycle_candle',lambda row:True)
    monkeypatch.setattr(app,'market_open_gate_reason',lambda **kw:None)
    result=asyncio.run(app.process_closed_bar(close,now=close+timedelta(seconds=2)))
    assert result['A']['status']=='STARTUP_POSITION_MONITOR_PENDING'
    assert len(ledger.fills())==1
    q={'source':app.market.source,'currency':'USD_RESEARCH','timestamp_valid':True,'age_ms':0,'event_at':datetime.now(UTC).isoformat(),'bid':'100','ask':'100.1','bid_qty':'2','ask_qty':'2'}
    asyncio.run(app.process_stop_quote(q))
    assert app.market_status['startup_position_monitor_checked'] is True
    assert ledger.account('B')['state']=='OPEN_LONG' and len(ledger.fills())==1
    import json
    (tmp_path/'data').mkdir();(tmp_path/'data'/'formal-mode.json').write_text(json.dumps({'activated_at_utc':(close-timedelta(minutes=15)).isoformat()}))
    monkeypatch.setattr(app,'ROOT',tmp_path);monkeypatch.setattr(app,'DATA_ROOT',tmp_path/'data')
    async def get_quote():return dict(q)
    async def get_book():return {'age_ms':0}
    calls=[]
    async def run(**kwargs):
        calls.append(app.market_status.get('startup_position_monitor_checked'))
        return {a:{'status':'VALID'} for a in 'ABC'}
    monkeypatch.setattr(app.market,'latest_quote',get_quote);monkeypatch.setattr(app.market,'orderbook',get_book)
    monkeypatch.setattr(scheduler,'run_closed_cycle',run)
    result=asyncio.run(app.process_closed_bar(close,now=close+timedelta(seconds=2)))
    assert result['A']['status']=='VALID' and calls==[True]


def test_rule_driven_closed_bar_dispatches_strategy_pool_and_isolates_duplicates(tmp_path, monkeypatch):
    import json
    from backend import app
    from backend.rule_cycle import RuleCycleRunner
    from backend.strategy_pool import StrategyPool

    now = datetime.now(UTC)
    close = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    ledger = Ledger(tmp_path / 'formal.sqlite3')
    AgentOrchestrator(str(tmp_path / 'formal.sqlite3'))
    runner = RuleCycleRunner(ledger, tmp_path / 'formal.sqlite3')
    paper_calls = []
    async def run_closed_cycle(**kwargs):
        paper_calls.append(kwargs['candle_close'])
        return {agent: {'status': 'VALID', 'action': 'WAIT'} for agent in 'ABC'}

    pool = StrategyPool(tmp_path / 'pool.sqlite3')
    document = {
        'format': 'btc-lab-declarative-strategy/v2', 'strategy_id': 'pa-dispatch-test',
        'version': '1.0.0', 'name': 'PA dispatch integration', 'market': 'BTC/USD',
        'timeframe': '15m', 'description': 'RULE_DRIVEN natural-cycle integration fixture',
        'parameters': {},
        'risk': {'initial_equity': '10000', 'currency': 'USD_RESEARCH', 'max_leverage': 5, 'risk_fraction': '0.02'},
        'entry': {'long': {'op': 'gt', 'args': [1, 2]}, 'short': {'op': 'gt', 'args': [1, 2]}},
        'position': {
            'direction': 'both', 'entry_timing': 'next_open',
            'stop': {'kind': 'swing_atr', 'lookback': 3, 'atr_period': 14, 'buffer_atr': 0.1,
                     'min_distance_atr': 0.6, 'max_distance_atr': 2.5},
            'target_r': 2, 'max_hold_bars': 96, 'same_bar_priority': 'stop',
        },
    }
    strategy_ids = [
        'pa-trend-40-breakout', 'pa-sma20-pullback-confirm', 'pa-full-engulfing-reversal',
        'pa-gapless-three-bar-reversal', 'pa-trend-false-break-reclaim', 'pa-inside-bar-break',
        'pa-big-candle-continuation', 'pa-three-bar-hhhl',
    ]
    for strategy_id in strategy_ids:
        candidate = {**document, 'strategy_id': strategy_id, 'name': strategy_id}
        pool.upload(candidate)
        pool.enable(strategy_id, '1.0.0')

    rows = []
    for index in range(288):
        opened = close - timedelta(minutes=15 * (288 - index))
        ended = opened + timedelta(minutes=15)
        rows.append(Candle('BTC/USD', '15Min', opened.isoformat(), ended.isoformat(),
                           '100', '101', '99', '100', '1', 1, '100', True, source='BINANCE_USDM_PERP', currency='USD_RESEARCH').__dict__)
    quote_time = now
    quote = {
        'symbol': 'BTC/USD', 'bid': '100', 'ask': '100.1', 'bid_qty': '2', 'ask_qty': '2',
        'event_at': quote_time.isoformat(), 'received_at': quote_time.isoformat(), 'age_ms': 0,
        'timestamp_valid': True, 'source': 'BINANCE_USDM_PERP', 'currency': 'USD_RESEARCH',
    }
    async def latest_quote():
        return dict(quote)
    async def no_broadcast(_):
        return None
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'formal-mode.json').write_text(
        json.dumps({'activated_at_utc': (close - timedelta(hours=1)).isoformat()}), encoding='utf-8'
    )
    monkeypatch.setenv('BTC_REQUIRE_STARTUP_MONITOR', 'false')
    monkeypatch.setattr(app, 'ROOT', tmp_path)
    monkeypatch.setattr(app, 'DATA_ROOT', tmp_path / 'data')
    monkeypatch.setattr(app, 'formal_mode', 'RULE_DRIVEN')
    monkeypatch.setattr(app, 'ledger', ledger)
    monkeypatch.setattr(app, 'paper_runner', runner)
    monkeypatch.setattr(app, 'paper_scheduler', type('Scheduler', (), {
        'enabled': True, 'run_closed_cycle': staticmethod(run_closed_cycle),
    })())
    monkeypatch.setattr(app, 'strategy_pool', pool)
    monkeypatch.setattr(app, 'candles', rows)
    monkeypatch.setattr(app, 'market_status', {
        'feed_health': 'LIVE', 'gap_count_288': 0, 'startup_position_monitor_checked': True,
        'quote': quote,
    })
    monkeypatch.setattr(app.market, 'status', {
        'websocket': 'CONNECTED', 'websocket_recovery_state': 'HEALTHY',
        'last_quote': quote, 'websocket_last_quote_received_at': quote_time.isoformat(),
    })
    monkeypatch.setattr(app.market, 'latest_quote', latest_quote)
    monkeypatch.setattr(app, 'websocket_quote_health', lambda *args, **kwargs: 'FRESH')
    monkeypatch.setattr(app, 'market_open_gate_reason', lambda **kwargs: None)
    monkeypatch.setattr(app, 'broadcast', no_broadcast)

    result = asyncio.run(app.process_closed_bar(close, now=now, snapshot_path=tmp_path / 'unused.png'))
    accounts = pool.list()
    assert result['A']['status'] == result['B']['status'] == result['C']['status'] == 'VALID'
    assert paper_calls == [close]
    assert {account['id'] for account in accounts} == set(strategy_ids)
    assert all(account['last_cycle'] == close.isoformat() for account in accounts)
    assert all(account['last_status'] == 'OK' and account['last_action'] == 'WAIT' for account in accounts), [
        (account['id'], account['last_status'], account['last_action']) for account in accounts
    ]
    assert all(account['position_direction'] == 'FLAT' and account['equity'] == '10000.00' for account in accounts)

    duplicate = asyncio.run(app.process_closed_bar(close, now=now, snapshot_path=tmp_path / 'unused.png'))
    assert duplicate['A']['status'] == 'VALID'
    assert paper_calls == [close, close]
    assert all(account['last_cycle'] == close.isoformat() for account in pool.list())
    assert all(pool.fills(strategy_id) == [] for strategy_id in strategy_ids)

    next_close = close + timedelta(minutes=15)
    rows.append(Candle('BTC/USD', '15Min', close.isoformat(), next_close.isoformat(),
                       '100', '101', '99', '100', '1', 1, '100', True, source='BINANCE_USDM_PERP', currency='USD_RESEARCH').__dict__)
    def fail_cycle(*args, **kwargs):
        raise RuntimeError('isolated PA failure fixture')
    monkeypatch.setattr(pool, 'cycle', fail_cycle)
    isolated = asyncio.run(app.process_closed_bar(next_close, now=next_close + timedelta(seconds=2)))
    assert isolated['A']['status'] == isolated['B']['status'] == isolated['C']['status'] == 'VALID'
    assert paper_calls == [close, close, next_close]

    blocked_close = next_close + timedelta(minutes=15)
    rows.append(Candle('BTC/USD', '15Min', next_close.isoformat(), blocked_close.isoformat(),
                       '100', '101', '99', '100', '1', 1, '100', True, source='BINANCE_USDM_PERP', currency='USD_RESEARCH').__dict__)
    monkeypatch.setattr(app, 'market_open_gate_reason', lambda **kwargs: 'MARKET_STREAM_DISCONNECTED')
    blocked = asyncio.run(app.process_closed_bar(blocked_close, now=blocked_close + timedelta(seconds=2)))
    assert all(item['status'] == 'MARKET_STREAM_DISCONNECTED' for item in blocked.values())
    assert paper_calls == [close, close, next_close]

    incomplete_close = blocked_close + timedelta(minutes=15)
    rows.append(Candle('BTC/USD', '15Min', blocked_close.isoformat(), incomplete_close.isoformat(),
                       '100', '101', '99', '100', '1', 1, '100', True, source='BINANCE_USDM_PERP', currency='USD_RESEARCH').__dict__)
    monkeypatch.setattr(app, 'market_open_gate_reason', lambda **kwargs: None)
    monkeypatch.setattr(app, '_closed_cycle_candles', lambda *args: (_ for _ in ()).throw(ValueError('CLOSED_WINDOW_INCOMPLETE')))
    incomplete = asyncio.run(app.process_closed_bar(incomplete_close, now=incomplete_close + timedelta(seconds=2)))
    assert all(item['status'] == 'CLOSED_WINDOW_INCOMPLETE' for item in incomplete.values())
    assert paper_calls == [close, close, next_close]

