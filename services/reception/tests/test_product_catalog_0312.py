import tempfile
import threading
import unittest
from pathlib import Path

from reception_service.errors import ServiceError
from reception_service.product_catalog import buyer_note, candidate, lookup, reference
from reception_service.repository import Repository


class ProductCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'db.sqlite3'
        self.repo = Repository(self.path, '974310521')
        self.context = {'recent_messages': [{'message_id': 'm1', 'role': 'buyer', 'text': "This is a inquiry card. Inquiry id is INQ123; Product id is 123456789; Product Name is Custom Logo Bag; The note of the buyer's inquiry is 100 pieces"}]}
        self.found = candidate(self.context)
        self.cancel = threading.Event()

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_reply_target_in_historical_evidence_only(self):
        old = self.context['recent_messages'][0]
        historical = {'message_id': 'old-unrelated', 'role': 'buyer', 'text': 'Product id is 11111'}
        recent = {'message_id': 'seller-ack', 'role': 'seller', 'text': 'Thank you'}
        context = {'recent_messages': [recent], 'historical_evidence': [historical, old],
                   'reply_target': {'source_message_id': old['message_id']}}
        self.assertEqual(candidate(context)['product_id'], '123456789')
        context['reply_target']['source_message_id'] = 'missing'
        self.assertIsNone(candidate(context))

    def test_semantic_card_and_title_evidence(self):
        self.assertEqual(self.found['product_id'], '123456789')
        self.assertEqual(self.found['inquiry_ids'], ['INQ123'])
        self.assertNotIn('Custom Logo', buyer_note(self.context['recent_messages'][0]['text']))

    def test_no_link_is_reused_after_reopen(self):
        class Facade:
            calls = 0
            def lookup_product(self, seller_id, product_id, request_id, *, cancel_event=None):
                self.calls += 1
                return {'product_id': product_id, 'product_url': None, 'summary': ''}
        facade = Facade()
        first = lookup(self.repo, '974310521', self.found, facade, 'one', self.cancel)
        self.assertEqual(first['lookup']['status'], 'no_link')
        self.repo.close()
        self.repo = Repository(self.path, '974310521')
        second = lookup(self.repo, '974310521', self.found, facade, 'two', self.cancel)
        self.assertEqual(second['catalog_product_id'], first['catalog_product_id'])
        self.assertEqual(facade.calls, 1)

    def test_concurrent_claim_and_not_applicable_is_local(self):
        entered, release = threading.Event(), threading.Event()
        class Facade:
            calls = 0
            def lookup_product(self, seller_id, product_id, request_id, *, cancel_event=None):
                self.calls += 1
                entered.set()
                release.wait(5)
                return {'product_id': product_id, 'product_url': 'https://www.alibaba.com/product-detail/item_123456789.html', 'summary': ''}
        facade = Facade()
        results = []
        t = threading.Thread(target=lambda: results.append(lookup(self.repo, '974310521', self.found, facade, 'one', self.cancel)))
        t.start()
        self.assertTrue(entered.wait(5))
        pending = lookup(self.repo, '974310521', self.found, facade, 'two', self.cancel)
        self.assertEqual(pending['lookup']['status'], 'querying')
        release.set()
        t.join(5)
        self.assertEqual(facade.calls, 1)
        a = reference(self.found, results[0], {'suitability': 'not_applicable', 'suitability_reason': 'another model'}, self.context)
        b = reference(self.found, results[0], {'suitability': 'applicable'}, self.context)
        self.assertIsNone(a['product_id'])
        self.assertEqual(a['observed_product_id'], b['product_id'])
        self.assertEqual(a['catalog_product_id'], b['catalog_product_id'])

    def test_source_url_and_id_must_match(self):
        source = self.context['recent_messages'][0]
        source['text'] += ' https://www.alibaba.com/product-detail/item_123456789.html?tracking=1'
        row = {'catalog_product_id': 'x', 'lookup': {'status': 'no_link', 'reason': '', 'queried_at': 1}}
        ref = reference(self.found, row, {'product_url': 'https://www.alibaba.com/product-detail/item_999123456789.html'}, self.context)
        self.assertEqual(ref['product_url'], 'https://www.alibaba.com/product-detail/item_123456789.html')
        bad = reference(self.found, row, {'product_id': '2', 'product_images': ['forged'], 'suitability': 'not_applicable'}, self.context, ['safe'])
        self.assertEqual(bad['suitability'], 'unreviewed')
        self.assertEqual(bad['product_images'], ['safe'])

    def test_failure_and_untrusted_url(self):
        class Facade:
            calls = 0
            def lookup_product(self, *args, **kwargs):
                self.calls += 1
                raise ServiceError('lookup_failed', 'failed')
        facade = Facade()
        row = lookup(self.repo, '974310521', self.found, facade, 'one', self.cancel)
        self.assertEqual(row['lookup']['status'], 'failed')
        self.repo.close()
        self.repo = Repository(self.path, '974310521')
        self.assertEqual(lookup(self.repo, '974310521', self.found, facade, 'two', self.cancel)['lookup']['status'], 'failed')
        self.assertEqual(facade.calls, 1)
        result = reference(self.found, row, {'product_url': 'https://evil.example/item'}, self.context)
        self.assertIsNone(result['product_url'])
        self.assertEqual(result['product_images'], [])
        self.assertIsNone(result['sku'])

    def test_unavailable_and_interrupted_are_persisted(self):
        unavailable = lookup(self.repo, '974310521', self.found, object(), 'one', self.cancel)
        self.assertEqual(unavailable['lookup']['status'], 'no_link')
        self.assertEqual(unavailable['lookup']['reason'], 'unavailable')
        class Facade:
            def lookup_product(self, *args, **kwargs):
                raise ServiceError('native_timeout', 'timeout')
        other = dict(self.found, product_id='87654321')
        self.assertEqual(lookup(self.repo, '974310521', other, Facade(), 'two', self.cancel)['lookup']['status'], 'unknown')


