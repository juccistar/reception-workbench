import asyncio
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from reception_service.errors import ServiceError
from reception_service.model_router import ModelRouter
from reception_service.model_providers.runtime import ModelRuntime
from reception_service.model_providers.owned_sync import owned_sync


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.retired = []
    async def generate(self, **kw):
        await owned_sync(kw['before_send'])
        self.calls.append(kw)
        self.started.set()
        while self.block and not self.release.is_set():
            await asyncio.sleep(.01)
        return {'output': {'ok': True}, 'usage': {}, 'model': 'test', 'stop_reason': 'stop'}
    async def invalidate_profile(self, profile_id):
        self.retired.append(profile_id)
    async def aclose(self):
        pass


class GenerationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.json'
        capabilities = {name: {'status': 'passed', 'checked_at': 1} for name in
                        ('text', 'structure', 'single_image', 'multi_image', 'combined', 'translation')}
        self.settings = {'format': 'model-settings-v1', 'directory_revision': 1,
            'profiles': [{'profile_id': 'p', 'protocol': 'chat-completions', 'base_url': 'https://example.com',
                          'model': 'test', 'credential_ref': 'c', 'has_key': True, 'config_revision': 1,
                          'credential_revision': 1, 'capability_revision': 1, 'capabilities': capabilities}],
            'routes': {p: {'profile_id': 'p', 'route_revision': 1} for p in ('reception', 'translation')}}
        self.router = ModelRouter()
        self.addCleanup(self.router.close)
        self.fake = FakeTransport()
        self.router._transport = self.fake
        self.configure()
    def configure(self):
        self.path.write_text(json.dumps(self.settings), encoding='utf8')
        return self.router.configure({'format': 'model-runtime-v1', 'authority_file': str(self.path),
                                      'settings': self.settings, 'credentials': {'p': 'secret'}})
    def test_callback_does_not_hold_router_lock_during_configuration(self):
        business_lock = threading.Lock()
        entered = threading.Event()
        errors = []
        def callback():
            entered.set()
            with business_lock:
                pass
        def call():
            try:
                self.router.generate('reception', 'system', {}, on_call_start=callback)
            except ServiceError as exc:
                errors.append(exc.code)
        with business_lock:
            thread = threading.Thread(target=call, daemon=True)
            thread.start()
            self.assertTrue(entered.wait(2))
            configured = threading.Event()
            updater = threading.Thread(target=lambda: (self.configure(), configured.set()), daemon=True)
            updater.start()
            self.assertTrue(configured.wait(1))
        thread.join(2)
        updater.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)

    def test_timeout_while_callback_blocked_prevents_send(self):
        self.router._runtime = ModelRuntime(cleanup_timeout=.05)
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def callback():
            entered.set()
            release.wait(2)
        def call():
            try:
                self.router.generate('reception', 'system', {}, cancel=cancelled, on_call_start=callback)
            except ServiceError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=call)
        thread.start()
        self.assertTrue(entered.wait(2))
        cancelled.set()
        thread.join(2)
        self.assertEqual(errors, ['model_cleanup_timeout'])
        release.set()
        time.sleep(.1)
        self.assertEqual(self.fake.calls, [])

    def test_blocked_reception_callback_allows_translation_and_keeps_slot(self):
        self.router._runtime = ModelRuntime(cleanup_timeout=.05)
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def callback():
            entered.set()
            release.wait(5)
        def reception():
            try:
                self.router.generate('reception', 'system', {}, cancel=cancel, on_call_start=callback)
            except ServiceError as exc:
                errors.append(exc.code)
        first = threading.Thread(target=reception)
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            result = self.router.generate('translation', 'system', {})
            self.assertEqual(result['output'], {'ok': True})
            cancel.set()
            first.join(2)
            self.assertEqual(errors, ['model_cleanup_timeout'])
            second_started = threading.Event()
            second_cancel = threading.Event()
            def second_call():
                try:
                    self.router.generate('reception', 'system', {}, cancel=second_cancel,
                                         on_call_start=second_started.set)
                except ServiceError:
                    pass
            second = threading.Thread(target=second_call)
            second.start()
            self.assertFalse(second_started.wait(.15))
            second_cancel.set()
            second.join(2)
            self.assertEqual(len(self.fake.calls), 1)
        finally:
            release.set()
            first.join(2)
        time.sleep(.1)
        self.assertEqual(len(self.fake.calls), 1)

    def test_authority_read_does_not_block_model_loop(self):
        from unittest.mock import patch
        from reception_service import model_router
        original = model_router._read_authority
        entered, release, ping, cancel = (threading.Event() for _ in range(4))
        self.router._runtime = ModelRuntime(cleanup_timeout=.05)
        errors = []
        def read(path):
            if threading.current_thread().name.startswith('model-callback'):
                entered.set()
                release.wait(5)
            return original(path)
        def call():
            try:
                self.router.generate('reception', 'system', {}, cancel=cancel)
            except ServiceError as exc:
                errors.append(exc.code)
        async def signal():
            ping.set()
        with patch.object(model_router, '_read_authority', read):
            thread = threading.Thread(target=call)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.router._runtime.schedule(signal)
                self.assertTrue(ping.wait(1))
                cancel.set()
                thread.join(2)
                self.assertEqual(errors, ['model_cleanup_timeout'])
            finally:
                release.set()
                thread.join(2)
            time.sleep(.1)
        self.assertFalse(self.fake.calls)

    def test_configuration_change_during_callback_prevents_send(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def callback():
            entered.set()
            release.wait(5)
        def call():
            try:
                self.router.generate('reception', 'system', {}, on_call_start=callback)
            except ServiceError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=call)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.settings['routes']['reception']['route_revision'] += 1
            self.configure()
        finally:
            release.set()
            thread.join(3)
        self.assertTrue(errors)
        self.assertEqual(self.fake.calls, [])

    def test_real_transport_with_controlled_http(self):
        import httpx
        from reception_service.model_providers.transport import ProviderTransport
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}], 'model': 'test'})
        self.router._transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(respond))
        result = self.router.generate('reception', 'system', {'x': 1})
        self.assertEqual(result['output'], {'ok': True})
        self.assertEqual(len(requests), 1)

    def test_generation_image_evidence_and_before_send(self):
        data = b'owned bytes'
        image = dict(alias='p1', image_id='i', source_id='s', source_version='1', role='buyer', mime='image/png',
                     data=data, sha256=hashlib.sha256(data).hexdigest(), width=1, height=1)
        starts = []
        result = self.router.generate('reception', 'system', {}, images=[image], on_call_start=lambda: starts.append(1))
        self.assertEqual(starts, [1])
        self.assertEqual(result['image_input_evidence']['images'][0]['sha256'], image['sha256'])
        self.assertNotIn('secret', repr(result))
        self.assertEqual(len(self.fake.calls), 1)
    def test_missing_notification_rejects_result(self):
        self.fake.block = True
        errors = []
        def run():
            try:
                self.router.generate('reception', 'system', {})
            except ServiceError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(self.fake.started.wait(2))
        self.settings['profiles'][0]['capability_revision'] += 1
        self.path.write_text(json.dumps(self.settings), encoding='utf8')
        self.fake.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
    def test_capability_change_cancels_both_purposes(self):
        self.fake.block = True
        errors = []
        def run(purpose):
            try:
                self.router.generate(purpose, 'system', {})
            except ServiceError as exc:
                errors.append(exc.code)
        threads = [threading.Thread(target=run, args=(purpose,)) for purpose in ('reception', 'translation')]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 2
        while len(self.fake.calls) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(self.fake.calls), 2)
        self.settings['profiles'][0]['capability_revision'] += 1
        self.assertEqual(self.configure(), {'reception', 'translation'})
        for thread in threads:
            thread.join(2)
        self.assertEqual(sorted(errors), ['model_cancelled', 'model_cancelled'])

    def test_close_cancels_active_request(self):
        self.fake.block = True
        errors = []
        def run():
            try:
                self.router.generate('reception', 'system', {})
            except ServiceError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(self.fake.started.wait(2))
        self.router.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['model_cancelled'])

    def test_same_profile_translation_route_change_only_cancels_translation(self):
        self.fake.block = True
        results = []
        def run():
            try:
                results.append(self.router.generate('reception', 'system', {}))
            except ServiceError as exc:
                results.append(exc.code)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(self.fake.started.wait(2))
        self.settings['routes']['translation']['route_revision'] += 1
        self.assertEqual(self.configure(), {'translation'})
        self.fake.release.set()
        thread.join(2)
        self.assertIsInstance(results[0], dict)


class CleanupTest(unittest.TestCase):
    def test_timeout_keeps_slot_until_cleanup_and_close_is_bounded(self):
        runtime = ModelRuntime(cleanup_timeout=.08)
        release = threading.Event()
        started = threading.Event()
        second = threading.Event()
        async def first():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                while not release.is_set():
                    await asyncio.sleep(.01)
        errors = []
        def call():
            try:
                runtime.run('reception', first, deadline=.05)
            except ServiceError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=call)
        thread.start()
        self.assertTrue(started.wait(2))
        thread.join(2)
        self.assertEqual(errors, ['model_cleanup_timeout'])
        async def next_call():
            second.set()
        queued_cancel = threading.Event()
        def queued():
            try:
                runtime.run('reception', next_call, cancel=queued_cancel)
            except ServiceError:
                pass
        waiting = threading.Thread(target=queued)
        waiting.start()
        self.assertFalse(second.wait(.1))
        queued_cancel.set()
        waiting.join(2)
        with self.assertRaises(ServiceError) as error:
            runtime.close()
        self.assertEqual(error.exception.code, 'model_cleanup_timeout')
        release.set()
        time.sleep(.1)
        runtime.close()
        self.assertFalse(second.is_set())
