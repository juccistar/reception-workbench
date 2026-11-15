"""Retired product lookup boundaries and current AI-authored product bindings."""
import copy
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test_support'))
from reception_test_support.service import SyntheticReceptionService, create_synthetic_service
from reception_service.errors import ServiceError
from reception_service.product_catalog import buyer_note, candidate, lookup, reference, project_case
from reception_service.repository import Repository


class ProductCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'db.sqlite3'
        self.repo = Repository(self.path, '974310521')
        self.message = {'message_id': 'm1', 'role': 'buyer', 'text':
            "This is a inquiry card. Inquiry id is INQ123; Product id is 123456789; Product Name is Custom Logo Bag; The note of the buyer's inquiry is 100 pieces"}

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_current_and_historical_cards_or_links_are_not_programmatically_parsed(self):
        old = {**self.message, 'text': self.message['text'] + ' https://www.alibaba.com/product-detail/bag_123456789.html'}
        for context in ({'recent_messages': [old]},
                        {'recent_messages': [], 'historical_evidence': [old], 'reply_target': {'source_message_id': 'm1'}},
                        {'recent_messages': [{**old, 'attachments': [{'cardType': 'product', 'fields': {'productId': '123456789'}}]}]}):
            with self.subTest(context=context):
                before = copy.deepcopy(context)
                self.assertIsNone(candidate(context))
                self.assertEqual(context, before)
        self.assertEqual(buyer_note(self.message['text']), self.message['text'])

    def test_retired_lookup_refuses_without_network_or_persistent_mutation(self):
        facade = Mock(spec=['lookup_product'])
        historical = {'catalog_product_id': 'old', 'product_id': '123456789',
                      'lookup': {'status': 'unknown'}, 'request_id': 'original-request'}
        self.repo.put('product_catalog', 'old', historical, history=True)
        with self.assertRaises(ServiceError) as caught:
            lookup(self.repo, '974310521', {'product_id': '123456789'}, facade, 'new-request', threading.Event())
        self.assertEqual(caught.exception.code, 'product_lookup_retired')
        facade.lookup_product.assert_not_called()
        self.assertEqual(self.repo.list('product_catalog'), [historical])
        self.repo.close()
        self.repo = Repository(self.path, '974310521')
        self.assertEqual(self.repo.get('product_catalog', 'old'), historical)

    def test_retired_reference_and_source_link_do_not_recreate_a_catalogue(self):
        from reception_service.product_catalog import source_url, remember_source_url
        found = {'product_id': '123456789', 'source_message_ids': ['m1']}
        old = {'catalog_product_id': 'old', 'lookup': {'status': 'unknown'}}
        context = {'recent_messages': [self.message]}
        raw = {'product_url': 'https://www.alibaba.com/product-detail/bag_123456789.html',
               'sku': {'items': ['red']}, 'product_images': ['unverified-image']}
        before = copy.deepcopy((found, old, context, raw))
        self.assertIsNone(reference(found, old, raw, context, ['unverified-image'], raw['sku']))
        self.assertIsNone(source_url(found, context))
        self.assertEqual(remember_source_url(self.repo, found, context, old), old)
        self.assertEqual((found, old, context, raw), before)
        self.assertEqual(self.repo.list('product_catalog'), [])

    def test_case_projection_hides_legacy_fields_without_deleting_stored_history(self):
        case = {'case_id': 'c', 'seller_id': '974310521', 'conversation_id': 'buyer-c',
                'revision': 3, 'requirements_format': 'ai-v1', 'requirements': {
                    'product_id': {'value': '123456789', 'evidence_ids': ['m1']},
                    'product_url': {'value': 'https://wrong.invalid/item', 'evidence_ids': ['m1']}},
                'facts': {'product': {'value': 'old title'}}, 'summary': 'old summary',
                'product_ref': {'catalog_product_id': 'old'}, 'requirement_items': [{'value': 'old'}]}
        self.repo.put('case', 'c', case, history=True)
        projected = project_case(self.repo, case)
        self.assertEqual(projected['requirements']['product_url']['value'],
                         'https://www.alibaba.com/product-detail/_123456789.html')
        self.assertEqual(projected['facts'], {})
        for field in ('summary', 'product_ref', 'requirement_items'):
            self.assertNotIn(field, projected)
        self.assertEqual(self.repo.get('case', 'c'), case)

    def test_custom_product_projection_has_no_platform_link(self):
        case = {'case_id': 'c', 'seller_id': '974310521', 'conversation_id': 'buyer-c',
                'requirements': {'product_id': {'value': '定制-1234567', 'evidence_ids': ['m1']},
                                 'product_url': {'value': 'https://wrong.invalid/item', 'evidence_ids': ['m1']}}}
        self.assertIsNone(project_case(self.repo, case)['requirements']['product_url']['value'])
        self.assertEqual(case['requirements']['product_url']['value'], 'https://wrong.invalid/item')


