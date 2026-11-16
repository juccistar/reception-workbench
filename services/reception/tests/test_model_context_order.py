"""Reception material, customer-context order and provider-wire regressions."""
from copy import deepcopy
import hashlib
import json
import unittest

import httpx

from reception_service.integrations.reception_prompts import (
    AUTOMATIC, MANUAL, POLISH, REQUIREMENTS, WHATSAPP, IMAGE_NATIVE,
)
from reception_service.model_payload import model_json, project_model_payload
from reception_service.model_providers.transport import ProviderTransport


PROMPTS = {'automatic': AUTOMATIC, 'manual_suggestion': MANUAL, 'polish': POLISH,
           'requirements': REQUIREMENTS, 'whatsapp_reply': WHATSAPP}


def context(mode='automatic'):
    policy = [{'source_id': 'p', 'title': 'policy', 'content': '固定规则' * 1500}]
    base = {'task_mode': mode, 'reply_target': {'source_message_id': 'a'},
            'conversation_summary': {'body': '摘要a'}, 'wa_summary': '摘要a',
            'knowledge': policy, 'guidance': policy, 'style': policy,
            'collection_rules': {'max_questions_per_reply': 2, 'max_questions_per_field': 2},
            'supplement': {'source_id': 's', 'body': '补充a', 'attachments': []},
            'recent_messages': [{'message_id': 'a', 'role': 'buyer', 'timestamp_ms': 1,
                                 'text': 'https://example.test/item?a=1'}],
            'case': {'requirements': {'quantity': {'value': '10', 'evidence_ids': ['a']}}},
            'product_context': {'product_id': '123', 'reference_only': True},
            'supplier_context': {'usable': True, 'source_id': 'offer-a', 'customer_wording': '运费另计'},
            'case_catalog': [{'case_ref': 'a', 'state': 'open', 'procurement_key': 'k',
                              'requirements': {}, 'knowledge': policy, 'guidance': policy, 'style': policy}],
            'unsent_reply': {'body': 'Frozen https://example.test/a?x=1'}}
    if mode == 'whatsapp_reply':
        base.update(whatsapp_messages=[{'source_id': 'a', 'text': 'hello', 'media': {'status': 'ready', 'private': 'x'}}],
                    international_background={'summary': {'body': '国际站摘要'}, 'messages': [{'text': '原消息'}],
                                              'requirements': {'quantity': '10'}, 'supplement': {'body': '原补充'}},
                    model_image_refs=['private'], extra_section={'unknown': ['x', 'y']})
    return base


def baseline(value):
    return json.dumps(project_model_payload(value), ensure_ascii=False, allow_nan=False)


def flatten(envelope):
    """Recover the original semantic projection from the new wire container."""
    return {**envelope['context'], **{key: value for key, value in envelope.items() if key != 'context'}}


def prefix(a, b):
    return next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))


