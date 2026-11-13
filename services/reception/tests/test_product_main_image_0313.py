"""Derived links and independent product images in isolated service workspaces."""
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
import test_product_library_0313 as fixtures
from reception_service.image_assets import decode_image
from reception_service.model_payload import model_json
from reception_service.product_identity import product_kind, product_link


class ProductMainImageTests(unittest.TestCase):
    setUp = fixtures.ProductLibraryTests.setUp
    tearDown = fixtures.ProductLibraryTests.tearDown
    demand = fixtures.ProductLibraryTests.demand
    product = fixtures.ProductLibraryTests.product

    def wait_image(self, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.service._schedule_procurement()
            record = self.service.repo.get('product_main_image', identifier)
            if record and record['status'] not in ('loading', 'not_loaded'):
                return record
            threading.Event().wait(.01)
        self.fail('Synthetic product image did not finish')

    def test_custom_id_has_no_platform_link_or_native_image_lookup(self):
        self.assertEqual(product_kind('定制-6262662'), 'custom')
        self.assertIsNone(product_link('定制-6262662'))
        self.assertIsNone(product_link('123/../../etc'))
        with patch.object(self.service.integrations, 'read_product_display') as read:
            case = self.demand(pid='定制-6262662')
            self.service._schedule_procurement()
            product = self.product()
            self.assertEqual(product['platform'], 'custom')
            self.assertEqual(product['product_id'], '定制-6262662')
            self.assertIsNone(product['product_url'])
            self.assertIsNone(case['requirements']['product_url']['value'])
            self.assertEqual(self.service.get_product_main_image(product['local_product_id'])['status'], 'unavailable')
            read.assert_not_called()

    def test_coincident_local_codes_do_not_merge_unrelated_custom_demands(self):
        first = self.demand(pid='定制-6262662')
        second = self.demand('demo-sofia', pid='定制-6262662')
        self.assertNotEqual(first['requirements']['product_id']['value'], second['requirements']['product_id']['value'])
        self.assertTrue(second['requirements']['product_id']['value'].startswith('定制-'))
        self.assertEqual(len(self.service.list_products()['items']), 2)
        again = self.service.repo.get('case', second['case_id'])
        old = again['requirements']['product_id']['value']
        with self.service._lock, self.service.repo.transaction():
            self.service._sync_requirement_product(again)
        self.assertEqual(again['requirements']['product_id']['value'], old)

    def test_image_metadata_is_automatic_and_read_once_without_procurement_grant(self):
        self.demand()
        identifier = self.product()['local_product_id']
        with patch.object(self.service.integrations, 'read_product_display',
                return_value={'product_id': '1600000000123', 'main_image_url': None, 'source': 'no_image'}) as read:
            self.assertEqual(self.wait_image(identifier)['status'], 'unavailable')
            self.assertFalse(self.service.get_procurement_settings()['authorized'])
            for _ in range(5):
                self.service.get_product_main_image(identifier)
                self.service.get_conversation('demo-emma')
                self.service._schedule_procurement()
            self.assertEqual(read.call_count, 1)
            self.assertEqual(self.service.list_procurement_tasks()['items'][0]['status'], 'waiting_authorization')

    def test_ready_main_image_is_local_display_and_sourcing_only(self):
        case = self.demand(product_images=['private-customer-image'])
        identifier = self.product()['local_product_id']
        revision = self.service.repo.get('case', case['case_id'])['revision']
        fingerprint = self.service.get_context(case['case_id'])['fingerprint']
        buffer = io.BytesIO()
        Image.new('RGB', (20, 10), 'green').save(buffer, format='PNG')
        asset = decode_image(buffer.getvalue(), 'image/png')
        url = 'https://sc04.alicdn.com/kf/synthetic-public-main.png'
        self.service.mode = 'live'
        try:
            with patch.object(self.service.integrations, 'read_product_display', return_value={
                    'product_id': '1600000000123', 'main_image_url': url, 'source': 'synthetic',
                    'result_sha256': 'f' * 64, 'primary_verified': False,
                    'selection_rule': 'lowest_image_index', 'product_binding': 'request_parameters'}), \
                    patch('reception_service.product_presentation.fetch_image', return_value=asset) as fetch:
                self.assertEqual(self.wait_image(identifier)['status'], 'ready')
                fetch.assert_called_once_with(url)
        finally:
            self.service.mode = 'demo'
        detail = self.service.get_conversation('demo-emma')
        preview = self.service.call('get_product_main_image', {'local_product_id': identifier})
        self.assertTrue(preview['data_url'].startswith('data:image/jpeg;base64,'))
        self.assertNotIn('source_url', preview)
        self.assertFalse(preview['primary_verified'])
        self.assertEqual(preview['selection_rule'], 'lowest_image_index')
        self.assertIn('展示和找货参考', preview['message'])
        self.assertEqual(self.service.repo.get('product_main_image', identifier)['product_binding'], 'request_parameters')
        self.assertNotIn('data_url', detail['case']['product_main_image'])
        self.assertEqual(detail['case']['requirements']['product_images']['value'], ['private-customer-image'])
        self.assertEqual(detail['case']['revision'], revision)
        self.assertEqual(self.service.get_context(case['case_id'])['fingerprint'], fingerprint)
        payload = model_json(self.service.get_context(case['case_id']))
        self.assertNotIn('main_image', payload)
        self.assertNotIn(url, payload)
        self.assertNotIn('www.alibaba.com/product-detail/_1600000000123', payload)
        context = self.service._source_context(self.service.repo.get('library_product', identifier), case)
        self.assertEqual(context['main_image_url'], url)
        self.assertEqual(self.service.repo.list('message_image'), [])

    def test_wrong_product_image_is_never_downloaded(self):
        self.demand()
        identifier = self.product()['local_product_id']
        self.service.mode = 'live'
        try:
            with patch.object(self.service.integrations, 'read_product_display', return_value={
                    'product_id': '999999', 'main_image_url': 'https://sc04.alicdn.com/kf/other.png'}), \
                    patch('reception_service.product_presentation.fetch_image') as fetch:
                self.assertEqual(self.wait_image(identifier)['status'], 'failed')
                fetch.assert_not_called()
        finally:
            self.service.mode = 'demo'

    def test_waiting_for_main_image_does_not_claim_the_reception_ai_slot(self):
        self.demand()
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {'product_id': '1600000000123', 'main_image_url': None, 'source': 'no_image'}
        try:
            with patch.object(self.service.integrations, 'read_product_display', side_effect=slow), \
                    patch.object(self.service.integrations, 'source_product', wraps=self.service.integrations.source_product) as source:
                self.service.set_procurement_enabled(True)
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.service._generation_active)
                source.assert_not_called()
                note = self.service.save_conversation_supplement('demo-emma', '仍可保存当前客户补充', None, 'image-wait-note')
                self.assertEqual(note['body'], '仍可保存当前客户补充')
                release.set()
                task = self.service.list_procurement_tasks()['items'][0]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and self.service.repo.get('procurement_task', task['task_id'])['status'] in ('queued', 'running'):
                    self.service._schedule_procurement()
                    threading.Event().wait(.01)
                self.assertEqual(self.service.repo.get('procurement_task', task['task_id'])['status'], 'blocked')
                self.assertEqual(source.call_count, 1)
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
