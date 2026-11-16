"""Persistent event loop and bounded model execution slots."""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

from .encoding import failure


class ModelRuntime:
    """A timed-out cleanup keeps its task and slots until it actually exits."""
    def __init__(self, cleanup_timeout=2, *, budget_directory=None, budget_factory=None):
        self._budget_directory = budget_directory
        self._budget_factory = budget_factory
        self._budget = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._shutdown = None
        self._stop_requested = False
        self._cleanup_timeout = cleanup_timeout
        self._thread = threading.Thread(target=self._serve, name='model-runtime', daemon=True)
        self._thread.start()
        self._ready.wait()

    def _serve(self):
        asyncio.set_event_loop(self._loop)
        self._slots = {'reception': asyncio.Semaphore(1), 'translation': asyncio.Semaphore(1)}
        self._total = asyncio.Semaphore(2)
        self._heavy = asyncio.Semaphore(1)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def run(self, purpose, operation, *, cancel=None, deadline=120, heavy=False, on_abandon=None, on_complete=None):
        if purpose not in ('reception', 'translation') or not callable(operation) or deadline <= 0:
            if on_complete is not None:
                on_complete()
            raise failure('model_invalid_input')
        end = time.monotonic() + deadline
        future = concurrent.futures.Future()
        task_ready = threading.Event()
        abandoned = threading.Event()
        task_holder = []
        inputs_completed = False
        def complete_inputs():
            nonlocal inputs_completed
            if not inputs_completed:
                inputs_completed = True
                if on_complete is not None:
                    on_complete()
        async def execute():
            token = None
            try:
                if self._budget is None:
                    from ..model_request_budget import ModelRequestBudget
                    factory = self._budget_factory or ModelRequestBudget
                    self._budget = factory(directory=self._budget_directory)
                token = await self._budget.acquire(purpose, heavy)
                async with self._slots[purpose]:
                    async with self._total:
                        if heavy:
                            async with self._heavy:
                                return await operation()
                        return await operation()
            finally:
                try:
                    complete_inputs()
                finally:
                    if token is not None:
                        self._budget.release(token)
        def start():
            if abandoned.is_set():
                if on_complete is not None:
                    on_complete()
                future.cancel()
                task_ready.set()
                return
            task = self._loop.create_task(execute())
            task_holder.append(task)
            def complete(done):
                try:
                    complete_inputs()
                except BaseException as error:
                    future.set_exception(error)
                    return
                if done.cancelled():
                    future.cancel()
                elif done.exception() is not None:
                    future.set_exception(done.exception())
                else:
                    future.set_result(done.result())
            task.add_done_callback(complete)
            task_ready.set()
        with self._lock:
            if self._closed:
                if on_complete is not None:
                    on_complete()
                raise failure('model_cancelled')
            try:
                self._loop.call_soon_threadsafe(start)
            except BaseException:
                if on_complete is not None:
                    on_complete()
                raise
        if not task_ready.wait(self._cleanup_timeout):
            abandoned.set()
            if on_abandon is not None:
                on_abandon()
            def cancel_late():
                if task_holder and not task_holder[0].done() and not task_holder[0].cancelling():
                    task_holder[0].cancel()
            self._loop.call_soon_threadsafe(cancel_late)
            raise failure('model_cleanup_timeout')
        while True:
            remaining = end - time.monotonic()
            cancelled = cancel is not None and cancel.is_set()
            if cancelled or remaining <= 0:
                if on_abandon is not None:
                    on_abandon()
                if not future.done():
                    def cancel_task():
                        task = task_holder[0]
                        if not task.done() and not task.cancelling():
                            task.cancel()
                    self._loop.call_soon_threadsafe(cancel_task)
                try:
                    future.result(timeout=self._cleanup_timeout)
                except concurrent.futures.TimeoutError:
                    raise failure('model_cleanup_timeout') from None
                except BaseException:
                    pass
                raise failure('model_cancelled' if cancelled else 'model_timeout')
            try:
                return future.result(timeout=min(0.05, remaining))
            except concurrent.futures.TimeoutError:
                continue
            except concurrent.futures.CancelledError:
                raise failure('model_cancelled') from None

    def schedule(self, operation):
        with self._lock:
            if self._closed:
                return
            def start():
                task = self._loop.create_task(operation())
                def consume(done):
                    if not done.cancelled():
                        done.exception()
                task.add_done_callback(consume)
            self._loop.call_soon_threadsafe(start)

    def close(self, finalizer=None):
        with self._lock:
            if self._shutdown is None:
                self._closed = True
                async def shutdown():
                    current = asyncio.current_task()
                    tasks = [task for task in asyncio.all_tasks() if task is not current]
                    for task in tasks:
                        if not task.cancelling():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    if finalizer is not None:
                        await finalizer()
                self._shutdown = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
                # Ownership survives a caller's cleanup timeout. Stop only after
                # cancelled tasks and the finalizer have actually completed.
                self._shutdown.add_done_callback(lambda _done: self._loop.call_soon_threadsafe(self._loop.stop))
                self._stop_requested = True
            future = self._shutdown
        try:
            future.result(timeout=self._cleanup_timeout)
        except concurrent.futures.TimeoutError:
            raise failure('model_cleanup_timeout') from None
        except Exception:
            raise failure('model_cleanup_failed') from None
        with self._lock:
            if self._thread.is_alive() and not self._stop_requested:
                self._stop_requested = True
                self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(self._cleanup_timeout)
        if self._thread.is_alive():
            raise failure('model_cleanup_timeout')
