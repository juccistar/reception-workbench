"""Run blocking model callbacks without losing ownership on cancellation."""
import asyncio
import concurrent.futures
import threading

_WORKERS = 4
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix='model-callback')
_slots = threading.BoundedSemaphore(_WORKERS)


async def owned_sync(callback, *, complete_on_cancel=False):
    """Retain real worker ownership; durable finalizers cannot be skipped by cancel."""
    # Do not queue unlimited work, including across independent event loops.
    while not _slots.acquire(blocking=False):
        try:
            await asyncio.sleep(.01)
        except asyncio.CancelledError:
            if not complete_on_cancel:
                raise
    try:
        worker = _executor.submit(callback)
    except BaseException:
        _slots.release()
        raise
    worker.add_done_callback(lambda _done: _slots.release())
    future = asyncio.wrap_future(worker)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # Shield preserves the real future, including across repeated cancellation
        # during shutdown. Its result is discarded after the worker exits.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if complete_on_cancel:
            return future.result()
        if future.done() and not future.cancelled():
            future.exception()
        raise
