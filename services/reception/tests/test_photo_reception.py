"""Original images join one business request; synthetic adapters and temporary DBs."""
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


class PhotoReceptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.schedule = patch.object(ReceptionService, '_schedule', return_value=None)
        self.schedule.start()
        self.service = ReceptionService(Path(self.temp.name))
        self.release, self.started = threading.Event(), threading.Event()
        self.contexts, self.inputs = [], []
        self.block = self.before_start = False
        self.message = self.ingest()
        self.real_generate = self.service.integrations.generate
        self.observer = patch.object(self.service.integrations, 'observe_photo', side_effect=AssertionError('Separate vision job must not run'))
        self.observer.start()
        self.generator = patch.object(self.service.integrations, 'generate', side_effect=self.generate)
        self.generator.start()
        self.service.set_scope('selected', ['photo-cid'], None)
        self.service.set_reception(True, auto_send=False)

    def tearDown(self):
        self.release.set()
        self.service.close()
        self.generator.stop()
        self.observer.stop()
        self.schedule.stop()
        self.temp.cleanup()

    def ingest(self, mid='photo-1', cid='photo-cid', text='[image]', stamp=1900000000000, role='buyer', attachments=None):
        message = dict(message_id=mid, conversation_id=cid, seller_id='demo-seller', buyer_id='buyer-' + cid,
                       role=role, own=role == 'seller', text=text, attachments=attachments or [], timestamp_ms=stamp)
        self.service._ingest(dict(conversation_id=cid, buyer_id=message['buyer_id'], customer_name='Synthetic photo'),
                             [message], has_more=False, full=True)
        return message

    def bind_sources(self, mids, cid='photo-cid'):
        messages = [self.service.repo.get('message', encoded([cid, mid])) for mid in mids]
        sellers = [m for m in messages if m['role'] == 'seller']
        if sellers:
            self.ingest('buyer-select-' + str(len(self.service.repo.list('message', conversation_id=cid))), cid=cid,
                        text='I want these designs', stamp=1900000000100,
                        attachments=[{'kind': 'quote', 'content': {'messageId': m['message_id']}} for m in sellers])
        case = self.service.get_conversation(cid)['case']
        for m in messages:
            media = self.service.get_requirement_media(case['case_id'])
            self.service.set_product_image_source(case['case_id'], m['message_id'], photo_version(m), True,
                                                  media['revision'], 'bind-' + cid + '-' + m['message_id'])

    def generate(self, context, request_id, *, on_call_start=None, photo_inputs=None):
        self.contexts.append(copy.deepcopy(context))
        self.inputs.append(copy.deepcopy(list(photo_inputs)) if photo_inputs is not None else None)
        if self.block:
            if not self.before_start:
                on_call_start()
            self.started.set()
            self.release.wait(5)
            if not self.before_start:
                on_call_start = None
        return self.real_generate(context, request_id, on_call_start=on_call_start, photo_inputs=photo_inputs)

    def generate_ack(self, cid='photo-cid', automatic=True):
        case = self.service.get_conversation(cid)['case']
        return self.service._enqueue('generate_draft', {'case_id': case['case_id'],
            'expected_case_revision': case['revision']}, automatic=automatic)

    def wait(self, ack):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            job = self.service.repo.get('job', ack['job_id'])
            if job['status'] not in ('queued', 'running'):
                return job
            time.sleep(.005)
        self.fail('Synthetic image business task did not finish')

    def completed(self, ack):
        job = self.wait(ack)
        self.assertEqual(job['status'], 'succeeded', job)
        return job

    def preview(self):
        return self.service.get_message_image('photo-cid', 'photo-1', photo_version(self.message))

    def block_generation(self, before_start=False):
        self.block, self.before_start = True, before_start
        ack = self.generate_ack()
        self.assertTrue(self.started.wait(3))
        return ack

    def assert_discarded(self, ack):
        self.release.set()
        job = self.wait(ack)
        self.assertIn(job['status'], ('cancelled', 'stale', 'deferred'), job)
        self.assertFalse(any(d['status'] == 'pending' for d in self.service.repo.list('draft', case_id=job['case_id'])))
        return job

    def test_originals_join_one_business_query_without_independent_vision(self):
        load = self.service.integrations.load_photo
        with patch.object(self.service.integrations, 'load_photo', wraps=load) as fetched:
            job = self.completed(self.generate_ack())
            self.service._queue.join()
            self.assertEqual(fetched.call_count, 1)
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertEqual(job.get('vision_calls_started', 0), 0)
        self.assertEqual(len(self.contexts), 1)
        self.assertFalse(self.contexts[0]['photo_context']['pixel_access'])
        self.assertEqual(self.inputs[0][0]['message']['message_id'], 'photo-1')
        self.assertTrue(self.inputs[0][0]['asset']['original_b64'])
        self.assertNotIn('original_b64', str(self.contexts))
        self.assertEqual(self.preview()['status'], 'ready')
        detail = self.service.get_conversation('photo-cid')
        self.assertEqual(detail['case']['facts'], {})
        self.assertEqual(detail['messages'][-1]['text'], '[image]')
        self.assertEqual(len(detail['draft']['photo_read_images']), 1)
        self.assertIsNone(self.service.repo.get('photo_observation', encoded(['photo-cid', 'photo-1'])))

    def test_automatic_reply_receives_buyer_and_seller_originals_with_roles(self):
        self.ingest('seller-reference', stamp=1900000000001, role='seller')
        self.ingest('buyer-followup', text='Can you quote the design you sent?', stamp=1900000000002)
        self.bind_sources(['photo-1', 'seller-reference'])
        self.completed(self.generate_ack())
        refs = self.contexts[-1]['photo_context']['direct_images']
        self.assertEqual([(row['message_id'], row['role']) for row in refs],
                         [('photo-1', 'buyer'), ('seller-reference', 'seller')])
        self.assertEqual([item['message']['role'] for item in self.inputs[-1]], ['buyer', 'seller'])
        self.assertFalse(self.contexts[-1]['photo_context']['pixel_access'])
        detail = self.service.get_conversation('photo-cid')
        self.assertEqual([row['role'] for row in detail['draft']['photo_read_images']], ['buyer', 'seller'])
        facts = detail['case']['facts']
        self.assertTrue(all(set(fact.get('evidence_ids', [])) <= {'buyer-select-3'} for fact in facts.values()))
        self.assertEqual(self.service.repo.list('photo_observation'), [])

    def test_automatic_reply_prepares_a_seller_image_without_any_buyer_image(self):
        cid = 'seller-only-cid'
        with patch.object(self.service, '_queue_recent_photos', return_value=None):
            self.ingest('seller-only-image', cid=cid, stamp=1900000000010, role='seller')
            self.ingest('seller-only-question', cid=cid, text='Can you explain this design?', stamp=1900000000011)
        self.bind_sources(['seller-only-image'], cid=cid)
        self.service.set_scope('selected', [cid], None)
        self.service.set_reception(True, auto_send=False)
        self.completed(self.generate_ack(cid=cid))
        self.assertEqual([item['message']['role'] for item in self.inputs[-1]], ['seller'])
        context = self.contexts[-1]
        self.assertEqual(context['image_manifest'][0]['alias'], 'p1')
        self.assertEqual(context['image_manifest'][0]['message_id'], 'seller-only-image')
        self.assertEqual(context['image_transport'], 'read_compat')
        self.assertTrue(any(row['text'] == '[图片 p1]' and row['role'] == 'seller' for row in context['message_view']))

    def test_manual_suggestion_receives_both_sides_when_seller_spoke_last(self):
        self.ingest('seller-reference', stamp=1900000000001, role='seller')
        self.bind_sources(['photo-1', 'seller-reference'])
        self.ingest('seller-last', text='Please review these.', stamp=1900000000101, role='seller')
        self.completed(self.service.call('check_ai'))
        inputs = []
        generate = self.service.integrations.generate_manual
        def capture(context, request, *, photo_inputs=None, **options):
            inputs.append((copy.deepcopy(context), copy.deepcopy(list(photo_inputs)) if photo_inputs is not None else None))
            return generate(context, request, photo_inputs=photo_inputs, **options)
        with patch.object(self.service.integrations, 'generate_manual', new=capture):
            self.completed(self.service.generate_manual_suggestion('photo-cid', 'manual-with-seller-image',
                           case_id=self.service.get_conversation('photo-cid')['case']['case_id']))
        self.assertEqual([row['role'] for row in inputs[-1][0]['photo_context']['direct_images']], ['buyer', 'seller'])
        self.assertEqual([item['message']['role'] for item in inputs[-1][1]], ['buyer', 'seller'])

    def test_mixed_images_include_all_buyer_and_seller_originals(self):
        for number in range(1, 6):
            self.ingest('mixed-' + str(number), stamp=1900000000000 + number,
                        role='seller' if number % 2 else 'buyer')
        self.ingest('buyer-question', text='Please help compare these designs.', stamp=1900000000006)
        self.bind_sources(['photo-1'] + ['mixed-' + str(n) for n in range(1, 6)])
        self.completed(self.generate_ack())
        refs = self.contexts[-1]['photo_context']['direct_images']
        self.assertEqual([row['message_id'] for row in refs], ['photo-1', 'mixed-1', 'mixed-2', 'mixed-3', 'mixed-4', 'mixed-5'])
        self.assertEqual({row['role'] for row in refs}, {'buyer', 'seller'})
        self.assertEqual(len(self.inputs[-1]), 6)

    def test_ambiguous_buyer_images_are_not_prepared_for_model(self):
        with patch.object(self.service, '_queue_recent_photos', return_value=None):
            self.ingest('photo-2', stamp=1900000000001)
        with patch.object(self.service.integrations, 'load_photo') as loaded:
            self.completed(self.generate_ack())
        loaded.assert_not_called()
        self.assertEqual(self.inputs[-1], [])
        self.assertEqual(self.contexts[-1]['photo_context']['direct_images'], [])

    def test_multiple_images_share_one_call_and_original_cache_is_reused(self):
        self.ingest('photo-2', stamp=1900000000001)
        self.bind_sources(['photo-1', 'photo-2'])
        load = self.service.integrations.load_photo
        with patch.object(self.service.integrations, 'load_photo', wraps=load) as fetched:
            self.completed(self.generate_ack())
            self.completed(self.generate_ack(automatic=False))
            self.assertEqual(fetched.call_count, 2)
        self.assertEqual(self.service._budget()['ai_used'], 2)
        self.assertEqual([len(items) for items in self.inputs], [2, 2])
        self.assertEqual([row['message_id'] for row in self.contexts[0]['photo_context']['direct_images']], ['photo-1', 'photo-2'])

    def test_generation_waits_for_running_preview_without_loading_twice(self):
        started = threading.Event()
        original = self.service.integrations.load_photo
        def load(message):
            started.set()
            self.release.wait(5)
            return original(message)
        params = {'conversation_id': 'photo-cid', 'message_id': 'photo-1',
                  'source_version': photo_version(self.message)}
        with patch.object(self.service.integrations, 'load_photo', side_effect=load) as loaded:
            preview = self.service.call('load_message_image', params)
            self.assertTrue(started.wait(3))
            generation_id = 'simulated-concurrent-generation'
            self.service.repo.put('job', generation_id, {'job_id': generation_id, 'kind': 'generate_draft',
                                  'status': 'running', 'stage': 'analysis', 'conversation_id': 'photo-cid',
                                  'case_id': self.service.get_conversation('photo-cid')['case']['case_id']})
            outcomes = []
            def prepare():
                try:
                    outcomes.append(self.service._prepare_direct_photos('photo-cid', generation_id,
                                    self.service._epoch, self.service.integrations))
                except Exception as error:
                    outcomes.append(error)
            worker = threading.Thread(target=prepare)
            worker.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = self.service.repo.get('job', generation_id)
                items = (job.get('photo_coverage') or {}).get('items', [])
                if any(item.get('reason') == 'loading_elsewhere' for item in items):
                    break
                time.sleep(.01)
            else:
                self.fail('generation did not wait for running preview: ' + str(job))
            self.release.set()
            self.completed(preview)
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(loaded.call_count, 1)
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(callable(outcomes[0]), outcomes)

    def test_download_failure_remains_unseen_and_does_not_leak_private_error(self):
        with patch.object(self.service.integrations, 'load_photo', side_effect=ServiceError('private', 'private URL with secret')):
            self.completed(self.generate_ack())
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertEqual(self.contexts[0]['photo_context']['direct_images'], [])
        self.assertEqual(self.preview()['status'], 'failed')
        self.assertNotIn('secret', str(self.preview()))

    def test_five_images_are_loaded_into_the_business_request(self):
        for number in range(2, 6):
            self.ingest('photo-' + str(number), stamp=1900000000000 + number)
        self.bind_sources(['photo-' + str(n) for n in range(1, 6)])
        self.completed(self.generate_ack())
        refs = self.contexts[0]['photo_context']['direct_images']
        self.assertEqual([row['message_id'] for row in refs], ['photo-1', 'photo-2', 'photo-3', 'photo-4', 'photo-5'])
        self.assertEqual(self.contexts[0]['photo_context']['events'][0]['availability'], 'ready_for_read')
        self.assertEqual(self.service._budget()['ai_used'], 1)

    def test_pause_discards_late_answer_without_replaying_on_restart(self):
        ack = self.block_generation()
        self.service.set_reception(False)
        self.assert_discarded(ack)
        self.service.close()
        self.service = ReceptionService(Path(self.temp.name))
        self.assertFalse(self.service.get_snapshot()['reception']['running'])
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertEqual(self.preview()['status'], 'ready')

    def test_revocation_before_call_does_not_charge_generation(self):
        ack = self.block_generation(before_start=True)
        self.service.set_reception(False)
        self.assert_discarded(ack)
        self.assertEqual(self.service._budget()['ai_used'], 0)

    def test_changed_image_hash_discards_inflight_answer(self):
        ack = self.block_generation()
        key = encoded(['photo-cid', 'photo-1'])
        state = self.service.repo.get('message_image', key)
        asset = self.service.repo.get('image_asset', state['sha256'])
        state['sha256'] = 'b' * 64
        self.service.repo.put('image_asset', 'b' * 64, dict(asset, sha256='b' * 64))
        self.service.repo.put('message_image', key, state)
        self.assert_discarded(ack)

    def test_unknown_send_stays_blocking_after_late_image_answer(self):
        ack = self.block_generation()
        self.service.repo.put('operation', 'unknown-image-send', {
            'operation_id': 'unknown-image-send', 'conversation_id': 'photo-cid',
            'case_id': self.service.get_conversation('photo-cid')['case']['case_id'], 'status': 'unknown', 'kind': 'send'})
        self.assert_discarded(ack)
        self.assertEqual(self.service.repo.get('operation', 'unknown-image-send')['status'], 'unknown')

    def test_takeover_discards_late_answer(self):
        ack = self.block_generation()
        self.service.takeover('photo-cid', True)
        self.assert_discarded(ack)

    def test_raw_message_revision_discards_late_answer(self):
        ack = self.block_generation()
        self.ingest(text=' [image] ')
        self.assert_discarded(ack)

    def test_connection_change_discards_late_answer(self):
        ack = self.block_generation()
        with self.service._lock:
            self.service._monitor_generation += 1
        self.assert_discarded(ack)

    def test_manual_input_survives_late_answer(self):
        ack = self.block_generation()
        self.service.save_chat_draft('photo-cid', 'My own response.', None, 'manual-input')
        self.assert_discarded(ack)
        self.assertEqual(self.service.repo.get('chat_draft', 'photo-cid')['body'], 'My own response.')

    def test_reading_changes_do_not_invalidate_image_generation(self):
        ack = self.block_generation()
        conversation = self.service.repo.get('conversation', 'photo-cid')
        conversation['unread_count'] = 0
        self.service.repo.put('conversation', 'photo-cid', conversation)
        self.release.set()
        self.completed(ack)
        draft = self.service.get_conversation('photo-cid')['draft']
        self.service._validate_send(draft['draft_id'], draft['revision'], manual=True)

    def test_original_hash_change_during_send_preflight_revokes_submission(self):
        self.completed(self.generate_ack())
        detail = self.service.get_conversation('photo-cid')
        draft = detail['draft']
        authorizations = []
        def changed(intent, authorization):
            state = self.service.repo.get('message_image', encoded(['photo-cid', 'photo-1']))
            asset = self.service.repo.get('image_asset', state['sha256'])
            self.service.repo.put('image_asset', 'b' * 64, dict(asset, sha256='b' * 64))
            self.service.repo.put('message_image', encoded(['photo-cid', 'photo-1']), dict(state, sha256='b' * 64))
            authorizations.append(authorization())
            return {'status': 'rejected', 'submission_started': False}
        with patch.object(self.service.integrations, 'send', side_effect=changed):
            self.service._perform_send(draft, detail['case'], self.service._epoch, self.service.integrations)
        self.assertEqual(authorizations, [False])
        operation = self.service.repo.list('operation', conversation_id='photo-cid')[0]
        self.assertEqual(operation['status'], 'rejected')
        self.assertFalse(operation['submission_started'])

    def test_manual_send_runs_between_original_downloads(self):
        self.ingest('photo-2', stamp=1900000000001)
        self.bind_sources(['photo-1', 'photo-2'])
        self.ingest('other-msg', cid='other-cid', text='Hello')
        started, order = threading.Event(), []
        original_load = self.service.integrations.load_photo
        original_send = self.service.integrations.send_chat
        def load(message):
            order.append(message['message_id'])
            if message['message_id'] == 'photo-2':
                started.set()
                self.release.wait(5)
            return original_load(message)
        def send(*args, **kwargs):
            order.append('manual-send')
            return original_send(*args, **kwargs)
        with patch.object(self.service.integrations, 'load_photo', side_effect=load), patch.object(self.service.integrations, 'send_chat', side_effect=send):
            ack = self.generate_ack()
            self.assertTrue(started.wait(3))
            draft = self.service.save_chat_draft('other-cid', 'Manual response.', None, 'other-edit')
            sent = self.service.call('send_chat', {'conversation_id': 'other-cid', 'draft_id': draft['draft_id'],
                'expected_revision': draft['revision'], 'client_request_id': 'other-send'})
            self.completed(sent)
            self.release.set()
            self.completed(ack)
        self.assertEqual(order, ['photo-2', 'manual-send', 'photo-1'])


if __name__ == '__main__':
    unittest.main()
