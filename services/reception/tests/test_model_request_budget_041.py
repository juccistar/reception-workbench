import asyncio
from contextlib import closing
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.model_request_budget import ModelRequestBudget
from reception_service.model_providers.runtime import ModelRuntime
from reception_service.model_providers import checker


def _worker(directory, purpose, entered, release):
    runtime = ModelRuntime(budget_directory=directory)
    async def operation():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.02)
    try:
        runtime.run(purpose, operation, deadline=10)
    finally:
        runtime.close()


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_cross_process_purpose_and_total(self):
        ctx = multiprocessing.get_context('spawn')
        release = ctx.Event()
        events = [ctx.Event() for _ in range(3)]
        processes = [ctx.Process(target=_worker, args=(self.directory, purpose, event, release))
                     for purpose, event in zip(('reception', 'translation', 'reception'), events)]
        try:
            for process in processes[:2]:
                process.start()
            self.assertTrue(events[0].wait(8))
            self.assertTrue(events[1].wait(8))
            processes[2].start()
            self.assertFalse(events[2].wait(.5))
            release.set()
            self.assertTrue(events[2].wait(8))
            for process in processes:
                process.join(8)
                self.assertEqual(process.exitcode, 0)
        finally:
            release.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()

    def test_alive_dead_and_reused_pid(self):
        identities = {os.getpid(): 'current', 12345: 'old'}
        budget = ModelRequestBudget(self.directory, probe=lambda pid: identities.get(pid))
        with closing(sqlite3.connect(budget.path, isolation_level=None)) as db:
            db.execute('INSERT INTO requests(token,owner,pid,created,purpose,heavy,granted) VALUES(?,?,?,?,?,?,1)', ('held', 'other', 12345, 'old', 'reception', 0))
        self.assertFalse(budget._step('wait', 'reception', False, True))
        self.assertFalse(budget._step('wait', 'reception', False, False))
        identities[12345] = 'new'
        self.assertTrue(budget._step('wait', 'reception', False, False))
        budget.release('wait')
        with closing(sqlite3.connect(budget.path, isolation_level=None)) as db:
            db.execute('INSERT INTO requests(token,owner,pid,created,purpose,heavy,granted) VALUES(?,?,?,?,?,?,1)', ('dead', 'other', 12345, 'new', 'translation', 0))
        del identities[12345]
        self.assertTrue(budget._step('next', 'translation', False, True))
        budget.release('next')

    def test_owner_fairness_and_eligible_purpose(self):
        budget = ModelRequestBudget(self.directory)
        self.assertTrue(budget._step('held', 'reception', False, True))
        self.assertFalse(budget._step('a', 'reception', False, True))
        with closing(sqlite3.connect(budget.path, isolation_level=None)) as db:
            db.execute("UPDATE requests SET owner='other' WHERE token='a'")
        self.assertFalse(budget._step('b', 'reception', False, True))
        self.assertTrue(budget._step('translation', 'translation', False, True))
        budget.release('held')
        self.assertFalse(budget._step('b', 'reception', False, False))
        self.assertTrue(budget._step('a', 'reception', False, False))
        for token in ('a', 'b', 'translation'):
            budget.release(token)

    def test_queue_limit_and_cancel(self):
        budget = ModelRequestBudget(self.directory, max_queue=1)
        self.assertTrue(budget._step('held', 'reception', False, True))
        self.assertFalse(budget._step('waiting', 'reception', False, True))
        with self.assertRaises(Exception) as caught:
            budget._step('extra', 'reception', False, True)
        self.assertEqual(caught.exception.code, 'model_resource_busy')
        budget.release('waiting')
        async def cancel_wait():
            task = asyncio.create_task(budget.acquire('reception'))
            await asyncio.sleep(.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(cancel_wait())
        with closing(sqlite3.connect(budget.path, isolation_level=None)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM requests WHERE granted=0').fetchone()[0], 0)
        budget.release('held')

    def test_timeout_keeps_lease_until_cleanup_finishes(self):
        runtime = ModelRuntime(cleanup_timeout=.05, budget_directory=self.directory)
        done = threading.Event()
        async def slow():
            try:
                await asyncio.sleep(10)
            finally:
                await asyncio.sleep(.3)
                done.set()
        with self.assertRaises(Exception):
            runtime.run('reception', slow, deadline=.1)
        with closing(sqlite3.connect(Path(self.directory) / 'budget.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM requests WHERE granted=1').fetchone()[0], 1)
        self.assertTrue(done.wait(2))
        runtime.close()
        with closing(sqlite3.connect(Path(self.directory) / 'budget.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0], 0)

    def test_image_lease_remains_held_during_input_cleanup(self):
        runtime = ModelRuntime(budget_directory=self.directory)
        cleanup_entered, finish_cleanup = threading.Event(), threading.Event()
        outcomes, cleanups = [], []
        async def operation():
            return 'done'
        def cleanup():
            cleanups.append(True)
            cleanup_entered.set()
            finish_cleanup.wait(4)
        def worker():
            outcomes.append(runtime.run('reception', operation, heavy=True, deadline=8, on_complete=cleanup))
        thread = threading.Thread(target=worker)
        try:
            thread.start()
            self.assertTrue(cleanup_entered.wait(2))
            other = ModelRequestBudget(self.directory)
            self.assertFalse(other._step('next-image', 'translation', True, True))
            finish_cleanup.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, ['done'])
            self.assertEqual(cleanups, [True])
            self.assertTrue(other._step('next-image', 'translation', True, False))
            other.release('next-image')
        finally:
            finish_cleanup.set()
            thread.join(5)
            runtime.close()

    def test_cleanup_failure_releases_admission_and_reaches_caller(self):
        runtime = ModelRuntime(budget_directory=self.directory)
        async def operation():
            return 'done'
        def cleanup():
            raise ValueError('synthetic cleanup failure')
        try:
            with self.assertRaisesRegex(ValueError, 'synthetic cleanup failure'):
                runtime.run('reception', operation, on_complete=cleanup, deadline=2)
            with closing(sqlite3.connect(Path(self.directory) / 'budget.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
        finally:
            runtime.close()

    def test_checker_waits_for_shared_reception_slot(self):
        budget = ModelRequestBudget(self.directory)
        self.assertTrue(budget._step('held', 'reception', False, True))
        calls = []
        class Transport:
            async def generate(self, **kwargs):
                calls.append(True)
                return {}
            async def aclose(self):
                pass
        packet = {'format': 'model-check-v1', 'purpose': 'reception', 'api_key': 'x', 'profile': {'profile_id': 'x', 'protocol': 'chat-completions', 'base_url': 'https://example.invalid', 'model': 'x', 'config_revision': 1, 'credential_revision': 1}}
        try:
            with patch.dict(os.environ, {'RECEPTION_MODEL_BUDGET_DIRECTORY': self.directory}):
                result = checker.run_check(packet, transport=Transport(), deadline=.2)
            self.assertEqual(result['code'], 'model_timeout')
            self.assertEqual(calls, [])
        finally:
            budget.release('held')

    def test_checker_marks_images_heavy(self):
        calls = []
        class Runtime:
            def run(self, purpose, operation, **kwargs):
                calls.append(kwargs['heavy'])
                return {}
            def close(self, finalizer):
                pass
        packet = {'format': 'model-check-v1', 'purpose': 'reception', 'api_key': 'x', 'profile': {'profile_id': 'x', 'protocol': 'chat-completions', 'base_url': 'https://example.invalid', 'model': 'x', 'config_revision': 1, 'credential_revision': 1}}
        with patch.object(checker, 'ModelRuntime', Runtime):
            checker.run_check(packet, transport=object())
        self.assertEqual(calls, [False, False, True, True, True, True, True])


if __name__ == '__main__':
    unittest.main()
