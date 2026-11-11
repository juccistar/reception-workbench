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

    def ingest(self, mid='photo-one', cid='photo-conversation', text='[image]', role='buyer'):
        message = dict(message_id=mid, conversation_id=cid, buyer_id='photo-buyer', seller_id='demo-seller',
                       role=role, own=role == 'seller', text=text, attachments=[], timestamp_ms=1900000000000)
        self.service._ingest(dict(conversation_id=cid, buyer_id='photo-buyer', customer_name='Synthetic photo'),
                             [message], has_more=False, full=True)
        return message

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
        self.assertNotIn('data:image', str(detail))
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

    def test_seller_image_is_unavailable_and_not_mislabeled(self):
        message = self.ingest(mid='seller-image', role='seller')
        state = self.service.call('get_message_image', self.parameters(message))
        self.assertEqual(state['status'], 'unavailable')
        with self.assertRaises(ServiceError):
            self.service.call('load_message_image', self.parameters(message))


if __name__ == '__main__':
    unittest.main()