class ContextOrderTests(unittest.TestCase):
    def test_projection_equivalence_and_no_mutation_for_all_modes(self):
        for mode in PROMPTS:
            with self.subTest(mode=mode):
                value = context(mode)
                original = deepcopy(value)
                projected = project_model_payload(value)
                envelope = json.loads(model_json(value))
                self.assertEqual(flatten(envelope), projected)
                self.assertEqual(value, original)
                material = [key for key in ('knowledge', 'guidance', 'style') if key in projected]
                self.assertEqual(list(envelope), [*material, 'context'])
                self.assertTrue(all(key not in envelope['context'] for key in material))
                if mode == 'whatsapp_reply':
                    self.assertEqual(envelope['context']['extra_section'], original['extra_section'])
                    self.assertNotIn('model_image_refs', envelope['context'])
                    self.assertEqual(envelope['context']['whatsapp_messages'][0]['media'], {'status': 'ready'})
                if mode == 'polish':
                    self.assertNotIn('guidance', envelope)
                    self.assertNotIn('case_catalog', envelope['context'])
                    self.assertEqual(envelope['context']['unsent_reply']['body'], value['unsent_reply']['body'])

    def test_customer_context_has_the_agreed_five_part_order(self):
        for mode in PROMPTS:
            with self.subTest(mode=mode):
                customer = json.loads(model_json(context(mode)))['context']
                keys = list(customer)
                self.assertEqual(keys[0], 'supplement')
                chat = 'whatsapp_messages' if mode == 'whatsapp_reply' else 'message_view'
                if mode != 'polish':
                    self.assertLess(keys.index('supplement'), keys.index('conversation_summary'))
                    requirements = 'international_background' if mode == 'whatsapp_reply' else 'requirements'
                    self.assertLess(keys.index('conversation_summary'), keys.index(requirements))
                    for background in ('case', 'requirements', 'product_context', 'supplier_context', 'case_catalog'):
                        if background in customer:
                            self.assertLess(keys.index(background), keys.index(chat))
                self.assertLess(keys.index(chat), keys.index('collection_rules'))
                self.assertLess(keys.index('collection_rules'), keys.index('reply_target'))
                if mode == 'whatsapp_reply':
                    self.assertEqual(list(customer['international_background']),
                                     ['supplement', 'summary', 'requirements', 'messages'])
                    self.assertLess(keys.index('international_background'), keys.index(chat))
                    self.assertLess(keys.index('extra_section'), keys.index('reply_target'))
                if mode == 'polish':
                    self.assertEqual(keys[-1], 'unsent_reply')

    def test_case_specific_material_and_supply_stay_with_their_own_case(self):
        value = context()
        first = value['case_catalog'][0]
        first['supplier_context'] = {'source_id': 'offer-a', 'customer_wording': 'A产品条件'}
        second = deepcopy(first)
        second.update(case_ref='b', procurement_key='other',
                      guidance=[{'source_id': 'only-b', 'content': '仅用于B产品'}],
                      supplier_context={'source_id': 'offer-b', 'customer_wording': 'B产品条件'})
        value['case_catalog'].append(second)
        envelope = json.loads(model_json(value))
        cases = envelope['context']['case_catalog']
        self.assertEqual(cases, project_model_payload(value)['case_catalog'])
        self.assertEqual(cases[1]['guidance'][0]['source_id'], 'only-b')
        self.assertNotIn('only-b', [row['source_id'] for row in envelope['guidance']])
        self.assertEqual(cases[0]['supplier_context']['source_id'], 'offer-a')
        self.assertEqual(cases[1]['supplier_context']['source_id'], 'offer-b')

    def test_continuation_and_frozen_input_remain_in_the_final_target_block(self):
        value = context('manual_suggestion')
        value.update(current_case_ref='a', selected_case_ref='a',
                     continuation={'phase': 'supplement', 'source_message_id': 'a',
                                   'business_note_source_id': 's', 'issue_ids': ['shipping']},
                     followup_context={'source_message_id': 'a'})
        envelope = json.loads(model_json(value))
        customer = envelope['context']
        self.assertEqual(flatten(envelope), project_model_payload(value))
        self.assertGreater(list(customer).index('continuation'), list(customer).index('message_view'))
        self.assertEqual(customer['continuation'], value['continuation'])
        self.assertEqual(customer['reply_target'], value['reply_target'])

    def test_changed_dynamic_data_keeps_the_material_prefix(self):
        for mode in PROMPTS:
            with self.subTest(mode=mode):
                first = context(mode)
                second = deepcopy(first)
                second['reply_target']['source_message_id'] = 'b'
                second['supplement']['body'] = '补充b'
                second['recent_messages'][0]['text'] = 'changed'
                second['case']['requirements']['quantity']['value'] = '20'
                if mode == 'whatsapp_reply':
                    second['whatsapp_messages'][0]['source_id'] = 'b'
                old = prefix(baseline(first), baseline(second))
                new = prefix(model_json(first), model_json(second))
                self.assertGreater(new, old + 10000)

    def test_target_changes_preserve_the_existing_message_prefix(self):
        for mode in PROMPTS:
            with self.subTest(mode=mode):
                first = context(mode)
                second = deepcopy(first)
                second['reply_target']['source_message_id'] = 'next'
                before, after = model_json(first), model_json(second)
                key = 'whatsapp_messages' if mode == 'whatsapp_reply' else 'message_view'
                end = before.index(']', before.index('"' + key + '"')) + 1
                self.assertEqual(before[:end], after[:end])
                second = deepcopy(first)
                if mode == 'whatsapp_reply':
                    second['whatsapp_messages'].append({'source_id': 'b', 'text': 'new buyer message'})
                else:
                    second['recent_messages'].append({'message_id': 'b', 'timestamp_ms': 2, 'role': 'buyer', 'text': 'new buyer message'})
                self.assertTrue(model_json(second).startswith(before[:end - 1]))

    def test_summary_changes_occur_before_the_current_chat(self):
        for mode in ('automatic', 'manual_suggestion', 'requirements', 'whatsapp_reply'):
            with self.subTest(mode=mode):
                first = context(mode)
                second = deepcopy(first)
                second['conversation_summary']['body'] = '新摘要'
                second['wa_summary'] = '新摘要'
                before, after = model_json(first), model_json(second)
                key = 'whatsapp_messages' if mode == 'whatsapp_reply' else 'message_view'
                self.assertLess(prefix(before, after), before.index('"' + key + '"'))

    def test_serialization_does_not_change_semantic_fingerprint_or_limits(self):
        from reception_service.errors import ServiceError
        from reception_service.model_payload import MODEL_CONTEXT_CHARS
        from reception_service.reception_context import semantic_fingerprint
        for mode in PROMPTS:
            value = context(mode)
            fingerprint = semantic_fingerprint(value)
            model_json(value)
            self.assertEqual(semantic_fingerprint(value), fingerprint)
        with self.assertRaises(ServiceError) as too_long:
            model_json({'task_mode': 'whatsapp_reply', 'knowledge': [{'content': 'x' * MODEL_CONTEXT_CHARS}]})
        self.assertEqual(too_long.exception.code, 'context_too_long')
        with self.assertRaises(ServiceError) as invalid:
            model_json({'task_mode': 'whatsapp_reply', 'extra_section': {'value': float('nan')}})
        self.assertEqual(invalid.exception.code, 'invalid_context')

    def test_budget_counts_the_actual_envelope_without_truncation(self):
        from reception_service.errors import ServiceError
        from reception_service.model_payload import MODEL_CONTEXT_CHARS
        value = {'task_mode': 'whatsapp_reply', 'whatsapp_messages': [{'text': ''}]}
        text = 'x' * (MODEL_CONTEXT_CHARS - len(model_json(value)))
        value['whatsapp_messages'][0]['text'] = text
        encoded = model_json(value)
        self.assertEqual(len(encoded), MODEL_CONTEXT_CHARS)
        self.assertEqual(json.loads(encoded)['context']['whatsapp_messages'][0]['text'], text)
        value['whatsapp_messages'][0]['text'] += 'x'
        with self.assertRaises(ServiceError) as too_long:
            model_json(value)
        self.assertEqual(too_long.exception.code, 'context_too_long')


class ProviderOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_envelopes_preserve_fixed_prompts_content_and_images(self):
        data = b'synthetic image bytes'
        image = {'data': data, 'mime': 'image/png', 'alias': 'p1', 'sha256': hashlib.sha256(data).hexdigest(),
                 'image_id': 'i', 'source_id': 's', 'source_version': 'v', 'role': 'buyer', 'width': 1, 'height': 1}
        for protocol, mode in ((p, m) for p in ('chat-completions', 'anthropic') for m in PROMPTS):
            value = context(mode)
            prompt = PROMPTS[mode] + '\n' + IMAGE_NATIVE
            requests = []

            async def handler(request):
                requests.append(json.loads(request.content))
                response = ({'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}
                            if protocol == 'chat-completions' else
                            {'id': 'm', 'type': 'message', 'role': 'assistant', 'model': 'test',
                             'content': [{'type': 'text', 'text': '{}'}], 'stop_reason': 'end_turn',
                             'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}})
                return httpx.Response(200, json=response)

            transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(handler))
            profile = {'profile_id': 'p', 'protocol': protocol, 'base_url': 'http://localhost:8080/v1', 'model': 'test'}
            try:
                for payload in (baseline(value), model_json(value)):
                    await transport.generate(profile=profile, api_key='test', system_prompt=prompt,
                                             user_payload=payload, images=[image])
            finally:
                await transport.aclose()
            old, new = requests
            for index, body in enumerate((old, new)):
                user = body['messages'][-1]
                self.assertEqual(user['role'], 'user')
                decoded = json.loads(user['content'][0]['text'])
                if index == 1:
                    self.assertEqual(list(decoded)[-1], 'context')
                    decoded = flatten(decoded)
                self.assertEqual(decoded, project_model_payload(value), (protocol, mode))
                user['content'][0]['text'] = '<same projected content>'
            self.assertEqual(old, new)
            if protocol == 'anthropic':
                self.assertEqual(new['system'], prompt)
            else:
                self.assertEqual(new['messages'][0], {'role': 'system', 'content': prompt})


if __name__ == '__main__':
    unittest.main()
