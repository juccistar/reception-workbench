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

    def test_manual_refresh_clears_old_group_and_coalesces(self):
        self.demand()
        identifier = self.product()['local_product_id']
        buffer = io.BytesIO()
        Image.new('RGB', (10, 10), 'red').save(buffer, format='PNG')
        asset = decode_image(buffer.getvalue(), 'image/png')
        with self.service._lock, self.service.repo.transaction():
            self.service.repo.put('image_asset', asset['sha256'], asset)
            self.service.repo.put('product_main_image', identifier, {
                'seller_id': self.service.seller_id, 'local_product_id': identifier,
                'product_id': '1600000000123', 'status': 'ready', 'updated_at': 9999999999999,
                'asset_sha256': asset['sha256'], 'images': [{'status': 'ready', 'asset_sha256': asset['sha256']}]})
        entered, release = threading.Event(), threading.Event()
        def lookup(*args, **kwargs):
            entered.set()
            release.wait(3)
            raise ValueError('synthetic failure')
        try:
            with patch.object(self.service.integrations, 'read_product_display', side_effect=lookup):
                first = self.service.call('request_product_images', {'local_product_id': identifier, 'client_request_id': 'one'})
                self.assertTrue(entered.wait(2))
                second = self.service.call('request_product_images', {'local_product_id': identifier, 'client_request_id': 'two'})
                self.assertEqual(first, second)
                record = self.service.repo.get('product_main_image', identifier)
                self.assertGreater(record['updated_at'], 9999999999999)
                self.assertFalse(record.get('asset_sha256'))
                self.assertEqual(record.get('images'), [])
                release.set()
                self.assertEqual(self.wait_image(identifier)['status'], 'failed')
                self.assertFalse(self.service.repo.get('product_main_image', identifier).get('asset_sha256'))
        finally:
            release.set()

    def test_manual_join_of_automatic_backfill_clears_legacy_image(self):
        self.demand()
        identifier = self.product()['local_product_id']
        buffer = io.BytesIO()
        Image.new('RGB', (10, 10), 'red').save(buffer, format='PNG')
        asset = decode_image(buffer.getvalue(), 'image/png')
        with self.service._lock, self.service.repo.transaction():
            self.service.repo.put('image_asset', asset['sha256'], asset)
            self.service.repo.put('product_main_image', identifier, {'seller_id': self.service.seller_id,
                'local_product_id': identifier, 'product_id': '1600000000123', 'status': 'ready',
                'asset_sha256': asset['sha256'], 'updated_at': 10})
        entered, release = threading.Event(), threading.Event()
        def lookup(*args, **kwargs):
            entered.set()
            release.wait(3)
            raise ValueError('offline')
        try:
            with patch.object(self.service.integrations, 'read_product_display', side_effect=lookup):
                self.service._product_main_image_next_schedule = 0
                self.service._schedule_procurement()
                self.assertTrue(entered.wait(2))
                old = self.service.get_product_main_image(identifier)
                job = self.service.request_product_images('join', local_product_id=identifier)
                current = self.service.get_product_main_image(identifier)
                self.assertEqual(job['job_id'], current['job_id'])
                self.assertGreater(current['updated_at'], old['updated_at'])
                self.assertFalse(current['images'])
                release.set()
                self.assertEqual(self.wait_image(identifier)['status'], 'failed')
        finally:
            release.set()

    def test_refresh_cleanup_preserves_shared_asset_and_invalidates_reference(self):
        self.demand()
        identifier = self.product()['local_product_id']
        assets = []
        for color in ('red', 'blue'):
            buffer = io.BytesIO()
            Image.new('RGB', (10, 10), color).save(buffer, format='PNG')
            assets.append(decode_image(buffer.getvalue(), 'image/png'))
        with self.service._lock, self.service.repo.transaction():
            for asset in assets:
                self.service.repo.put('image_asset', asset['sha256'], asset)
            self.service.repo.put('message_image', 'synthetic-shared', {'asset_sha256': assets[1]['sha256']})
            self.service.repo.put('product_main_image', identifier, {'seller_id': self.service.seller_id,
                'local_product_id': identifier, 'product_id': '1600000000123', 'status': 'ready', 'updated_at': 42,
                'asset_sha256': assets[0]['sha256'], 'images': [{'status': 'ready', 'asset_sha256': a['sha256']} for a in assets]})
        ref = self.service.get_product_main_image(identifier)['image_ref']
        entered, release = threading.Event(), threading.Event()
        def lookup(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {'product_id': '1600000000123', 'source': 'no_image', 'images': []}
        try:
            with patch.object(self.service.integrations, 'read_product_display', side_effect=lookup):
                self.service.request_product_images('cleanup', local_product_id=identifier)
                self.assertTrue(entered.wait(2))
                self.assertIsNone(self.service.repo.get('image_asset', assets[0]['sha256']))
                self.assertIsNotNone(self.service.repo.get('image_asset', assets[1]['sha256']))
                from reception_service.errors import ServiceError
                with self.assertRaises(ServiceError):
                    self.service._image_original_describe(ref)
                release.set()
                self.assertEqual(self.wait_image(identifier)['status'], 'unavailable')
        finally:
            release.set()

    def test_manual_failure_types_and_retry_old_loading(self):
        self.demand()
        identifier = self.product()['local_product_id']
        for index, (result, expected, code) in enumerate([
            ({'product_id': '1600000000123', 'source': 'unavailable', 'images': []}, 'failed', 'product_display_invalid_response'),
            ({'product_id': '1600000000123', 'source': 'no_image', 'images': []}, 'unavailable', None),
            ({'product_id': '1600000000123', 'source': 'product_images', 'images': [{'image_index': 0, 'url': 'https://sc04.alicdn.com/a'}]}, 'failed', 'product_image_download_failed')]):
            with self.subTest(expected=expected, code=code):
                with self.service._lock, self.service.repo.transaction():
                    self.service.repo.put('product_main_image', identifier, {'seller_id': self.service.seller_id,
                        'local_product_id': identifier, 'product_id': '1600000000123', 'status': 'loading', 'job_id': 'old'})
                with patch.object(self.service.integrations, 'read_product_display', return_value=result):
                    self.service.request_product_images('type-' + str(index), local_product_id=identifier)
                    record = self.wait_image(identifier)
                    self.assertEqual(record['status'], expected)
                    self.assertEqual(record.get('error_code'), code)

    def test_missing_binding_is_repaired_without_new_procurement_or_case_revision(self):
        case = self.demand()
        identifier = self.product()['local_product_id']
        before = self.service.repo.get('case', case['case_id'])
        tasks = self.service.repo.list('procurement_task')
        with self.service._lock, self.service.repo.transaction():
            self.service.repo.db.execute("DELETE FROM entities WHERE kind='requirement_product_binding' AND id=?", (case['case_id'],))
        with patch.object(self.service.integrations, 'read_product_display', return_value={
                'product_id': '1600000000123', 'source': 'no_image', 'images': []}):
            self.service.request_product_images('repair', case_id=case['case_id'])
            self.wait_image(identifier)
        self.assertEqual(self.service.repo.get('requirement_product_binding', case['case_id'])['local_product_id'], identifier)
        self.assertEqual(self.service.repo.get('case', case['case_id']), before)
        self.assertEqual(self.service.repo.list('procurement_task'), tasks)

    def test_manual_refresh_rejects_foreign_and_ambiguous_references(self):
        self.demand()
        identifier = self.product()['local_product_id']
        from reception_service.errors import ServiceError
        for params in ({'local_product_id': 'foreign'}, {'local_product_id': identifier, 'case_id': 'x'}, {}):
            with self.subTest(params=params), self.assertRaises(ServiceError):
                self.service.call('request_product_images', {**params, 'client_request_id': 'request'})

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

    def test_five_large_previews_fit_transport_without_changing_original_assets(self):
        import random
        self.demand()
        identifier = self.product()['local_product_id']
        image = Image.frombytes('RGB', (1000, 1000), random.Random(315).randbytes(3_000_000))
        output = io.BytesIO()
        image.save(output, format='PNG')
        asset = decode_image(output.getvalue(), 'image/png')
        self.assertGreater(len(asset['preview_data_url']), 175_000)
        with self.service._lock, self.service.repo.transaction():
            self.service.repo.put('image_asset', asset['sha256'], asset)
            self.service.repo.put('product_main_image', identifier, {
                'seller_id': self.service.seller_id, 'local_product_id': identifier,
                'product_id': '1600000000123', 'status': 'ready', 'image_group_version': 1,
                'images': [{'status': 'ready', 'asset_sha256': asset['sha256']} for _ in range(5)]})
        preview = self.service.get_product_main_image(identifier)
        self.assertEqual(len(preview['images']), 5)
        self.assertLess(len(json.dumps(preview).encode('utf-8')), 1_200_000)
        self.assertTrue(all(len(row['data_url']) <= 175_000 for row in preview['images']))
        self.assertEqual(self.service.repo.get('image_asset', asset['sha256']), asset)

    def test_large_transparent_gallery_preview_preserves_alpha(self):
        import base64
        import random
        from reception_service.product_presentation import _gallery_preview
        image = Image.frombytes('RGBA', (500, 500), random.Random(41).randbytes(1_000_000))
        output = io.BytesIO()
        image.save(output, format='PNG')
        preview = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
        self.assertGreater(len(preview), 175_000)
        resized = _gallery_preview(preview)
        self.assertTrue(resized.startswith('data:image/png;base64,'))
        with Image.open(io.BytesIO(base64.b64decode(resized.split(',')[1]))) as actual:
            self.assertLess(actual.convert('RGBA').getchannel('A').getextrema()[0], 255)

    def test_changed_product_during_lookup_does_not_store_or_download_old_image(self):
        self.demand()
        identifier = self.product()['local_product_id']
        entered, release = threading.Event(), threading.Event()
        def lookup(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {'product_id': '1600000000123', 'source': 'product_images',
                    'images': [{'image_index': 0, 'url': 'https://sc04.alicdn.com/a.png'}]}
        self.service.mode = 'live'
        try:
            with patch.object(self.service.integrations, 'read_product_display', side_effect=lookup), \
                    patch('reception_service.product_presentation.fetch_image') as fetch:
                self.service.request_product_images('changed-product', local_product_id=identifier)
                self.assertTrue(entered.wait(2))
                with self.service._lock, self.service.repo.transaction():
                    product = self.service.repo.get('library_product', identifier)
                    product['product_id'] = '1600000000999'
                    self.service.repo.put('library_product', identifier, product)
                release.set()
                deadline = time.monotonic() + 3
                while identifier in self.service._product_main_image_active and time.monotonic() < deadline:
                    threading.Event().wait(.01)
                self.assertNotIn(identifier, self.service._product_main_image_active)
                fetch.assert_not_called()
                self.assertEqual(self.service.get_product_main_image(identifier)['status'], 'not_loaded')
        finally:
            release.set()
            self.service.mode = 'demo'

    def test_group_partial_failure_and_legacy_backfill_once(self):
        self.demand()
        identifier = self.product()['local_product_id']
        buffer = io.BytesIO()
        Image.new('RGB', (20, 10), 'green').save(buffer, format='PNG')
        asset = decode_image(buffer.getvalue(), 'image/png')
        urls = [f'https://sc04.alicdn.com/kf/{i}.png' for i in range(3)]
        with self.service._lock, self.service.repo.transaction():
            self.service.repo.put('image_asset', asset['sha256'], asset)
            self.service.repo.put('product_main_image', identifier, {
                'seller_id': self.service.seller_id, 'local_product_id': identifier,
                'product_id': '1600000000123', 'status': 'ready', 'asset_sha256': asset['sha256'],
                'source_url': urls[0]})
        self.service.mode = 'live'
        try:
            result = {'product_id': '1600000000123', 'images': [
                {'image_index': i, 'url': url} for i, url in enumerate(urls)],
                'selection_rule': 'lowest_image_index', 'product_binding': 'request_parameters'}
            with patch.object(self.service.integrations, 'read_product_display', return_value=result) as read, \
                    patch('reception_service.product_presentation.fetch_image', side_effect=[ValueError('bad'), asset, asset]):
                record = self.wait_image(identifier)
                self.assertEqual(record['status'], 'ready')
                preview = self.service.get_product_main_image(identifier)
                self.assertEqual([image['status'] for image in preview['images']], ['failed', 'ready', 'ready'])
                self.assertTrue(preview['data_url'].startswith('data:image/jpeg;base64,'))
                self.assertEqual(self.service._source_main_image_url(identifier), urls[1])
                snapshot = self.service.get_conversation('demo-emma')['case']['product_main_image']
                self.assertNotIn('data_url', str(snapshot))
                for _ in range(3):
                    self.service._product_main_image_next_schedule = 0
                    self.service._schedule_procurement()
                self.assertEqual(read.call_count, 1)
        finally:
            self.service.mode = 'demo'

    def test_old_unfinished_and_failed_records_are_not_backfilled(self):
        self.demand()
        identifier = self.product()['local_product_id']
        for status in ('loading', 'unknown', 'failed'):
            with self.subTest(status=status):
                with self.service._lock, self.service.repo.transaction():
                    self.service.repo.put('product_main_image', identifier, {
                        'seller_id': self.service.seller_id, 'local_product_id': identifier,
                        'product_id': '1600000000123', 'status': status, 'asset_sha256': 'old'})
                self.service._product_main_image_next_schedule = 0
                with patch.object(self.service.integrations, 'read_product_display') as read:
                    self.service._schedule_procurement()
                    self.assertEqual(self.service.repo.get('product_main_image', identifier)['status'], status)
                    read.assert_not_called()

    def test_legacy_image_remains_visible_during_backfill(self):
        self.demand()
        identifier = self.product()['local_product_id']
        buffer = io.BytesIO()
        Image.new('RGB', (20, 10), 'green').save(buffer, format='PNG')
        asset = decode_image(buffer.getvalue(), 'image/png')
        with self.service._lock, self.service.repo.transaction():
            self.service.repo.put('image_asset', asset['sha256'], asset)
            self.service.repo.put('product_main_image', identifier, {
                'seller_id': self.service.seller_id, 'local_product_id': identifier,
                'product_id': '1600000000123', 'status': 'ready', 'asset_sha256': asset['sha256']})
        entered, release = threading.Event(), threading.Event()
        def lookup(*args, **kwargs):
            entered.set()
            release.wait(3)
            raise ValueError('offline')
        try:
            with patch.object(self.service.integrations, 'read_product_display', side_effect=lookup):
                self.service._product_main_image_next_schedule = 0
                self.service._schedule_procurement()
                self.assertTrue(entered.wait(2))
                preview = self.service.get_product_main_image(identifier)
                self.assertEqual(preview['status'], 'loading')
                self.assertTrue(preview['data_url'].startswith('data:image/jpeg;base64,'))
                release.set()
                record = self.wait_image(identifier)
                self.assertEqual(record['status'], 'ready')
                self.assertEqual(record['image_group_version'], 1)
        finally:
            release.set()

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
