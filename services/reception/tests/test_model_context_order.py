from copy import deepcopy
import hashlib
import json
import unittest

import httpx

from reception_service.model_payload import model_json, project_model_payload
from reception_service.model_providers.transport import ProviderTransport


def context(mode='automatic'):
    policy = [{'source_id': 'p', 'title': 'policy', 'content': '固定规则' * 1500}]
    base = {'task_mode': mode, 'reply_target': {'source_message_id': 'a'},
            'conversation_summary': {'body': '摘要a'}, 'wa_summary': '摘要a',
            'knowledge': policy, 'guidance': policy, 'style': policy,
            'collection_rules': {'fields': ['quantity', 'price']},
            'supplement': {'source_id': 's', 'body': '补充a', 'attachments': []},
            'recent_messages': [{'message_id': 'a', 'role': 'buyer', 'timestamp_ms': 1,
                                 'text': 'https://example.test/item?a=1'}],
            'case': {'requirements': {'quantity': {'value': '10', 'evidence_ids': ['a']}}},
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


def prefix(a, b):
    return next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))


class ContextOrderTests(unittest.TestCase):
    def test_projection_equivalence_and_no_mutation_for_all_modes(self):
        for mode in ('automatic', 'manual', 'polish', 'requirements', 'whatsapp_reply'):
            with self.subTest(mode=mode):
                value = context(mode)
                original = deepcopy(value)
                old, new = baseline(value), model_json(value)
                self.assertEqual(json.loads(old), json.loads(new))
                self.assertEqual(len(old), len(new))
                self.assertEqual(value, original)
                stable = [key for key in ('payload_version', 'task_mode', 'collection_rules', 'knowledge', 'guidance', 'style') if key in json.loads(old)]
                self.assertEqual(list(json.loads(new))[:len(stable)], stable)
                if mode != 'whatsapp_reply' and mode != 'polish':
                    self.assertEqual(list(json.loads(new)['case_catalog'][0])[:4], ['case_ref', 'knowledge', 'guidance', 'style'])
                if mode == 'whatsapp_reply':
                    self.assertEqual(json.loads(new)['extra_section'], original['extra_section'])
                    self.assertEqual(list(json.loads(new)['international_background']), ['messages', 'requirements', 'supplement', 'summary'])
                if mode == 'polish':
                    self.assertEqual(json.loads(new)['unsent_reply']['body'], value['unsent_reply']['body'])

    def test_changed_dynamic_data_has_long_stable_prefix(self):
        for mode in ('automatic', 'manual', 'polish', 'requirements', 'whatsapp_reply'):
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

    def test_target_and_summary_changes_preserve_the_existing_message_prefix(self):
        for mode in ('automatic', 'manual', 'polish', 'requirements', 'whatsapp_reply'):
            with self.subTest(mode=mode):
                first = context(mode)
                second = deepcopy(first)
                second['reply_target']['source_message_id'] = 'next'
                second['conversation_summary']['body'] = '新摘要'
                second['wa_summary'] = '新摘要'
                before, after = model_json(first), model_json(second)
                key = 'whatsapp_messages' if mode == 'whatsapp_reply' else 'message_view'
                end = before.index(']', before.index('"' + key + '"')) + 1
                self.assertEqual(before[:end], after[:end])
                second = deepcopy(first)
                if mode == 'whatsapp_reply':
                    second['whatsapp_messages'].append({'source_id': 'b', 'text': 'new buyer message'})
                else:
                    second['recent_messages'].append({'message_id': 'b', 'timestamp_ms': 2, 'role': 'buyer', 'text': 'new buyer message'})
                # Appending a message changes the array suffix, not its existing rows.
                self.assertTrue(model_json(second).startswith(before[:end - 1]))

    def test_serialization_does_not_change_semantic_fingerprint_or_limits(self):
        from reception_service.errors import ServiceError
        from reception_service.model_payload import MODEL_CONTEXT_CHARS
        from reception_service.reception_context import semantic_fingerprint
        for mode in ('automatic', 'manual', 'polish', 'requirements', 'whatsapp_reply'):
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


class ProviderOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_envelopes_and_image_blocks_are_equivalent(self):
        data = b'synthetic image bytes'
        image = {'data': data, 'mime': 'image/png', 'alias': 'p1', 'sha256': hashlib.sha256(data).hexdigest(),
                 'image_id': 'i', 'source_id': 's', 'source_version': 'v', 'role': 'buyer', 'width': 1, 'height': 1}
        for protocol, mode in ((p, m) for p in ('chat-completions', 'anthropic')
                               for m in ('automatic', 'manual', 'polish', 'requirements', 'whatsapp_reply')):
            value = context(mode)
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
                    await transport.generate(profile=profile, api_key='test', system_prompt='fixed system',
                                             user_payload=payload, images=[image])
            finally:
                await transport.aclose()
            old, new = requests
            for body in (old, new):
                messages = body['messages']
                user = messages[-1]
                self.assertEqual(user['role'], 'user')
                self.assertEqual(json.loads(user['content'][0]['text']), json.loads(baseline(value)), (protocol, mode))
                user['content'][0]['text'] = '<same projected content>'
            self.assertEqual(old, new)
            if protocol == 'anthropic':
                self.assertEqual(new['system'], 'fixed system')
            else:
                self.assertEqual(new['messages'][0], {'role': 'system', 'content': 'fixed system'})


if __name__ == '__main__':
    unittest.main()
