"""Current chat, Chinese requirements and display-image projection contracts."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from reception_service.model_payload import model_json, project_model_payload
from reception_service.integrations.live import LiveIntegration

PID = '1601904072453'
URL = 'https://www.alibaba.com/product-detail/Synthetic-Bracelet_' + PID + '.html'
OWNER = {'seller_id': 's', 'buyer_id': 'b', 'conversation_id': 'c'}


class ProductPayloadTests(unittest.TestCase):
    def context(self):
        message = {**OWNER, 'message_id': 'm1', 'role': 'buyer', 'timestamp_ms': 1,
                   'text': 'I want this item: ' + URL}
        requirements = {'product': {'value': '定制手链', 'evidence_ids': ['m1']},
                        'product_id': {'value': PID, 'evidence_ids': ['m1']},
                        'product_url': {'value': URL, 'evidence_ids': ['m1']}}
        return {**OWNER, 'task_mode': 'automatic', 'case': {**OWNER, 'procurement_key': 'main', 'requirements': requirements},
                'recent_messages': [message], 'historical_evidence': [], 'knowledge': [],
                'case_catalog': [{'case_ref': 'c1', 'state': 'collecting', 'requirements': deepcopy(requirements)}],
                'product_context': {'product_id': PID, 'product_url': URL,
                                    'product_main_image': {'url': 'https://example.com/display.png'}}}

    def test_chat_link_and_chinese_requirements_are_preserved(self):
        payload = project_model_payload(self.context())
        self.assertIn(URL, payload['message_view'][0]['text'])
        self.assertEqual(payload['requirements']['product']['value'], '定制手链')
        self.assertEqual(payload['requirements']['product_id']['value'], PID)
        self.assertNotIn('product_url', payload['requirements'])
        self.assertNotIn('product_url', payload['case_catalog'][0]['requirements'])

    def test_display_image_does_not_enter_model_context(self):
        payload = project_model_payload(self.context())
        self.assertEqual(payload['product_context'], {'product_id': PID})
        self.assertNotIn('display.png', json.dumps(payload))

    def test_derived_product_link_does_not_change_message_or_case(self):
        context = self.context()
        before = deepcopy(context)
        payload = project_model_payload(context)
        self.assertEqual(context, before)
        self.assertIn(URL, payload['message_view'][0]['text'])
        self.assertNotIn('product_url', payload['requirements'])

    def test_raw_chat_text_including_card_and_image_links_is_preserved(self):
        context = self.context()
        context['recent_messages'][0]['text'] += ' image https://img.alicdn.com/source.png'
        context['recent_messages'].append({**OWNER, 'message_id': 'm2', 'role': 'buyer', 'timestamp_ms': 2,
            'text': json.dumps({'cardType': 'product', 'fields': {'productUrl': URL, 'skus': [{'imageUrl': 'https://img.alicdn.com/sku.png'}]}})})
        payload = project_model_payload(context)
        self.assertEqual([r['text'] for r in payload['message_view']], [r['text'] for r in context['recent_messages']])

    def test_polish_keeps_literal_body_without_requirement_context(self):
        context = self.context()
        context.update(task_mode='polish', unsent_reply={'body': 'See ' + URL + '. Price is 15 USD, freight excluded.'})
        payload = project_model_payload(context)
        self.assertEqual(payload['unsent_reply']['body'], context['unsent_reply']['body'])
        self.assertNotIn('product_context', payload)
        self.assertNotIn('requirements', payload)
        self.assertNotIn('case_catalog', payload)

    def test_invalid_display_url_does_not_break_request_encoding(self):
        context = self.context()
        context['product_context']['product_url'] = {'unexpected': 'object'}
        payload = json.loads(model_json(context))['context']
        self.assertNotIn('product_url', payload['product_context'])
        self.assertIn('I want this item', payload['message_view'][0]['text'])

    def test_manual_returns_requirement_delta_and_polish_omits_it(self):
        live = object.__new__(LiveIntegration)
        live.resolve_identity = lambda seller: seller
        context = self.context()
        change = {'action': 'update', 'case_ref': 'c1', 'trigger_evidence_ids': ['m1'],
                  'set_fields': {'product': {'value': '手链', 'evidence_ids': ['m1'], 'source_excerpts': {'m1': 'this item'}}}}
        raw = {'reply_messages': [{'body': 'Thank you.', 'reply_sources': ['m1']}],
               'requirement_changes': [change], '_host_model_result': {}}
        live._assistance_query = lambda *args, **kwargs: deepcopy(raw)
        with patch('reception_service.integrations.live.target'):
            self.assertEqual(live.generate_manual(context, 'r')['requirement_changes'], [change])
            context['unsent_reply'] = {'body': 'Thank you.'}
            live._assistance_query = lambda *args, **kwargs: {'reply': 'Thank you.', '_host_model_result': {}}
            self.assertNotIn('requirement_changes', live.generate_manual(context, 'polish'))

    def test_requirements_returns_validated_delta(self):
        live = object.__new__(LiveIntegration)
        live.resolve_identity = lambda seller: seller
        context = self.context()
        change = {'action': 'update', 'case_ref': 'c1', 'trigger_evidence_ids': ['m1'],
                  'set_fields': {'product': {'value': '手链', 'evidence_ids': ['m1'], 'source_excerpts': {'m1': 'this item'}}}}
        live._assistance_query = lambda *args, **kwargs: {'requirement_changes': [change], '_host_model_result': {}}
        with patch('reception_service.integrations.live.target'):
            self.assertEqual(live.generate_requirements(context, 'r')['requirement_changes'], [change])


if __name__ == '__main__':
    unittest.main()
