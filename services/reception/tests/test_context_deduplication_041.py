"""Request-local deduplication checks with isolated synthetic data."""
import json
import unittest

import test_whatsapp_account_media_041 as wa_helpers
from reception_service.model_inputs import resolve_whatsapp_images
from reception_service.errors import ServiceError

from reception_service.integrations.reception_prompts import MANUAL
from reception_service.photos import photo_version
from reception_service.reception_context import dialogue_window, shared_context
from reception_service.repository import Repository, encoded
import test_native_image_requests_041 as native_helpers


class ContextDeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.native = native_helpers.NativeImageRequests('test_business_requests_and_text_only')
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.repo = Repository(None, 's')
        self.addCleanup(self.repo.close)
        self.owner = dict(seller_id='s', buyer_id='b', conversation_id='c', case_id='active')
        self.repo.put('conversation', 'c', self.owner)

    def message(self, mid, text, timestamp, role='buyer', **extra):
        row = dict(seller_id='s', buyer_id='b', conversation_id='c', message_id=mid,
                   role=role, own=role == 'seller', text=text, timestamp_ms=timestamp, attachments=[], **extra)
        self.repo.put('message', encoded(['c', mid]), row)
        return row

    def ready(self, row):
        asset = self.native.asset
        self.repo.put('image_asset', asset['sha256'], asset)
        self.repo.put('message_image', encoded(['c', row['message_id']]), {
            **{k: row[k] for k in ('seller_id', 'buyer_id', 'conversation_id', 'message_id')},
            'source_version': photo_version(row), 'sha256': asset['sha256'], 'status': 'ready'})

    def test_repeated_references_single_image_and_history_on_wire_each_request(self):
        image = self.message('image', '[image]', 1)
        self.ready(image)
        self.message('old', 'unique historical text', 2)
        for i in range(51):
            self.message('text-%02d' % i, 'same body', i + 3)
        self.message('quote1', 'I want this one', 60, referenced_message_id='image')
        self.message('quote2', 'I want this one', 61, referenced_message_id='image')
        case = {**self.owner, 'revision': 1, 'state': 'collecting', 'requirements_format': 'ai-v1',
                'requirements': {'product_images': {'value': [], 'evidence_ids': ['image', 'image']},
                                 'customization': {'value': 'logo', 'evidence_ids': ['image', 'old']},
                                 'quantity': {'value': 'one', 'evidence_ids': ['image', 'old']}}}
        self.repo.put('case', 'active', case)
        context = shared_context(self.repo, self.owner, case, task_mode='manual')
        context['case'] = case
        ids = [r['message_id'] for r in context['historical_evidence'] + context['recent_messages']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids.count('image'), 1)
        self.assertEqual(ids.count('old'), 1)
        self.assertEqual(len(context['photo_context']['direct_images']), 1)
        self.assertEqual(len(context['image_manifest']), 1)
        inputs = [{'message': image, 'asset': self.native.asset}]
        for protocol in ('chat-completions', 'anthropic'):
            self.native.configure(protocol)
            for _ in range(2):
                self.native.facade._business_query(MANUAL, context, inputs)
                request = self.native.requests[-1]
                blocks = request['messages'][0 if protocol == 'anthropic' else 1]['content']
                self.assertEqual(len(blocks), 2)
                payload = json.loads(blocks[0]['text'])['context']
                self.assertEqual([x['message_id'] for x in payload['image_manifest']], ['image'])
                self.assertEqual(payload['image_manifest'][0]['role'], 'buyer')
                view = payload['message_view']
                self.assertEqual(sum(x['message_id'] == 'image' for x in view), 1)
                self.assertEqual(sum(x['message_id'] == 'old' for x in view), 1)
                self.assertGreater(sum(x['text'] == 'same body' for x in view), 1)

    def test_recent_and_required_history_do_not_overlap(self):
        messages = [dict(message_id=str(i), role='buyer', text='same', timestamp_ms=i) for i in range(55)]
        recent, history = dialogue_window(messages, {'evidence_ids': ['0', '0', '10', '10']},
                                          {'source_message_ids': ['0', '10']}, ['0', '10'])
        self.assertEqual(len(recent), 50)
        self.assertEqual([x['message_id'] for x in history], ['0'])
        self.assertIn('10', [x['message_id'] for x in recent])

    def test_same_pixels_different_buyer_and_seller_sources_remain_distinct(self):
        buyer = self.message('buyer-photo', '[image]', 1)
        seller = self.message('seller-photo', '[image]', 2, role='seller')
        self.message('quote', 'I want this one', 3, referenced_message_id='seller-photo')
        self.ready(buyer)
        self.ready(seller)
        case = {**self.owner, 'revision': 1, 'state': 'collecting', 'requirements': {
            'product_images': {'value': [], 'evidence_ids': ['buyer-photo']}}}
        self.repo.put('case', 'active', case)
        context = shared_context(self.repo, self.owner, case, task_mode='manual')
        refs = context['photo_context']['direct_images']
        self.assertEqual([(x['message_id'], x['role']) for x in refs],
                         [('buyer-photo', 'buyer'), ('seller-photo', 'seller')])
        self.assertEqual(refs[0]['image_sha256'], refs[1]['image_sha256'])
        self.assertNotEqual(refs[0]['image_id'], refs[1]['image_id'])
        self.assertEqual([x['alias'] for x in context['image_manifest']], ['p1', 'p2'])

    def test_ready_and_pending_references_attach_only_ready_once(self):
        ready = self.message('ready', '[image]', 1)
        pending = self.message('pending', '[image]', 2)
        self.ready(ready)
        self.repo.put('message_image', encoded(['c', 'pending']), {
            **{k: pending[k] for k in ('seller_id', 'buyer_id', 'conversation_id', 'message_id')},
            'source_version': photo_version(pending), 'status': 'pending'})
        case = {**self.owner, 'revision': 1, 'state': 'collecting', 'requirements': {
            'customization': {'value': 'images', 'evidence_ids': ['ready', 'pending', 'ready']},
            'product_images': {'value': [], 'evidence_ids': ['pending', 'ready']}}}
        self.repo.put('case', 'active', case)
        context = shared_context(self.repo, self.owner, case, task_mode='manual')
        self.assertEqual([x['message_id'] for x in context['photo_context']['direct_images']], ['ready'])
        self.assertEqual([x['message_id'] for x in context['image_manifest']], ['ready'])
        self.assertEqual({x['message_id'] for x in context['photo_context']['events']}, {'ready', 'pending'})

    def test_repeated_whatsapp_media_ingestion_retains_one_source(self):
        fixture = wa_helpers.MediaTests('test_resolver_and_model_evidence')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        identity = fixture.host.add()
        fixture.finish(identity)
        with self.assertRaises(ServiceError):
            fixture.finish(identity)
        refs, rows = fixture.host._account_image_context('聊天', 's')
        self.assertEqual(len(refs), 1)
        self.assertEqual(len(rows), 1)
        messages = [dict(rows[0], role='buyer')]
        images, manifest = resolve_whatsapp_images(
            {'model_image_refs': refs, 'whatsapp_messages': messages},
            lambda ref: fixture.host._account_resolve_image(ref, *identity[:3]))
        self.assertEqual(len(images), 1)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(images[0]['source_id'], rows[0]['source_id'])


if __name__ == '__main__':
    unittest.main()
