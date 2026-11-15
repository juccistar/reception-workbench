import base64
import hashlib
import unittest
from types import SimpleNamespace

from reception_service.errors import ServiceError
from reception_service.model_providers.encoding import anthropic_result, chat_result, image_blocks, parse_json


class ModelEncodingTest(unittest.TestCase):
    def setUp(self):
        data = b'controlled image bytes'
        self.image = dict(alias='p1', image_id='i', source_id='s', source_version='v1', role='buyer',
                          mime='image/png', data=data, sha256=hashlib.sha256(data).hexdigest(), width=1, height=1)

    def test_provider_images_encode_the_actual_bytes(self):
        for protocol in ('chat-completions', 'anthropic'):
            blocks, manifest = image_blocks([self.image], protocol)
            encoded = (blocks[0]['image_url']['url'].split(',', 1)[1] if protocol == 'chat-completions'
                       else blocks[0]['source']['data'])
            self.assertEqual(base64.b64decode(encoded), self.image['data'])
            self.assertEqual(manifest[0]['sha256'], hashlib.sha256(base64.b64decode(encoded)).hexdigest())
            self.assertNotIn('data', manifest[0])
        self.assertEqual(image_blocks([], 'anthropic'), ([], []))

    def test_image_hash_and_duplicate_alias_are_checked(self):
        bad = dict(self.image, sha256='0' * 64)
        for images in ([bad], [self.image, self.image]):
            with self.assertRaises(ServiceError):
                image_blocks(images, 'chat-completions')

    def test_strict_business_json(self):
        for raw in ('{"a":1,"a":2}', '{"n":NaN}', '[1]', '{', 'x' * (1024 * 1024 + 1)):
            with self.assertRaises(ServiceError):
                parse_json(raw)
        self.assertEqual(parse_json('{"a":1}'), {'a': 1})

    def test_response_structure_limits(self):
        for raw in ('{"x":' + '[' * 100 + '0' + ']' * 100 + '}',
                    '{"x":[' + ','.join('0' for _ in range(21000)) + ']}',
                    '{"x":"' + 'x' * 262145 + '"}'):
            with self.assertRaises(ServiceError):
                parse_json(raw)

    def test_reject_incomplete_or_tool_responses(self):
        for reason, message in [('length', {'content': '{}'}), ('stop', {'content': '{}', 'tool_calls': [1]}),
                                ('stop', {'content': '{}', 'refusal': 'refused'})]:
            with self.assertRaises(ServiceError):
                chat_result({'choices': [{'finish_reason': reason, 'message': message}]})
        self.assertEqual(chat_result({'choices': [{'finish_reason': 'stop', 'message': {'content': '{"a":1}'}}]}), {'a': 1})
        for reason, kind in [('max_tokens', 'text'), ('end_turn', 'tool_use')]:
            with self.assertRaises(ServiceError):
                anthropic_result(SimpleNamespace(stop_reason=reason, content=[SimpleNamespace(type=kind, text='{}')]))
        self.assertEqual(anthropic_result(SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(type='text', text='{}')])), {})


if __name__ == '__main__':
    unittest.main()
