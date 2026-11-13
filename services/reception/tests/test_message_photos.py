"""Synthetic photo pipeline: no real account, URL, message or model calls."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.photos import photo_version
from reception_service.repository import encoded
from reception_service.service import ReceptionService


class MessagePhotoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.schedule = patch.object(ReceptionService, '_schedule', return_value=None)
        self.schedule.start()
        self.service = ReceptionService(Path(self.temp.name))
        self.release = threading.Event()
        self.message = self.ingest()
        self.params = self.parameters(self.message)

    def tearDown(self):
        self.release.set()
        self.service.close()
        self.schedule.stop()
        self.temp.cleanup()

    def ingest(self, mid='photo-one', cid='photo-conversation', text='[image]', role='buyer', attachments=None):
        message = dict(message_id=mid, conversation_id=cid, buyer_id='photo-buyer', seller_id='demo-seller',
                       role=role, own=role == 'seller', text=text, attachments=attachments or [], timestamp_ms=1900000000000)
        self.service._ingest(dict(conversation_id=cid, buyer_id='photo-buyer', customer_name='Synthetic photo'),
                             [message], has_more=False, full=True)
        return message

    def bind_sources(self, cid, mids):
        case = self.service.get_conversation(cid)['case']
        for mid in mids:
            message = self.service.repo.get('message', encoded([cid, mid]))
            media = self.service.get_requirement_media(case['case_id'])
            self.service.set_product_image_source(case['case_id'], mid, photo_version(message), True,
                                                  media['revision'], 'bind-' + cid + '-' + mid)
        return case['case_id']

    @staticmethod
    def parameters(message):
        return dict(conversation_id=message['conversation_id'], message_id=message['message_id'],
                    source_version=photo_version(message))

    def wait(self, ack):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            job = self.service.repo.get('job', ack['job_id'])
            if job['status'] not in ('queued', 'running'):
                return job
            self.release.wait(0.005) if not self.release.is_set() else time.sleep(0.005)
        self.fail('photo task did not finish')

    def test_generation_serializes_sources_and_parallelizes_only_downloads(self):
        facade = self.service.integrations
        asset = facade._synthetic_photo_asset()
        for number in range(4):
            self.ingest(mid='photo-extra-' + str(number))
        case_id = self.bind_sources('photo-conversation', ['photo-one'] + ['photo-extra-' + str(n) for n in range(4)])
        generation_id = 'synthetic-generation'
        self.service.repo.put('job', generation_id, {'job_id': generation_id, 'kind': 'generate_draft',
                              'status': 'running', 'stage': 'analysis', 'conversation_id': 'photo-conversation', 'case_id': case_id})
        started = threading.Event()
        mutex = threading.Lock()
        active, peak, source_threads = [0], [0], []
        owner = threading.get_ident()

        def source(message):
            source_threads.append(threading.get_ident())
            return message['message_id']

        def download(mid):
            with mutex:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                if active[0] == 2:
                    started.set()
            started.wait(2)
            with mutex:
                active[0] -= 1
            if mid == 'photo-extra-0':
                raise ValueError('private synthetic provider details')
            return copy.deepcopy(asset)

        with patch.object(self.service._queue, 'put'), patch.object(self.service, '_yield_to_ready_work'), \
             patch.object(self.service, '_generation_cancel_event', return_value=threading.Event()), \
             patch.object(facade, 'read_photo_source', side_effect=source, create=True), \
             patch.object(facade, 'fetch_photo_source', side_effect=download, create=True):
            guard = self.service._prepare_direct_photos('photo-conversation', generation_id, self.service._epoch, facade)
            with self.service._lock:
                guard()
        self.assertEqual(peak[0], 2)
        self.assertEqual(source_threads, [owner] * 5)
        report = self.service.repo.get('job', generation_id)['photo_coverage']
        self.assertEqual((report['total_count'], report['ready_count'], report['failed_count']), (5, 4, 1))
        self.assertNotIn('private synthetic', str(report))
        self.assertGreater(guard.photo_deadline, time.monotonic())

    def test_five_eight_twenty_images_prepare_despite_preview_backpressure(self):
        facade = self.service.integrations
        asset = facade._synthetic_photo_asset()
        with patch.object(self.service._queue, 'put'), patch.object(self.service, '_yield_to_ready_work'), \
             patch.object(self.service, '_generation_cancel_event', return_value=threading.Event()), \
             patch.object(facade, 'read_photo_source', side_effect=lambda message: message['message_id'], create=True), \
             patch.object(facade, 'fetch_photo_source', side_effect=lambda source: copy.deepcopy(asset), create=True):
            for count in (5, 8, 20):
                cid = 'images-' + str(count)
                for index in range(count):
                    self.ingest(mid='image-' + str(index), cid=cid)
                case_id = self.bind_sources(cid, ['image-' + str(index) for index in range(count)])
                job_id = 'generation-' + str(count)
                self.service.repo.put('job', job_id, {'job_id': job_id, 'kind': 'generate_draft',
                                      'status': 'running', 'stage': 'analysis', 'conversation_id': cid, 'case_id': case_id})
                self.service._prepare_direct_photos(cid, job_id, self.service._epoch, facade)
                report = self.service.repo.get('job', job_id)['photo_coverage']
                with self.subTest(count=count):
                    self.assertEqual((report['total_count'], report['ready_count'], report['pending_count']),
                                     (count, count, 0))

    def test_cancelled_preparation_reports_pending_without_loading(self):
        generation_id = 'cancelled-generation'
        case_id = self.bind_sources('photo-conversation', ['photo-one'])
        self.service.repo.put('job', generation_id, {'job_id': generation_id, 'kind': 'generate_draft',
                              'status': 'running', 'stage': 'analysis', 'conversation_id': 'photo-conversation', 'case_id': case_id})
        cancelled = threading.Event()
        cancelled.set()
        facade = self.service.integrations
        with patch.object(self.service, '_generation_cancel_event', return_value=cancelled), \
             patch.object(facade, 'load_photo') as load:
            with self.assertRaises(ServiceError) as caught:
                self.service._prepare_direct_photos('photo-conversation', generation_id, self.service._epoch, facade)
        self.assertEqual(caught.exception.code, 'cancelled')
        load.assert_not_called()
        report = self.service.repo.get('job', generation_id)['photo_coverage']
        self.assertEqual(report['pending_count'], 1)
        self.assertEqual(report['items'][0]['reason'], 'cancelled')

    def test_old_references_and_images_after_accepted_source_are_candidates(self):
        messages = [{'message_id': f'm-{i:03}', 'timestamp_ms': i, 'role': 'buyer',
                     'text': '[image]' if i in (1, 5, 20, 30) else 'message', 'attachments': []}
                    for i in range(90)]
        drafts = [{'status': 'accepted', 'source_message_id': 'm-015'}]
        case = {'facts': {'product': {'evidence_ids': ['m-001']}}}
        def list_rows(kind, **kwargs):
            return messages if kind == 'message' else drafts if kind == 'draft' else []
        def get_row(kind, key, default=None):
            return {'case_id': 'case'} if kind == 'conversation' else case if kind == 'case' else default
        with patch.object(self.service.repo, 'list', side_effect=list_rows), \
             patch.object(self.service.repo, 'get', side_effect=get_row):
            images = self.service._vision_candidates('synthetic', include_seller=True)
        self.assertEqual([row['message_id'] for row in images], ['m-001', 'm-020', 'm-030'])

    def test_candidates_cover_fifty_text_messages_and_interleaved_image_burst(self):
        rows = []
        for index in range(55):
            rows.append({'message_id': f'text-{index:03}', 'timestamp_ms': index * 100,
                         'role': 'buyer', 'text': 'synthetic', 'attachments': []})
            if index == 10:
                rows.extend({'message_id': f'image-{number:03}', 'timestamp_ms': index * 100 + number + 1,
                             'role': 'seller' if number % 2 else 'buyer', 'text': '[image]', 'attachments': []}
                            for number in range(20))
        with patch.object(self.service.repo, 'list', return_value=rows):
            images = self.service._vision_candidates('synthetic', include_seller=True)
        self.assertEqual([item['message_id'] for item in images], [f'image-{number:03}' for number in range(20)])

    def test_preview_preserves_original_and_survives_restart(self):
        before = copy.deepcopy(self.service.repo.get('message', encoded(['photo-conversation', 'photo-one'])))
        conversation_before = self.service.repo.get('conversation', 'photo-conversation')
        job = self.wait(self.service.call('load_message_image', self.params))
        self.assertEqual(job['status'], 'succeeded', job)
        preview = self.service.call('get_message_image', self.params)
        self.assertEqual(preview['status'], 'ready')
        self.assertTrue(preview['data_url'].startswith('data:image/jpeg;base64,'))
        self.assertNotIn('original_b64', preview)
        self.assertEqual(before, self.service.repo.get('message', encoded(['photo-conversation', 'photo-one'])))
        self.assertEqual(conversation_before, self.service.repo.get('conversation', 'photo-conversation'))
        detail = self.service.get_conversation('photo-conversation')
        self.assertEqual(detail['messages'][0]['attachments'][0]['availability'], 'ready')
        self.assertNotIn('data:image', str(detail['messages']))
        self.assertFalse(self.service.get_context(detail['case']['case_id'])['photo_context']['pixel_access'])
        self.assertEqual(self.service.call('load_message_image', self.params)['job_id'], job['job_id'])
        self.service.close()
        self.service = ReceptionService(Path(self.temp.name))
        self.assertEqual(preview, self.service.call('get_message_image', self.params))

    def test_full_workspace_backup_restores_image_bytes_and_binding(self):
        from reception_service.workspace_backup import backup_workspace, restore_workspace
        self.assertEqual(self.wait(self.service.call('load_message_image', self.params))['status'], 'succeeded')
        preview = self.service.call('get_message_image', self.params)
        asset = self.service.repo.get('image_asset', preview['sha256'])
        self.service.close()
        archive = tempfile.TemporaryDirectory()
        self.addCleanup(archive.cleanup)
        destination = Path(archive.name)
        backup = backup_workspace(Path(self.temp.name), destination / 'backup')
        restored = restore_workspace(backup, destination / 'restored')
        self.service = ReceptionService(restored)
        self.assertEqual(preview, self.service.call('get_message_image', self.params))
        self.assertEqual(asset, self.service.repo.get('image_asset', preview['sha256']))
        self.assertFalse(self.service.get_snapshot()['reception']['running'])

    def test_duplicate_requests_share_job_and_late_result_is_stale(self):
        started = threading.Event()
        original = self.service.integrations.load_photo
        def blocked(message):
            started.set()
            self.release.wait(4)
            return original(message)
        with patch.object(self.service.integrations, 'load_photo', side_effect=blocked) as load:
            first = self.service.call('load_message_image', self.params)
            self.assertTrue(started.wait(2))
            self.assertEqual(first, self.service.call('load_message_image', self.params))
            self.ingest(text='Corrected text')
            self.release.set()
            self.assertEqual(self.wait(first)['status'], 'stale')
            self.assertEqual(load.call_count, 1)
        self.assertFalse(self.service.repo.list('image_asset'))
        with self.assertRaises(ServiceError):
            self.service.call('get_message_image', self.params)

    def test_failures_do_not_repeat_without_explicit_bounded_retry(self):
        with patch.object(self.service.integrations, 'load_photo', side_effect=ValueError('private URL')) as load:
            first = self.service.call('load_message_image', self.params)
            self.assertEqual(self.wait(first)['status'], 'failed')
            self.assertEqual(first, self.service.call('load_message_image', self.params))
            state = self.service.call('get_message_image', self.params)
            self.assertEqual(state['status'], 'failed')
            self.assertNotIn('private URL', str(state))
            with self.assertRaises(ServiceError):
                self.service.call('load_message_image', {**self.params, 'retry': True})
            for _ in range(2):
                record = self.service.repo.get('message_image', encoded(['photo-conversation', 'photo-one']))
                with patch('reception_service.message_photos.now_ms', return_value=record['attempted_at'] + 60001):
                    ack = self.service.call('load_message_image', {**self.params, 'retry': True})
                    self.assertEqual(self.wait(ack)['status'], 'failed')
            with self.assertRaises(ServiceError):
                self.service.call('load_message_image', {**self.params, 'retry': True})
            self.assertEqual(load.call_count, 3)

    def test_cross_customer_and_old_version_rejected(self):
        other = self.ingest(cid='other-conversation', mid='other-photo')
        for change in ({'conversation_id': other['conversation_id']}, {'source_version': '0' * 64},
                       {'message_id': other['message_id']}):
            for method in ('get_message_image', 'load_message_image'):
                with self.subTest(change=change, method=method), self.assertRaises(ServiceError):
                    self.service.call(method, {**self.params, **change})

    def test_global_queue_is_bounded(self):
        started = threading.Event()
        original = self.service.integrations.load_photo
        def blocked(message):
            started.set()
            self.release.wait(4)
            return original(message)
        with patch.object(self.service.integrations, 'load_photo', side_effect=blocked):
            jobs = [self.service.call('load_message_image', self.params)]
            self.assertTrue(started.wait(2))
            for index in range(3):
                message = self.ingest(mid='photo-' + str(index))
                jobs.append(self.service.call('load_message_image', self.parameters(message)))
            fifth = self.ingest(mid='fifth')
            with self.assertRaises(ServiceError) as error:
                self.service.call('load_message_image', self.parameters(fifth))
            self.assertEqual(error.exception.code, 'image_queue_full')
            self.release.set()
            for job in jobs:
                self.assertEqual(self.wait(job)['status'], 'succeeded')

    def test_settings_change_discards_inflight_result(self):
        started = threading.Event()
        original = self.service.integrations.load_photo
        def blocked(message):
            started.set()
            self.release.wait(4)
            return original(message)
        with patch.object(self.service.integrations, 'load_photo', side_effect=blocked):
            ack = self.service.call('load_message_image', self.params)
            self.assertTrue(started.wait(2))
            with self.service._lock:
                self.service._monitor_generation += 1
            self.release.set()
            self.assertEqual(self.wait(ack)['status'], 'cancelled')
        self.assertFalse(self.service.repo.list('image_asset'))
        self.assertEqual(self.service.call('get_message_image', self.params)['status'], 'failed')

    def test_settings_change_cancels_jobs_that_are_still_queued(self):
        started = threading.Event()
        original = self.service.integrations.load_photo
        def blocked(message):
            started.set()
            self.release.wait(4)
            return original(message)
        with patch.object(self.service.integrations, 'load_photo', side_effect=blocked) as load:
            first = self.service.call('load_message_image', self.params)
            self.assertTrue(started.wait(2))
            other = self.ingest(mid='queued-photo')
            second = self.service.call('load_message_image', self.parameters(other))
            with self.service._lock:
                self.service._monitor_generation += 1
            self.release.set()
            self.assertEqual(self.wait(first)['status'], 'cancelled')
            self.assertEqual(self.wait(second)['status'], 'cancelled')
            self.assertEqual(load.call_count, 1)

    def test_message_reverting_to_prior_version_gets_a_current_job(self):
        started = threading.Event()
        original = self.service.integrations.load_photo
        def blocked(message):
            started.set()
            self.release.wait(4)
            return original(message)
        with patch.object(self.service.integrations, 'load_photo', side_effect=blocked):
            old_a = self.service.call('load_message_image', self.params)
            self.assertTrue(started.wait(2))
            version_b = self.ingest(text=' [image] ')
            job_b = self.service.call('load_message_image', self.parameters(version_b))
            self.ingest()
            new_a = self.service.call('load_message_image', self.params)
            self.assertNotEqual(old_a, new_a)
            self.release.set()
            self.assertEqual(self.wait(old_a)['status'], 'stale')
            self.assertEqual(self.wait(job_b)['status'], 'failed')
            self.assertEqual(self.wait(new_a)['status'], 'succeeded')
            self.assertEqual(self.service.call('get_message_image', self.params)['status'], 'ready')

    def test_manual_send_runs_before_remaining_optional_photo_jobs(self):
        started = threading.Event()
        order = []
        original = self.service.integrations.load_photo
        send_original = self.service.integrations.send_chat
        def blocked(message):
            order.append(message['message_id'])
            if message['message_id'] == 'photo-one':
                started.set()
                self.release.wait(4)
            return original(message)
        def send(*args, **kwargs):
            order.append('manual-send')
            return send_original(*args, **kwargs)
        with patch.object(self.service.integrations, 'load_photo', side_effect=blocked), patch.object(self.service.integrations, 'send_chat', side_effect=send):
            first = self.service.call('load_message_image', self.params)
            self.assertTrue(started.wait(2))
            other = self.ingest(mid='photo-two')
            second = self.service.call('load_message_image', self.parameters(other))
            draft = self.service.call('save_chat_draft', {'conversation_id': 'photo-conversation', 'body': 'Synthetic manual reply.',
                'expected_revision': None, 'client_mutation_id': 'photo-test-edit'})
            sent = self.service.call('send_chat', {'conversation_id': 'photo-conversation', 'draft_id': draft['draft_id'],
                'expected_revision': draft['revision'], 'client_request_id': 'photo-test-send'})
            self.release.set()
            self.assertEqual(self.wait(first)['status'], 'succeeded')
            self.assertEqual(self.wait(sent)['status'], 'succeeded')
            self.assertEqual(self.wait(second)['status'], 'succeeded')
        self.assertEqual(order, ['photo-one', 'manual-send', 'photo-two'])

    def test_seller_image_preview_and_context_survive_restart_without_relabeling(self):
        message = self.ingest(mid='seller-image', role='seller')
        self.ingest(mid='buyer-selection', text='I want this one',
                    attachments=[{'kind': 'quote', 'content': {'messageId': 'seller-image'}}])
        params = self.parameters(message)
        state = self.service.call('get_message_image', params)
        self.assertEqual(state['status'], 'not_loaded')
        self.assertEqual(self.wait(self.service.call('load_message_image', params))['status'], 'succeeded')
        preview = self.service.call('get_message_image', params)
        self.assertEqual(preview['status'], 'ready')
        self.assertTrue(preview['data_url'].startswith('data:image/jpeg;base64,'))
        stored = self.service.repo.get('message', encoded(['photo-conversation', 'seller-image']))
        self.assertEqual(stored, message)
        detail = self.service.get_conversation('photo-conversation')
        projected = next(row for row in detail['messages'] if row['message_id'] == 'seller-image')
        self.assertEqual(projected['role'], 'seller')
        self.assertTrue(projected['own'])
        self.assertEqual(projected['attachments'][0]['availability'], 'ready')
        photo = self.service.get_context(detail['case']['case_id'])['photo_context']
        self.assertEqual(photo['direct_images'][0]['role'], 'seller')
        self.assertFalse(photo['pixel_access'])
        self.service.close()
        self.service = ReceptionService(Path(self.temp.name))
        self.assertEqual(self.service.call('get_message_image', params), preview)


if __name__ == '__main__':
    unittest.main()
