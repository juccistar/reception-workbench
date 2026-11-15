"""Actual transport boundaries govern durable model attempts, including late cleanup."""
import asyncio
import json
import threading
import time
import unittest

import httpx

from reception_service.errors import ServiceError
from reception_service.model_attempts import AttemptLifecycle, claim
from reception_service.model_providers.runtime import ModelRuntime
from reception_service.model_providers.transport import ProviderTransport
from reception_service.repository import Repository
from reception_service.translation_provider import RouterTranslator
import test_model_generation_040 as generation_fixture


def response(output=None):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps({'ok': True} if output is None else output)}}]})


class AttemptLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = generation_fixture.GenerationTest('test_generation_image_evidence_and_before_send')
        self.fixture.setUp()
        self.repo = Repository(self.fixture.path.parent / 'attempts.sqlite3', 'seller')
        # Cleanup order must wait for real model tasks before closing their ledger.
        self.addCleanup(self.fixture.temp.cleanup)
        self.addCleanup(self.repo.close)
        self.addCleanup(self.fixture.router.close)
        self.router = self.fixture.router
        self.calls = 0
        self.deadlines = []

    def lifecycle(self, gate=None, completed=None, purpose='reception'):
        tokens = {}
        def start():
            if gate:
                gate()
            tokens.update(claim(self.repo, seller_id='seller', purpose=purpose, channel='fixture',
                                source={'message_id': 'original'}))
        class Observed(AttemptLifecycle):
            def complete(inner, *args, **kwargs):
                try:
                    return super().complete(*args, **kwargs)
                finally:
                    if completed:
                        completed.set()
        return Observed(self.repo, start, tokens)

    def transport(self, handler):
        async def send(request):
            self.calls += 1
            self.deadlines.append(self.repo.list('model_attempt_chain_v1')[0]['deadline_ms'])
            result = handler(request)
            return await result if hasattr(result, '__await__') else result
        self.router._transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(send))

    def row(self):
        rows = self.repo.list('model_attempt_chain_v1')
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_late_claim_after_cleanup_timeout_is_finished_and_zero_http_refunded(self):
        self.router._runtime = ModelRuntime(cleanup_timeout=.03)
        entered, release, cancel, finished = (threading.Event() for _ in range(4))
        def gate():
            entered.set()
            release.wait(5)
        self.transport(lambda request: response())
        lifecycle = self.lifecycle(gate, finished)
        errors = []
        def invoke():
            try:
                self.router.generate('reception', 'rules', {}, cancel=cancel, on_call_start=lifecycle)
            except ServiceError as exc:
                lifecycle.fallback('unknown')
                errors.append(exc.code)
        thread = threading.Thread(target=invoke)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            cancel.set()
            thread.join(2)
            self.assertEqual(errors, ['model_cleanup_timeout'])
            self.assertEqual(self.repo.list('model_attempt_chain_v1'), [])
        finally:
            release.set()
            thread.join(2)
        self.assertTrue(finished.wait(2))
        self.assertEqual(self.calls, 0)
        self.assertEqual((self.row()['attempts'], self.row()['status']), (0, 'unsubmitted'))
        self.router.generate('reception', 'rules', {}, on_call_start=self.lifecycle())
        self.assertEqual((self.calls, self.row()['attempts'], self.row()['status']), (1, 1, 'completed'))

    def test_explicit_429_retries_once_and_preserves_deadline(self):
        self.transport(lambda request: httpx.Response(429, headers={'retry-after': '0'}, json={'error': 'limited'})
                       if self.calls == 1 else response())
        result = self.router.generate('reception', 'rules', {}, on_call_start=self.lifecycle())
        self.assertEqual(result['output'], {'ok': True})
        self.assertEqual((self.calls, self.row()['attempts'], self.row()['status']), (2, 2, 'completed'))
        self.assertEqual(len(set(self.deadlines)), 1)

    def test_repeated_429_never_exceeds_two_outbound_requests(self):
        self.transport(lambda request: httpx.Response(429, headers={'retry-after': '0'}))
        for _ in range(2):
            with self.assertRaises(ServiceError):
                self.router.generate('reception', 'rules', {}, on_call_start=self.lifecycle())
        self.assertEqual((self.calls, self.row()['attempts'], self.row()['status']), (2, 2, 'unsubmitted'))

    def test_unknown_network_result_is_not_replayed(self):
        def lost(request):
            raise httpx.ReadError('connection lost after request', request=request)
        self.transport(lost)
        for _ in range(2):
            with self.assertRaises(ServiceError):
                self.router.generate('reception', 'rules', {}, on_call_start=self.lifecycle())
        self.assertEqual((self.calls, self.row()['attempts'], self.row()['status']), (1, 1, 'unknown'))

    def test_retry_after_outside_deadline_does_not_send_or_reset_on_new_job(self):
        self.transport(lambda request: httpx.Response(429, headers={'retry-after': '1'}))
        with self.assertRaises(ServiceError) as limited:
            self.router.generate('reception', 'rules', {}, on_call_start=self.lifecycle(), deadline=time.monotonic() + .1)
        self.assertEqual(limited.exception.code, 'model_rate_limited')
        deadline = self.row()['deadline_ms']
        with self.assertRaises(ServiceError) as still_limited:
            self.router.generate('reception', 'rules', {}, on_call_start=self.lifecycle(), deadline=time.monotonic() + 180)
        self.assertEqual(still_limited.exception.code, 'model_rate_limited')
        self.assertEqual(self.calls, 1)
        threading.Event().wait(.12)
        with self.assertRaises(ServiceError) as expired:
            self.router.generate('reception', 'rules', {}, on_call_start=self.lifecycle(), deadline=time.monotonic() + 180)
        self.assertEqual(expired.exception.code, 'model_attempt_expired')
        self.assertEqual((self.calls, self.row()['deadline_ms']), (1, deadline))

    def test_translation_uses_the_same_bounded_429_lifecycle(self):
        self.transport(lambda request: httpx.Response(429, headers={'retry-after': '0'}) if self.calls == 1
                       else response({'items': [{'id': 't1', 'text': '你好'}]}))
        translated = RouterTranslator(self.router).translate(
            [{'key': 'part', 'text': 'Hello', 'speaker': 'buyer', 'context': []}], 'job',
            on_call_start=self.lifecycle(purpose='translation'))
        self.assertEqual(translated['items'], [{'key': 'part', 'text': '你好'}])
        self.assertEqual(self.calls, 2)
        self.assertLessEqual(self.row()['deadline_ms'], int(time.time() * 1000) + 30000)

    def test_configuration_change_in_admission_refunds_zero_http_claim(self):
        self.transport(lambda request: response())
        def configure():
            self.fixture.settings['profiles'][0]['config_revision'] += 1
            self.fixture.settings['profiles'][0]['model'] = 'changed'
            self.fixture.configure()
        with self.assertRaises(ServiceError):
            self.router.generate('reception', 'rules', {}, on_call_start=self.lifecycle(configure))
        self.assertEqual((self.calls, self.row()['attempts'], self.row()['status']), (0, 0, 'unsubmitted'))

    def test_shutdown_during_late_admission_still_finishes_ledger(self):
        self.router._runtime = ModelRuntime(cleanup_timeout=.03)
        entered, release, finished = (threading.Event() for _ in range(3))
        def gate():
            entered.set()
            release.wait(5)
        self.transport(lambda request: response())
        lifecycle = self.lifecycle(gate, finished)
        errors = []
        def invoke():
            try:
                self.router.generate('reception', 'rules', {}, on_call_start=lifecycle)
            except ServiceError as exc:
                lifecycle.fallback('unknown')
                errors.append(exc.code)
        thread = threading.Thread(target=invoke)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ServiceError) as closing:
                self.router.close()
            self.assertEqual(closing.exception.code, 'model_cleanup_timeout')
        finally:
            release.set()
            thread.join(2)
        self.assertTrue(finished.wait(2))
        self.assertTrue(errors)
        self.assertEqual((self.calls, self.row()['attempts'], self.row()['status']), (0, 0, 'unsubmitted'))

    def test_cancelled_finalizer_waiting_for_worker_still_runs(self):
        from reception_service.model_providers import owned_sync as module
        completed = threading.Event()
        async def run():
            held = 0
            try:
                for _ in range(module._WORKERS):
                    self.assertTrue(module._slots.acquire(blocking=False))
                    held += 1
                task = asyncio.create_task(module.owned_sync(completed.set, complete_on_cancel=True))
                await asyncio.sleep(.02)
                task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
            finally:
                for _ in range(held):
                    module._slots.release()
            await asyncio.wait_for(task, 1)
        asyncio.run(run())
        self.assertTrue(completed.is_set())

    def test_5xx_does_not_prove_non_submission(self):
        self.transport(lambda request: httpx.Response(503))
        with self.assertRaises(ServiceError):
            self.router.generate('reception', 'rules', {}, on_call_start=self.lifecycle())
        self.assertEqual((self.calls, self.row()['status']), (1, 'unknown'))


if __name__ == '__main__':
    unittest.main()
