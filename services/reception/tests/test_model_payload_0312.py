"""Product references survive projection with explicit reference-only semantics."""
from copy import deepcopy
import json
import unittest

from reception_service.model_payload import model_json, project_model_payload
from reception_service.integrations.live import LiveIntegration

PID = '1601904072453'
URL = 'https://www.alibaba.com/product-detail/Synthetic-Bracelet_' + PID + '.html'


class ProductPayloadTests(unittest.TestCase):
    def context(self):
        return {'task_mode': 'automatic', 'case': {'procurement_key': 'main', 'facts': {}},
                'recent_messages': [{'message_id': 'm1', 'role': 'buyer', 'timestamp_ms': 1,
                                     'text': 'I want this item: ' + URL}],
                'historical_evidence': [], 'knowledge': [],
                'product_context': {'candidate': {'product_id': PID, 'source_message_ids': ['m1'],
                                                  'inquiry_ids': ['synthetic-inquiry'], 'original_title': 'Custom Logo Bracelet'},
                    'catalog': {'catalog_product_id': 'local-product', 'seller_id': 'private-owner', 'request_id': 'private-job',
                                'product_id': PID, 'lookup': {'status': 'completed', 'reason': '', 'queried_at': 1},
                                'result': {'product_id': PID, 'product_url': URL, 'summary': 'price_info: 10 USD; not verified',
                                           'reference_only': True, 'source': 'native_product_lookup'}},
                    'allowed_images': [{'media_id': 'media-m1', 'source_message_id': 'm1', 'role': 'buyer',
                                        'private_path': 'C:/must-not-be-exposed.png'}]}}

    def test_product_links_and_query_result_survive_with_no_tool_permission(self):
        payload = project_model_payload(self.context())
        self.assertIn(URL, payload['message_view'][0]['text'])
        product = payload['product_context']
        self.assertFalse(product['allow_product_lookup'])
        self.assertTrue(product['reference_only'])
        self.assertFalse(product['listing_details_verified'])
        self.assertEqual(product['catalog']['lookup']['status'], 'completed')
        self.assertEqual(product['catalog']['result']['product_url'], URL)
        self.assertIn('10 USD', product['catalog']['result']['summary'])
        encoded = json.dumps(payload)
        self.assertNotIn('private-owner', encoded)
        self.assertNotIn('private-job', encoded)
        self.assertNotIn('private_path', encoded)
        self.assertEqual(product['allowed_images'][0]['media_id'], 'media-m1')

    def test_no_link_and_case_nonadoption_are_independent_and_both_visible(self):
        context = self.context()
        context['product_context']['catalog']['lookup']['status'] = 'no_link'
        context['product_context']['catalog']['result']['product_url'] = None
        context['case']['product_ref'] = {'product_id': None, 'observed_product_id': PID,
            'product_url': None, 'suitability': 'not_applicable', 'suitability_reason': 'Customer wants a different design',
            'lookup': {'status': 'no_link', 'reason': 'No URL returned', 'queried_at': 1}}
        payload = project_model_payload(context)
        self.assertEqual(payload['product_context']['catalog']['lookup']['status'], 'no_link')
        self.assertFalse(payload['product_context']['allow_product_lookup'])
        self.assertEqual(payload['product_ref']['observed_product_id'], PID)
        self.assertEqual(payload['product_ref']['suitability'], 'not_applicable')

    def test_shared_public_link_is_available_without_rewriting_original_no_link_result(self):
        context = self.context()
        catalog = context['product_context']['catalog']
        catalog['lookup']['status'] = 'no_link'
        catalog['result']['product_url'] = None
        catalog['known_product'] = {'product_url': URL, 'customer_message': 'must stay private',
                                    'source_message_id': 'another-customer-source'}
        payload = project_model_payload(context)['product_context']['catalog']
        self.assertEqual(payload['lookup']['status'], 'no_link')
        self.assertIsNone(payload['result']['product_url'])
        self.assertEqual(payload['known_product']['product_url'], URL)
        self.assertNotIn('another-customer-source', json.dumps(payload))
        self.assertNotIn('must stay private', json.dumps(payload))

    def test_plain_product_link_is_kept_but_image_urls_and_raw_card_catalogue_are_not(self):
        context = self.context()
        context['recent_messages'][0]['text'] += ' image https://img.alicdn.com/source.png'
        context['recent_messages'].append({'message_id': 'm2', 'role': 'buyer', 'timestamp_ms': 2,
            'text': json.dumps({'cardType': 'product', 'fields': {'productUrl': URL, 'skus': [{'imageUrl': 'https://img.alicdn.com/sku.png'}]}})})
        payload = project_model_payload(context)
        self.assertIn(URL, payload['message_view'][0]['text'])
        self.assertNotIn('img.alicdn.com', json.dumps(payload))
        self.assertNotIn('skus', json.dumps(payload['message_view'][1]))

    def test_polish_keeps_frozen_literal_links_without_product_mutation_context(self):
        context = self.context()
        context.update(task_mode='polish', unsent_reply={'body': 'See ' + URL + '. Price is 15 USD, freight excluded.'})
        payload = project_model_payload(context)
        self.assertEqual(payload['unsent_reply']['body'], context['unsent_reply']['body'])
        self.assertNotIn('product_context', payload)
        self.assertNotIn('product_ref', payload)

    def test_invalid_optional_url_does_not_break_encoding_of_legitimate_context(self):
        context = self.context()
        context['product_context']['catalog']['result']['product_url'] = {'unexpected': 'object'}
        payload = json.loads(model_json(context))
        self.assertIsNone(payload['product_context']['catalog']['result']['product_url'])
        self.assertIn('I want this item', payload['message_view'][0]['text'])

    def test_live_manual_preserves_product_extension_but_polish_does_not(self):
        live = object.__new__(LiveIntegration)
        live.resolve_identity = lambda seller: seller
        raw = {'reply': 'Thank you for the details.', 'product_ref': {'product_id': PID}}
        live._assistance_query = lambda *args, **kwargs: deepcopy(raw)
        context = {'seller_id': '1', 'buyer_id': '2', 'conversation_id': '1-2#test',
                   'case_id': None, 'supplement': '', 'recent_messages': []}
        self.assertEqual(live.generate_manual(context, 'r')['product_ref']['product_id'], PID)
        context['unsent_reply'] = {'body': 'Thank you.'}
        self.assertNotIn('product_ref', live.generate_manual(context, 'polish'))

    def test_requirements_output_retains_raw_reference_for_service_validation(self):
        live = object.__new__(LiveIntegration)
        live.resolve_identity = lambda seller: seller
        live._assistance_query = lambda *args, **kwargs: {'facts': {}, 'product_ref': {'product_id': PID}}
        context = {'case': {'seller_id': '1', 'buyer_id': '2', 'conversation_id': '1-2#test'},
                   'recent_messages': []}
        self.assertEqual(live.generate_requirements(context, 'r')['product_ref']['product_id'], PID)


if __name__ == '__main__':
    unittest.main()
