import os
import subprocess
import sys
from pathlib import Path
import pytest


def test_app_bridge_routes_market_without_additional_connection(tmp_path):
    """Use a fresh interpreter and isolated databases, never the running ledger."""
    code = r'''
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from backend import app

class Bridge:
    def __init__(self):
        self.bars = []
        self.quotes = []
        self.failures = []
    def submit_bar(self, value):
        self.bars.append(value)
        return True
    def submit_quote(self, value):
        self.quotes.append(value)
        return True
    def fail_dispatch(self, reason):
        self.failures.append(reason)
    async def status(self, quote):
        return {'status':'AUTO_PAPER', 'execution_ready':True}

async def main():
    bridge=Bridge()
    app.d_bridge=bridge
    app._e_scheduler_started_at=datetime.now(timezone.utc)-timedelta(minutes=1)
    close=datetime.now(timezone.utc)
    app._schedule_d_market_from_loop(close)
    assert bridge.bars == [close]
    seen=[]
    async def stops(quote): seen.append(('stop', quote['symbol']))
    async def broadcast(value): seen.append(('broadcast', value['type']))
    app.process_stop_quote=stops
    app.broadcast=broadcast
    event={'T':'q','S':'BTC/USD','source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'t':datetime.now(timezone.utc).isoformat(),'bp':'100','ap':'101','bs':'1','as':'1'}
    await app.stream_events(event)
    assert seen == [('stop','BTC/USD'),('broadcast','market_event')]
    assert len(bridge.quotes)==1 and bridge.quotes[0]['symbol']=='BTC/USD'
    assert (await app.get_d_status())['execution_ready'] is True
    def fail(_): raise RuntimeError('isolated dispatch error')
    bridge.submit_quote=fail
    await app.stream_events(event)
    assert seen[-1] == ('broadcast','market_event')
    assert bridge.failures == ['D_QUOTE_SUBMIT_FAILED']
    bridge.submit_quote=lambda value: bridge.quotes.append(value) or True
    before=len(bridge.quotes)
    for mutation in ({'S':'ETH/USD'}, {'timestamp_valid':False}, {'source':'OTHER'}, {'bp':'nan'}):
        await app.stream_events({**event, **mutation})
        assert len(bridge.quotes)==before
        assert bridge.failures[-1]=='D_RAW_QUOTE_INVALID'
    bridge.submit_bar=lambda _:False
    app._schedule_d_market_from_loop(close)
    assert bridge.failures[-1] == 'D_BAR_SUBMIT_FAILED'
    bridge.submit_quote=lambda _:False
    await app.stream_events(event)
    assert bridge.failures[-1] == 'D_QUOTE_SUBMIT_FAILED'

asyncio.run(main())
'''
    env = os.environ.copy()
    env['BTC_LAB_DB'] = str(tmp_path / 'paper.sqlite3')
    env['BTC_LAB_CANDLE_DB'] = str(tmp_path / 'candles.sqlite3')
    env['D_AUTO_PAPER_ENABLED'] = 'false'
    proc = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize('stop_during_abc_failure', [False, True])
