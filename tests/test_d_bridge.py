"""End-to-end D orchestration tests: isolated SQLite, supplied market events only."""
import asyncio
from datetime import datetime, timedelta, timezone

from backend.d_bridge import DBridge
from backend.d_signal import evaluate


class Archive:
    def __init__(self, bars): self.bars = bars
    def read(self, start, end):
        return [b for b in self.bars if start <= datetime.fromisoformat(b['open_at']) < end]


def bars_with_real_signal():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    previous = 100.0
    for index in range(961):
        close = 100 if index < 900 else 100 + index - 900 if index < 930 else 130 - .2 * (index - 930)
        opening = previous
        high = max(opening, close) + .7
        low = min(opening, close) - .7
        volume = 100
        if index == 959:
            opening, low, high, close, volume = 124, 122, 124.5, 124.3, 200
        if index == 960:
            opening, low, high, close, volume = 124.2, 124, 126, 125.95, 200
        opening += 1000; high += 1000; low += 1000; close += 1000
        at = start + timedelta(minutes=15 * index)
        rows.append({'symbol':'BTC/USD','source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH',
                     'interval':'15Min','open':str(opening),'high':str(high),'low':str(low),
                     'close':str(close),'volume':str(volume),'is_closed':True,
                     'open_at':at.isoformat(),'close_at':(at+timedelta(minutes=15)).isoformat()})
        previous = close - 1000
    return rows


def quote(at,bid,ask):
    return {'symbol':'BTC/USD','source':'ALPACA_US_BTCUSD_SPOT','currency':'USD_RESEARCH',
            'event_at':at.isoformat(),'received_at':at.isoformat(),'timestamp_valid':True,
            'bid':str(bid),'ask':str(ask)}


def test_real_signal_to_open_targets_and_restarts(tmp_path):
    bars = bars_with_real_signal()
    assert evaluate(bars[:960])['status'] == 'WATCH'
    detected = evaluate(bars[:961], evaluate(bars[:960])['state'])
    assert detected['status'] == 'PENDING'
    archive = Archive(bars)
    candidate_close = datetime.fromisoformat(bars[959]['close_at'])
    signal_close = datetime.fromisoformat(bars[960]['close_at'])
    current = [candidate_close + timedelta(seconds=1)]
    def clock(): return current[0]
    def make_bridge():
        return DBridge(tmp_path/'observe.sqlite',tmp_path/'trade.sqlite',archive,
                       feed_healthy=lambda: True,candles_complete=lambda: True,clock=clock)
    async def scenario():
        bridge = make_bridge()
        await bridge.start()
        assert bridge.stream_validated()
        assert bridge.submit_quote(quote(current[0],1124.29,1124.30))
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.status()['protection'] == 'READY'
        assert bridge.submit_bar(candidate_close)
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.status()['state']['phase']=='WATCH'
        current[0] = signal_close + timedelta(seconds=2)
        assert bridge.submit_bar(signal_close)
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.status()['state']['phase']=='PENDING'
        # A quote generated before confirmation cannot open even when delivered after it.
        stale_event = signal_close + timedelta(seconds=1)
        prior = quote(stale_event,1125.94,1125.95)
        prior['received_at'] = current[0].isoformat()
        assert bridge.submit_quote(prior)
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.ledger.fills()==[]
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0],1125.94,1125.95))
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.ledger.account()['state']=='OPEN_LONG'
        assert len(bridge.runtime.ledger.fills()) == 1
        await bridge.close()
        bridge = make_bridge()
        await bridge.start()
        assert bridge.stream_validated()
        assert bridge.runtime.ledger.account()['state']=='OPEN_LONG'
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0],1140,1140.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.ledger.account()['state']=='FLAT'
        assert [f['reason'] for f in bridge.runtime.ledger.fills()]==['D_ENTRY','TP1','TP2']
        await bridge.close()
        bridge=make_bridge()
        await bridge.start()
        assert bridge.stream_validated()
        assert len(bridge.runtime.ledger.fills())==3
        await bridge.close()
    asyncio.run(scenario())


