import asyncio,json,sys
from datetime import UTC,datetime,timedelta
from types import SimpleNamespace
from backend.market import AlpacaMarketData


def test_protected_authenticated_fresh_quote_recovers_once(monkeypatch,tmp_path):
    messages = [json.dumps([{'T':'success'}]),json.dumps([{'T':'subscription'}])]
    now=datetime.now(UTC).isoformat()
    messages.extend([json.dumps([{'T':'q','S':'BTC/USD','t':now,'bp':100,'ap':101}])]*2)
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def send(self,raw): pass
        async def recv(self): return messages.pop(0)
    monkeypatch.setitem(sys.modules,'websockets',SimpleNamespace(connect=lambda *a,**k:Socket()))
    path=tmp_path/'recovery.jsonl'; adapter=AlpacaMarketData(credentials=('test','test'),recovery_history_path=path)
    adapter.status['websocket_recovery_state']='LONG_PROTECTION'
    async def run():
        stop=asyncio.Event(); seen=[]
        async def receive(event):
            seen.append(adapter.confirm_market_recovered(quote_fresh=True,candles_complete=True,candles_gap_count=0))
            if len(seen)==2: stop.set()
        await adapter.websocket(receive,stop)
        assert seen==[True,True]
    asyncio.run(run())
    assert adapter.status['websocket_recovery_state']=='HEALTHY'
    assert [json.loads(x)['event'] for x in path.read_text().splitlines()].count('recovery_succeeded')==1
    adapter._enter_long_protection('simulated_later_disconnect')
    messages.extend([json.dumps([{'T':'success'}]),json.dumps([{'T':'subscription'}])])
    fresh=datetime.now(UTC).isoformat()
    messages.extend([json.dumps([{'T':'q','S':'BTC/USD','t':fresh,'bp':100,'ap':101}])]*2)
    asyncio.run(run())
    assert [json.loads(x)['event'] for x in path.read_text().splitlines()].count('recovery_succeeded')==2
    restored=AlpacaMarketData(credentials=('test','test'),recovery_history_path=path)
    assert restored.status['websocket_recovery_state']=='HEALTHY'


def test_stale_quote_does_not_release_protection(monkeypatch):
    messages=[json.dumps([{'T':'success'}]),json.dumps([{'T':'subscription'}]),json.dumps([{'T':'q','S':'BTC/USD','t':(datetime.now(UTC)-timedelta(minutes=5)).isoformat(),'bp':100,'ap':101}])]
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send(self,raw):pass
        async def recv(self):return messages.pop(0)
    monkeypatch.setitem(sys.modules,'websockets',SimpleNamespace(connect=lambda *a,**k:Socket()))
    adapter=AlpacaMarketData(credentials=('test','test'));adapter.status['websocket_recovery_state']='LONG_PROTECTION'
    async def run():
        stop=asyncio.Event()
        async def receive(event):
            assert not adapter.confirm_market_recovered(quote_fresh=True,candles_complete=True,candles_gap_count=0)
            stop.set()
        await adapter.websocket(receive,stop)
    asyncio.run(run());assert adapter.status['websocket_recovery_state']=='LONG_PROTECTION'


def test_unauthenticated_and_no_quote_cannot_release(monkeypatch):
    class NoAuth:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send(self,raw):pass
        async def recv(self):return json.dumps([{'T':'error','code':402}])
    monkeypatch.setitem(sys.modules,'websockets',SimpleNamespace(connect=lambda *a,**k:NoAuth()))
    adapter=AlpacaMarketData(credentials=('test','test'));adapter.status['websocket_recovery_state']='LONG_PROTECTION'
    asyncio.run(adapter.websocket(lambda e: asyncio.sleep(0),asyncio.Event()))
    assert adapter.status['websocket_recovery_state']=='LONG_PROTECTION'
    assert not adapter.confirm_market_recovered(quote_fresh=True,candles_complete=True,candles_gap_count=0)


def test_rest_fresh_without_stream_never_clears():
    adapter=AlpacaMarketData(credentials=('test','test'));adapter.status['websocket_recovery_state']='LONG_PROTECTION'
    assert not adapter.confirm_market_recovered(quote_fresh=True,candles_complete=True,candles_gap_count=0)


