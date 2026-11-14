import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.ledger import Ledger
from backend.vision import AgentOrchestrator
from backend.rule_cycle import RuleCycleRunner
from backend.market import market_open_gate_reason


def test_time_exit_survives_entry_gate_and_bad_quote(tmp_path, monkeypatch):
    import backend.rule_cycle as rules
    db = tmp_path / 'paper.sqlite3'
    ledger = Ledger(db)
    AgentOrchestrator(str(db))
    runner = RuleCycleRunner(ledger, db)
    runner.v3 = SimpleNamespace(signal_b=lambda *x: None, signal_c=lambda *x: {
        'direction': 'LONG', 'stop': '99', 'setup': 'T3', 'obstacle': None})
    monkeypatch.setattr(rules, 'json_signal', lambda *x: None)
    start = datetime(2026, 10, 31, 9, 30, tzinfo=timezone.utc)
    q = dict(source='ALPACA_US_BTCUSD_SPOT', currency='USD_RESEARCH', timestamp_valid=True,
             age_ms=0, event_at=(start+timedelta(seconds=1)).isoformat(), bid='100', ask='100.1',
             bid_qty='1000', ask_qty='1000', event_id='test')
    book = dict(source=q['source'], timestamp_valid=True, age_ms=0,
                bids=[{'p':'100','s':'1000'}], asks=[{'p':'100.1','s':'1000'}])
    bars = [dict(open='100', high='101', low='99', close='100')]*288
    asyncio.run(runner.run_cycle(candles=bars, candle_close=start, quote=q, orderbook=book))
    assert ledger.account('C')['state'] == 'OPEN_LONG'
    runner.mark_time_exits(start+timedelta(minutes=165))
    assert runner._meta('C')[6] is None
    due = start+timedelta(minutes=180)
    # Execute the real closed-bar handler against isolated state, without importing
    # backend.app (which would construct the production ledger and acquire locks).
    import ast
    from pathlib import Path
    from time import monotonic
    source = ast.parse((Path(__file__).parents[1]/'backend/app.py').read_text(encoding='utf-8-sig'))
    names = {'process_closed_bar', '_closed_cycle_candles', '_valid_cycle_candle'}
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] +
                        [n for n in source.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names], type_ignores=[])
    closed = []
    for i in range(288):
        end = due-timedelta(minutes=15*(287-i))
        closed.append(dict(bars[0], close_at=end.isoformat(), open_at=(end-timedelta(minutes=15)).isoformat(),
            is_closed=True, source=q['source'], currency=q['currency'], symbol='BTC/USD', interval='15min'))
    env = dict(datetime=datetime, UTC=timezone.utc, timedelta=timedelta, monotonic=monotonic,
        paper_scheduler=SimpleNamespace(enabled=True), paper_runner=runner, RuleCycleRunner=RuleCycleRunner,
        formal_mode='RULE_DRIVEN', ledger=ledger, candles=closed,
        market_status={'feed_health':'LIVE','gap_count_288':0},
        market=SimpleNamespace(status={'websocket':'CONNECTION_LIMIT_BLOCKED','websocket_recovery_state':'LONG_PROTECTION'}),
        websocket_quote_health=lambda *a, **k:'STALE', market_open_gate_reason=market_open_gate_reason)
    exec(compile(ast.fix_missing_locations(module), '<isolated-closed-bar>', 'exec'), env)
    future = asyncio.run(env['process_closed_bar'](due, now=due-timedelta(seconds=1)))
    assert future['C']['status'] == 'CANDLE_NOT_CLOSED'
    assert runner._meta('C')[6] is None
    closed[-2]['is_closed'] = False
    incomplete = asyncio.run(env['process_closed_bar'](due, now=due))
    assert incomplete['C']['status'] == 'CLOSED_WINDOW_INCOMPLETE'
    assert runner._meta('C')[6] is None
    closed[-2]['is_closed'] = True
    blocked = asyncio.run(env['process_closed_bar'](due, now=due+timedelta(seconds=40)))
    assert all(x['status'] == 'MARKET_STREAM_DISCONNECTED' for x in blocked.values())
    runner.mark_time_exits(due)
    assert runner._meta('C')[6] == 'RULE_TIME_EXIT'
    assert market_open_gate_reason(feed_health='LIVE', gap_count=0, candle_set_valid=True,
        websocket_state='CONNECTION_LIMIT_BLOCKED', recovery_state='LONG_PROTECTION',
        websocket_quote_health_status='FRESH') == 'MARKET_STREAM_DISCONNECTED'
    runner.monitor_targets(dict(q, age_ms=60001))
    runner.monitor_targets(dict(q, timestamp_valid=False))
    assert ledger.account('C')['state'] == 'OPEN_LONG'
    runner.monitor_targets(dict(q, bid_qty='0', event_at=(due+timedelta(seconds=40)).isoformat()))
    runner.mark_time_exits(due)
    runner.monitor_targets(q)
    assert ledger.account('C')['state'] == 'FLAT'
    with sqlite3.connect(db) as c:
        assert c.execute("select count(*) from fills where agent='C' and position_effect='CLOSE'").fetchone()[0] == 1
        rows = c.execute("select payload from audit_events where kind='RULE_TIME_EXIT_DUE'").fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0][0])['due_at'] == due.isoformat()