def test_overflow_keeps_existing_stop_monitored_without_inventing_lost_fill(tmp_path, monkeypatch):
    import threading
    bars = bars_with_real_signal()
    candidate = datetime.fromisoformat(bars[959]['close_at'])
    signal = datetime.fromisoformat(bars[960]['close_at'])
    current = [candidate + timedelta(seconds=1)]
    complete = [True]
    def make_bridge():
        return DBridge(tmp_path/'obs.sqlite', tmp_path/'ledger.sqlite', Archive(bars),
                       feed_healthy=lambda: True, candles_complete=lambda: complete[0],
                       clock=lambda: current[0], max_pending=1)
    async def scenario():
        bridge = make_bridge()
        await bridge.start()
        assert bridge.stream_validated()
        assert bridge.submit_quote(quote(current[0], 1124, 1124.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 3)
        assert bridge.submit_bar(candidate)
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 3)
        current[0] = signal + timedelta(seconds=1)
        assert bridge.submit_bar(signal)
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 3)
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0], 1125.94, 1125.95))
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 3)
        assert bridge.runtime.ledger.account()['state'] == 'OPEN_LONG'
        entered, release = threading.Event(), threading.Event()
        original = bridge.dispatcher._handler
        def block_once(kind, payload):
            bridge.dispatcher._handler = original
            entered.set()
            assert release.wait(5)
            original(kind, payload)
        bridge.dispatcher._handler = block_once
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0], 1125, 1125.01))
        assert await asyncio.to_thread(entered.wait, 3)
        assert bridge.submit_bar(signal)
        # This lost stop-crossing quote is never treated as an executed stop.
        assert not bridge.submit_quote(quote(current[0], 1, 1.01))
        release.set()
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 5)
        if bridge.dispatcher._failure_task:
            await bridge.dispatcher._failure_task
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0], 1125, 1125.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 5)
        assert bridge.runtime.ledger.account()['state'] == 'OPEN_LONG'
        assert len(bridge.runtime.ledger.fills()) == 1
        assert bridge.runtime.status()['protection'] == 'UNCERTAIN'
        assert not bridge.submit_bar(signal)
        await bridge.close()
        bridge = make_bridge()
        await bridge.start()
        assert bridge.stream_validated()
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0], 1125, 1125.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 5)
        assert bridge.runtime.ledger.account()['state'] == 'OPEN_LONG'
        assert len(bridge.runtime.ledger.fills()) == 1
        assert bridge.runtime.status()['protection'] == 'UNCERTAIN'
        assert bridge._entry_gate.blocked.is_set()
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0], 1100, 1100.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 5)
        assert bridge.runtime.ledger.account()['state'] == 'FLAT'
        assert len(bridge.runtime.ledger.fills()) == 2
        assert bridge.runtime.status()['protection'] == 'UNCERTAIN'
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0], 1125, 1125.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 5)
        assert bridge.runtime.status()['protection'] == 'UNCERTAIN'
        assert bridge._entry_gate.blocked.is_set()
        await bridge.close()
        bridge = make_bridge()
        await bridge.start()
        assert bridge.runtime.status()['protection'] == 'UNCERTAIN'
        assert bridge.runtime.ledger.account()['state'] == 'FLAT'
        assert len(bridge.runtime.ledger.fills()) == 2
        assert bridge.stream_validated()
        pending = bridge.runtime.intents.pending
        monkeypatch.setattr(bridge.runtime.intents, 'pending', lambda: [{'status':'PENDING'}])
        for seconds in (1, 6, 11):
            current[0] += timedelta(seconds=5 if seconds > 1 else 1)
            assert bridge.submit_quote(quote(current[0], 1125, 1125.01))
            await asyncio.wait_for(bridge.dispatcher._queue.join(), 5)
            assert bridge.runtime.status()['protection'] == 'UNCERTAIN'
        monkeypatch.setattr(bridge.runtime.intents, 'pending', pending)
        complete[0] = False
        current[0] += timedelta(seconds=5)
        assert bridge.submit_quote(quote(current[0], 1125, 1125.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 5)
        assert bridge.runtime.status()['protection'] == 'UNCERTAIN'
        complete[0] = True
        from backend import d_runtime
        original_pending_quote = d_runtime.pending_verified_quote
        monkeypatch.setattr(d_runtime, 'pending_verified_quote', lambda *a, **kw: {'status':'BLOCKED'})
        current[0] += timedelta(seconds=5)
        assert bridge.submit_quote(quote(current[0], 1125, 1125.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 5)
        assert bridge.runtime.status()['protection'] == 'UNCERTAIN'
        monkeypatch.setattr(d_runtime, 'pending_verified_quote', original_pending_quote)
        for _ in range(2):
            current[0] += timedelta(seconds=5)
            assert bridge.submit_quote(quote(current[0], 1125, 1125.01))
            await asyncio.wait_for(bridge.dispatcher._queue.join(), 5)
        assert bridge.runtime.status()['protection'] == 'READY'
        assert not bridge._entry_gate.blocked.is_set()
        import sqlite3
        with sqlite3.connect(tmp_path/'obs.sqlite') as db:
            audit = db.execute('SELECT reason,trade_id,close_fill_id,quote_count,history_verified FROM d_gap_recovery_audit').fetchall()
        assert len(audit) == 1 and audit[0][0] == 'D_EVENT_QUEUE_OVERFLOW'
        assert audit[0][1] and audit[0][2] and audit[0][3] == 3 and audit[0][4] == 0
        await bridge.close()
    asyncio.run(scenario())


def test_watchdog_detects_missing_feed_without_quote(tmp_path):
    current = [True]
    async def scenario():
        bridge=DBridge(tmp_path/'obs.sqlite',tmp_path/'trade.sqlite',Archive([]),
                       feed_healthy=lambda:current[0],candles_complete=lambda:True)
        await bridge.start()
        assert bridge.stream_validated()
        current[0]=False
        await asyncio.sleep(1.2)
        assert bridge.runtime.status()['protection']=='UNCERTAIN'
        assert bridge.runtime.status()['reason']=='MARKET_STREAM_UNAVAILABLE'
        await bridge.close()
    asyncio.run(scenario())


def test_status_exposes_missing_midnight_and_loss_streak_pause(tmp_path, monkeypatch):
    from backend import d_circuit_replay
    now = datetime.now(timezone.utc).replace(microsecond=0)
    async def scenario():
        b = DBridge(tmp_path/'obs.sqlite',tmp_path/'ledger.sqlite',Archive([]),
                    feed_healthy=lambda:True,candles_complete=lambda:True,clock=lambda:now)
        await b.start(); assert b.stream_validated()
        original = d_circuit_replay.proven_flat_midnight_equity
        def missing(*args): raise RuntimeError('D_MIDNIGHT_POSITION_UNVALUED')
        monkeypatch.setattr(d_circuit_replay,'proven_flat_midnight_equity',missing)
        q=quote(now,100,100.01)
        assert b.submit_quote(q)
        await asyncio.wait_for(b.dispatcher._queue.join(),3)
        status=await b.status(q)
        assert status['execution_ready'] is False
        assert status['reason']=='D_MIDNIGHT_POSITION_UNVALUED'
        monkeypatch.setattr(d_circuit_replay,'proven_flat_midnight_equity',original)
        for index in range(3):
            assert b.runtime.circuit.record_closed_trade(trade_id=f'test-{index}',net_pnl='-1',
                closed_at=now-timedelta(seconds=3-index))['accepted']
        status=await b.status(q)
        assert status['execution_ready'] is False
        assert status['reason']=='LOSS_STREAK_PAUSE'
        await b.close()
    asyncio.run(scenario())


def test_crash_before_overflow_persistence_latches_existing_position(tmp_path):
    import subprocess, sys, textwrap
    script = textwrap.dedent('''
        import asyncio, os, sys, threading
        from datetime import datetime, timezone, timedelta
        from pathlib import Path
        from backend.d_bridge import DBridge
        from backend.d_executor import enqueue_verified_quote
        from test_d_bridge import Archive, quote
        root=Path(sys.argv[1]); now=datetime.now(timezone.utc).replace(microsecond=0)
        async def run():
            b=DBridge(root/'obs.sqlite',root/'ledger.sqlite',Archive([]),feed_healthy=lambda:True,candles_complete=lambda:True,clock=lambda:now,max_pending=1)
            await b.start()
            q=quote(now,100,100.01); q['event_id']='entry'
            assert enqueue_verified_quote(b.runtime.ledger,q,now)['accepted']
            close=(now.replace(minute=now.minute//15*15,second=0)+timedelta(minutes=15)).isoformat()
            assert b.runtime.ledger.open_position(quantity='1',price='100',stop='90',leverage='5',idempotency_key='entry',expected_version=0,signal_id='s',atr='5',entry_bar_close=close,quote_event_id='entry')['accepted']
            assert b.runtime.ledger.complete_quote('entry')['accepted']
            entered=threading.Event(); persist_started=threading.Event(); never=threading.Event()
            def blocked(kind,value): entered.set(); never.wait(30)
            def blocked_persist(reason): persist_started.set(); never.wait(30)
            b.dispatcher._handler=blocked; b.dispatcher._on_failure=blocked_persist
            assert b.submit_quote(quote(now,100,100.01))
            assert await asyncio.to_thread(entered.wait,3)
            assert b.submit_bar(now)
            assert not b.submit_quote(quote(now,89,89.01))
            assert await asyncio.to_thread(persist_started.wait,3)
            os._exit(0)
        asyncio.run(run())
    ''')
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            env={**__import__('os').environ, 'PYTHONPATH': 'tests'},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
    async def recover():
        bridge = DBridge(tmp_path/'obs.sqlite', tmp_path/'ledger.sqlite', Archive([]),
                         feed_healthy=lambda: True, candles_complete=lambda: True,
                         clock=lambda: now)
        await bridge.start()
        assert bridge.stream_validated()
        assert bridge.submit_quote(quote(now,100,100.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.ledger.account()['state']=='OPEN_LONG'
        assert len(bridge.runtime.ledger.fills()) == 1
        assert bridge.runtime.status()['reason']=='RESTART_POSITION_PRICE_GAP'
        assert bridge._entry_gate.blocked.is_set()
        later=now+timedelta(seconds=1)
        bridge.clock=lambda:later
        assert bridge.submit_quote(quote(later,89,89.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.ledger.account()['state']=='FLAT'
        assert len(bridge.runtime.ledger.fills())==2
        assert bridge.runtime.status()['protection']=='UNCERTAIN'
        assert bridge._entry_gate.blocked.is_set()
        await bridge.close()
        bridge = DBridge(tmp_path/'obs.sqlite', tmp_path/'ledger.sqlite', Archive([]),
                         feed_healthy=lambda: True, candles_complete=lambda: True,
                         clock=lambda: later)
        await bridge.start()
        assert bridge.stream_validated()
        # Replay three distinct old quotes spanning ten seconds after the close.
        for seconds in (12, 7, 2):
            old = quote(later-timedelta(seconds=seconds),100,100.01)
            old['received_at'] = later.isoformat()
            assert bridge.submit_quote(old)
            await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.status()['protection']=='UNCERTAIN'
        for seconds in (1, 6):
            at=later+timedelta(seconds=seconds); bridge.clock=lambda:at
            assert bridge.submit_quote(quote(at,100,100.01))
            await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        await bridge.close()
        bridge = DBridge(tmp_path/'obs.sqlite', tmp_path/'ledger.sqlite', Archive([]),
                         feed_healthy=lambda: True, candles_complete=lambda: True,
                         clock=lambda: later+timedelta(seconds=6))
        await bridge.start(); assert bridge.stream_validated()
        # These two previously accepted quotes cannot count again on restart.
        for seconds in (1, 6):
            at=later+timedelta(seconds=seconds)
            replay=quote(at,100,100.01)
            replay['received_at']=(later+timedelta(seconds=6)).isoformat()
            assert bridge.submit_quote(replay)
            await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.status()['protection']=='UNCERTAIN'
        assert bridge._entry_gate.blocked.is_set()
        await bridge.close()
    asyncio.run(recover())



def test_fault_ordering_around_entry_transaction(tmp_path, monkeypatch):
    import threading
    from backend import d_runtime, d_signal
    from test_d_runtime import fixture, Archive as ShortArchive, quote as make_quote

    async def run_case(name, phase):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        signal, bar = fixture(now)
        close = datetime.fromisoformat(signal['signal_close'])
        current = [close - timedelta(seconds=2)]
        bridge = DBridge(tmp_path/(name+'obs.sqlite'), tmp_path/(name+'trade.sqlite'), ShortArchive(bar),
                         feed_healthy=lambda: True, candles_complete=lambda: True,
                         clock=lambda: current[0], max_pending=1)
        await bridge.start()
        assert bridge.stream_validated()
        assert bridge.submit_quote(make_quote(current[0], name+'validation', 100, 100.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 3)
        current[0] = close + timedelta(seconds=1)
        assert bridge.submit_bar(close)
        await asyncio.wait_for(bridge.dispatcher._queue.join(), 3)
        entered, release = threading.Event(), threading.Event()
        target = 'handoff_observed_intent' if phase == 'handoff' else 'process_pending_entry'
        original = getattr(d_runtime, target)
        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        if phase == 'commit':
            def commit_inside_lock(db):
                with bridge._entry_gate.lock:
                    if bridge._entry_gate.blocked.is_set(): return False
                    entered.set()
                    assert release.wait(5)
                    db.commit()
                    return True
            monkeypatch.setattr(bridge._entry_gate, 'commit', commit_inside_lock)
        else:
            monkeypatch.setattr(d_runtime, target, blocked)
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(make_quote(current[0],name+'entry',100.08,100.10))
        assert await asyncio.to_thread(entered.wait, 3)
        assert bridge.submit_bar(close)
        assert not bridge.submit_bar(close)
        assert bridge.dispatcher.failure == 'D_EVENT_QUEUE_OVERFLOW'
        # Invalidation must not wait for the handler's ledger work.
        assert bridge._entry_gate.blocked.is_set()
        release.set()
        await asyncio.wait_for(bridge.dispatcher.close(), 5)
        fills = bridge.runtime.ledger.fills()
        assert len(fills) == (1 if phase == 'commit' else 0)
        assert bridge.runtime.status()['protection'] == 'UNCERTAIN'
        monkeypatch.setattr(d_runtime, target, original)
        await bridge.close()
        reopened = DRuntime(tmp_path/(name+'obs.sqlite'), tmp_path/(name+'trade.sqlite'), ShortArchive(bar))
        assert reopened.status()['protection'] == 'UNCERTAIN'
        assert len(reopened.ledger.fills()) == len(fills)
        reopened.close()

    from backend.d_runtime import DRuntime
    def candidate(bars, state):
        signal, _ = fixture(datetime.now(timezone.utc).replace(second=0,microsecond=0))
        return {'status':'PENDING','reason':'D_CONFIRM','state':{'phase':'PENDING','last_close':signal['signal_close'],'signal':signal},'signal':signal}
    monkeypatch.setattr(d_signal, 'evaluate', candidate)
    asyncio.run(run_case('before', 'handoff'))
    asyncio.run(run_case('during', 'process'))
    asyncio.run(run_case('commit', 'commit'))


def test_stream_disconnect_rearms_only_after_fresh_quote_then_later_real_signal(tmp_path):
    bars = bars_with_real_signal()
    candidate = datetime.fromisoformat(bars[959]['close_at'])
    signal = datetime.fromisoformat(bars[960]['close_at'])
    current = [candidate - timedelta(seconds=10)]
    healthy = [True]
    async def scenario():
        bridge = DBridge(tmp_path/'obs.sqlite', tmp_path/'trade.sqlite', Archive(bars),
                         feed_healthy=lambda: healthy[0], candles_complete=lambda: True,
                         clock=lambda: current[0])
        await bridge.start()
        assert bridge.stream_validated()
        assert bridge.submit_quote(quote(current[0],1124,1124.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        healthy[0] = False
        await bridge.stream_unavailable('TEST_DISCONNECT')
        assert bridge._entry_gate.blocked.is_set()
        assert not bridge.stream_validated()
        healthy[0] = True
        assert bridge.stream_validated()
        assert bridge._entry_gate.blocked.is_set()
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0],1124,1124.01))
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert not bridge._entry_gate.blocked.is_set()
        current[0] = candidate + timedelta(seconds=1)
        assert bridge.submit_bar(candidate)
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        current[0] = signal + timedelta(seconds=1)
        assert bridge.submit_bar(signal)
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        current[0] += timedelta(seconds=1)
        assert bridge.submit_quote(quote(current[0],1125.94,1125.95))
        await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
        assert bridge.runtime.ledger.account()['state'] == 'OPEN_LONG'
        await bridge.close()
    asyncio.run(scenario())
