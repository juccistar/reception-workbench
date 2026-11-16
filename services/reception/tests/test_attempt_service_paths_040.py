"""Synthetic service paths exercise real router admission without network calls."""
import asyncio
import hashlib
import threading
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test_support'))

from reception_service.integrations.live import LiveIntegration
from reception_service.model_providers.owned_sync import owned_sync
import test_manual_reception


class Transport:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.gate = False
        self.sent = 0

    async def generate(self, **kwargs):
        self.entered.set()
        while self.gate and not self.release.is_set():
            await asyncio.sleep(.005)
        await owned_sync(kwargs['before_send'])
        self.sent += 1
        return {'output': {'action': 'ask', 'reply_messages': [{'body': 'Noted.',
                 'reply_sources': ['synthetic-source']}], 'reply': 'Noted.',
                 'reply_sources': ['synthetic-source']}, 'usage': {}, 'model': 'fixture', 'stop_reason': 'stop'}

    async def invalidate_profile(self, profile_id):
        pass

    async def aclose(self):
        pass


class ServiceAttemptPaths(unittest.TestCase):
    def setUp(self):
        self.fixture = test_manual_reception.ManualReceptionTests('test_manual_requires_verified_identity_and_successful_read')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.service.checks['ai']['status'] = 'passed'
        self.transport = Transport()
        self.service._model_router._transport = self.transport
        live = LiveIntegration.__new__(LiveIntegration)
        live.settings = {}
        live._query_state = threading.local()
        live._model_router = self.service._model_router
        live.resolve_identity = lambda seller_id: seller_id
        self.fixture.facade.generate = live.generate
        self.fixture.facade.generate_manual = live.generate_manual
        target = patch('reception_service.integrations.live.target')
        target.start()
        self.addCleanup(target.stop)
        self.addCleanup(self.transport.release.set)

    def set_capability_status(self, status):
        import copy
        import json
        router = self.service._model_router
        settings = copy.deepcopy(router._settings)
        for capability in settings['profiles'][0]['capabilities'].values():
            capability['status'] = status
        Path(router._authority).write_text(json.dumps({**settings, 'encrypted_keys': {}}), encoding='utf-8')
        router.configure({'format': 'model-runtime-v1', 'authority_file': router._authority,
                          'settings': settings, 'credentials': {'fixture': 'synthetic'}})

    def request(self, kind, suffix=''):
        if kind == 'automatic':
            self.fixture.start_assisted()
            self.fixture.ingest(mid='new-authorized-source', stamp=self.service._started_at_ms + 1)
            return self.service._enqueue('generate_draft', self.fixture.generation_params(), automatic=True)
        params = {'conversation_id': self.fixture.cid, 'case_id': self.fixture.case_id,
                  'client_request_id': 'request-' + kind + suffix}
        if kind == 'polish':
            body = 'Thank you.'
            draft = self.service.save_chat_draft(self.fixture.cid, body, None, 'draft')
            params.update(input_body=body, input_revision=draft['revision'],
                          input_hash=hashlib.sha256(body.encode()).hexdigest())
        return self.service.generate_manual_suggestion(**params)

    def check_path(self, kind, cancel):
        self.transport.gate = True
        ack = self.request(kind)
        self.assertTrue(self.transport.entered.wait(3), self.service.repo.get('job', ack['job_id']))
        self.assertEqual(self.service.repo.list('model_attempt_chain_v1'), [])
        if cancel:
            self.service.set_reception(False)
        self.transport.release.set()
        job = self.fixture.wait_job(ack)
        rows = self.service.repo.list('model_attempt_chain_v1')
        if cancel:
            self.assertNotEqual(job['status'], 'succeeded', job)
            self.assertEqual(self.transport.sent, 0)
            self.assertEqual(rows, [])
        else:
            self.assertEqual(job['status'], 'succeeded', job)
            self.assertEqual(self.transport.sent, 1)
            self.assertEqual([(r['attempts'], r['status']) for r in rows], [(1, 'completed')])

    def test_automatic_429_retries_with_same_source_and_successful_job(self):
        import json
        import httpx
        from reception_service.model_providers.transport import ProviderTransport
        calls = []
        async def respond(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, headers={'retry-after': '0'})
            output = {'action': 'ask', 'reply_messages': [{'body': 'Noted.', 'reply_sources': ['synthetic-source']}],
                      'reply': 'Noted.', 'reply_sources': ['synthetic-source']}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]})
        self.service._model_router._transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(respond))
        job = self.fixture.wait_job(self.request('automatic'))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(len(calls), 2)
        rows = self.service.repo.list('model_attempt_chain_v1')
        self.assertEqual([(row['attempts'], row['status']) for row in rows], [(2, 'completed')])

    def test_manual_generation_uses_saved_route_without_session_check(self):
        import json
        import httpx
        from reception_service.model_providers.transport import ProviderTransport
        calls = []
        async def respond(request):
            calls.append(request)
            output = {'action': 'ask', 'reply_messages': [{'body': 'Noted.', 'reply_sources': ['synthetic-source']}],
                      'reply': 'Noted.', 'reply_sources': ['synthetic-source']}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]})
        self.service._model_router._transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(respond))
        with patch.object(self.service, 'check_ai', side_effect=AssertionError('unexpected AI check'), create=True):
            for status in ('not_checked', 'failed'):
                self.set_capability_status(status)
                for kind in ('suggestion', 'polish'):
                    with self.subTest(status=status, kind=kind):
                        self.service.checks['ai']['status'] = status
                        job = self.fixture.wait_job(self.request(kind, '-' + status))
                        self.assertEqual(job['status'], 'succeeded', job)
                        self.assertEqual(self.service.checks['ai']['status'], status)
        self.assertEqual(len(calls), 4)

    def test_manual_image_request_reaches_provider_with_unchecked_capabilities(self):
        import json
        import httpx
        from reception_service.errors import ServiceError
        from reception_service.model_providers.transport import ProviderTransport
        from test_model_inputs_040 import fixture
        calls = []
        async def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"reply":"ok"}'}}]})
        self.service._model_router._transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(respond))
        self.set_capability_status('failed')
        live = LiveIntegration.__new__(LiveIntegration)
        live.settings = {}
        live._query_state = threading.local()
        live._model_router = self.service._model_router
        context, photos = fixture()
        context['task_mode'] = 'manual_suggestion'
        output, refs = live._business_query('rules', context, photos)
        self.assertEqual(output['answer']['reply'], 'ok')
        self.assertEqual(len(refs), 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('image_url', str(calls[0]))
        context['task_mode'] = 'automatic'
        with self.assertRaises(ServiceError) as error:
            live._business_query('rules', context, photos)
        self.assertEqual(error.exception.code, 'model_not_ready')
        self.assertEqual(len(calls), 1)

    def test_manual_provider_failure_preserves_safe_error(self):
        import httpx
        from reception_service.model_providers.transport import ProviderTransport
        async def respond(request):
            return httpx.Response(401, json={'error': {'message': 'secret provider detail'}})
        self.service._model_router._transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(respond))
        self.service.checks['ai']['status'] = 'not_checked'
        self.set_capability_status('failed')
        with patch.object(self.service, 'check_ai', side_effect=AssertionError('unexpected AI check'), create=True):
            job = self.fixture.wait_job(self.request('suggestion'))
        self.assertEqual(job['status'], 'failed', job)
        self.assertEqual(job['error']['code'], 'model_auth_error')
        self.assertNotIn('secret provider detail', str(job))

    def test_automatic_claim(self): self.check_path('automatic', False)
    def test_suggestion_claim(self): self.check_path('suggestion', False)
    def test_polish_claim(self): self.check_path('polish', False)
    def test_automatic_cancel_before_claim(self): self.check_path('automatic', True)
    def test_suggestion_cancel_before_claim(self): self.check_path('suggestion', True)
    def test_polish_cancel_before_claim(self): self.check_path('polish', True)
