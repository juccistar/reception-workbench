import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from reception_service.errors import ServiceError
from reception_service.repository import encoded
from reception_service.service import ReceptionService
from reception_service.translation_provider import TranslationProviderError


class FakeProvider:
    def __init__(self):
        self.calls = []

    def translate(self, items, request_id, *, cancel_event=None):
        self.calls.append(copy.deepcopy(items))
        return {'items': [{'key': item['key'], 'text': '译:' + item['text']} for item in items],
                'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}


class ExternalTranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ReceptionService(Path(self.temp.name))
        self.cid = self.service.repo.list('conversation')[0]['conversation_id']
        self.service.mode = 'live'
        self.provider = FakeProvider()
        self.service._new_translation_provider = lambda: self.provider
        self.save_settings()
        owner = self.service.repo.get('conversation', self.cid)
        self.messages = []
        for index, role in enumerate(('buyer', 'seller', 'buyer')):
            row = {'message_id': 'synthetic-translation-' + str(index), 'conversation_id': self.cid,
                   'seller_id': owner['seller_id'], 'buyer_id': owner['buyer_id'], 'role': role,
                   'own': role == 'seller', 'text': 'private message ' + str(index),
                   'attachments': [], 'timestamp_ms': 2000000000000 + index}
            self.service.repo.put('message', encoded([self.cid, row['message_id']]), row)
            self.messages.append(row)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def save_settings(self, *, enabled=True, model='model-a'):
        settings = self.service.get_translation_config()
        return self.service.save_translation_settings(enabled, 'https://example.com/v1', model,
                                                      settings['revision'], 'a' * 64, 'synthetic-secret')

    def item(self, index):
        return {'kind': 'message', 'source_id': self.messages[index]['message_id']}

    def wait(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.repo.get('job', job_id)
            if job['status'] not in ('queued', 'running', 'deferred'):
                return job
            time.sleep(.01)
        self.fail('translation job did not finish')

    def request(self, items, request_id):
        return self.service.request_translations(self.cid, items, request_id)['job_id']

    def status(self, item):
        return self.service.get_translations(self.cid, [item])['items'][0]

    def test_previous_prompt_cache_remains_readable_without_automatic_retranslation(self):
        s = self.service
        identity, new_key, payload = s._translation_sources(self.cid, [self.item(0)])[0]
        settings = s.get_translation_config()
        old_key = hashlib.sha256(encoded([2, s.seller_id, self.cid, identity, 'zh-CN', settings['base_url'],
                                         settings['model'], 'chat-v2', payload['context']]).encode('utf-8')).hexdigest()
        self.assertNotEqual(new_key, old_key)
        s.repo.put('translation_v2', old_key, {'status': 'ready', 'text': '旧版译文仍然保留', 'error': None})
        row = self.status(self.item(0))
        self.assertEqual(row['status'], 'ready')
        self.assertEqual(row['text'], '旧版译文仍然保留')
        self.assertTrue(row['legacy'])
        self.assertEqual(row['provider'], '第三方历史缓存')
        self.assertEqual(self.provider.calls, [])
        self.assertIsNotNone(s.repo.get('translation_v2', old_key))

    def test_messages_batch_without_ai_or_changes_to_originals(self):
        s = self.service
        s.checks['ai']['status'] = 'failed'
        before_messages = copy.deepcopy(s.repo.list('message', conversation_id=self.cid))
        before_cases = copy.deepcopy(s.repo.list('case'))
        before_drafts = copy.deepcopy(s.repo.list('chat_draft'))
        before_budget = copy.deepcopy(s.get_snapshot()['reception']['budget'])
        # The third-party path must not invoke the Accio translation integration.
        s.integrations.translate = lambda *args, **kwargs: self.fail('Accio translation was called')
        items = [self.item(0), self.item(1)]
        job = self.wait(self.request(items, 'batch'))
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual([x['speaker'] for x in self.provider.calls[0]], ['buyer', 'seller'])
        self.assertTrue(all(self.status(item)['text'].startswith('译:') for item in items))
        self.assertEqual(before_messages, s.repo.list('message', conversation_id=self.cid))
        self.assertEqual(before_cases, s.repo.list('case'))
        self.assertEqual(before_drafts, s.repo.list('chat_draft'))
        self.assertEqual(before_budget, s.get_snapshot()['reception']['budget'])
        with self.assertRaises(ServiceError):
            s.request_translations(self.cid, [{'kind': 'case_text', 'source_id': 'synthetic', 'field': 'summary'}], 'case')

    def test_manual_suggestion_uses_owned_current_source_and_cache_without_key(self):
        s, cid = self.service, self.cid
        owner = s.repo.get('conversation', cid)
        s.mode = 'demo'
        generation = s.generate_manual_suggestion(cid, 'owned-suggestion')['job_id']
        self.assertEqual(self.wait(generation)['status'], 'succeeded')
        s.mode = 'live'
        suggestion = s.get_conversation(cid)['manual_suggestion']
        item = {'kind': 'manual_suggestion', 'source_id': suggestion['suggestion_id'],
                'source_hash': hashlib.sha256(suggestion['body'].encode('utf-8')).hexdigest()}
        original = copy.deepcopy(s.repo.get('manual_suggestion', cid))
        self.assertEqual(self.wait(self.request([item], 'manual-owned'))['status'], 'succeeded')
        self.assertEqual(self.provider.calls[-1][0]['speaker'], 'seller')
        translated = self.status(item)['text']
        self.assertEqual(s.repo.get('manual_suggestion', cid), original)
        s.close()
        self.service = ReceptionService(Path(self.temp.name))
        self.service.mode = 'live'
        self.assertFalse(self.service.get_translation_info()['has_key'])
        self.assertEqual(self.status(item)['text'], translated)
        with self.assertRaises(ServiceError):
            self.status({**item, 'source_hash': '0' * 64})
        with self.assertRaises(ServiceError):
            self.status({**item, 'source_id': 'another-suggestion'})
        self.service.save_conversation_supplement(cid, 'Updated seller terms', None, 'changed-terms')
        self.assertEqual(self.status(item)['text'], translated)
        with self.assertRaises(ServiceError):
            self.request([item], 'stale-suggestion')
        foreign_cid = 'synthetic-other-conversation'
        self.service.repo.put('conversation', foreign_cid, {
            **owner, 'conversation_id': foreign_cid, 'buyer_id': 'another-buyer'})
        with self.assertRaises(ServiceError):
            self.service.get_translations(foreign_cid, [item])
        replacement = {**suggestion, 'suggestion_id': 'manual-owned-2', 'body': 'New suggestion'}
        self.service.repo.put('manual_suggestion', cid, replacement)
        with self.assertRaises(ServiceError):
            self.status(item)
        self.assertEqual(owner['seller_id'], self.service.seller_id)

    def test_replaced_manual_suggestion_discards_late_translation(self):
        s, cid = self.service, self.cid
        s.mode = 'demo'
        self.assertEqual(self.wait(s.generate_manual_suggestion(cid, 'late-manual')['job_id'])['status'], 'succeeded')
        s.mode = 'live'
        suggestion = s.get_conversation(cid)['manual_suggestion']
        item = {'kind': 'manual_suggestion', 'source_id': suggestion['suggestion_id'],
                'source_hash': hashlib.sha256(suggestion['body'].encode('utf-8')).hexdigest()}
        started, release = threading.Event(), threading.Event()
        def late(items, request_id, *, cancel_event=None):
            started.set()
            release.wait(4)
            return {'items': [{'key': row['key'], 'text': 'Late translation'} for row in items]}
        s._new_translation_provider = lambda: type('Late', (), {'translate': staticmethod(late)})()
        job = self.request([item], 'replace-manual')
        try:
            self.assertTrue(started.wait(2))
            s.repo.put('manual_suggestion', cid, {**suggestion, 'suggestion_id': 'replacement'})
        finally:
            release.set()
        self.assertEqual(self.wait(job)['status'], 'stale')
        with self.assertRaises(ServiceError):
            self.status(item)

    def test_local_gateway_settings_persist_and_invalid_remote_http_is_explained(self):
        s = self.service
        settings = s.get_translation_config()
        before = copy.deepcopy(s.get_snapshot()['reception'])
        result = s.save_translation_settings(True, 'http://127.0.0.1:3425/v1',
                                             'codex/gpt-6-astra', settings['revision'],
                                             'a' * 64, 'magpie')
        self.assertEqual(result['base_url'], 'http://127.0.0.1:3425/v1')
        self.assertTrue(result['enabled'])
        self.assertTrue(result['has_key'])
        self.assertEqual(s.get_snapshot()['reception'], before)
        with self.assertRaises(ServiceError) as caught:
            s.save_translation_settings(True, 'http://remote.example.com/v1',
                                         'codex/gpt-6-astra', result['revision'],
                                         'a' * 64, 'magpie')
        self.assertIn('本机网关可使用 HTTP', str(caught.exception))
        self.assertEqual(s.get_translation_info()['base_url'], result['base_url'])
        s.close()
        self.service = ReceptionService(Path(self.temp.name))
        self.assertEqual(self.service.get_translation_info()['base_url'], result['base_url'])
        self.assertFalse(self.service.get_translation_info()['has_key'])

    def test_shared_connection_preflight_is_read_only_and_enable_binding_is_private(self):
        s = self.service
        before = s.get_translation_config()
        result = s.call('validate_translation_settings', {'base_url': 'http://LOCALHOST.:3425/v1/', 'model': '  shared-model  '})
        self.assertEqual(result, {'base_url': 'http://localhost:3425/v1', 'model': 'shared-model'})
        self.assertEqual(s.get_translation_config(), before)
        with self.assertRaises(ServiceError):
            s.call('validate_translation_settings', {'base_url': 'https://10.0.0.1/v1', 'model': 'shared-model'})
        self.assertEqual(s.get_translation_config(), before)
        root = str(Path(self.temp.name).resolve())
        saved = s.save_translation_settings(True, result['base_url'], result['model'], before['revision'],
                                            'a' * 64, 'shared-test-key', enabled_data_root=root)
        self.assertEqual(s.get_translation_config()['enabled_data_root'], root)
        self.assertNotIn('enabled_data_root', saved)
        self.assertNotIn('shared-test-key', json.dumps(s.get_snapshot()))
        s.save_translation_settings(False, result['base_url'], result['model'], saved['revision'],
                                     'a' * 64, 'shared-test-key')
        self.assertIsNone(s.get_translation_config()['enabled_data_root'])

    def test_cache_reopen_and_invalidation(self):
        item = self.item(2)
        self.assertEqual(self.wait(self.request([item], 'cached'))['status'], 'succeeded')
        cached = self.status(item)['text']
        self.service.close()
        self.service = ReceptionService(Path(self.temp.name))
        self.service.mode = 'live'
        self.service._new_translation_provider = lambda: self.provider
        self.assertFalse(self.service.get_translation_info()['has_key'])
        self.assertEqual(self.status(item)['text'], cached)
        self.save_settings(model='model-b')
        self.assertEqual(self.status(item)['status'], 'pending')
        self.save_settings(model='model-a')
        self.assertEqual(self.status(item)['text'], cached)
        previous = dict(self.messages[1], text='changed context')
        self.service.repo.put('message', encoded([self.cid, previous['message_id']]), previous)
        self.assertEqual(self.status(item)['status'], 'pending')
        self.service.repo.put('message', encoded([self.cid, previous['message_id']]), self.messages[1])
        changed = dict(self.messages[2], text='changed original')
        self.service.repo.put('message', encoded([self.cid, changed['message_id']]), changed)
        self.assertEqual(self.status(item)['status'], 'pending')

    def test_singleflight_and_business_does_not_preempt(self):
        started, release = threading.Event(), threading.Event()
        provider = self.provider
        def slow(items, request_id, *, cancel_event=None):
            started.set()
            self.assertTrue(release.wait(4))
            self.assertFalse(cancel_event.is_set())
            return FakeProvider.translate(provider, items, request_id, cancel_event=cancel_event)
        provider.translate = slow
        first = self.request([self.item(0)], 'one')
        try:
            self.assertTrue(started.wait(2))
            second = self.request([self.item(0)], 'two')
            business = self.service.call('check_connection', {})['job_id']
            self.assertNotIn(self.wait(business)['status'], ('queued', 'running'))
            # A follower cannot be complete before its shared translation exists.
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline:
                status = self.service.repo.get('job', second)['status']
                if status not in ('queued', 'running', 'deferred'):
                    break
                time.sleep(.01)
            self.assertIn(status, ('queued', 'running', 'deferred'))
        finally:
            release.set()
        self.assertEqual(self.wait(first)['status'], 'succeeded')
        self.assertEqual(self.wait(second)['status'], 'succeeded')
        self.assertEqual(len(provider.calls), 1)

    def test_long_message_is_split_without_losing_content(self):
        row = dict(self.messages[0], text='long message ' * 1000)
        self.service.repo.put('message', encoded([self.cid, row['message_id']]), row)
        self.assertEqual(self.wait(self.request([self.item(0)], 'long'))['status'], 'succeeded')
        parts = [part['text'] for batch in self.provider.calls for part in batch]
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(parts), row['text'])
        self.assertEqual(self.status(self.item(0))['text'].replace('译:', '').replace('\n', ''), row['text'])

    def test_late_response_cannot_restore_cleared_or_changed_cache(self):
        for action in ('settings', 'clear', 'disable'):
            with self.subTest(action=action):
                self.save_settings()
                self.service.clear_translation_cache()
                started, release = threading.Event(), threading.Event()
                def late(items, request_id, *, cancel_event=None):
                    started.set()
                    release.wait(4)
                    return {'items': [{'key': item['key'], 'text': 'late result'} for item in items]}
                provider = type('Late', (), {'translate': staticmethod(late)})()
                self.service._new_translation_provider = lambda: provider
                job = self.request([self.item(0)], 'late-' + action)
                try:
                    self.assertTrue(started.wait(2))
                    if action == 'clear':
                        self.service.clear_translation_cache()
                    elif action == 'disable':
                        self.save_settings(enabled=False)
                    else:
                        self.save_settings(model='new-model')
                finally:
                    release.set()
                self.assertEqual(self.wait(job)['status'], 'cancelled')
                self.assertNotEqual(self.status(self.item(0))['text'], 'late result')

    def test_shutdown_discards_late_response(self):
        started, release = threading.Event(), threading.Event()
        def late(items, request_id, *, cancel_event=None):
            started.set()
            release.wait(4)
            return {'items': [{'key': item['key'], 'text': 'late shutdown result'} for item in items]}
        self.service._new_translation_provider = lambda: type('Late', (), {'translate': staticmethod(late)})()
        self.request([self.item(0)], 'shutdown')
        self.assertTrue(started.wait(2))
        closer = threading.Thread(target=self.service.close)
        closer.start()
        try:
            self.assertTrue(self.service._stop.wait(2))
        finally:
            release.set()
            closer.join(4)
        self.assertFalse(closer.is_alive())
        self.service = ReceptionService(Path(self.temp.name))
        self.service.mode = 'live'
        self.assertNotEqual(self.status(self.item(0))['text'], 'late shutdown result')

    def test_retry_is_bounded_and_synthetic_check_is_private(self):
        attempts = []
        def failing(items, request_id, *, cancel_event=None):
            attempts.append(copy.deepcopy(items))
            raise TranslationProviderError('network_error', '', retryable=True, retry_after=.01)
        self.service._new_translation_provider = lambda: type('Fail', (), {'translate': staticmethod(failing)})()
        job = self.wait(self.request([self.item(0)], 'retry'))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(len(attempts), 3)
        self.service._new_translation_provider = lambda: self.provider
        job = self.wait(self.service.check_translation_provider()['job_id'])
        self.assertEqual(job['status'], 'succeeded')
        sent = json.dumps(self.provider.calls)
        self.assertNotIn('private message', sent)
        self.assertNotIn(self.cid, sent)

    def test_settings_do_not_revoke_reception_or_expose_key(self):
        s = self.service
        s.mode = 'demo'
        s.set_reception(True)
        before = copy.deepcopy(s.get_snapshot()['reception'])
        authorized_scope = copy.deepcopy(s._authorized_scope)
        self.save_settings(model='model-b')
        self.assertEqual(before, s.get_snapshot()['reception'])
        self.assertEqual(authorized_scope, s._authorized_scope)
        self.assertNotIn('synthetic-secret', json.dumps(s.get_translation_config()))
        self.assertNotIn('synthetic-secret', json.dumps(s.get_translation_info()))
        self.assertNotIn('synthetic-secret', json.dumps(s._control.get('meta', 'translation_settings')))
        self.assertNotIn('synthetic-secret', json.dumps(s.get_snapshot()))

    def test_foreign_sources_and_wrong_hash_never_reach_provider(self):
        for item in ({'kind': 'message', 'source_id': 'foreign'}, {**self.item(0), 'source_hash': 'wrong'}):
            with self.assertRaises(ServiceError):
                self.request([item], 'invalid-' + item['source_id'])
        row = dict(self.messages[0], seller_id='another-seller')
        self.service.repo.put('message', encoded([self.cid, row['message_id']]), row)
        with self.assertRaises(ServiceError):
            self.request([self.item(0)], 'wrong-owner')
        self.assertEqual(self.provider.calls, [])

    def test_singleflight_followers_share_failure(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def failing(items, request_id, *, cancel_event=None):
            calls.append(items)
            started.set()
            release.wait(3)
            raise TranslationProviderError('invalid_config', '')
        self.provider.translate = failing
        first = self.request([self.item(0)], 'failing-first')
        try:
            self.assertTrue(started.wait(2))
            second = self.request([self.item(0)], 'failing-follower')
            self.assertIn(self.service.repo.get('job', second)['status'], ('queued', 'running', 'deferred'))
        finally:
            release.set()
        self.assertEqual(self.wait(first)['status'], 'failed')
        self.assertEqual(self.wait(second)['status'], 'failed')
        self.assertEqual(len(calls), 1)

    def test_mixed_request_waits_for_shared_message_before_finishing(self):
        started, release = threading.Event(), threading.Event()
        provider = self.provider
        def slow(items, request_id, *, cancel_event=None):
            if any(item['text'] == self.messages[0]['text'] for item in items):
                started.set()
                release.wait(3)
            return FakeProvider.translate(provider, items, request_id, cancel_event=cancel_event)
        provider.translate = slow
        first = self.request([self.item(0)], 'mixed-first')
        try:
            self.assertTrue(started.wait(2))
            second = self.request([self.item(0), self.item(1)], 'mixed-follower')
            time.sleep(.15)
            self.assertIn(self.service.repo.get('job', second)['status'], ('queued', 'running', 'deferred'))
        finally:
            release.set()
        self.assertEqual(self.wait(first)['status'], 'succeeded')
        self.assertEqual(self.wait(second)['status'], 'succeeded')
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(all(self.status(self.item(index))['status'] == 'ready' for index in (0, 1)))

    def test_clearing_chat_cache_preserves_originals_and_legacy_requirement_cache(self):
        self.service.repo.put('translation', 'legacy-case-display', {'status': 'ready', 'text': '旧需求展示'})
        original = copy.deepcopy(self.service.repo.list('message', conversation_id=self.cid))
        self.wait(self.request([self.item(0)], 'clear-cache'))
        self.service.clear_translation_cache()
        self.assertEqual(self.status(self.item(0))['status'], 'pending')
        self.assertEqual(self.service.repo.get('translation', 'legacy-case-display')['text'], '旧需求展示')
        self.assertEqual(self.service.repo.list('message', conversation_id=self.cid), original)


if __name__ == '__main__':
    unittest.main()
