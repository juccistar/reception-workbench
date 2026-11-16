import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.model_providers.runtime import ModelRuntime


class ModelRuntimeTest(unittest.TestCase):
    def setUp(self):
        budget = tempfile.TemporaryDirectory()
        self.addCleanup(budget.cleanup)
        self.enterContext(patch('reception_service.model_request_budget._directory',
                                return_value=Path(budget.name) / 'model-budget'))

    def test_cancel_waits_for_async_cleanup(self):
        cleanup_started, allow_cleanup, cleaned = threading.Event(), threading.Event(), threading.Event()
        budget_released = threading.Event()
        class Budget:
            def __init__(self, directory=None):
                pass
            async def acquire(self, purpose, heavy):
                return object()
            def release(self, token):
                budget_released.set()
        runtime = ModelRuntime(budget_factory=Budget)
        self.addCleanup(runtime.close)
        cancel = threading.Event()
        started = threading.Event()
        async def operation():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                while not allow_cleanup.is_set():
                    await asyncio.sleep(0.01)
                cleaned.set()
        result = []
        def call():
            try:
                runtime.run('reception', operation, cancel=cancel)
            except ServiceError as exc:
                result.append(exc.code)
        thread = threading.Thread(target=call)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            cancel.set()
            self.assertTrue(cleanup_started.wait(2))
            self.assertTrue(thread.is_alive())
            self.assertFalse(budget_released.is_set())
        finally:
            allow_cleanup.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(cleaned.is_set())
        self.assertTrue(budget_released.is_set())
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
        second_admitted = threading.Event()
        class Budget:
            def __init__(self, directory=None):
                self.reception_count = 0
            async def acquire(self, purpose, heavy):
                if purpose == 'reception':
                    self.reception_count += 1
                    if self.reception_count == 2:
                        second_admitted.set()
                return object()
            def release(self, token):
                pass
        runtime = ModelRuntime(budget_factory=Budget)
        self.addCleanup(runtime.close)
        first_started, release, second_started = threading.Event(), threading.Event(), threading.Event()
        translation_started = threading.Event()
        errors = []
        async def first():
            first_started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        async def second():
            second_started.set()
        async def translation():
            translation_started.set()
        def run(purpose, operation):
            try:
                runtime.run(purpose, operation)
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=run, args=('reception', first)),
                   threading.Thread(target=run, args=('reception', second)),
                   threading.Thread(target=run, args=('translation', translation))]
        started_threads = []
        try:
            threads[0].start()
            started_threads.append(threads[0])
            self.assertTrue(first_started.wait(2), errors)
            threads[1].start()
            started_threads.append(threads[1])
            self.assertTrue(second_admitted.wait(2), errors)
            threads[2].start()
            started_threads.append(threads[2])
            self.assertTrue(translation_started.wait(2), errors)
            self.assertFalse(second_started.is_set(), 'same-purpose operation entered the occupied slot')
        finally:
            release.set()
            for thread in started_threads:
                thread.join(3)
            if any(thread.is_alive() for thread in started_threads):
                try:
                    runtime.close()
                finally:
                    for thread in started_threads:
                        thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in started_threads))
        self.assertEqual(errors, [])
        self.assertTrue(second_started.is_set())


if __name__ == '__main__':
    unittest.main()