class ProductServiceTests(unittest.TestCase):
    def setUp(self):
        # Drive explicit generation here; background history refresh belongs to
        # the monitor tests and otherwise changes this fixture's frozen coverage.
        scheduler = patch.object(SyntheticReceptionService, '_schedule', return_value=None)
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.service = create_synthetic_service(Path(self.tmp.name))
        self.service.settings['required_fields'] = ['product', 'quantity', 'specifications', 'destination']
        self.facade = self.service.integrations
        self.facade.lookup_product = Mock(side_effect=AssertionError('retired lookup must not run'))
        self.contexts = []
        self.fields = {}
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False

        def generate(context, request_id, *, on_call_start=None, **kwargs):
            self.contexts.append(context)
            if on_call_start:
                on_call_start()
            self.entered.set()
            if self.block:
                self.release.wait(5)
            source = next(m['message_id'] for m in reversed(context['recent_messages']) if m['role'] == 'buyer')
            output = {'action': 'ask', 'reply_messages': [{'body': 'Thank you for your inquiry.', 'reply_sources': [source]}],
                      'conversation_summary': '客户咨询商品。'}
            if self.fields:
                output['requirement_changes'] = [{'action': 'update', 'case_ref': context['current_case_ref'],
                    'trigger_evidence_ids': [source], 'set_fields': {
                        key: {'value': value, 'evidence_ids': [source], 'source_excerpts': {source: excerpt}}
                        for key, (value, excerpt) in self.fields.items()}}]
            return output
        self.facade.generate = generate

    def tearDown(self):
        self.release.set()
        self.service.close()
        self.tmp.cleanup()

    def wait(self, ack):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            job = self.service.repo.get('job', ack['job_id'])
            if job and job['status'] not in ('queued', 'running'):
                return job
            threading.Event().wait(.01)
        self.fail('job timed out')

    def generate(self, cid):
        case = self.service.get_conversation(cid)['case']
        job = self.wait(self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']}))
        self.assertEqual(job['status'], 'succeeded', job)
        return job

    def demand(self, cid, quantity='100'):
        self.service.inject_demo(cid, f'I need bags 123456789, {quantity} pcs, blue, to Spain.')
        self.fields = {'product': ('手提袋', 'bags'), 'product_id': ('123456789', '123456789'),
                       'quantity': (quantity + '件', quantity + ' pcs'), 'specifications': ('蓝色', 'blue'),
                       'destination': ('西班牙', 'Spain')}

    def test_ai_product_id_reuses_product_without_sharing_customer_requirements(self):
        self.demand('demo-emma')
        self.assertEqual(self.generate('demo-emma')['status'], 'succeeded')
        a = self.service.get_conversation('demo-emma')['case']
        self.demand('demo-sofia', '20')
        self.assertEqual(self.generate('demo-sofia')['status'], 'succeeded')
        b = self.service.get_conversation('demo-sofia')['case']
        self.assertEqual(a['state'], 'waiting_quote')
        self.assertEqual(b['state'], 'waiting_quote')
        self.assertEqual(a['requirements']['quantity']['value'], '100件')
        self.assertEqual(b['requirements']['quantity']['value'], '20件')
        self.assertEqual(a['local_product_id'], b['local_product_id'])
        self.assertEqual(len(self.service.repo.list('library_product')), 1)
        self.assertEqual(len(self.service.repo.list('procurement_task')), 1)
        product = self.service.repo.list('library_product')[0]
        for field in ('quantity', 'destination', 'contact', 'budget'):
            self.assertNotIn(field, product)
        self.facade.lookup_product.assert_not_called()
        self.assertEqual(self.service.repo.list('product_catalog'), [])

    def test_card_or_link_alone_does_not_supply_product_customization_or_sku(self):
        text = "Product id is 123456789; Product Name is Custom Logo Bag; https://www.alibaba.com/product-detail/bag_123456789.html"
        self.service.inject_demo('demo-emma', text)
        message = self.service._latest_buyer('demo-emma')
        message['attachments'] = [{'kind': 'native-content', 'content': {'cardType': 'product',
                                  'fields': {'productId': '123456789', 'skus': [{'label': 'red'}]}}}]
        self.facade.messages = [copy.deepcopy(message) if row['message_id'] == message['message_id'] else row
                                for row in self.facade.messages]
        self.service._ingest({'conversation_id': 'demo-emma', 'buyer_id': message['buyer_id']}, [message])
        job = self.generate('demo-emma')
        self.assertEqual(job['status'], 'succeeded', job)
        case = self.service.get_conversation('demo-emma')['case']
        for field in ('product_id', 'customization'):
            self.assertFalse((case.get('requirements', {}).get(field) or {}).get('value'))
        self.assertIsNone(self.service._product_sku(case, {'product_id': '123456789'}))
        self.assertEqual(self.service.repo.list('library_product'), [])
        self.assertEqual(self.service._latest_buyer('demo-emma')['text'], text)
        self.facade.lookup_product.assert_not_called()

    def test_program_link_uses_formal_id_and_stays_out_of_model_requirement_fields(self):
        from reception_service.model_payload import project_model_payload
        self.demand('demo-emma')
        self.assertEqual(self.generate('demo-emma')['status'], 'succeeded')
        case = self.service.get_conversation('demo-emma')['case']
        self.assertEqual(case['requirements']['product_url']['value'],
                         'https://www.alibaba.com/product-detail/_123456789.html')
        context = self.service.get_context(case['case_id'])
        payload = project_model_payload(context)
        self.assertNotIn('product_url', payload['requirements'])
        self.assertNotIn('product_url', payload['case_catalog'][0]['requirements'])
        self.assertNotIn('product_url', self.service._product_context_for_case(case)['product_context'])

    def test_retired_requery_rpc_leaves_unknown_and_current_binding_unchanged(self):
        self.demand('demo-emma')
        self.assertEqual(self.generate('demo-emma')['status'], 'succeeded')
        case = self.service.get_conversation('demo-emma')['case']
        self.service.repo.put('operation', 'unknown-send', {'operation_id': 'unknown-send', 'status': 'unknown'})
        before = {kind: self.service.repo.list(kind) for kind in ('case', 'requirement_product_binding', 'product_catalog', 'operation', 'job')}
        with self.assertRaises(ServiceError) as caught:
            self.service.call('request_product_lookup', {'case_id': case['case_id'], 'expected_product_id': '123456789',
                                                        'client_request_id': 'retired-requery'})
        self.assertEqual(caught.exception.code, 'unknown_method')
        self.assertEqual({kind: self.service.repo.list(kind) for kind in before}, before)
        self.facade.lookup_product.assert_not_called()

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

    def test_restart_preserves_old_unknown_lookup_without_requery(self):
        from reception_service.product_catalog import claim
        row, _ = claim(self.service.repo, self.service.seller_id, '987654321', 'interrupted')
        self.service.close()
        self.service = create_synthetic_service(Path(self.tmp.name))
        row = self.service.repo.get('product_catalog', row['catalog_product_id'])
        self.assertEqual(row['lookup']['status'], 'unknown')
        again, claimed = claim(self.service.repo, self.service.seller_id, '987654321', 'new-job')
        self.assertFalse(claimed)
        self.assertEqual(again, row)
        self.assertEqual(again['request_id'], 'interrupted')

    def test_takeover_during_generation_prevents_product_and_requirement_application(self):
        self.demand('demo-emma')
        self.block = True
        case = self.service.get_conversation('demo-emma')['case']
        ack = self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']})
        self.assertTrue(self.entered.wait(5))
        self.service.takeover('demo-emma', True)
        self.release.set()
        self.assertNotEqual(self.wait(ack)['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('case', case['case_id']).get('requirements'), case.get('requirements'))
        self.assertEqual(self.service.repo.list('library_product'), [])
        self.assertEqual(self.service.repo.list('requirement_product_binding'), [])
        self.facade.lookup_product.assert_not_called()

    def test_source_change_during_generation_does_not_apply_old_product(self):
        self.demand('demo-emma')
        self.block = True
        case = self.service.get_conversation('demo-emma')['case']
        ack = self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']})
        self.assertTrue(self.entered.wait(5))
        self.service.inject_demo('demo-emma', 'Actually another item')
        self.release.set()
        self.assertNotEqual(self.wait(ack)['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('case', case['case_id']).get('requirements'), case.get('requirements'))
        self.assertEqual(self.service.repo.list('library_product'), [])
        self.assertEqual(self.service.repo.list('requirement_product_binding'), [])
        self.facade.lookup_product.assert_not_called()


if __name__ == '__main__':
    unittest.main()
