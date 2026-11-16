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
        self.case = dict(self.owner, case_id='case', facts={}, revision=1, state='collecting')
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

    def test_supplement_and_foreign_images_cannot_enter_requirement_mapping(self):
        from reception_service.context import validated_requirements
        from reception_service.requirement_changes import validate_changes
        from reception_service.errors import ServiceError
        buyer = self.message('buyer', text='I want the seller image')
        seller = self.message('seller', role='seller')
        manifest = [
            {'alias': 'p1', 'image_id': 'seller-img', 'message_id': 'seller', 'role': 'seller'},
            {'alias': 'p2', 'image_id': 'supp', 'origin': 'business_supplement', 'attachment_id': 'supp', 'role': 'seller'},
            {'alias': 'p3', 'image_id': 'foreign', 'message_id': 'foreign-message', 'role': 'buyer'}]
        context = {'case': self.case, 'recent_messages': [buyer, seller], 'historical_evidence': [],
                   'case_catalog': [{'case_ref': 'c1', 'requirements': {}}], 'image_manifest': manifest,
                   'photo_context': {'read_images': [{'image_id': x['image_id']} for x in manifest]}}
        def output(value):
            return {'requirement_changes': [{'action': 'update', 'case_ref': 'c1', 'trigger_evidence_ids': ['buyer'],
                     'set_fields': {'product_images': {'value': [value], 'evidence_ids': ['buyer', 'seller']}}}]}
        result, _ = validate_changes(output('p1'), context)
        self.assertEqual(len(result[0]['set_fields']['product_images']['value']), 1)
        for invalid in ('p2', 'p3'):
            with self.subTest(invalid=invalid), self.assertRaises(ServiceError):
                validate_changes(output(invalid), context)
        payload = project_model_payload(context)
        self.assertIn('media_id', payload['image_manifest'][0])
        self.assertNotIn('media_id', payload['image_manifest'][1])

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
        self.assertEqual(result['message_view'][0]['text'], json.dumps(card))
        self.assertEqual(result['message_view'][1]['text'], context['message_view'][1]['text'])
        self.assertNotIn('facts', result)
        self.assertNotIn('derived summary', json.dumps(result))
        self.assertNotIn('skuImages', json.dumps(result['message_semantics']))

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
        views = {row['message_id']: row['text'] for row in result['message_view']}
        self.assertEqual(views['buyer'], buyer['text'])
        for number in range(2):
            original = self.repo.get('message', encoded(['c', 'native-' + str(number)]))
            self.assertEqual(views[original['message_id']], original['text'])
        self.assertNotIn('facts', result)
        self.assertNotIn('requirement_items', result)
        self.assertNotIn('CATALOG_SECRET', json.dumps(result.get('case_catalog', [])))

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

    def photo_fixture(self):
        # Exercise the production chat-image queue through isolated synthetic support.
        from test_message_photos import MessagePhotoTests
        fixture = MessagePhotoTests(methodName="test_duplicate_requests_share_job_and_late_result_is_stale")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_late_source_change_finishes_stale_without_writing_asset(self):
        fixture = self.photo_fixture()
        service = fixture.service
        started = threading.Event()
        asset = service.integrations._synthetic_photo_asset()
        def load(message):
            started.set()
            fixture.release.wait(3)
            return copy.deepcopy(asset)
        with patch.object(service.integrations, "load_photo", side_effect=load):
            ack = service.call("load_message_image", fixture.params)
            self.assertEqual(ack, service.call("load_message_image", fixture.params))
            self.assertTrue(started.wait(3))
            fixture.ingest(text="changed source")
            fixture.release.set()
            self.assertEqual(fixture.wait(ack)["status"], "stale")
        self.assertIsNone(service.repo.get("image_asset", asset["sha256"]))

    def test_late_read_generation_or_closing_change_finishes_cancelled(self):
        for transition in ("read_generation", "closing"):
            with self.subTest(transition=transition):
                fixture = self.photo_fixture()
                service = fixture.service
                started = threading.Event()
                asset = service.integrations._synthetic_photo_asset()
                def load(message):
                    started.set()
                    fixture.release.wait(3)
                    return copy.deepcopy(asset)
                with patch.object(service.integrations, "load_photo", side_effect=load):
                    ack = service.call("load_message_image", fixture.params)
                    self.assertTrue(started.wait(3))
                    with service._lock:
                        if transition == "read_generation":
                            service._monitor_generation += 1
                        else:
                            service._closing = True
                    fixture.release.set()
                    self.assertEqual(fixture.wait(ack)["status"], "cancelled")
                self.assertIsNone(service.repo.get("image_asset", asset["sha256"]))

    def test_shutdown_finishes_queued_jobs_without_starting_downloads(self):
        fixture = self.photo_fixture()
        service = fixture.service
        messages = [fixture.message] + [fixture.ingest(mid="extra-" + str(i)) for i in range(4)]
        started = threading.Event()
        lock, calls = threading.Lock(), []
        def load(message):
            with lock:
                calls.append(message["message_id"])
                if len(calls) == 2:
                    started.set()
            fixture.release.wait(3)
            return service.integrations._synthetic_photo_asset()
        with patch.object(service.integrations, "load_photo", side_effect=load):
            jobs = [service.call("load_message_image", fixture.parameters(row)) for row in messages[:4]]
            self.assertTrue(started.wait(3))
            from reception_service.errors import ServiceError
            with self.assertRaises(ServiceError) as full:
                service._enqueue_photo(fixture.parameters(messages[4]))
            self.assertEqual(full.exception.code, "image_queue_full")
            with service._lock:
                service._closing = True
            stopped = threading.Thread(target=service._stop_requirement_media)
            stopped.start()
            fixture.release.set()
            stopped.join(3)
            self.assertFalse(stopped.is_alive())
        self.assertEqual(len(calls), 2)
        self.assertEqual([fixture.wait(job)["status"] for job in jobs], ["cancelled"] * 4)
        self.assertFalse(service.repo.list("image_asset"))

    def test_submit_failure_rolls_back_job_request_and_cache_and_releases_slot(self):
        fixture = self.photo_fixture()
        service = fixture.service
        params = fixture.params
        before_jobs = service.repo.list("job")
        with patch.object(service._requirement_media_pool, "submit", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                service._enqueue_photo(params)
        self.assertEqual(service.repo.list("job"), before_jobs)
        self.assertIsNone(service.repo.get("message_image", encoded([params["conversation_id"], params["message_id"]])))
        # The bounded queue remains available after transaction rollback.
        ack = service.call("load_message_image", params)
        self.assertEqual(fixture.wait(ack)["status"], "succeeded")
        self.assertEqual(service.call("get_message_image", params)["status"], "ready")

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
        card = self.message("card", text="product", attachments=[{"kind": "native-content", "content": {
            "cardType": "product", "fields": {"imageUrl": "https://img.alicdn.com/p.jpg"}}}])
        self.repo.put("message", encoded(["c", "card"]), card)
        self.assertEqual([row["kind"] for row in service.get_requirement_media("case")["items"]], ["customer_image"])
        from reception_service.errors import ServiceError
        with self.assertRaises(ServiceError) as error:
            service.load_requirement_media("case", "legacy-product-image", "load")
        self.assertEqual(error.exception.code, "image_unavailable")
        service._stop_requirement_media()
        restarted = Service(self.repo)
        try:
            reloaded = restarted.get_requirement_media("case")
            self.assertEqual(reloaded["cover_id"], updated["cover_id"])
            self.assertFalse(reloaded["items"][0]["is_product_source"])
            self.assertEqual(restarted.invalidated, [])
            self.assertNotIn("img.alicdn.com", json.dumps(reloaded))
        finally:
            restarted._stop_requirement_media()


if __name__ == '__main__':
    unittest.main()
