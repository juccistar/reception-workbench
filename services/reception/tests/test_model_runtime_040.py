import asyncio
import threading
import time
import unittest

from reception_service.errors import ServiceError
from reception_service.model_providers.runtime import ModelRuntime


class ModelRuntimeTest(unittest.TestCase):
    def test_cancel_waits_for_async_cleanup(self):
        runtime = ModelRuntime()
        self.addCleanup(runtime.close)
        cancel = threading.Event()
        started, cleaned = threading.Event(), threading.Event()
        async def operation():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.05)
                cleaned.set()
        result = []
        def call():
            try:
                runtime.run('reception', operation, cancel=cancel)
            except ServiceError as exc:
                result.append(exc.code)
        thread = threading.Thread(target=call)
        thread.start()
        self.assertTrue(started.wait(2))
        cancel.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(cleaned.is_set())
        self.assertEqual(result, ['model_cancelled'])

    def test_cancel_keeps_slot_until_blocking_callback_finishes(self):
        from reception_service.model_providers.owned_sync import owned_sync
        runtime = ModelRuntime(cleanup_timeout=2)
        self.addCleanup(runtime.close)
        cancel = threading.Event()
        started, release, second_started = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def blocked():
            started.set()
            release.wait(2)
        async def first():
            await owned_sync(blocked)
        async def second():
            second_started.set()
        def run_first():
            try:
                runtime.run('reception', first, cancel=cancel)
            except ServiceError as error:
                errors.append(error.code)
        a = threading.Thread(target=run_first)
        b = threading.Thread(target=lambda: runtime.run('reception', second))
        a.start()
        try:
            self.assertTrue(started.wait(2))
            cancel.set()
            b.start()
            self.assertFalse(second_started.wait(.15))
        finally:
            release.set()
        a.join(3)
        b.join(3)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(errors, ['model_cancelled'])
        self.assertTrue(second_started.is_set())

    def test_timeout_notifies_once_after_worker_input_and_budget_release(self):
        from reception_service.model_providers.owned_sync import owned_sync
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        order = []
        class Budget:
            def __init__(self, directory=None):
                pass
            async def acquire(self, purpose, heavy):
                order.append('acquire')
                return object()
            def release(self, token):
                order.append('budget_release')
        runtime = ModelRuntime(cleanup_timeout=.1, budget_factory=Budget)
        self.addCleanup(runtime.close)
        cancel = threading.Event()
        errors = []
        async def operation():
            def block():
                started.set()
                release.wait(5)
            await owned_sync(block)
        def notify():
            order.append('finished')
            finished.set()
        def run():
            try:
                runtime.run('reception', operation, cancel=cancel,
                            on_complete=lambda: order.append('inputs'), on_finished=notify)
            except ServiceError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            cancel.set()
            thread.join(2)
            self.assertEqual(errors, ['model_cleanup_timeout'])
            self.assertEqual(order, ['acquire'])
            self.assertFalse(finished.is_set())
        finally:
            release.set()
        self.assertTrue(finished.wait(3))
        self.assertEqual(order, ['acquire', 'inputs', 'budget_release', 'finished'])

    def test_each_purpose_has_one_slot(self):
        runtime = ModelRuntime()
        self.addCleanup(runtime.close)
        first_started, release, second_started = threading.Event(), threading.Event(), threading.Event()
        async def first():
            first_started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        async def second():
            second_started.set()
        a = threading.Thread(target=lambda: runtime.run('reception', first))
        b = threading.Thread(target=lambda: runtime.run('reception', second))
        a.start()
        self.assertTrue(first_started.wait(2))
        b.start()
        self.assertFalse(second_started.wait(0.1))
        release.set()
        a.join(2)
        b.join(2)
        self.assertTrue(second_started.is_set())
        self.assertFalse(a.is_alive() or b.is_alive())


if __name__ == '__main__':
    unittest.main()
