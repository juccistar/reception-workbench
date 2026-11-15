import threading
import unittest
from dataclasses import dataclass

from reception_service.execution import BusinessQueue
from reception_service.translation_provider import RouterTranslator, TranslationProviderError
from reception_service.translation_service import TranslationMixin, _hash


@dataclass(frozen=True)
class Snapshot:
    profile_id: str


class Router:
    def __init__(self):
        self.snapshot = Snapshot('first')
        self.calls = []

    def resolve(self, purpose, required_capabilities=()):
        return self.snapshot

    def assert_current(self, snapshot):
        if snapshot != self.snapshot:
            raise RuntimeError('stale')

    def generate(self, purpose, system_prompt, user_payload, **kwargs):
        self.calls.append((purpose, user_payload, kwargs))
        return {'output': {'items': [{'id': row['id'], 'text': '译文'} for row in user_payload['items']]}}


class ExecutionTranslationTests(unittest.TestCase):
    def test_claim_stays_owned_until_release(self):
        work = BusinessQueue()
        work.put(('job', 'generate_draft', {}, None, False))
        item = work.get_nowait()
        work.defer_completion(item)
        self.assertFalse(work.complete(item))
        self.assertEqual(work.unfinished_tasks, 1)
        self.assertTrue(work.release_completion(item))
        self.assertEqual(work.unfinished_tasks, 0)

    def test_router_translation_and_stale_snapshot(self):
        router = Router()
        provider = RouterTranslator(router)
        items = [{'key': 'private-key', 'text': 'hello', 'speaker': 'buyer', 'context': []}]
        self.assertEqual(provider.translate(items, 'job')['items'], [{'key': 'private-key', 'text': '译文'}])
        self.assertEqual(router.calls[0][0], 'translation')
        self.assertEqual(router.calls[0][1]['items'][0]['id'], 't1')
        self.assertNotIn('private-key', str(router.calls[0][1]))
        router.snapshot = Snapshot('second')
        with self.assertRaises(RuntimeError):
            provider.translate(items, 'job')
        self.assertEqual(len(router.calls), 1)

    def test_profile_changes_cache_identity(self):
        router = Router()
        service = TranslationMixin()
        service.mode = 'live'
        service._translation_router = router
        first = _hash(service._translation_cache_model())
        router.snapshot = Snapshot('second')
        self.assertNotEqual(first, _hash(service._translation_cache_model()))

    def test_cancel_does_not_call_router(self):
        router = Router()
        provider = RouterTranslator(router)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TranslationProviderError):
            provider.translate([], 'job', cancel_event=cancel)
        self.assertEqual(router.calls, [])


if __name__ == '__main__':
    unittest.main()
