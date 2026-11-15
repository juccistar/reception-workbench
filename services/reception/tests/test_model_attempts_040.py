import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from reception_service.errors import ServiceError
from reception_service.model_attempts import claim, claim_many, finish
from reception_service.repository import Repository
from reception_service.translation_provider import RouterTranslator
from reception_service.translation_service import TranslationMixin


class AttemptsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'repo.sqlite3'
        self.repo = Repository(self.path, 'seller')
        self.source = {'conversation_id': 'c', 'message_id': 'm', 'text_sha256': 'abc'}

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def claim(self, source=None):
        return claim(self.repo, seller_id='seller', purpose='reception', channel='international', source=source or self.source)

    def test_unknown_survives_restart_and_new_job_route_media(self):
        tokens = self.claim()
        finish(self.repo, tokens, outcome='unknown')
        self.repo.close()
        self.repo = Repository(self.path, 'seller')
        with self.assertRaises(ServiceError):
            self.claim()

    def test_unsubmitted_limit_and_old_token(self):
        old = self.claim()
        finish(self.repo, old, outcome='unsubmitted')
        new = self.claim()
        finish(self.repo, old, outcome='completed')
        with self.assertRaises(ServiceError):
            self.claim()
        finish(self.repo, new, outcome='unsubmitted')
        with self.assertRaises(ServiceError):
            self.claim()

    def test_batch_admission_atomic_and_recombination(self):
        first = {'source': 'a', 'part': 0}
        second = {'source': 'b', 'part': 0}
        kwargs = dict(seller_id='seller', purpose='translation', channel='chat')
        tokens = claim_many(self.repo, sources=[first], **kwargs)
        with self.assertRaises(ServiceError):
            claim_many(self.repo, sources=[first, second], **kwargs)
        other = claim_many(self.repo, sources=[second], **kwargs)
        finish(self.repo, tokens, outcome='unknown')
        finish(self.repo, other, outcome='completed')
        with self.assertRaises(ServiceError):
            claim_many(self.repo, sources=[second, first], **kwargs)

    def test_whatsapp_legacy_migration_and_coexisting_public_chain(self):
        from reception_service.model_attempts import claim_whatsapp, chain_id
        from reception_service.whatsapp import _key
        args = dict(seller_id='seller', account_id='a', chat_id='c', pending_message_id='m',
                    legacy_id=_key('seller', 'a', 'c', 'm'))
        source = {'account_id': 'a', 'chat_id': 'c', 'pending_message_id': 'm'}
        public = claim(self.repo, seller_id='seller', purpose='reception', channel='whatsapp', source=source)
        finish(self.repo, public, outcome='unsubmitted')
        self.repo.put('wa_generation_attempt_chain', args['legacy_id'], {'attempts': 1, 'in_flight': True})
        key = chain_id('seller', 'reception', 'whatsapp', source)
        for _ in range(2):
            with self.assertRaises(ServiceError):
                claim_whatsapp(self.repo, **args)
            row = self.repo.get('model_attempt_chain_v1', key)
            self.assertEqual((row['attempts'], row['status']), (2, 'unknown'))
        self.repo.close()
        self.repo = Repository(self.path, 'seller')
        with self.assertRaises(ServiceError):
            claim_whatsapp(self.repo, **args)
        args.update(pending_message_id='new', legacy_id=_key('seller', 'a', 'c', 'new'))
        self.assertTrue(claim_whatsapp(self.repo, **args))

    def test_whatsapp_only_definite_unsubmitted_can_claim_twice(self):
        from reception_service.model_attempts import claim_whatsapp
        args = dict(seller_id='seller', account_id='a', chat_id='c', pending_message_id='m', legacy_id='old')
        first = claim_whatsapp(self.repo, **args)
        finish(self.repo, first, outcome='unsubmitted')
        second = claim_whatsapp(self.repo, **args)
        finish(self.repo, first, outcome='completed')
        finish(self.repo, second, outcome='unsubmitted')
        with self.assertRaises(ServiceError): claim_whatsapp(self.repo, **args)

    def test_whatsapp_legacy_completed_and_inflight_public_token(self):
        from reception_service.model_attempts import claim_whatsapp, chain_id
        from reception_service.whatsapp import _key
        args = dict(seller_id='seller', account_id='a', chat_id='c', pending_message_id='m',
                    legacy_id=_key('seller', 'a', 'c', 'm'))
        source = {'account_id': 'a', 'chat_id': 'c', 'pending_message_id': 'm'}
        tokens = claim(self.repo, seller_id='seller', purpose='reception', channel='whatsapp', source=source)
        self.repo.put('wa_generation_attempt_chain', args['legacy_id'], {'attempts': 1, 'in_flight': False})
        with self.assertRaises(ServiceError): claim_whatsapp(self.repo, **args)
        finish(self.repo, tokens, outcome='unsubmitted')
        row = self.repo.get('model_attempt_chain_v1', chain_id('seller', 'reception', 'whatsapp', source))
        self.assertEqual((row['attempts'], row['status']), (2, 'completed'))
        with self.assertRaises(ServiceError): claim_whatsapp(self.repo, **args)

    def test_concurrent_same_source(self):
        barrier = threading.Barrier(3)
        results = []
        def run():
            barrier.wait()
            try:
                self.claim()
                results.append('claimed')
            except ServiceError:
                results.append('blocked')
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join()
        self.assertCountEqual(results, ['claimed', 'blocked'])

    def test_explicit_manual_operation_distinct(self):
        def manual(request):
            return claim(self.repo, seller_id='seller', purpose='manual', channel='international',
                         source={'conversation_id': 'c', 'client_request_id': request})
        finish(self.repo, manual('one'), outcome='unknown')
        with self.assertRaises(ServiceError): manual('one')
        self.assertTrue(manual('two'))

    def test_router_translator_uses_actual_send_callback(self):
        router = Mock(unsafe=True)
        router.resolve.return_value = SimpleNamespace()
        def generate(*args, on_call_start, **kwargs):
            on_call_start()
            return {'output': {'items': [{'id': 't1', 'text': '你好'}]}}
        router.generate.side_effect = generate
        translator = RouterTranslator(router)
        tokens = []
        result = translator.translate([{'key': 'k', 'text': 'hi', 'speaker': 'buyer', 'context': []}], 'job',
            on_call_start=lambda: tokens.append(self.claim()))
        self.assertEqual(result['items'][0]['text'], '你好')
        self.assertEqual(len(tokens), 1)
        with self.assertRaises(ServiceError): self.claim()

    def test_service_translation_claims_at_router_callback_and_blocks_new_job(self):
        router = Mock(unsafe=True)
        router.resolve.return_value = SimpleNamespace()
        calls = []
        def generate(*args, on_call_start, **kwargs):
            on_call_start()
            calls.append(1)
            raise RuntimeError('connection lost after upload')
        router.generate.side_effect = generate
        provider = RouterTranslator(router)
        fake = SimpleNamespace(repo=self.repo, seller_id='seller', mode='live', _lock=threading.RLock(),
            _stop=threading.Event(), _translation_generation=0, _translation_settings={'model': 'x'},
            _translation_current=lambda *a: None, _record_translation_usage=lambda *a, **kw: None,
            _job_update=lambda *a, **kw: None, _emit=lambda *a: None, _translation_fail=lambda *a: None)
        identity = {'kind': 'message', 'source_id': 'm', 'source_hash': 'hash', 'field': None}
        payload = {'text': 'hello', 'speaker': 'buyer', 'context': []}
        for job, cache_key in [('one', 'profile-a'), ('two', 'profile-b')]:
            task = {'job_id': job, 'cid': 'c', 'work': [(identity, cache_key, payload)],
                    'provider': provider, 'generation': 0, 'attempt': 0, 'check': False}
            TranslationMixin._run_translation(fake, task, threading.Event())
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