def test_closed_bar_checks_exits_before_stream_gate():
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).parents[1]/'backend/app.py').read_text(encoding='utf-8-sig'))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'process_closed_bar')
    calls = list(ast.walk(fn))
    registration = next(n for n in calls if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'mark_time_exits')
    gate = next(n for n in calls if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'market_open_gate_reason')
    validation = next(n for n in calls if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_closed_cycle_candles')
    assert validation.lineno < registration.lineno < gate.lineno


def test_rest_market_loop_dispatches_without_websocket(tmp_path):
    import ast
    from pathlib import Path
    from backend.market import Candle, claim_closed_bar_for_dispatch
    close = datetime(2026, 10, 31, 12, 30, tzinfo=timezone.utc)
    bars = [Candle('BTC/USD', '15Min', (close-timedelta(minutes=15*(288-i))).isoformat(),
        (close-timedelta(minutes=15*(287-i))).isoformat(), '100', '101', '99', '100', '1', 1, '100', True)
        for i in range(288)]
    path = tmp_path/'.run/market-decision-watermark.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'close_at':(close-timedelta(minutes=15)).isoformat()}))
    stop = asyncio.Event()
    calls = []
    async def fetch(*args):
        return bars
    async def broadcast(*args):
        pass
    async def dispatch(value):
        calls.append(value)
        stop.set()
    market = SimpleNamespace(credentials=True, bars=fetch,
        status={'websocket':'CONNECTION_LIMIT_BLOCKED','websocket_recovery_state':'LONG_PROTECTION'},
        confirm_market_recovered=lambda **kw: False)
    source = ast.parse((Path(__file__).parents[1]/'backend/app.py').read_text(encoding='utf-8-sig'))
    loop = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'market_loop')
    env = dict(asyncio=asyncio, datetime=datetime, UTC=timezone.utc, timedelta=timedelta,
        sqlite3=sqlite3, market=market, market_status={}, stop_event=stop,
        candle_archive=SimpleNamespace(store=lambda rows:None), candle_hash=lambda rows:'test-hash',
        websocket_quote_health=lambda *a, **kw:'STREAM_UNAVAILABLE', broadcast=broadcast,
        claim_closed_bar_for_dispatch=claim_closed_bar_for_dispatch, ROOT=tmp_path,
        process_closed_bar=dispatch)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])), '<isolated-rest-loop>', 'exec'), env)
    asyncio.run(asyncio.wait_for(env['market_loop'](), timeout=2))
    assert calls == [close]
    assert env['market_status']['feed_health'] == 'LIVE'
    assert market.status['websocket_recovery_state'] == 'LONG_PROTECTION'
    assert claim_closed_bar_for_dispatch(close, path) == (False, 'CYCLE_ALREADY_SEEN')


def test_worker_exit_does_not_require_parent_exit(tmp_path):
    import os
    import subprocess
    import sys
    from backend.market import _StreamLease
    lock = tmp_path/'test-scheduler.lock'
    child = subprocess.Popen([sys._base_executable, '-u', '-c',
        "import msvcrt,time,sys; f=open(sys.argv[1],'w+b'); f.write(b'0'); f.flush(); f.seek(0); "
        "msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); "
        "print('LOCK_HELD',flush=True); time.sleep(30)", str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'LOCK_HELD'
        parent_pid = os.getpid()
        probe = _StreamLease(lock)
        assert not probe.acquire()
        child.terminate()
        child.wait(timeout=5)
        assert os.getpid() == parent_pid  # The parent is alive, but owns no worker lease.
        assert probe.acquire()
        probe.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
