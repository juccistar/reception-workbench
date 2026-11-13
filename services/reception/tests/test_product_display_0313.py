import hashlib
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from reception_service.errors import ServiceError
from reception_service.integrations.product_display import read_product_display


class ProductDisplayTests(unittest.TestCase):
    def setUp(self):
        self.native = Mock()
        self.native._identity = Mock(return_value=('123', 'identity-hash'))
        self.native.cli = 'workctl.exe'

    def test_invalid_image_url_does_not_discard_valid_sibling(self):
        self.native.process.run.return_value = {'success': True, 'data': json.dumps({'basicInfo': {'images': [
            {'imageIndex': 0, 'originalImageUrl': 'https://evil.test/a'},
            {'imageIndex': 2, 'originalImageUrl': 'https://sc04.alicdn.com/good.jpg'}]}})}
        result = read_product_display(self.native, '123', '456', 'request')
        self.assertEqual(result['source'], 'product_images')
        self.assertEqual(result['images'][0]['image_index'], 2)
        self.assertEqual(result['invalid_count'], 1)

    def test_empty_success_is_no_image_and_uses_exact_command(self):
        reply = {'success': True, 'data': json.dumps({'basicInfo': {'images': []}})}
        observed = []
        def process(args, *, timeout):
            self.assertEqual(args[:5], ['workctl.exe', 'icbu', 'product', 'list-information', '--json-file'])
            self.assertEqual(args[6:], ['--format', 'json'])
            self.assertEqual(timeout, 35)
            path = Path(args[5])
            self.assertTrue(path.is_absolute())
            content = path.read_bytes()
            self.assertFalse(content.startswith(b'\xef\xbb\xbf'))
            self.assertEqual(json.loads(content.decode('utf-8')), {'productId': 456, 'queryType': 'trunk', 'componentList': ['images']})
            observed.append(path)
            return reply
        self.native.process.run.side_effect = process
        result = read_product_display(self.native, '123', '456', 'request')
        expected_hash = hashlib.sha256(json.dumps(reply, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        self.assertEqual(result, {'product_id': '456', 'main_image_url': None, 'images': [],
                                  'source': 'no_image', 'result_sha256': expected_hash,
                                  'product_binding': 'request_parameters', 'primary_verified': False, 'selection_rule': None})
        self.native._identity.assert_called_once_with('123')
        self.native.process.run.assert_called_once()
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertFalse(observed[0].parent.exists())

    def test_unknown_structure_is_unavailable_never_guesses_image(self):
        self.native.process.run.return_value = {'success': True, 'data': {'productId': '456', 'mainImage': 'https://s.alicdn.com/image.jpg', 'title': 'private'}}
        result = read_product_display(self.native, '123', '456', 'request')
        self.assertEqual(result['source'], 'unavailable')
        self.assertIsNone(result['main_image_url'])
        self.assertEqual(len(result['result_sha256']), 64)
        self.assertNotIn('private', str(result))

    def test_observed_component_selects_lowest_index(self):
        images = [{'imageIndex': 2, 'originalImageUrl': 'https://sc04.alicdn.com/kf/second.jpg'},
                  {'imageIndex': 0, 'originalImageUrl': 'https://sc04.alicdn.com/kf/H1462bd114b734b39b558c45f8f62188fT.jpg'}]
        self.native.process.run.return_value = {'success': True, 'data': json.dumps({'basicInfo': {'featureMap': {}, 'images': images}})}
        result = read_product_display(self.native, '123', '456', 'request')
        self.assertEqual(result['main_image_url'], images[1]['originalImageUrl'])
        self.assertEqual(result['product_binding'], 'request_parameters')
        self.assertFalse(result['primary_verified'])
        self.assertEqual(result['selection_rule'], 'lowest_image_index')

    def test_image_group_is_sorted_deduplicated_and_limited(self):
        images = [{'imageIndex': i, 'originalImageUrl': f'https://sc04.alicdn.com/{i if i != 3 else 1}.jpg'}
                  for i in reversed(range(8))]
        self.native.process.run.return_value = {'success': True, 'data': json.dumps({'basicInfo': {'images': images}})}
        result = read_product_display(self.native, '123', '456', 'request')
        self.assertEqual([image['image_index'] for image in result['images']], [0, 1, 2, 4, 5])
        self.assertEqual(len({image['url'] for image in result['images']}), 5)
        self.native.process.run.assert_called_once()

    def test_empty_and_ambiguous_component_handling(self):
        cases = [([], 'no_image'),
                 ([{'imageIndex': 0, 'originalImageUrl': 'https://sc04.alicdn.com/a'}, {'imageIndex': 0, 'originalImageUrl': 'https://sc04.alicdn.com/b'}], 'unavailable'),
                 ([{'imageIndex': 0, 'originalImageUrl': 'https://evil.test/a'}], 'invalid_images'),
                 ([{'imageIndex': True, 'originalImageUrl': 'https://sc04.alicdn.com/a'}], 'unavailable'),
                 ([{'imageIndex': 1, 'url': 'https://sc04.alicdn.com/a'}], 'invalid_images')]
        for images, expected in cases:
            with self.subTest(images=images):
                self.native.process.run.return_value = {'success': True, 'data': json.dumps({'basicInfo': {'images': images}})}
                result = read_product_display(self.native, '123', '456', 'request')
                self.assertEqual(result['source'], expected)
                self.assertIsNone(result['main_image_url'])

    def test_explicit_mismatched_product_id_is_rejected(self):
        for data in ({'productId': 999, 'basicInfo': {'images': []}}, {'basicInfo': {'productId': '999', 'images': []}}):
            with self.subTest(data=data):
                self.native.process.run.return_value = {'success': True, 'data': json.dumps(data)}
                with self.assertRaises(ServiceError):
                    read_product_display(self.native, '123', '456', 'request')

    def test_failure_and_oversize_are_errors(self):
        for reply in ({'success': False, 'data': None}, {'success': True, 'data': 'a' * 256001}):
            with self.subTest(reply_type=type(reply['data'])):
                self.native.process.run.return_value = reply
                with self.assertRaises(ServiceError):
                    read_product_display(self.native, '123', '456', 'request')

    def test_cancellation_after_query_discards_result(self):
        event = threading.Event()
        def process(*args, **kwargs):
            event.set()
            return {'success': True, 'data': None}
        self.native.process.run.side_effect = process
        with self.assertRaises(ServiceError):
            read_product_display(self.native, '123', '456', 'request', cancel_event=event)
        self.native.process.run.assert_called_once()

    def test_invalid_ids_never_invoke_identity(self):
        for seller, product in [('123', '本地定制-456'), ('123', '456/1'), ('abc', '456')]:
            with self.subTest(seller=seller, product=product):
                with self.assertRaises(ServiceError):
                    read_product_display(self.native, seller, product, 'request')
        self.native._identity.assert_not_called()

    def test_wrong_identity_and_cancellation_fail_closed(self):
        self.native._identity.return_value = ('999', 'identity-hash')
        with self.assertRaises(ServiceError):
            read_product_display(self.native, '123', '456', 'request')
        self.native.process.run.assert_not_called()
        event = threading.Event()
        event.set()
        self.native._identity.reset_mock()
        with self.assertRaises(ServiceError):
            read_product_display(self.native, '123', '456', 'request', cancel_event=event)
        self.native._identity.assert_not_called()


if __name__ == '__main__':
    unittest.main()
