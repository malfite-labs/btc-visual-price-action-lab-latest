import asyncio
import threading
from backend.d_dispatch import DDispatcher


def test_dispatcher_is_disabled_until_started_and_serial():
    async def scenario():
        events = []
        d = DDispatcher(lambda kind, value: events.append((kind, value)))
        assert not d.submit('quote', 0)
        d.start()
        assert d.submit('bar', 1)
        assert d.submit('quote', 2)
        await asyncio.wait_for(d._queue.join(), timeout=3)
        assert events == [('bar', 1), ('quote', 2)]
        assert d.failure is None
        await d.close()
    asyncio.run(scenario())


def test_overflow_is_visible_and_blocks_subsequent_events():
    async def scenario():
        started = threading.Event()
        release = threading.Event()
        def blocked(kind, value):
            started.set()
            release.wait(2)
        d = DDispatcher(blocked, max_pending=1)
        d.start()
        assert d.submit('quote', 1)
        await asyncio.to_thread(started.wait, 2)
        assert d.submit('quote', 2)
        assert not d.submit('quote', 3)
        assert d.failure == 'D_EVENT_QUEUE_OVERFLOW'
        assert not d.submit('bar', 4)
        release.set()
        await d.close()
    asyncio.run(scenario())


def test_handler_failure_blocks_new_events():
    async def scenario():
        seen = []
        def fail(kind, value):
            if value == 1:
                raise RuntimeError('failure')
            seen.append(value)
        d = DDispatcher(fail)
        d.start()
        assert d.submit('quote', 1)
        await asyncio.wait_for(d._queue.join(), 3)
        assert d.failure == 'D_DISPATCH_RuntimeError'
        assert not d.submit('bar', 2)
        assert d.submit('quote', 3)
        await asyncio.wait_for(d._queue.join(), 3)
        assert seen == [3]
        await d.close()
    asyncio.run(scenario())


def test_close_waits_for_inflight_handler():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        processed = []
        def handler(kind, value):
            entered.set()
            release.wait(3)
            processed.append(value)
        d = DDispatcher(handler)
        d.start()
        assert d.submit('quote', 1)
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(d.close())
        await asyncio.sleep(.05)
        assert not closing.done()
        assert not d.submit('quote', 2)
        release.set()
        await asyncio.wait_for(closing, 2)
        assert processed == [1]
    asyncio.run(scenario())


def test_overflow_calls_failure_hook_once_and_persists_protection(tmp_path):
    from backend.d_runtime import DRuntime
    class Archive:
        def read(self, start, end): return []
    runtime = DRuntime(tmp_path/'observation.sqlite',tmp_path/'ledger.sqlite',Archive())
    async def scenario():
        started = threading.Event()
        release = threading.Event()
        def blocked(kind, value):
            started.set()
            release.wait(3)
        d = DDispatcher(blocked, max_pending=1, on_failure=runtime.mark_stream_unavailable)
        d.start()
        assert d.submit('quote',1)
        assert await asyncio.to_thread(started.wait,2)
        assert d.submit('quote',2)
        assert not d.submit('quote',3)
        release.set()
        await d.close()
    asyncio.run(scenario())
    runtime.close()
    recovered=DRuntime(tmp_path/'observation.sqlite',tmp_path/'ledger.sqlite',Archive())
    assert recovered.status()['protection']=='UNCERTAIN'
    assert recovered.status()['reason']=='D_EVENT_QUEUE_OVERFLOW'


def test_slow_failure_persistence_never_blocks_event_loop():
    import time
    async def scenario():
        handler_started, handler_release = threading.Event(), threading.Event()
        failure_started, failure_release = threading.Event(), threading.Event()
        def blocked(kind, value):
            handler_started.set()
            handler_release.wait(3)
        def slow_persist(reason):
            failure_started.set()
            failure_release.wait(3)
        d= DDispatcher(blocked,max_pending=1,on_failure=slow_persist)
        d.start()
        assert d.submit('quote',1)
        assert await asyncio.to_thread(handler_started.wait,2)
        assert d.submit('quote',2)
        start=time.monotonic()
        assert not d.submit('quote',3)
        await asyncio.sleep(.03)
        assert time.monotonic()-start < .5
        assert failure_started.is_set()
        handler_release.set()
        failure_release.set()
        await asyncio.wait_for(d.close(),3)
        assert d.failure=='D_EVENT_QUEUE_OVERFLOW'
    asyncio.run(scenario())
