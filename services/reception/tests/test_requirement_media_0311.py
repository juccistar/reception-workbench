import copy
import json
import unittest
import threading
import time
from unittest.mock import patch

from reception_service.model_payload import project_model_payload
from reception_service.photos import photo_version
from reception_service.repository import Repository, encoded
from reception_service.requirement_media import RequirementMediaMixin, get_product_source_images
from reception_service.reception_context import dialogue_window, semantic_fingerprint, shared_context


class MediaService(RequirementMediaMixin):
    def __init__(self, repo):
        self.repo, self._lock, self.seller_id, self.account_verified = repo, threading.RLock(), 's', True
        self._epoch, self._closing = 1, False
        self.finished = threading.Event()
        self._init_requirement_media()

    def _require(self, kind, key): return self.repo.get(kind, key)
    def _photo_state(self, message): return {'status': 'not_loaded'}
    def _changed(self, *args): pass
    def _emit(self, event, job):
        if event == 'job.updated' and job.get('status') in ('succeeded', 'failed', 'stale', 'cancelled'):
            self.finished.set()


class RequirementMediaTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(None, 's')
        self.owner = dict(seller_id='s', buyer_id='b', conversation_id='c')
        self.case = dict(self.owner, case_id='case', facts={}, revision=1)
        self.repo.put('case', 'case', self.case)
        self.repo.put('conversation', 'c', dict(self.owner, case_id='case'))

    def tearDown(self):
        self.repo.close()

    def message(self, mid, text='[image]', role='buyer', **extra):
        return {**self.owner, 'message_id': mid, 'role': role, 'text': text, 'timestamp_ms': 1, 'attachments': [], **extra}

    def test_product_ids_require_buyer_basis_and_reuse_custom_id(self):
        from reception_service.context import validated_requirements
        buyer = self.message('m', text='Need custom bags, product ID 12345')
        context = {'case': self.case, 'recent_messages': [buyer], 'historical_evidence': []}
        product = {'value': '定制手提袋', 'evidence_ids': ['m']}
        def check(pid):
            return validated_requirements({'product': product, 'product_id': {'value': pid, 'evidence_ids': ['m']},
                'product_url': {'value': 'https://www.alibaba.com/product-detail/_12345.html', 'evidence_ids': ['m']}}, context)
        self.assertEqual(check('12345')['product_id']['value'], '12345')
        self.assertNotIn('product_id', check('1234'))
        self.assertNotIn('product_url', check('12345'))
        self.assertEqual(check('定制-6262662')['product_id']['value'], '定制-6262662')
        context['case'] = {**self.case, 'requirements': {'product_id': {'value': '定制-111', 'evidence_ids': ['m']}}}
        self.assertEqual(check('定制-222')['product_id']['value'], '定制-111')
        buyer['text'] = 'Hello'
        self.assertNotIn('product_id', check('定制-222'))
        context['case'] = self.case
        buyer['text'] = 'Need custom bags'
        self.assertNotIn('product_id', validated_requirements({'product_id': {'value': '定制-7', 'evidence_ids': ['m']}}, context))

    def test_product_and_supplier_projection_excludes_links_and_main_images(self):
        message_url = 'https://example.com/customer'
        context = {'message_view': [{'message_id': 'm', 'role': 'buyer', 'text': 'See ' + message_url}],
                   'case': {'requirements': {'product_url': {'value': 'https://www.alibaba.com/p'},
                                             'product': {'value': '包'}}},
                   'product_context': {'product_id': '123', 'product_url': 'https://example.com/product',
                                       'product_main_image': {'url': 'https://example.com/image'}},
                   'supplier_context': {'selection_revision': 2, 'offer_id': 'offer', 'offer_revision': 3,
                                        'product_revision': 4, 'customer_wording': '价格待商定',
                                        'nested': {'url': 'https://example.com/offer'},
                                        'product_main_image': 'https://example.com/main'}}
        payload = project_model_payload(context)
        self.assertNotIn('product_url', payload['requirements'])
        self.assertEqual(payload['product_context'], {'product_id': '123'})
        self.assertEqual(payload['supplier_context']['offer_revision'], 3)
        self.assertEqual(payload['supplier_context']['customer_wording'], '价格待商定')
        self.assertNotIn('https://', json.dumps(payload['supplier_context']))
        self.assertEqual(payload['message_view'][0]['text'], 'See ' + message_url)
        fingerprint = semantic_fingerprint(context)
        changed = copy.deepcopy(context)
        changed['case']['requirements']['product_url']['value'] = 'https://new.example/product'
        changed['case']['product_main_image'] = {'status': 'ready', 'sha256': 'abc'}
        changed['product_context']['product_main_image']['url'] = 'https://new.example/image'
        self.assertEqual(semantic_fingerprint(changed), fingerprint)
        changed['supplier_context']['offer_revision'] = 4
        self.assertNotEqual(semantic_fingerprint(changed), fingerprint)
        context.update(task_mode='polish', unsent_reply={'body': 'Use ' + message_url})
        self.assertEqual(project_model_payload(context)['unsent_reply']['body'], 'Use ' + message_url)

    def test_ai_image_reference_matches_safe_media_projection(self):
        from reception_service.context import validated_requirements
        image = self.message('image')
        self.repo.put('message', encoded(['c', 'image']), image)
        manifest = {'alias': 'p1', 'image_id': 'opened-image', 'message_id': 'image'}
        context = {'case': self.case, 'recent_messages': [image], 'historical_evidence': [],
                   'image_manifest': [manifest], 'photo_context': {'read_images': [{'image_id': 'opened-image'}]}}
        raw = {'product_images': {'value': ['p1'], 'evidence_ids': ['image']}}
        result = validated_requirements(raw, context)
        service = MediaService(self.repo)
        try:
            view = service.get_requirement_media('case')
            self.assertEqual(result['product_images']['value'], [view['items'][0]['media_id']])
            context['photo_context']['read_images'] = []
            self.assertNotIn('product_images', validated_requirements(raw, context))
        finally:
            service._stop_requirement_media()

    def test_one_candidate_ambiguity_and_seller_selection(self):
        one = self.message('i')
        self.assertEqual(get_product_source_images(self.repo, self.case, [one]), [one])
        two = self.message('j')
        self.assertEqual(get_product_source_images(self.repo, self.case, [one, two]), [])
        seller = self.message('seller', role='seller')
        quote = self.message('q', text='I want this one', attachments=[{'kind': 'quote', 'content': {'messageId': 'seller'}}])
        self.assertEqual(get_product_source_images(self.repo, self.case, [one, seller, quote]), [seller])
        self.assertEqual(get_product_source_images(self.repo, self.case, [seller]), [])
        foreign = dict(one, buyer_id='other')
        self.assertEqual(get_product_source_images(self.repo, self.case, [foreign]), [])

    def test_quote_does_not_leak_between_cases(self):
        image = self.message('seller', role='seller')
        quote = self.message('quote', text='I want this one', attachments=[{'kind': 'quote', 'content': {'messageId': 'seller'}}])
        case_a = dict(self.case, facts={'product': {'value': 'bag', 'evidence_ids': ['quote']}})
        case_b = dict(self.case, case_id='other', facts={})
        self.repo.put('case', 'other', case_b)
        self.assertEqual(get_product_source_images(self.repo, case_a, [image, quote]), [image])
        self.assertEqual(get_product_source_images(self.repo, case_b, [image, quote]), [])

    def test_old_product_evidence_is_included_once(self):
        image = self.message('old')
        rows = [image] + [self.message(str(i), text='text ' + str(i)) for i in range(55)]
        case = dict(self.case, facts={'product': {'value': 'bag', 'evidence_ids': ['old']}})
        selected = get_product_source_images(self.repo, case, rows)
        recent, historical = dialogue_window(rows, case, pending_message_ids=[m['message_id'] for m in selected])
        self.assertEqual([m['message_id'] for m in historical], ['old'])
        self.assertEqual(sum(m['message_id'] == 'old' for m in recent + historical), 1)

    def test_payload_filters_nested_cards_urls_and_sku_without_losing_text(self):
        card = {'cardType': 'product', 'fields': {'title': 'secret catalogue', 'skuImages': ['https://img.test/x']}}
        context = {'message_view': [{'message_id': 'm', 'role': 'buyer', 'text': json.dumps(card)},
                                    {'message_id': 'n', 'role': 'buyer', 'text': 'Need 20 red size L https://www.alibaba.com/product/x'}],
                   'case': {'summary': 'derived summary', 'facts': {'product': {'value': 'red bags', 'candidates': [{'value': json.dumps(card), 'sku': 'AB1'}]}}},
                   'message_semantics': {'m': [{'type': 'product_card', 'fields': card}]}}
        result = project_model_payload(context)
        data = json.dumps(result)
        for forbidden in ('secret catalogue', 'img.test', 'AB1', 'derived summary'):
            self.assertNotIn(forbidden, data)
        self.assertIn('https://www.alibaba.com/product/x', result['message_view'][1]['text'])
        self.assertNotIn('https://', json.dumps(result['facts']))
        self.assertIn('20 red size L', data)
        self.assertEqual(result['facts']['product']['value'], 'red bags')

    def test_native_cards_and_candidate_metadata_cannot_bypass_projection(self):
        card = {'cardType': 'product', 'fields': {'title': 'CATALOG_SECRET', 'sku': 'SKU_PRIVATE',
                                                   'imageUrl': 'https://img.alicdn.com/secret.jpg'}}
        for number, content in enumerate((card, json.dumps(card))):
            raw = {'kind': 'native-content', 'content': content}
            message = self.message('native-' + str(number), text=json.dumps(raw) + '\n[图片 p1]',
                                   attachments=[raw])
            self.repo.put('message', json.dumps(['c', message['message_id']], separators=(',', ':')), message)
        buyer = self.message('buyer', text='Need 20 red size L SKU: ZX123 https://www.alibaba.com/product/x')
        self.repo.put('message', json.dumps(['c', 'buyer'], separators=(',', ':')), buyer)
        case = dict(self.case, facts={'product': {'value': 'red bags', 'candidates': [
            {'value': json.dumps(card)}, {'value': 'SKU: ZX123 https://www.alibaba.com/product/x'}]}},
            requirement_items=[{'type': 'product', 'value': '20 red bags'}])
        context = shared_context(self.repo, self.owner, case, task_mode='automatic')
        context['case'] = case
        result = project_model_payload(context)
        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in ('CATALOG_SECRET', 'SKU_PRIVATE', 'ZX123', 'img.alicdn.com/secret'):
            self.assertNotIn(forbidden, serialized)
        buyer_view = next(row for row in result['message_view'] if row['message_id'] == 'buyer')
        self.assertIn('https://www.alibaba.com/product/x', buyer_view['text'])
        self.assertNotIn('https://', json.dumps(result['facts']))
        self.assertIn('20 red size L', serialized)
        self.assertEqual(result['facts']['product']['value'], 'red bags')
        self.assertEqual(result['requirement_items'][0]['value'], '20 red bags')

    def test_fingerprint_ignores_display_only_raw_fields(self):
        base = {'message_view': [{'message_id': 'a', 'role': 'buyer', 'text': 'hello'}],
                'case': {'revision': 1, 'facts': {}}, 'recent_messages': [{'attachments': [{'url': 'a'}]}]}
        changed = copy.deepcopy(base)
        changed['recent_messages'][0]['attachments'][0]['url'] = 'b'
        self.assertEqual(semantic_fingerprint(base), semantic_fingerprint(changed))
        changed['message_view'][0]['text'] = 'different'
        self.assertNotEqual(semantic_fingerprint(base), semantic_fingerprint(changed))

    def test_local_snapshot_exposes_only_safe_links_and_no_remote_images(self):
        import threading
        class Service(RequirementMediaMixin):
            def __init__(self, repo):
                self.repo, self._lock, self.seller_id, self.account_verified = repo, threading.RLock(), 's', True
            def _require(self, kind, key):
                return self.repo.get(kind, key)
            def _photo_state(self, message):
                return {'status': 'not_loaded', 'source_version': photo_version(message)}
        message = self.message('card', text='See https://www.alibaba.com/product/123',
            attachments=[{'kind': 'native-content', 'content': {'cardType': 'product', 'fields': {
                'imageUrl': 'https://cdn.example/main.jpg', 'skus': [{'skuLabel': 'red', 'imageUrl': 'https://cdn.example/red.jpg'}]}}}])
        self.repo.put('message', json.dumps(['c', 'card'], separators=(',', ':')), message)
        service = Service(self.repo)
        result = service.get_requirement_media('case')
        self.assertEqual(result['items'], [])
        self.assertEqual(result, service.get_requirement_media('case'))
        self.assertNotIn('cdn.example', json.dumps(result))
        self.assertTrue(all(not x['is_product_source'] for x in result['items']))

    def media_service(self):
        service = MediaService(self.repo)
        self.addCleanup(service._stop_requirement_media)
        card = self.message('card', text='product', attachments=[{'kind': 'native-content', 'content': {
            'cardType': 'product', 'fields': {'imageUrl': 'https://img.alicdn.com/p.jpg'}}}])
        self.repo.put('message', encoded(['c', 'card']), card)
        item = next(x for x in service.get_requirement_media('case')['items'] if x['kind'] == 'product_image')
        return service, card, item

    def test_late_source_change_finishes_stale_without_writing_asset(self):
        service, card, item = self.media_service()
        started, release = threading.Event(), threading.Event()
        asset = {'sha256': 'a' * 64, 'preview_data_url': 'data:image/png;base64,AA==', 'original_b64': 'AA=='}
        def fetch(url):
            started.set()
            release.wait(3)
            return asset
        with patch('reception_service.requirement_media.fetch_image', side_effect=fetch):
            ack = service.load_requirement_media('case', item['media_id'], 'stale-request')
            self.assertTrue(started.wait(3))
            card['text'] = 'updated'
            self.repo.put('message', encoded(['c', 'card']), card)
            release.set()
            self.assertTrue(service.finished.wait(3))
        job = self.repo.get('job', ack['job_id'])
        cache = self.repo.get('requirement_media_cache', encoded(['case', item['media_id']]))
        self.assertEqual((job['status'], job['stage']), ('stale', 'stale'))
        self.assertIsNotNone(job['completed_at'])
        self.assertEqual(cache['status'], 'failed')
        self.assertNotIn('sha256', cache)
        self.assertIsNone(self.repo.get('image_asset', asset['sha256']))

    def test_late_epoch_or_closing_change_finishes_cancelled(self):
        for transition in ('epoch', 'closing'):
            with self.subTest(transition=transition):
                service, card, item = self.media_service()
                started, release = threading.Event(), threading.Event()
                asset = {'sha256': transition * 10, 'preview_data_url': 'data:image/png;base64,AA=='}
                def fetch(url):
                    started.set()
                    release.wait(3)
                    return asset
                with patch('reception_service.requirement_media.fetch_image', side_effect=fetch):
                    ack = service.load_requirement_media('case', item['media_id'], 'cancel-' + transition)
                    self.assertTrue(started.wait(3))
                    with service._lock:
                        if transition == 'epoch':
                            service._epoch += 1
                        else:
                            service._closing = True
                    release.set()
                    self.assertTrue(service.finished.wait(3))
                job = self.repo.get('job', ack['job_id'])
                cache = self.repo.get('requirement_media_cache', encoded(['case', item['media_id']]))
                self.assertEqual((job['status'], job['stage']), ('cancelled', 'cancelled'))
                self.assertIsNotNone(job['completed_at'])
                self.assertEqual(cache['status'], 'failed')
                self.assertNotIn('sha256', cache)
                self.assertIsNone(self.repo.get('image_asset', asset['sha256']))
                service._stop_requirement_media()

    def test_shutdown_finishes_queued_jobs_without_starting_downloads(self):
        service, card, item = self.media_service()
        both_started, release = threading.Event(), threading.Event()
        lock, calls = threading.Lock(), []
        def fetch(url):
            with lock:
                calls.append(url)
                if len(calls) == 2:
                    both_started.set()
            release.wait(3)
            return {'sha256': 'c' * 64, 'preview_data_url': 'data:image/png;base64,AA=='}
        with patch('reception_service.requirement_media.fetch_image', side_effect=fetch):
            jobs = [service.load_requirement_media('case', item['media_id'], 'queued-' + str(i)) for i in range(3)]
            self.assertTrue(both_started.wait(3))
            stopped = threading.Thread(target=service._stop_requirement_media)
            stopped.start()
            deadline = time.monotonic() + 3
            while not service._requirement_media_stopped and time.monotonic() < deadline:
                threading.Event().wait(.005)
            self.assertTrue(service._requirement_media_stopped)
            release.set()
            stopped.join(3)
            self.assertFalse(stopped.is_alive())
        self.assertEqual(len(calls), 2)
        self.assertEqual([self.repo.get('job', ack['job_id'])['status'] for ack in jobs], ['cancelled'] * 3)
        self.assertEqual(self.repo.get('requirement_media_cache', encoded(['case', item['media_id']]))['status'], 'failed')
        self.assertIsNone(self.repo.get('image_asset', 'c' * 64))
        self.assertEqual(service._requirement_media_active, set())

    def test_submit_failure_rolls_back_job_request_and_cache_and_releases_slot(self):
        service, card, item = self.media_service()
        with patch.object(service._requirement_media_pool, 'submit', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                service.load_requirement_media('case', item['media_id'], 'submit-request')
        self.assertIsNone(self.repo.get('requirement_media_request', 'submit-request'))
        self.assertIsNone(self.repo.get('requirement_media_cache', encoded(['case', item['media_id']])))
        self.assertEqual(self.repo.list('job'), [])
        self.assertEqual(service._requirement_media_active, set())
        acquired = [service._requirement_media_slots.acquire(blocking=False) for _ in range(4)]
        self.assertEqual(acquired, [True] * 4)
        for _ in acquired:
            service._requirement_media_slots.release()
        asset = {'sha256': 'b' * 64, 'preview_data_url': 'data:image/png;base64,AA==', 'original_b64': 'AA=='}
        with patch('reception_service.requirement_media.fetch_image', return_value=asset):
            ack = service.load_requirement_media('case', item['media_id'], 'submit-request')
            self.assertTrue(service.finished.wait(3))
        self.assertEqual(self.repo.get('job', ack['job_id'])['status'], 'succeeded')

    def test_cover_source_revision_and_restart(self):
        class Service(RequirementMediaMixin):
            def __init__(self, repo):
                self.repo, self._lock, self.seller_id, self.account_verified = repo, threading.RLock(), 's', True
                self._epoch, self._closing, self.invalidated = 1, False, []
                self._init_requirement_media()
            def _require(self, kind, key): return self.repo.get(kind, key)
            def _photo_state(self, message): return {'status': 'not_loaded'}
            def _invalidate(self, cid): self.invalidated.append(cid)
            def _changed(self, *args): pass
            def _emit(self, *args): pass
        service = Service(self.repo)
        image = self.message('photo')
        self.repo.put('message', json.dumps(['c', 'photo'], separators=(',', ':')), image)
        before = service.get_requirement_media('case')
        photo = before['items'][0]
        covered = service.set_requirement_media_cover('case', photo['media_id'], before['revision'], 'cover')
        self.assertEqual(service.invalidated, [])
        self.assertEqual(covered, service.set_requirement_media_cover('case', photo['media_id'], before['revision'], 'cover'))
        updated = service.set_product_image_source('case', 'photo', photo['source_version'], False, covered['revision'], 'source')
        self.assertEqual(service.invalidated, ['c'])
        self.assertFalse(updated['items'][0]['is_product_source'])
        card = self.message('card', text='product', attachments=[{'kind': 'native-content', 'content': {
            'cardType': 'product', 'fields': {'imageUrl': 'https://img.alicdn.com/p.jpg'}}}])
        self.repo.put('message', json.dumps(['c', 'card'], separators=(',', ':')), card)
        product = next(x for x in service.get_requirement_media('case')['items'] if x['kind'] == 'product_image')
        started, release = threading.Event(), threading.Event()
        def fetch(url):
            started.set()
            release.wait(3)
            return {'sha256': 'a' * 64, 'preview_data_url': 'data:image/png;base64,AA==', 'original_b64': 'AA=='}
        with patch('reception_service.requirement_media.fetch_image', side_effect=fetch):
            ack = service.load_requirement_media('case', product['media_id'], 'load')
            self.assertTrue(started.wait(3))
            self.assertEqual(ack, service.load_requirement_media('case', product['media_id'], 'load'))
            card['text'] = 'changed'
            self.repo.put('message', json.dumps(['c', 'card'], separators=(',', ':')), card)
            release.set()
            service._stop_requirement_media()
        self.assertIsNone(self.repo.get('image_asset', 'a' * 64))
        self.assertEqual(service.invalidated, ['c'])
        restarted = Service(self.repo)
        reloaded = restarted.get_requirement_media('case')
        self.assertEqual(reloaded['cover_id'], updated['cover_id'])
        self.assertFalse(next(x for x in reloaded['items'] if x['kind'] == 'customer_image')['is_product_source'])
        product = next(x for x in reloaded['items'] if x['kind'] == 'product_image')
        completed = threading.Event()
        restarted._changed = lambda *args: completed.set() if self.repo.get('image_asset', 'b' * 64) else None
        asset = {'sha256': 'b' * 64, 'preview_data_url': 'data:image/png;base64,AA==', 'original_b64': 'AA=='}
        fingerprint = semantic_fingerprint(shared_context(self.repo, self.owner, self.case, task_mode='automatic'))
        with patch('reception_service.requirement_media.fetch_image', return_value=asset):
            restarted.load_requirement_media('case', product['media_id'], 'load2')
            self.assertTrue(completed.wait(3))
        self.assertEqual(fingerprint, semantic_fingerprint(shared_context(self.repo, self.owner, self.case, task_mode='automatic')))
        self.assertEqual(restarted.invalidated, [])
        restarted._stop_requirement_media()
        again = Service(self.repo)
        self.assertTrue(any(x.get('data_url') == asset['preview_data_url'] for x in again.get_requirement_media('case')['items']))
        again._stop_requirement_media()


if __name__ == '__main__':
    unittest.main()
