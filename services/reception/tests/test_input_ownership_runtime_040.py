import asyncio
import base64
import hashlib
import io
import threading

from PIL import Image
import unittest

from reception_service.integrations.live import LiveIntegration
from reception_service.media_resources import PROCESS_MEDIA_BUDGET
from reception_service.model_router import ModelRouter
import test_model_generation_040 as generation_fixture

from reception_service.errors import ServiceError
from reception_service.model_providers.runtime import ModelRuntime


class InputOwnershipRuntimeTest(unittest.TestCase):
    def make_live(self, router):
        live = LiveIntegration.__new__(LiveIntegration)
        live.settings = {}
        live._model_router = router
        live._query_state = threading.local()
        return live

    def test_resolve_failure_returns_input_reservation(self):
        router = ModelRouter()
        self.addCleanup(router.close)
        live = self.make_live(router)
        before = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        with self.assertRaises(ServiceError):
            live._business_query('system', {}, [])
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], before)

    def test_successful_handoff_returns_input_and_model_reservation(self):
        fixture = generation_fixture.GenerationTest('test_generation_image_evidence_and_before_send')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        live = self.make_live(fixture.router)
        before = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        result, _ = live._business_query('system', {}, [])
        self.assertEqual(result['answer'], {'ok': True})
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], before)

    def test_oversized_text_rejected_before_transport(self):
        fixture = generation_fixture.GenerationTest('test_generation_image_evidence_and_before_send')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        live = self.make_live(fixture.router)
        before = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        with self.assertRaises(ServiceError):
            live._business_query('system', {'task_mode': 'whatsapp_reply', 'body': 'x' * 120001}, [])
        self.assertFalse(fixture.fake.calls)
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], before)

    def test_image_resolver_and_model_share_owned_input(self):
        fixture = generation_fixture.GenerationTest('test_generation_image_evidence_and_before_send')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        live = self.make_live(fixture.router)
        output = io.BytesIO()
        Image.new('RGB', (2, 2), 'white').save(output, format='PNG')
        data = output.getvalue()
        sha = hashlib.sha256(data).hexdigest()
        before = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        def resolve(_ref):
            self.assertGreater(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], before)
            return {'original_b64': base64.b64encode(data).decode(), 'mime': 'image/png', 'sha256': sha}
        live._whatsapp_image_resolver = resolve
        context = {'task_mode': 'whatsapp_reply',
                   'model_image_refs': [{'source_id': 's', 'source_version': 'v', 'role': 'buyer', 'image_id': 'i', 'sha256': sha}],
                   'whatsapp_messages': [{'source_id': 's', 'role': 'buyer', 'media': {'source_version': 'v'}}]}
        result, _ = live._business_query('system', context, [])
        self.assertEqual(result['answer'], {'ok': True})
        self.assertEqual(len(fixture.fake.calls[0]['images']), 1)
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], before)

    def test_live_timeout_keeps_reservation_until_transport_cleanup(self):
        fixture = generation_fixture.GenerationTest('test_generation_image_evidence_and_before_send')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.router._runtime = ModelRuntime(cleanup_timeout=.03)
        started, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        async def slow_transport(**kwargs):
            kwargs['before_send']()
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                while not release.is_set():
                    await asyncio.sleep(.01)
        fixture.fake.generate = slow_transport
        live = self.make_live(fixture.router)
        before = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        errors = []
        def invoke():
            try:
                live._business_query('system', {}, [], cancel_event=cancelled)
            except ServiceError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=invoke)
        thread.start()
        self.assertTrue(started.wait(2))
        cancelled.set()
        thread.join(2)
        self.assertEqual(errors, ['model_cleanup_timeout'])
        self.assertGreater(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], before)
        release.set()
        fixture.router.close()
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], before)

    def test_queued_cancellation_completes_owner(self):
        runtime = ModelRuntime(cleanup_timeout=.2)
        entered, release, cancelled, finished = (threading.Event() for _ in range(4))
        async def first():
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.01)
        first_thread = threading.Thread(target=lambda: runtime.run('reception', first))
        first_thread.start()
        self.assertTrue(entered.wait(1))
        errors = []
        def queued():
            try:
                runtime.run('reception', lambda: first(), cancel=cancelled, on_complete=finished.set)
            except ServiceError as exc:
                errors.append(exc.code)
        second_thread = threading.Thread(target=queued)
        second_thread.start()
        cancelled.set()
        second_thread.join(2)
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, ['model_cancelled'])
        self.assertTrue(finished.is_set())
        release.set()
        first_thread.join(2)
        runtime.close()

    def test_timed_out_cleanup_keeps_owner_until_task_finishes(self):
        runtime = ModelRuntime(cleanup_timeout=.03)
        entered, release, finished, cancelled = (threading.Event() for _ in range(4))
        async def operation():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                while not release.is_set():
                    await asyncio.sleep(.01)
        errors = []
        def invoke():
            try:
                runtime.run('reception', operation, cancel=cancelled, on_complete=finished.set)
            except ServiceError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=invoke)
        thread.start()
        self.assertTrue(entered.wait(1))
        cancelled.set()
        thread.join(2)
        self.assertEqual(errors, ['model_cleanup_timeout'])
        self.assertFalse(finished.is_set())
        release.set()
        self.assertTrue(finished.wait(2))
        runtime.close()
