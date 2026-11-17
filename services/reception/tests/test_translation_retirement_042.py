import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from reception_service.translation_provider import RouterTranslator
from reception_service.repository import Repository
from reception_service.translation_service import TranslationMixin, _hash


class TranslationRetirementTests(unittest.TestCase):
    def test_legacy_migration_resumes_and_removes_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Repository(Path(directory) / 'control.sqlite3')
            repo = Repository(Path(directory) / 'seller.sqlite3', 'seller-a')
            service = TranslationMixin()
            service._lock = threading.RLock()
            service._control, service.repo = control, repo
            service.seller_id, service.mode = 'seller-a', 'live'
            service._translation_settings = {'model': ''}
            service._translation_migration_stop = threading.Event()
            service._translation_migration_thread = None
            service.settings = {}
            service._chat_owner = lambda cid: {'conversation_id': cid, 'seller_id': 'seller-a', 'buyer_id': 'buyer-a'}
            service._translation_cache_model = lambda: ['unconfigured']
            from reception_service.repository import encoded
            source = {'conversation_id': 'c', 'message_id': 'm', 'seller_id': 'seller-a',
                      'buyer_id': 'buyer-a', 'role': 'buyer', 'text': 'hello'}
            repo.put('message', encoded(['c', 'm']), source)
            identity, _, payload = service._translation_sources('c', [{'kind': 'message', 'source_id': 'm'}])[0]
            old_key = _hash([2, 'seller-a', 'c', identity, 'zh-CN', 'https://old.invalid/v1', 'old-model', 'chat-v2', []])
            original = {'status': 'ready', 'text': '历史译文'}
            repo.put('translation_v2', old_key, original)
            repo.put('translation_v2', 'unmatchable', {'status': 'ready', 'text': '保留'})
            control.put('meta', 'translation_settings', {'base_url': 'https://old.invalid/v1', 'model': 'old-model',
                                                        'enabled': True, 'credential_ref': 'old-ref'})
            service._translation_migration_stop.set()
            service._retire_translation_settings()
            service._translation_migration_thread.join(2)
            self.assertIsNone(control.get('meta', 'translation_settings'))
            self.assertNotIn('credential_ref', repo.get('meta', 'translation_retirement'))
            repo.close()
            control.close()
            control = Repository(Path(directory) / 'control.sqlite3')
            repo = Repository(Path(directory) / 'seller.sqlite3', 'seller-a')
            service._control, service.repo = control, repo
            service._translation_migration_stop.clear()
            service._translation_migration_thread = None
            service._retire_translation_settings()
            service._translation_migration_thread.join(2)
            state = repo.get('meta', 'translation_retirement')
            self.assertEqual((state['matched'], state['unmatched']), (1, 1))
            self.assertNotIn('base_url', state)
            self.assertNotIn('model', state)
            self.assertEqual(repo.get('translation_v2', old_key), original)
            self.assertEqual(repo.get('translation_v2', 'unmatchable')['text'], '保留')
            self.assertEqual(service.get_translations('c', [{'kind': 'message', 'source_id': 'm'}])['items'][0]['text'], '历史译文')
            service._retire_translation_settings()
            self.assertEqual(repo.get('meta', 'translation_retirement'), state)
            repo.close()
            control.close()

    def test_router_translator_attempts_unchecked_route_and_validates_output(self):
        router = Mock(unsafe=True)
        router.resolve.return_value = object()
        router.generate.return_value = {'output': {'items': [{'id': 't1', 'text': '译文'}]}}
        provider = RouterTranslator(router)
        result = provider.translate([{'key': 'k', 'text': 'hello', 'speaker': 'buyer', 'context': []}], 'r')
        self.assertEqual(result['items'][0]['text'], '译文')
        self.assertTrue(router.resolve.call_args.kwargs['allow_unchecked'])
        self.assertTrue(router.generate.call_args.kwargs['_allow_unchecked'])
        self.assertEqual(router.assert_current.call_count, 2)

    def test_previous_prompt_translation_is_history_not_a_fresh_cache_hit(self):
        from reception_service.repository import encoded
        with tempfile.TemporaryDirectory() as directory:
            control = Repository(Path(directory) / 'control.sqlite3')
            repo = Repository(Path(directory) / 'seller.sqlite3', 'seller-a')
            try:
                service = TranslationMixin()
                service._lock = threading.RLock()
                service._control, service.repo = control, repo
                service.seller_id, service.mode = 'seller-a', 'live'
                service._translation_settings = {'model': ''}
                service.settings = {}
                service._chat_owner = lambda cid: {'conversation_id': cid, 'seller_id': 'seller-a', 'buyer_id': 'buyer-a'}
                service._translation_cache_model = lambda: ['synthetic-route']
                service._new_translation_provider = Mock(side_effect=AssertionError('history display must not call a model'))
                source = {'conversation_id': 'c', 'message_id': 'm', 'seller_id': 'seller-a',
                          'buyer_id': 'buyer-a', 'role': 'seller', 'text': 'I will ask our warehouse.'}
                repo.put('message', encoded(['c', 'm']), source)
                item = {'kind': 'message', 'source_id': 'm'}
                with patch('reception_service.translation_service.TRANSLATION_PROMPT_VERSION', 'chat-v3-reviewed-039'):
                    identity, old_key, payload = service._translation_sources('c', [item])[0]
                old_record = {**identity, 'conversation_id': 'c', 'seller_id': 'seller-a',
                              'context_hash': _hash(payload['context']), 'status': 'ready',
                              'text': '我去问一下仓库。', 'error': None}
                repo.put('translation_v2', old_key, old_record)
                _, current_key, current_payload = service._translation_sources('c', [item])[0]
                self.assertNotEqual(current_key, old_key)
                self.assertEqual(current_payload, payload)
                shown = service.get_translations('c', [item])['items'][0]
                self.assertEqual(shown['text'], old_record['text'])
                self.assertTrue(shown['legacy'])
                self.assertEqual(shown['provider'], '第三方历史缓存')
                self.assertEqual(shown['cache_key'], current_key)
                self.assertIsNone(repo.get('translation_v2', current_key))
                self.assertEqual(repo.get('translation_v2', old_key), old_record)
                self.assertEqual(repo.get('message', encoded(['c', 'm'])), source)
                service._new_translation_provider.assert_not_called()
            finally:
                repo.close()
                control.close()

    def test_history_projection_requires_matching_identity_and_context(self):
        service = TranslationMixin()
        service._lock = threading.RLock()
        service.seller_id = 'seller-a'
        service.mode = 'live'
        service._translation_settings = {'model': ''}
        identity = {'kind': 'message', 'source_id': 'm', 'field': None, 'source_hash': 'hash'}
        payload = {'text': 'original', 'context': []}
        service._translation_sources = lambda cid, items: [(identity, 'new-key', payload)]
        service._control = Mock()
        service._control.get.side_effect = lambda kind, key, default=None: default
        service.repo = Mock()
        record = {**identity, 'conversation_id': 'c', 'seller_id': 'seller-a', 'status': 'ready',
                  'context_hash': _hash([]), 'text': 'old translation'}
        service.repo.get.return_value = None
        service.repo.list.return_value = [record]
        self.assertEqual(service.get_translations('c', [{}])['items'][0]['text'], 'old translation')
        for changed in ({'seller_id': 'seller-b'}, {'source_hash': 'other'}, {'context_hash': _hash(['changed'])}):
            service.repo.list.return_value = [{**record, **changed}]
            self.assertEqual(service.get_translations('c', [{}])['items'][0]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