def test_app_callbacks_drive_real_d_roundtrip_in_isolated_databases(tmp_path, stop_during_abc_failure):
    code = r'''
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, 'tests')
from test_d_bridge import Archive, bars_with_real_signal, quote
from backend import app
from backend.d_bridge import DBridge

root=Path(sys.argv[1])
stop_during_abc_failure = sys.argv[2] == 'True'
bars=bars_with_real_signal()
archive=Archive(bars)
candidate=datetime.fromisoformat(bars[959]['close_at'])
signal=datetime.fromisoformat(bars[960]['close_at'])
current=[candidate+timedelta(seconds=1)]
class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None): return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)
app.datetime=FakeDatetime
app._e_scheduler_started_at=candidate-timedelta(minutes=1)
async def no_op(*args): pass
app.process_stop_quote=no_op
app.broadcast=no_op
async def event(bridge, bid, ask):
    event={'T':'q','S':'BTC/USD','source':'BINANCE_USDM_PERP','currency':'USD_RESEARCH','timestamp_valid':True,'t':current[0].isoformat(),'bp':str(bid),'ap':str(ask),'bs':'1','as':'1'}
    await app.stream_events(event)
    await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
async def main():
    def create():
        return DBridge(root/'observe.sqlite',root/'trade.sqlite',archive,
                       feed_healthy=lambda:True,candles_complete=lambda:True,clock=lambda:current[0])
    bridge=create()
    await bridge.start()
    app.d_bridge=bridge
    assert bridge.stream_validated()
    await event(bridge,1124.29,1124.30)
    app._schedule_d_market_from_loop(candidate)
    await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
    assert bridge.runtime.status()['state']['phase']=='WATCH'
    current[0]=signal+timedelta(seconds=2)
    app._schedule_d_market_from_loop(signal)
    await asyncio.wait_for(bridge.dispatcher._queue.join(),3)
    assert bridge.runtime.status()['state']['phase']=='PENDING'
    current[0]+=timedelta(seconds=1)
    await event(bridge,1125.94,1125.95)
    assert bridge.runtime.ledger.account()['state']=='OPEN_LONG'
    await bridge.close()
    bridge=create()
    await bridge.start()
    app.d_bridge=bridge
    assert bridge.stream_validated()
    current[0]+=timedelta(seconds=1)
    if stop_during_abc_failure:
        async def fail_abc(*args): raise RuntimeError('ABC isolated failure')
        app.process_stop_quote=fail_abc
        await event(bridge,1100,1100.01)
        expected=['D_ENTRY','STOP_DUE']
    else:
        await event(bridge,1140,1140.01)
        expected=['D_ENTRY','TP1','TP2']
    assert bridge.runtime.ledger.account()['state']=='FLAT'
    assert [x['reason'] for x in bridge.runtime.ledger.fills()] == expected
    await bridge.close()
    bridge=create()
    await bridge.start()
    assert len(bridge.runtime.ledger.fills())==len(expected)
    await bridge.close()
asyncio.run(main())
'''
    env = os.environ.copy()
    env['BTC_LAB_DB'] = str(tmp_path / 'app-paper.sqlite3')
    env['BTC_LAB_CANDLE_DB'] = str(tmp_path / 'app-candles.sqlite3')
    env['D_AUTO_PAPER_ENABLED'] = 'false'
    proc = subprocess.run([sys.executable, '-c', code, str(tmp_path), str(stop_during_abc_failure)],
                          cwd=Path(__file__).resolve().parents[1], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_slow_d_initialization_does_not_block_other_tasks(tmp_path):
    code = r'''
import asyncio, time
import backend.d_paper as d_paper
from backend import app
started = []
def slow_init(_path): time.sleep(0.6)
d_paper.initialize_store = slow_init
class Market:
    credentials = ('test', 'test')
    async def websocket(self, *_):
        started.append('ws')
        await asyncio.Event().wait()
app.market = Market()
async def task(name):
    started.append(name)
    await asyncio.Event().wait()
app.market_loop = lambda: task('market')
app.quote_loop = lambda: task('quote')
async def main():
    before = asyncio.get_running_loop().time()
    async with app.lifespan(app.app):
        await asyncio.sleep(0.05)
        assert set(started) == {'market', 'quote', 'ws'}
        assert asyncio.get_running_loop().time() - before < 0.5
asyncio.run(main())
'''
    env = os.environ.copy()
    env.update(BTC_LAB_DB=str(tmp_path / 'paper.sqlite3'),
               BTC_LAB_CANDLE_DB=str(tmp_path / 'candles.sqlite3'),
               PAPER_AUTOMATION_ENABLED='false', D_AUTO_PAPER_ENABLED='false')
    proc = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr

