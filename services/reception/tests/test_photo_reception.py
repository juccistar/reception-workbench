"""Photo-to-reception state transitions with synthetic adapters and temporary DBs."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.photos import photo_version, photo_observation_id
from reception_service.repository import encoded, now_ms
from reception_service.service import ReceptionService


class PhotoReceptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.schedule = patch.object(ReceptionService, '_schedule', return_value=None)
        self.schedule.start()
        self.service = ReceptionService(Path(self.temp.name))
        self.release = threading.Event()
        self.observed = []
        self.contexts = []
        self.message = self.ingest()
        self.observe_patch = patch.object(self.service.integrations, 'observe_photo', side_effect=self.observe)
        self.observe_patch.start()
        self.generate_patch = patch.object(self.service.integrations, 'generate', side_effect=self.generate)
        self.generate_patch.start()
        self.service.set_scope('selected', ['photo-cid'], 10)
        self.service.set_reception(True, auto_send=False)

    def tearDown(self):
        self.release.set()
        self.service.close()
        self.generate_patch.stop()
        self.observe_patch.stop()
        self.schedule.stop()
        self.temp.cleanup()

    def ingest(self, mid='photo-1', cid='photo-cid', text='[image]', stamp=1900000000000, attachments=None):
        message = dict(message_id=mid, conversation_id=cid, seller_id='demo-seller', buyer_id='buyer-' + cid,
                       role='buyer', own=False, text=text, attachments=attachments or [], timestamp_ms=stamp)
        self.service._ingest(dict(conversation_id=cid, buyer_id=message['buyer_id'], customer_name='Synthetic photo'),
                             [message], has_more=False, full=True)
        return message

    def observe(self, message, asset, request_id, *, on_start):
        on_start()
        self.observed.append(message['message_id'])
        return dict(description='A blue bag.', visible_text=['DEMO'], uncertainties=['Material is unconfirmed.'],
                    request_id='native-' + request_id, image_sha256=asset['sha256'])

    def generate(self, context, request_id):
        self.contexts.append(copy.deepcopy(context))
        observations = context['photo_context']['observations']
        sources = [o['observation_id'] for o in observations] or [context['recent_messages'][-1]['message_id']]
        # Careful clarification remains suitable when only part of a batch was observed.
        body = 'Could you confirm which product and style you need?'
        return dict(action='ask', facts={}, reply=body, summary=context['case']['summary'],
                    question_keys=[], new_case=False, procurement_key=context['case']['procurement_key'],
                    conflicts=[], missing=['product', 'quantity'], reply_sources=sources)

    def generate_ack(self, cid='photo-cid', automatic=True):
        case = self.service.get_conversation(cid)['case']
        return self.service._enqueue('generate_draft', {'case_id': case['case_id'],
                    'expected_case_revision': case['revision']}, automatic=automatic)

    def wait(self, ack):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            job = self.service.repo.get('job', ack['job_id'])
            if job['status'] not in ('queued', 'running'):
                return job
            time.sleep(.005)
        self.fail('synthetic photo reception did not finish')

    def completed(self, ack):
        job = self.wait(ack)
        self.assertEqual(job['status'], 'succeeded', job)
        return job

    def preview(self, message=None):
        message = message or self.message
        return self.service.get_message_image(message['conversation_id'], message['message_id'], photo_version(message))

    def record(self, message=None):
        message = message or self.message
        return self.service.repo.get('photo_observation', encoded([message['conversation_id'], message['message_id']]))

    def test_photo_preview_observation_and_text_share_worker_and_preserve_facts(self):
        load = self.service.integrations.load_photo
        with patch.object(self.service.integrations, 'load_photo', wraps=load) as fetched:
            job = self.completed(self.generate_ack())
            self.service._queue.join()
            self.assertEqual(fetched.call_count, 1)
        self.assertEqual(self.observed, ['photo-1'])
        self.assertEqual(len(self.contexts), 1)
        self.assertTrue(self.contexts[0]['photo_context']['observations_available'])
        self.assertFalse(self.contexts[0]['photo_context']['pixel_access'])
        self.assertEqual(self.service._budget()['ai_used'], 2)
        self.assertEqual(self.service._budget()['used'], 0)
        self.assertEqual(job['vision_calls_started'], 1)
        self.assertEqual(job['ai_reserved_remaining'], 0)
        self.assertEqual(self.preview()['vision_status'], 'ready')
        self.assertEqual(self.preview()['vision_description'], 'A blue bag.')
        self.assertNotIn('request_id', self.preview())
        detail = self.service.get_conversation('photo-cid')
        self.assertEqual(detail['case']['facts'], {})
        self.assertEqual(detail['messages'][-1]['text'], '[image]')
        self.assertTrue(self.record()['request_id'].startswith('native-vision_'))

    def test_multi_image_calls_charge_once_and_later_generation_reuses_observations(self):
        second = self.ingest('photo-2', stamp=1900000000001)
        self.completed(self.generate_ack())
        self.assertEqual(self.observed, ['photo-2', 'photo-1'])
        self.assertEqual(self.service._budget()['ai_used'], 3)
        self.assertEqual(len(self.contexts[0]['photo_context']['observations']), 2)
        self.completed(self.generate_ack(automatic=False))
        self.assertEqual(self.observed, ['photo-2', 'photo-1'])
        self.assertEqual(self.service._budget()['ai_used'], 4)
        self.assertEqual(self.preview(second)['vision_status'], 'ready')

    def test_failure_costs_one_call_then_text_fallback_without_automatic_retry(self):
        def failed(message, asset, request_id, *, on_start):
            on_start()
            self.observed.append(message['message_id'])
            raise ServiceError('vision_unavailable', 'private response with secret URL')
        self.service.integrations.observe_photo.side_effect = failed
        self.completed(self.generate_ack())
        self.assertEqual(self.service._budget()['ai_used'], 2)
        self.assertFalse(self.contexts[-1]['photo_context']['observations_available'])
        self.assertEqual(self.record()['status'], 'failed')
        self.assertNotIn('secret', str(self.record()))
        self.assertEqual(self.preview()['vision_status'], 'failed')
        self.ingest('buyer-followup', text='Can you help?', stamp=1900000000002)
        self.completed(self.generate_ack())
        self.assertEqual(self.observed, ['photo-1'])
        self.assertEqual(self.service._budget()['ai_used'], 3)

    def test_precheck_failure_is_free_and_explicit_manual_retry_is_bounded(self):
        attempts = []
        def unavailable(message, asset, request_id, *, on_start):
            attempts.append(request_id)
            raise ServiceError('workspace_unavailable', 'No workspace')
        self.service.integrations.observe_photo.side_effect = unavailable
        self.completed(self.generate_ack())
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertFalse(self.record()['attempt_started'])
        self.completed(self.generate_ack(automatic=False))
        self.assertEqual(len(attempts), 1)
        for expected in (2, 3, 3):
            record = self.record()
            record['attempted_at'] = now_ms() - 60001
            self.service.repo.put('photo_observation', encoded(['photo-cid', 'photo-1']), record)
            self.completed(self.generate_ack(automatic=False))
            self.assertEqual(len(attempts), expected)
        self.assertEqual(self.record()['attempts'], 3)

    def test_old_single_call_cap_allows_photo_and_text(self):
        self.service._scope['max_auto_replies'] = 1
        self.completed(self.generate_ack())
        self.assertEqual(self.observed, ['photo-1'])
        self.assertTrue(self.contexts[0]['photo_context']['observations_available'])
        self.assertEqual(self.service._budget()['ai_used'], 2)
        self.assertEqual(self.preview()['vision_status'], 'ready')

    def test_old_cap_allows_both_photos_and_text(self):
        self.service._scope['max_auto_replies'] = 2
        second = self.ingest('photo-2', stamp=1900000000001)
        self.completed(self.generate_ack())
        self.assertEqual(self.observed, ['photo-2', 'photo-1'])
        self.assertEqual(self.service._budget()['ai_used'], 3)
        self.assertEqual(self.preview(second)['vision_status'], 'ready')
        self.assertEqual(self.preview()['vision_status'], 'ready')
        events = self.contexts[-1]['photo_context']['events']
        self.assertEqual([e['message_id'] for e in events], ['photo-1', 'photo-2'])
        self.assertEqual([e['availability'] for e in events], ['observed', 'observed'])

    def test_visual_and_text_calls_continue_across_customers(self):
        self.ingest(cid='second-cid')
        self.service._scope['max_auto_replies'] = 4
        self.service._frozen_scope['conversation_ids'].append('second-cid')
        started = threading.Event()
        original = self.observe
        def blocked(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0]['conversation_id'] == 'photo-cid':
                started.set()
                self.release.wait(6)
            return result
        self.service.integrations.observe_photo.side_effect = blocked
        first = self.generate_ack()
        self.assertTrue(started.wait(3))
        second = self.generate_ack('second-cid')
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.release.set()
        self.completed(first)
        self.completed(second)
        self.assertEqual(self.service._budget()['ai_used'], 4)
        self.assertIsNone(self.service._budget()['ai_remaining'])

    def test_only_four_recent_photos_are_observed_and_others_stay_unseen(self):
        for index in range(2, 6):
            self.ingest('photo-' + str(index), stamp=1900000000000 + index)
        self.completed(self.generate_ack())
        self.assertEqual(self.observed, ['photo-5', 'photo-4', 'photo-3', 'photo-2'])
        events = self.contexts[0]['photo_context']['events']
        self.assertEqual(events[0]['availability'], 'unavailable')
        self.assertEqual(sum(e['availability'] == 'observed' for e in events), 4)
        self.assertEqual(self.service._budget()['ai_used'], 5)

    def block_observation(self):
        started = threading.Event()
        original = self.observe
        def blocked(*args, **kwargs):
            result = original(*args, **kwargs)
            started.set()
            self.release.wait(6)
            return result
        self.service.integrations.observe_photo.side_effect = blocked
        ack = self.generate_ack()
        self.assertTrue(started.wait(3))
        self.assertEqual(self.preview()['vision_status'], 'processing')
        return ack

    def assert_discarded(self, ack):
        self.release.set()
        job = self.wait(ack)
        self.assertEqual(job['status'], 'cancelled', job)
        self.assertFalse(self.contexts)
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertFalse(self.service.get_context(job['case_id'])['photo_context']['observations_available'])
        self.assertFalse(self.service.repo.list('draft', case_id=job['case_id']))

    def test_pause_discards_late_observation_and_abandoned_attempt_is_not_replayed(self):
        ack = self.block_observation()
        self.service.set_reception(False)
        self.assert_discarded(ack)
        self.assertEqual(self.preview()['vision_status'], 'failed')
        self.service.set_reception(True)
        self.service.integrations.observe_photo.side_effect = self.observe
        self.completed(self.generate_ack(automatic=False))
        self.assertEqual(self.observed, ['photo-1'])
        self.assertFalse(self.contexts[-1]['photo_context']['observations_available'])

    def test_revoked_during_prechecks_never_charges_or_invokes_model(self):
        preparing = threading.Event()
        def precheck(message, asset, request_id, *, on_start):
            preparing.set()
            self.release.wait(6)
            on_start()
            self.fail('revoked callback must prevent the model call')
        self.service.integrations.observe_photo.side_effect = precheck
        ack = self.generate_ack()
        self.assertTrue(preparing.wait(3))
        self.service.set_reception(False)
        self.release.set()
        self.assertEqual(self.wait(ack)['status'], 'cancelled')
        self.assertEqual(self.service._budget()['ai_used'], 0)
        self.assertIsNone(self.record())
        self.assertFalse(self.contexts)

    def test_inflight_image_hash_change_discards_the_result(self):
        ack = self.block_observation()
        key = encoded(['photo-cid', 'photo-1'])
        state = self.service.repo.get('message_image', key)
        old = self.service.repo.get('image_asset', state['sha256'])
        state['sha256'] = 'b' * 64
        self.service.repo.put('image_asset', 'b' * 64, dict(old, sha256='b' * 64))
        self.service.repo.put('message_image', key, state)
        self.assert_discarded(ack)

    def test_unknown_submission_discards_visual_result_and_keeps_blocker(self):
        ack = self.block_observation()
        self.service.repo.put('operation', 'unknown-photo-send', dict(operation_id='unknown-photo-send',
            conversation_id='photo-cid', case_id=self.service.get_conversation('photo-cid')['case']['case_id'],
            status='unknown', kind='send'))
        self.assert_discarded(ack)
        self.assertEqual(self.service.repo.get('operation', 'unknown-photo-send')['status'], 'unknown')

    def test_close_does_not_downgrade_preview_completed_inline(self):
        ack = self.block_observation()
        preview_job_id = self.service.repo.get('message_image', encoded(['photo-cid', 'photo-1']))['job_id']
        self.assertEqual(self.service.repo.get('job', preview_job_id)['status'], 'succeeded')
        self.service._pause(closing=True)
        self.assert_discarded(ack)
        self.service._worker.join(timeout=3)
        self.assertFalse(self.service._worker.is_alive())
        self.assertEqual(self.service.repo.get('job', preview_job_id)['status'], 'succeeded')

    def test_takeover_discards_late_observation(self):
        ack = self.block_observation()
        self.service.takeover('photo-cid', True)
        self.assert_discarded(ack)

    def test_raw_revision_discards_late_observation(self):
        ack = self.block_observation()
        self.ingest(text=' [image] ')
        self.assert_discarded(ack)

    def test_connection_change_discards_late_observation(self):
        ack = self.block_observation()
        with self.service._lock:
            self.service._monitor_generation += 1
        self.assert_discarded(ack)

    def test_manual_input_survives_and_discards_late_observation(self):
        ack = self.block_observation()
        self.service.save_chat_draft('photo-cid', 'My own response.', None, 'manual-during-vision')
        self.assert_discarded(ack)
        self.assertEqual(self.service.repo.get('chat_draft', 'photo-cid')['body'], 'My own response.')

    def test_reading_does_not_invalidate_observation_but_image_hash_change_does(self):
        conversation = self.service.repo.get('conversation', 'photo-cid')
        conversation['unread_count'] = 2
        self.service.repo.put('conversation', 'photo-cid', conversation)
        ack = self.block_observation()
        conversation = self.service.repo.get('conversation', 'photo-cid')
        conversation['unread_count'] = 0
        self.service.repo.put('conversation', 'photo-cid', conversation)
        self.release.set()
        self.completed(ack)
        self.assertTrue(self.contexts[-1]['photo_context']['observations_available'])
        draft = self.service.repo.list('draft', case_id=self.contexts[-1]['case']['case_id'])[-1]
        self.service._budget_used = 1
        self.service._validate_send(draft['draft_id'], draft['revision'], manual=True)
        state = self.service.repo.get('message_image', encoded(['photo-cid', 'photo-1']))
        state['sha256'] = 'b' * 64
        self.service.repo.put('message_image', encoded(['photo-cid', 'photo-1']), state)
        self.service.repo.put('image_asset', 'b' * 64, {'sha256': 'b' * 64})
        with self.assertRaises(ServiceError) as error:
            self.service._validate_send(draft['draft_id'], draft['revision'], manual=True)
        self.assertEqual(error.exception.code, 'stale_photo')

    def test_unstarted_result_is_rejected_without_charging_a_visual_call(self):
        def unstarted(message, asset, request_id, *, on_start):
            return dict(description='A blue bag.', visible_text=[], uncertainties=[],
                        request_id='fake-native', image_sha256=asset['sha256'])
        self.service.integrations.observe_photo.side_effect = unstarted
        self.completed(self.generate_ack())
        self.assertEqual(self.record()['status'], 'failed')
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertFalse(self.contexts[-1]['photo_context']['observations_available'])

    def test_abandoned_request_survives_restart_without_replaying(self):
        ack = self.block_observation()
        self.service.set_reception(False)
        self.assert_discarded(ack)
        self.service.close()
        self.service = ReceptionService(Path(self.temp.name))
        self.assertFalse(self.service.get_snapshot()['reception']['running'])
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertEqual(self.preview()['vision_status'], 'failed')
        with patch.object(self.service.integrations, 'observe_photo', side_effect=self.observe), patch.object(
                self.service.integrations, 'generate', side_effect=self.generate):
            self.completed(self.generate_ack(automatic=False))
        self.assertEqual(self.observed, ['photo-1'])
        self.assertEqual(self.service._budget()['ai_used'], 2)
        self.assertFalse(self.contexts[-1]['photo_context']['observations_available'])

    def test_wrong_hash_cannot_become_current_observation(self):
        def wrong(message, asset, request_id, *, on_start):
            result = self.observe(message, asset, request_id, on_start=on_start)
            result['image_sha256'] = 'b' * 64
            return result
        self.service.integrations.observe_photo.side_effect = wrong
        self.completed(self.generate_ack())
        self.assertEqual(self.record()['status'], 'failed')
        self.assertFalse(self.contexts[-1]['photo_context']['observations_available'])

    def test_effective_observation_change_invalidates_draft_and_confirmation(self):
        self.service.integrations.observe_photo.side_effect = ServiceError('vision_unavailable', 'Unavailable')
        self.completed(self.generate_ack())
        case = self.service.get_conversation('photo-cid')['case']
        draft = self.service.repo.list('draft', case_id=case['case_id'])[-1]
        confirmation = self.service.prepare_send(draft['draft_id'], draft['revision'])
        record = self.record()
        record['attempted_at'] = now_ms() - 60001
        self.service.repo.put('photo_observation', encoded(['photo-cid', 'photo-1']), record)
        self.service.integrations.observe_photo.side_effect = self.observe
        self.completed(self.generate_ack(automatic=False))
        self.assertTrue(self.service.repo.get('confirmation', confirmation['confirmation_id'])['invalidated'])
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'stale')
        self.assertEqual(self.record()['status'], 'ready')

    def test_observation_changed_during_native_preflight_revokes_submission(self):
        self.completed(self.generate_ack())
        case = self.service.get_conversation('photo-cid')['case']
        draft = self.service.repo.list('draft', case_id=case['case_id'])[-1]
        self.service.set_reception(True, auto_send=True)
        original_send = self.service.integrations.send
        authorizations = []
        def changed(intent, authorization):
            record = self.record()
            record['description'] = 'A red bag.'
            self.service.repo.put('photo_observation', encoded(['photo-cid', 'photo-1']), record)
            authorizations.append(authorization())
            return original_send(intent, authorization)
        with patch.object(self.service.integrations, 'send', side_effect=changed):
            ack = self.service._enqueue('automatic_send', {'draft_id': draft['draft_id'],
                      'expected_revision': draft['revision']}, automatic=True)
            self.wait(ack)
        self.assertEqual(authorizations, [False])
        operations = self.service.repo.list('operation', conversation_id='photo-cid')
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]['status'], 'rejected')
        self.assertFalse(operations[0]['submission_started'])

    def test_manual_send_runs_between_photo_observations(self):
        self.ingest('photo-2', stamp=1900000000001)
        self.ingest('other-msg', cid='other-cid', text='Hello')
        started = threading.Event()
        order = []
        original_send = self.service.integrations.send_chat
        def observe(message, asset, request_id, *, on_start):
            order.append(message['message_id'])
            result = self.observe(message, asset, request_id, on_start=on_start)
            if message['message_id'] == 'photo-2':
                started.set()
                self.release.wait(6)
            return result
        def send(*args, **kwargs):
            order.append('manual-send')
            return original_send(*args, **kwargs)
        self.service.integrations.observe_photo.side_effect = observe
        with patch.object(self.service.integrations, 'send_chat', side_effect=send):
            ack = self.generate_ack()
            self.assertTrue(started.wait(3))
            draft = self.service.save_chat_draft('other-cid', 'Manual response.', None, 'other-edit')
            sent = self.service.call('send_chat', {'conversation_id': 'other-cid', 'draft_id': draft['draft_id'],
                    'expected_revision': draft['revision'], 'client_request_id': 'other-send'})
            self.release.set()
            self.completed(ack)
            self.completed(sent)
        self.assertEqual(order, ['photo-2', 'manual-send', 'photo-1'])


if __name__ == '__main__':
    unittest.main()