class ProductServiceTests(unittest.TestCase):
    def setUp(self):
        from reception_service.service import ReceptionService
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ReceptionService(Path(self.tmp.name))
        self.service.settings['required_fields'] = ['product', 'quantity', 'specifications', 'destination']
        self.facade = self.service.integrations
        self.lookups = []
        self.contexts = []
        self.output_suitability = 'applicable'
        self.lookup_entered = threading.Event()
        self.lookup_release = threading.Event()
        self.block = False
        def lookup_product(seller_id, product_id, request_id, *, cancel_event=None):
            self.lookups.append(product_id)
            self.lookup_entered.set()
            if self.block:
                self.lookup_release.wait(5)
            return {'product_id': product_id, 'product_url': None, 'summary': ''}
        def generate(context, request_id, *, on_call_start=None, **kwargs):
            self.contexts.append(context)
            if on_call_start:
                on_call_start()
            messages = context['recent_messages'] + context['historical_evidence']
            facts = {}
            for message in messages:
                if message['role'] != 'buyer':
                    continue
                for key, value in [('quantity', '100 pcs'), ('specifications', 'blue'), ('destination', 'Spain')]:
                    if value in message['text']:
                        facts[key] = {'value': value, 'evidence_ids': [message['message_id']]}
            source = next(m['message_id'] for m in reversed(context['recent_messages']) if m['role'] == 'buyer')
            return {'action': 'ask', 'reply': 'Thank you for your inquiry.', 'facts': facts,
                    'question_keys': [], 'reply_sources': [source], 'summary': 'inquiry',
                    'procurement_key': 'main', 'new_case': False, 'missing': [], 'conflicts': [],
                    'product_ref': {'product_id': '123456789', 'suitability': self.output_suitability,
                                    'suitability_reason': 'customer selection'}}
        self.facade.lookup_product = lookup_product
        self.facade.generate = generate
        for cid in ('demo-emma', 'demo-sofia'):
            case = self.service.get_conversation(cid)['case']
            case['facts'] = {}
            self.service.repo.put('case', case['case_id'], case)

    def tearDown(self):
        self.lookup_release.set()
        self.service.close()
        self.tmp.cleanup()

    def wait(self, ack):
        import time
        end = time.monotonic() + 8
        while time.monotonic() < end:
            job = self.service.repo.get('job', ack['job_id'])
            if job and job['status'] not in ('queued', 'running'):
                return job
            threading.Event().wait(.01)
        self.fail('job timed out')

    def generate(self, cid):
        case = self.service.get_conversation(cid)['case']
        return self.wait(self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']}))

    def card(self, cid):
        self.service.inject_demo(cid, "This is a inquiry card. Inquiry id is I123; Product id is 123456789; Product Name is Custom Logo Bag; The note of the buyer's inquiry is this product")

    def test_two_customers_reuse_and_waiting_quote(self):
        self.card('demo-emma')
        self.assertEqual(self.generate('demo-emma')['status'], 'succeeded')
        a = self.service.get_conversation('demo-emma')['case']
        self.assertNotIn('customization', a['facts'])
        self.service.inject_demo('demo-emma', '100 pcs blue to Spain')
        self.assertEqual(self.generate('demo-emma')['status'], 'succeeded')
        a = self.service.get_conversation('demo-emma')['case']
        self.assertEqual(a['state'], 'waiting_quote')
        self.assertNotIn('product', a['facts'])
        self.card('demo-sofia')
        self.assertEqual(self.generate('demo-sofia')['status'], 'succeeded')
        b = self.service.get_conversation('demo-sofia')['case']
        self.assertEqual(a['product_ref']['catalog_product_id'], b['product_ref']['catalog_product_id'])
        self.assertEqual(a['facts']['quantity']['value'], '100 pcs')
        self.assertNotIn('quantity', b['facts'])
        self.assertEqual(self.lookups, ['123456789'])
        self.assertEqual(self.service.get_snapshot()['handoff_cases'][0]['product_ref']['product_id'], '123456789')

    def test_message_link_is_shared_without_overwriting_lookup_evidence(self):
        url = 'https://www.alibaba.com/product-detail/bag_123456789.html'
        self.service.inject_demo('demo-emma', "This is a inquiry card. Inquiry id is I123; Product id is 123456789; Product Name is Custom Logo Bag; The note of the buyer's inquiry is this product " + url + '?private_tracking=secret')
        self.assertEqual(self.generate('demo-emma')['status'], 'succeeded')
        row = self.service.repo.list('product_catalog')[0]
        self.assertEqual(row['lookup']['status'], 'no_link')
        self.assertIsNone(row['result']['product_url'])
        self.assertEqual(row['known_product'], {'product_url': url, 'source': 'message_product_url', 'reference_only': True})
        self.card('demo-sofia')
        self.assertEqual(self.generate('demo-sofia')['status'], 'succeeded')
        b = self.service.get_conversation('demo-sofia')['case']['product_ref']
        self.assertEqual(b['product_url'], url)
        self.assertEqual(self.contexts[-1]['product_context']['catalog']['known_product']['product_url'], url)
        self.assertEqual(self.lookups, ['123456789'])
        self.assertNotIn('secret', str(row['known_product']))
        self.assertNotIn('I123', str(row['known_product']))

    def test_requery_rpc_and_source_invalidation(self):
        self.card('demo-emma')
        self.assertEqual(self.generate('demo-emma')['status'], 'succeeded')
        case = self.service.get_conversation('demo-emma')['case']
        params = {'case_id': case['case_id'], 'expected_product_id': '123456789', 'client_request_id': 'manual-lookup-1'}
        ack = self.service.call('request_product_lookup', params)
        self.assertEqual(self.service.call('request_product_lookup', params), ack)
        self.assertEqual(self.wait(ack)['status'], 'succeeded')
        self.assertEqual(len(self.lookups), 2)
        message = self.service._latest_buyer('demo-emma')
        self.service._ingest({'conversation_id': 'demo-emma', 'buyer_id': message['buyer_id']}, [{**message, 'text': 'No longer that product'}])
        ref = self.service.get_conversation('demo-emma')['case']['product_ref']
        self.assertTrue(ref['source_invalid'])
        with self.assertRaises(ServiceError):
            self.service.call('request_product_lookup', {**params, 'client_request_id': 'manual-lookup-2'})

    def test_current_case_image_references_match_media_items(self):
        from reception_service.photos import photo_version
        from reception_service.repository import encoded
        case = self.service.get_conversation('demo-emma')['case']
        image = {'seller_id': case['seller_id'], 'buyer_id': case['buyer_id'], 'conversation_id': case['conversation_id'],
                 'message_id': 'synthetic-image', 'role': 'buyer', 'text': '[image]', 'timestamp_ms': 10,
                 'attachments': [], 'own': False}
        self.service.repo.put('message', encoded([case['conversation_id'], image['message_id']]), image)
        self.service.repo.put('requirement_media', case['case_id'], {
            'seller_id': case['seller_id'], 'buyer_id': case['buyer_id'], 'conversation_id': case['conversation_id'],
            'sources': {image['message_id']: {'enabled': True, 'source_version': photo_version(image)}}})
        refs = self.service._product_image_refs(case)
        media = self.service.get_requirement_media(case['case_id'])['items']
        self.assertEqual(len(refs), 1)
        self.assertTrue(any(item['media_id'] == refs[0] and item['is_product_source'] for item in media))
        self.assertEqual(self.service._product_image_refs(self.service.get_conversation('demo-sofia')['case']), [])

    def test_sku_is_taken_only_from_matching_current_card(self):
        from reception_service.repository import encoded
        self.card('demo-emma')
        message = self.service._latest_buyer('demo-emma')
        message['attachments'] = [{'kind': 'native-content', 'content': {'cardType': 'product',
                                   'fields': {'productId': '123456789', 'skus': [{'label': 'red'}]}}}]
        with self.service._lock:
            self.service.repo.put('message', encoded([message['conversation_id'], message['message_id']]), message)
            case = self.service.get_conversation('demo-emma')['case']
            found = candidate(self.service.get_context(case['case_id']))
            self.assertEqual(self.service._product_sku(case, found), {'items': [{'label': 'red'}]})
            self.assertIsNone(self.service._product_sku(case, dict(found, product_id='555')))

    def test_concurrent_requery_reuses_job_without_blocking_local_save(self):
        self.card('demo-emma')
        self.assertEqual(self.generate('demo-emma')['status'], 'succeeded')
        case = self.service.get_conversation('demo-emma')['case']
        self.block = True
        self.lookup_entered.clear()
        params = {'case_id': case['case_id'], 'expected_product_id': '123456789', 'client_request_id': 'retry-a'}
        first = self.service.call('request_product_lookup', params)
        self.assertTrue(self.lookup_entered.wait(5))
        second = self.service.call('request_product_lookup', {**params, 'client_request_id': 'retry-b'})
        self.assertEqual(first, second)
        self.service.save_conversation_supplement('demo-emma', 'Synthetic local note', None, 'note-1')
        self.lookup_release.set()
        self.wait(first)
        self.assertEqual(len(self.lookups), 2)
        self.assertEqual(self.service.get_conversation('demo-emma')['supplement']['body'], 'Synthetic local note')

    def test_title_is_not_buyer_customization_evidence(self):
        from reception_service.context import validated_facts
        self.card('demo-emma')
        case = self.service.get_conversation('demo-emma')['case']
        message = self.service._latest_buyer('demo-emma')
        result = validated_facts({'facts': {'customization': {'value': 'Custom Logo',
                                 'evidence_ids': [message['message_id']]}}}, self.service.get_context(case['case_id']))
        self.assertNotIn('customization', result)

    def test_not_applicable_customer_does_not_affect_other_customer(self):
        self.output_suitability = 'not_applicable'
        self.card('demo-emma')
        self.assertEqual(self.generate('demo-emma')['status'], 'succeeded')
        a = self.service.get_conversation('demo-emma')['case']['product_ref']
        self.assertIsNone(a['product_id'])
        self.assertEqual(a['observed_product_id'], '123456789')
        self.output_suitability = 'applicable'
        self.card('demo-sofia')
        self.assertEqual(self.generate('demo-sofia')['status'], 'succeeded')
        b = self.service.get_conversation('demo-sofia')['case']['product_ref']
        self.assertEqual(b['product_id'], a['observed_product_id'])
        self.assertEqual(a['catalog_product_id'], b['catalog_product_id'])
        self.assertEqual(len(self.lookups), 1)

    def test_restart_converts_claim_to_unknown_without_retry(self):
        from reception_service.product_catalog import claim
        from reception_service.service import ReceptionService
        row, _ = claim(self.service.repo, self.service.seller_id, '987654321', 'interrupted')
        self.service.close()
        self.service = ReceptionService(Path(self.tmp.name))
        row = self.service.repo.get('product_catalog', row['catalog_product_id'])
        self.assertEqual(row['lookup']['status'], 'unknown')
        again, claimed = claim(self.service.repo, self.service.seller_id, '987654321', 'new-job')
        self.assertFalse(claimed)
        self.assertEqual(again['lookup']['status'], 'unknown')

    def test_manual_suggestion_and_organize_use_shared_catalog(self):
        self.card('demo-emma')
        def manual(context, request_id, *, on_call_start=None, **kwargs):
            on_call_start()
            self.assertEqual(context['product_context']['catalog']['lookup']['status'], 'no_link')
            return {'reply': 'Thank you.', 'product_ref': {'product_id': '123456789', 'suitability': 'applicable'}}
        self.facade.generate_manual = manual
        case = self.service.get_conversation('demo-emma')['case']
        ack = self.service.generate_manual_suggestion('demo-emma', 'manual-1', case['case_id'])
        self.assertEqual(self.wait(ack)['status'], 'succeeded')
        view = self.service.get_conversation('demo-emma')
        self.assertEqual(view['case']['product_ref']['product_id'], '123456789')
        self.assertEqual(view['manual_suggestion']['status'], 'ready')
        def requirements(context, request_id, *, on_call_start=None, **kwargs):
            on_call_start()
            return {'facts': {}, 'product_ref': {'product_id': '123456789', 'suitability': 'applicable'}}
        self.facade.generate_requirements = requirements
        ack = self.service.organize_requirements('demo-emma', case['case_id'], 'organize-1')
        self.assertEqual(self.wait(ack)['status'], 'succeeded')
        proposal = self.service.repo.list('requirement_proposal')[0]
        self.assertEqual(proposal['product_ref']['product_id'], '123456789')
        self.assertEqual(self.lookups, ['123456789'])

    def test_takeover_during_lookup_prevents_case_application(self):
        self.card('demo-emma')
        self.block = True
        case = self.service.get_conversation('demo-emma')['case']
        ack = self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']})
        self.assertTrue(self.lookup_entered.wait(5))
        self.service.takeover('demo-emma', True)
        self.lookup_release.set()
        self.assertNotEqual(self.wait(ack)['status'], 'succeeded')
        self.assertNotIn('product_ref', self.service.get_conversation('demo-emma')['case'])

    def test_source_change_during_lookup_does_not_apply_case(self):
        self.service.inject_demo('demo-emma', 'Product id is 123456789; https://www.alibaba.com/product-detail/bag_123456789.html')
        self.block = True
        case = self.service.get_conversation('demo-emma')['case']
        ack = self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']})
        self.assertTrue(self.lookup_entered.wait(5))
        self.service.inject_demo('demo-emma', 'Actually another item')
        self.lookup_release.set()
        self.assertNotEqual(self.wait(ack)['status'], 'succeeded')
        self.assertNotIn('product_ref', self.service.get_conversation('demo-emma')['case'])
        catalog = self.service.repo.list('product_catalog')[0]
        self.assertEqual(catalog['lookup']['status'], 'no_link')
        self.assertNotIn('known_product', catalog)


if __name__ == '__main__':
    unittest.main()
