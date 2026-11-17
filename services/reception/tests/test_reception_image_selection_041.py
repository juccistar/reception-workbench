"""Synthetic regression tests for reception image selection."""
import unittest
from io import BytesIO

from PIL import Image
from reception_service.image_assets import decode_image
from reception_service.photos import photo_version
from reception_service.reception_context import shared_context
from reception_service.repository import Repository, encoded
from reception_service.requirement_media import get_reception_source_images


class ReceptionImageSelectionTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(None, 'seller')
        self.addCleanup(self.repo.close)
        self.owner = {'seller_id': 'seller', 'buyer_id': 'buyer', 'conversation_id': 'cid', 'case_id': None}
        self.repo.put('conversation', 'cid', self.owner)
        self.image = {**self.owner, 'message_id': 'logo', 'role': 'buyer', 'own': False, 'text': '[image]',
                      'attachments': [], 'timestamp_ms': 1}
        self.image.pop('case_id')
        self.repo.put('message', encoded(['cid', 'logo']), self.image)
        output = BytesIO()
        Image.new('RGB', (3, 3), 'blue').save(output, 'PNG')
        asset = decode_image(output.getvalue(), 'image/png')
        self.repo.put('image_asset', asset['sha256'], asset)
        self.repo.put('message_image', encoded(['cid', 'logo']), {
            **{k: self.image[k] for k in ('seller_id', 'buyer_id', 'conversation_id', 'message_id')},
            'source_version': photo_version(self.image), 'sha256': asset['sha256'], 'status': 'ready'})

    def case(self, cid, requirements=None):
        case = {**self.owner, 'case_id': cid, 'revision': 1, 'state': 'collecting', 'requirements': requirements or {}}
        self.repo.put('case', cid, case)
        return case

    def ids(self, case=None, messages=None):
        return [m['message_id'] for m in get_reception_source_images(
            self.repo, self.owner, case, messages if messages is not None else [self.image])]

    def test_fresh_manual_context_includes_ready_sole_buyer_original(self):
        context = shared_context(self.repo, self.owner, None, task_mode='manual_suggestion')
        self.assertEqual([r['message_id'] for r in context['photo_context']['direct_images']], ['logo'])
        self.assertEqual(context['image_manifest'][0]['message_id'], 'logo')

    def test_current_case_non_product_evidence_and_no_old_case_absorption(self):
        old = self.case('old')
        active = self.case('active', {'customization': {'value': 'logo', 'evidence_ids': ['logo']}})
        self.owner['case_id'] = 'active'
        self.assertEqual(self.ids(active), ['logo'])
        self.assertEqual(self.ids(old), [])
        self.assertEqual(self.ids(), ['logo'])

    def test_disabled_source_is_not_reintroduced_by_requirement_evidence(self):
        case = self.case('active', {'customization': {'value': 'logo', 'evidence_ids': ['logo']}})
        self.repo.put('requirement_media', 'active', {**{k: self.owner[k] for k in ('seller_id', 'buyer_id', 'conversation_id')},
            'sources': {'logo': {'enabled': False, 'source_version': photo_version(self.image)}}})
        self.assertEqual(self.ids(case), [])

    def test_no_case_uses_only_current_dialogue_window(self):
        messages = [{**self.image, 'message_id': 'old', 'timestamp_ms': 1}]
        messages += [{**self.image, 'message_id': f'text-{i}', 'text': 'hello',
                      'timestamp_ms': i + 2} for i in range(51)]
        messages.append({**self.image, 'message_id': 'new', 'timestamp_ms': 100})
        self.assertEqual(self.ids(messages=messages), ['new'])

    def test_configured_window_and_current_case_boundaries(self):
        old = self.case('old')
        active = self.case('active', {'product': {'value': '已有产品', 'evidence_ids': []}})
        self.owner['case_id'] = 'active'
        rows = [{**self.image, 'message_id': 'text0', 'text': 'earlier', 'timestamp_ms': 0}, self.image,
                {**self.image, 'message_id': 'text1', 'text': 'first', 'timestamp_ms': 2},
                {**self.image, 'message_id': 'new1', 'timestamp_ms': 3},
                {**self.image, 'message_id': 'new2', 'timestamp_ms': 4},
                {**self.image, 'message_id': 'text2', 'text': 'last', 'timestamp_ms': 5}]
        selected = get_reception_source_images(self.repo, self.owner, active, rows, settings={'context_message_count': 1})
        self.assertEqual([m['message_id'] for m in selected], [])
        selected = get_reception_source_images(self.repo, self.owner, active, rows, settings={'context_message_count': 2})
        self.assertEqual([m['message_id'] for m in selected], ['new1', 'new2'])
        self.assertEqual(get_reception_source_images(self.repo, self.owner, old, rows, settings={'context_message_count': 2}), [])
        self.repo.put('requirement_media', 'active', {**{k: self.owner[k] for k in ('seller_id', 'buyer_id', 'conversation_id')},
            'sources': {'new1': {'enabled': False, 'source_version': photo_version(rows[3])}}})
        self.assertEqual([m['message_id'] for m in get_reception_source_images(
            self.repo, self.owner, active, rows, settings={'context_message_count': 2})], ['new2'])

    def test_polish_excludes_history_images(self):
        context = shared_context(self.repo, self.owner, None, task_mode='polish')
        self.assertEqual(context['photo_context']['direct_images'], [])

    def test_cached_original_preparation_uses_the_same_selection_as_context(self):
        import threading
        from reception_service.message_photos import MessagePhotosMixin
        for has_case in (False, True):
            with self.subTest(has_case=has_case):
                case = None
                if has_case:
                    self.case('old')
                    case = self.case('active', {'customization': {'value': 'logo', 'evidence_ids': ['logo']}})
                    self.owner['case_id'] = 'active'
                    self.repo.put('conversation', 'cid', self.owner)
                self.repo.put('message', encoded(['cid', 'logo']), self.image)
                self.repo.put('job', 'generation', {'case_id': case['case_id'] if case else None})
                class Worker(MessagePhotosMixin):
                    _lock = threading.RLock()
                    settings = {}
                    _epoch = 1
                    _closing = False
                    account_verified = True
                    seller_id = 'seller'
                    def _read_token(self, facade): return 'synthetic-token'
                    def _require_current_read(self, token): pass
                    def _require(self, kind, key): return self.repo.get(kind, key)
                    def _supplement(self, cid): return {}
                    def _generation_cancel_event(self, job): return None
                    def _yield_to_ready_work(self): pass
                    def _job_update(self, job, **fields): self.coverage = fields.get('photo_coverage', self.coverage)
                worker = Worker()
                worker.repo, worker.coverage = self.repo, None
                guard = worker._prepare_direct_photos('cid', 'generation', 1, object())
                guard()
                self.assertEqual(worker.coverage['ready_count'], 1)
                context = shared_context(self.repo, self.owner, case, task_mode='manual')
                inputs = worker._direct_photo_inputs(context)
                try:
                    self.assertEqual(len(inputs), 1)
                    self.assertEqual(inputs[0]['message']['message_id'], 'logo')
                    self.assertEqual(context['image_manifest'][0]['message_id'], 'logo')
                finally:
                    inputs.owner.close_if_owned()
                if has_case:
                    for mid, text, stamp in [('t0', 'earlier', 0), ('t1', 'first', 2),
                                             ('new', '[image]', 3), ('t2', 'last', 4)]:
                        row = {**self.image, 'message_id': mid, 'text': text, 'timestamp_ms': stamp}
                        self.repo.put('message', encoded(['cid', mid]), row)
                        if mid == 'new':
                            cached = self.repo.get('message_image', encoded(['cid', 'logo']))
                            self.repo.put('message_image', encoded(['cid', mid]), {
                                **cached, 'message_id': mid, 'source_version': photo_version(row)})
                    worker.settings = {'context_message_count': 2}
                    worker._prepare_direct_photos('cid', 'generation', 1, object())()
                    context = shared_context(self.repo, self.owner, case, task_mode='manual', settings=worker.settings)
                    self.assertEqual({item['message_id'] for item in worker.coverage['items']}, {'logo', 'new'})
                    self.assertEqual({item['message_id'] for item in context['photo_context']['direct_images']}, {'logo', 'new'})

    def test_multiple_buyer_images_and_foreign_images(self):
        other = {**self.image, 'message_id': 'other'}
        self.assertEqual(self.ids(messages=[self.image, other]), ['logo', 'other'])
        foreign = {**self.image, 'buyer_id': 'foreign'}
        self.assertEqual(self.ids(messages=[foreign]), [])
        seller = {**self.image, 'role': 'seller'}
        self.assertEqual(self.ids(messages=[seller]), [])


if __name__ == '__main__':
    unittest.main()