def test_authenticated_connection_without_quote_does_not_clear(monkeypatch):
    ready=[]
    class Socket:
        def __init__(self):self.messages=[json.dumps([{'T':'success'}]),json.dumps([{'T':'subscription'}])]
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send(self,raw):pass
        async def recv(self):
            if self.messages:return self.messages.pop(0)
            ready.append(True)
            await asyncio.Future()
    monkeypatch.setitem(sys.modules,'websockets',SimpleNamespace(connect=lambda *a,**k:Socket()))
    adapter=AlpacaMarketData(credentials=('test','test'));adapter.status['websocket_recovery_state']='LONG_PROTECTION'
    async def run():
        task=asyncio.create_task(adapter.websocket(lambda e:asyncio.sleep(0),asyncio.Event()))
        while not ready:await asyncio.sleep(0)
        assert adapter.status['websocket']=='CONNECTED'
        assert not adapter.confirm_market_recovered(quote_fresh=True,candles_complete=True,candles_gap_count=0)
        task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run());assert adapter.status['websocket_recovery_state']=='LONG_PROTECTION'


def test_new_session_cannot_reuse_previous_quote(monkeypatch):
    ready=[]
    class Socket:
        def __init__(self):self.messages=[json.dumps([{'T':'success'}]),json.dumps([{'T':'subscription'}])]
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send(self,raw):pass
        async def recv(self):
            if self.messages:return self.messages.pop(0)
            ready.append(True);await asyncio.Future()
    monkeypatch.setitem(sys.modules,'websockets',SimpleNamespace(connect=lambda *a,**k:Socket()))
    adapter=AlpacaMarketData(credentials=('test','test'))
    adapter.status.update(websocket_recovery_state='VALIDATING',last_quote={'timestamp_valid':True,'event_at':datetime.now(UTC).isoformat()},websocket_last_quote_received_at=datetime.now(UTC).isoformat())
    async def run():
        task=asyncio.create_task(adapter.websocket(lambda e:asyncio.sleep(0),asyncio.Event()))
        while not ready:await asyncio.sleep(0)
        assert adapter.status['last_quote'] is None
        assert not adapter.confirm_market_recovered(quote_fresh=True,candles_complete=True,candles_gap_count=0)
        task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_invalid_prices_do_not_clear_protection(monkeypatch):
    messages=[json.dumps([{'T':'success'}]),json.dumps([{'T':'subscription'}]),json.dumps([{'T':'q','S':'BTC/USD','t':datetime.now(UTC).isoformat(),'bp':101,'ap':100}])]
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send(self,raw):pass
        async def recv(self):return messages.pop(0)
    monkeypatch.setitem(sys.modules,'websockets',SimpleNamespace(connect=lambda *a,**k:Socket()))
    adapter=AlpacaMarketData(credentials=('test','test'));adapter.status['websocket_recovery_state']='LONG_PROTECTION'
    async def run():
        stop=asyncio.Event()
        async def receive(event):
            assert not adapter.confirm_market_recovered(quote_fresh=True,candles_complete=True,candles_gap_count=0)
            stop.set()
        await adapter.websocket(receive,stop)
    asyncio.run(run());assert adapter.status['websocket_recovery_state']=='LONG_PROTECTION'


def test_gate_allows_only_later_natural_cycle_after_recovery(tmp_path):
    from backend.market import claim_closed_bar_for_dispatch, market_open_gate_reason
    watermark=tmp_path/'watermark.json'
    close=datetime(2026,11,1,4,45,tzinfo=UTC)
    assert not claim_closed_bar_for_dispatch(close-timedelta(minutes=15),watermark)[0]
    assert claim_closed_bar_for_dispatch(close,watermark)[0]
    args=dict(feed_health='LIVE',gap_count=0,candle_set_valid=True,websocket_state='CONNECTED',websocket_quote_health_status='FRESH')
    assert market_open_gate_reason(**args,recovery_state='LONG_PROTECTION')=='MARKET_RECOVERY_NOT_VALIDATED'
    assert not claim_closed_bar_for_dispatch(close,watermark)[0]
    assert market_open_gate_reason(**args,recovery_state='HEALTHY') is None
    assert claim_closed_bar_for_dispatch(close+timedelta(minutes=15),watermark)[0]
