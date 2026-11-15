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
