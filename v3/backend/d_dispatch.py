"""Isolated serial D dispatcher. It is disabled unless explicitly started.

A queue overflow makes D unsafe rather than silently claiming stop monitoring.
The caller must expose ``failure`` and prevent new D entries until reconciled.
"""
import asyncio


class DDispatcher:
    def __init__(self, handler, *, max_pending=1024, on_failure=None, invalidate=None):
        if max_pending <= 0:
            raise ValueError('INVALID_QUEUE_SIZE')
        self._handler = handler
        self._on_failure = on_failure
        self._invalidate = invalidate
        self._queue = asyncio.Queue(maxsize=max_pending)
        self._task = None
        self._failure_task = None
        self._closing = False
        self.failure = None

    def _fail(self, reason):
        if self.failure is not None:
            return
        self.failure = reason
        if self._invalidate is not None:
            self._invalidate()
        if self._on_failure is not None:
            async def persist_failure():
                try:
                    await asyncio.to_thread(self._on_failure, reason)
                except Exception:
                    self.failure += ':PERSIST_FAILED'
            self._failure_task = asyncio.create_task(persist_failure())

    def start(self):
        if self._task is not None:
            raise RuntimeError('D_DISPATCH_ALREADY_STARTED')
        self._task = asyncio.create_task(self._run())

    def submit(self, kind, payload):
        if self._task is None or self._task.done() or self._closing:
            return False
        if kind not in ('bar', 'quote'):
            raise ValueError('INVALID_D_EVENT')
        if self.failure and kind != 'quote':
            return False
        try:
            self._queue.put_nowait((kind, payload))
            return True
        except asyncio.QueueFull:
            self._fail('D_EVENT_QUEUE_OVERFLOW')
            return False

    async def _run(self):
        while True:
            kind, payload = await self._queue.get()
            try:
                if kind is None:
                    return
                if self.failure is not None and kind != 'quote':
                    continue
                await asyncio.to_thread(self._handler, kind, payload)
            except Exception as exc:
                self._fail('D_DISPATCH_' + type(exc).__name__)
            finally:
                self._queue.task_done()

    async def close(self):
        if self._task is None:
            if self._failure_task is not None:
                await self._failure_task
            return
        self._closing = True
        if not self._task.done():
            drain = asyncio.create_task(self._queue.join())
            done, _ = await asyncio.wait((drain, self._task), return_when=asyncio.FIRST_COMPLETED)
            if drain in done and not self._task.done():
                await self._queue.put((None, None))
                await self._task
            else:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
        self._task = None
        if self._failure_task is not None:
            await self._failure_task
