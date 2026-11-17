"""Frozen multi-message replies through the real local service and native ledger."""
import copy
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_native_chat as native_fixtures
import test_native_automatic_chat as automatic_fixtures
from reception_service.repository import now_ms
from reception_service.service import ReceptionService


class ReplyBatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='arw-batch-synthetic-')
        self.addCleanup(self.temporary.cleanup)
        self.scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)
        self.service = ReceptionService(Path(self.temporary.name))
        self.addCleanup(lambda: self.service.close())
        self.owner = {'conversation_id': 'batch-customer', 'buyer_id': 'batch-buyer',
                      'seller_id': 'demo-seller', 'customer_name': 'Synthetic batch'}
        self.add_message('buyer-1', 'I need blue glasses, 500 pairs, black, ABS, 10 cm.')
        self.case_id = self.service.repo.get('conversation', self.owner['conversation_id'])['case_id']
        self.service.set_scope('selected', [self.owner['conversation_id']], None)
        self.service.set_reception(True, True)
        self.service._next_poll = time.monotonic() + 1000

    def add_message(self, mid, body, role='buyer'):
        row = {**self.owner, 'message_id': mid, 'role': role, 'text': body,
               'timestamp_ms': now_ms(), 'attachments': [], 'own': role == 'seller'}
        self.service.integrations.messages.append(copy.deepcopy(row))
        self.service._ingest(self.owner, [row])
        return row

    def idle(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not any(job['status'] in ('queued', 'running') for job in self.service.repo.list('job')):
                return
            threading.Event().wait(0.005)
        self.fail('Synthetic batch worker did not settle')

    def generate(self, count=2):
        self.service.integrations.generate = lambda context, request_id, **kw: {
            'action': 'ask', 'reply_messages': [
                {'body': ['Noted.', 'What is the delivery postcode?', 'Do you have a preferred delivery date?'][index],
                 'reply_sources': ['buyer-1']} for index in range(count)]}
        case = self.service.repo.get('case', self.case_id)
        self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']})
        self.idle()
        rows = self.service.repo.list('reply_batch', conversation_id=self.owner['conversation_id'])
        self.assertEqual(len(rows), 1, self.service.list_jobs())
        return rows[0]

    def tick(self):
        with self.service._lock, self.service.repo.transaction():
            self.service._schedule_reply_batches()
        self.idle()

    def test_progress_stages_follow_persisted_draft_and_real_send_receipt(self):
        observed = []
        original_emit = self.service._emit
        def emit(event, data):
            if event == 'job.updated':
                observed.append(copy.deepcopy(data))
            return original_emit(event, data)
        with patch.object(self.service, '_emit', side_effect=emit):
            batch = self.generate()
            self.tick()
        generation = [row for row in observed if row.get('kind') == 'generate_draft']
        stages = [row['stage'] for row in generation]
        self.assertLess(stages.index('save_draft'), stages.index('prepare_send'))
        prepared = next(row for row in generation if row['stage'] == 'prepare_send')
        self.assertTrue(self.service.repo.get('draft', prepared['draft_id']))
        self.assertTrue(any(row.get('kind') == 'automatic_send' and row['stage'] == 'submit_send' for row in observed))
        current = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual(current['parts'][0]['status'], 'accepted')
        self.assertTrue(current['parts'][0]['message_id'])

    def test_waiting_sync_takeover_stops_only_unsubmitted_tail(self):
        batch = self.generate()
        self.tick()
        batch = self.service.repo.get('reply_batch', batch['batch_id'])
        batch['status'] = 'waiting_sync'
        batch['parts'][1]['status'] = 'waiting_sync'
        self.service.repo.put('reply_batch', batch['batch_id'], batch)
        self.assertTrue(self.service._conversation_unresolved(self.owner['conversation_id']))
        self.service.begin_manual_edit(self.owner['conversation_id'])
        batch = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual(batch['status'], 'stopped')
        self.assertEqual([p['status'] for p in batch['parts']], ['accepted', 'cancelled'])
        self.assertFalse(self.service._conversation_unresolved(self.owner['conversation_id']))

    def test_recover_stopped_tail_requires_evidence_and_preserves_draft(self):
        batch = self.generate()
        self.tick()
        batch = self.service.repo.get('reply_batch', batch['batch_id'])
        with self.service._lock, self.service.repo.transaction():
            self.service._stop_reply_batch(batch, 'test')
        cid = self.owner['conversation_id']
        available = self.service._recoverable_reply_batches(cid)
        self.assertEqual([(row['accepted'], row['remaining']) for row in available], [(1, 1)])
        args = (cid, batch['batch_id'], batch['messages_hash'], None, 'recover-test')
        result = self.service.recover_reply_batch_draft(*args)
        self.assertEqual(result['message_parts'], [batch['parts'][1]['body']])
        self.assertEqual(self.service.recover_reply_batch_draft(*args), result)
        with self.assertRaises(Exception):
            self.service.recover_reply_batch_draft(cid, batch['batch_id'], batch['messages_hash'], result['revision'], 'recover-again')
        self.assertEqual(self.service.repo.get('reply_batch', batch['batch_id'])['status'], 'stopped')

    def test_recover_checks_each_rejected_attempt_and_unresolved_fence(self):
        batch = self.generate()
        self.tick()
        batch = self.service.repo.get('reply_batch', batch['batch_id'])
        with self.service._lock, self.service.repo.transaction():
            self.service._stop_reply_batch(batch, 'test')
            first = self.service.repo.get('operation', batch['parts'][0]['operation_ids'][0])
            op = {**first, 'operation_id': 'tail-op', 'part_id': batch['parts'][1]['part_id'], 'part_index': 1,
                  'intent': {**first['intent'], 'reply': batch['parts'][1]['body']},
                  'status': 'rejected', 'submission_started': False, 'message_id': None}
            batch['parts'][1]['operation_ids'] = ['tail-op']
            self.service.repo.put('operation', 'tail-op', op)
            self.service.repo.put('reply_batch', batch['batch_id'], batch)
        cid = self.owner['conversation_id']
        self.assertEqual(len(self.service._recoverable_reply_batches(cid)), 1)
        op['submission_started'] = None
        self.service.repo.put('operation', 'tail-op', op)
        self.assertEqual(self.service._recoverable_reply_batches(cid), [])
        op['submission_started'] = False
        self.service.repo.put('operation', 'tail-op', op)
        other = {**op, 'operation_id': 'other-op', 'batch_id': 'other', 'status': 'unknown'}
        self.service.repo.put('operation', 'other-op', other)
        self.assertEqual(self.service._recoverable_reply_batches(cid), [])

    def test_recover_rejects_missing_or_ambiguous_tail_operation(self):
        batch = self.generate()
        self.tick()
        batch = self.service.repo.get('reply_batch', batch['batch_id'])
        with self.service._lock, self.service.repo.transaction():
            self.service._stop_reply_batch(batch, 'test')
            batch['parts'][1]['operation_ids'] = ['missing']
            self.service.repo.put('reply_batch', batch['batch_id'], batch)
        self.assertEqual(self.service._recoverable_reply_batches(self.owner['conversation_id']), [])
        with self.assertRaises(Exception):
            self.service.recover_reply_batch_draft(self.owner['conversation_id'], batch['batch_id'], batch['messages_hash'], None, 'missing-op')

    def test_two_parts_use_distinct_receipts_and_self_history_does_not_cancel_tail(self):
        batch = self.generate()
        self.tick()
        first = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual(first['parts'][0]['status'], 'accepted', self.service.list_jobs())
        self.assertEqual(first['parts'][1]['status'], 'planned')
        self.assertTrue(self.service._conversation_unresolved(self.owner['conversation_id']))
        self.tick()
        final = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual(final['status'], 'accepted', final)
        self.assertEqual(len({part['message_id'] for part in final['parts']}), 2)
        sent = [row for row in self.service.repo.list('message', conversation_id=self.owner['conversation_id']) if row['role'] == 'seller']
        self.assertEqual([row['text'] for row in sent], [part['body'] for part in final['parts']])
        self.assertFalse(self.service._conversation_unresolved(self.owner['conversation_id']))
        self.tick()
        self.assertEqual(len(self.service.repo.list('operation', conversation_id=self.owner['conversation_id'])), 2)
        detail = self.service.get_conversation(self.owner['conversation_id'])
        self.assertEqual(detail['draft']['draft_id'], final['parent_draft_id'])
        self.assertEqual(detail['reply_batches'][0]['accepted'], 2)

    def test_three_parts_advance_only_after_each_distinct_receipt(self):
        batch = self.generate(3)
        for index in range(3):
            self.tick()
            current = self.service.repo.get('reply_batch', batch['batch_id'])
            self.assertEqual([part['status'] for part in current['parts']],
                             ['accepted'] * (index + 1) + ['planned'] * (2 - index))
            self.assertEqual(len(self.service.repo.list('operation', conversation_id=self.owner['conversation_id'])), index + 1)
        self.assertEqual(current['status'], 'accepted')
        self.assertEqual(len({part['message_id'] for part in current['parts']}), 3)
        self.tick()
        self.assertEqual(len(self.service.repo.list('operation', conversation_id=self.owner['conversation_id'])), 3)

    def test_pending_sync_and_scope_and_source_identity_stop_batch(self):
        batch = self.generate()
        cid = self.owner['conversation_id']
        with patch.object(self.service, '_message_sync_pending', return_value=True):
            self.assertFalse(self.service._reply_batch_basis_current(batch))
        with patch.object(self.service, '_in_scope', return_value=False):
            self.assertFalse(self.service._reply_batch_basis_current(batch))
        with patch.object(self.service.repo, 'automatic_source_facts',
                          return_value=({**self.added_source(), 'timestamp_ms': None}, False)):
            self.assertFalse(self.service._reply_batch_basis_current(batch))
        source = self.added_source()
        with patch.object(self.service.repo, 'automatic_source_facts',
                          return_value=({**source, 'buyer_id': 'other-buyer'}, False)):
            self.assertFalse(self.service._reply_batch_basis_current(batch))
        self.assertTrue(self.service._reply_batch_basis_current(batch))

    def added_source(self):
        return self.service._latest_buyer(self.owner['conversation_id'])

    def test_sync_echo_before_send_return_allows_tail(self):
        original = self.service.integrations.send
        calls, synced = [], []
        def send(intent, authorization):
            calls.append(intent)
            result = original(intent, authorization)
            synced.append(self.add_message(result['message_id'], intent['reply'], role='seller'))
            with self.service._lock, self.service.repo.transaction():
                self.service._schedule_reply_batches()
            result['timestamp_ms'] = synced[-1]['timestamp_ms'] + 5000
            return result
        self.service.integrations.send = send
        batch = self.generate()
        self.tick()
        self.tick()
        current = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual([row['status'] for row in current['parts']], ['accepted', 'accepted'])
        self.assertEqual(len(calls), 2)
        from reception_service.repository import encoded
        saved = self.service.repo.get('message', encoded([self.owner['conversation_id'], synced[0]['message_id']]))
        self.assertEqual(saved['timestamp_ms'], synced[0]['timestamp_ms'])

    def test_changes_during_send_stop_tail(self):
        for change in ('buyer', 'manual_same_text', 'authorization', 'basis', 'supplement', 'pending', 'wrong_id', 'wrong_body'):
            with self.subTest(change=change):
                if change != 'buyer':
                    self.tearDown()
                    self.doCleanups()
                    self.setUp()
                original = self.service.integrations.send
                calls = []
                def send(intent, authorization):
                    calls.append(intent)
                    result = original(intent, authorization)
                    echo_id = 'wrong-platform-id' if change == 'wrong_id' else result['message_id']
                    echo_body = 'Different body' if change == 'wrong_body' else intent['reply']
                    self.add_message(echo_id, echo_body, role='seller')
                    if change == 'buyer':
                        self.add_message('new-buyer', 'New instruction')
                    elif change == 'manual_same_text':
                        self.add_message('other-platform-id', intent['reply'], role='seller')
                    elif change == 'authorization':
                        self.service.set_reception(False, False)
                    elif change == 'pending':
                        self.service._message_sync_pending = lambda cid: True
                    elif change == 'supplement':
                        cid = self.owner['conversation_id']
                        self.service.repo.put('conversation_supplement', cid,
                            {'conversation_id': cid, 'body': 'New business information', 'revision': 1, 'updated_at': now_ms()})
                    elif change == 'basis':
                        case = self.service.repo.get('case', self.case_id)
                        case['revision'] += 1
                        self.service.repo.put('case', self.case_id, case)
                    return result
                self.service.integrations.send = send
                batch = self.generate()
                self.tick()
                self.tick()
                current = self.service.repo.get('reply_batch', batch['batch_id'])
                self.assertEqual([row['status'] for row in current['parts']],
                                 ['accepted', 'waiting_sync' if change == 'pending' else 'cancelled'])
                self.assertEqual(len(calls), 1)

    def test_unknown_middle_part_never_submits_tail_or_replays_prefix(self):
        original = self.service.integrations.send
        calls = []
        def send(intent, authorization):
            calls.append(copy.deepcopy(intent))
            if len(calls) == 2:
                return {'status': 'unknown', 'message_id': 'pending-original-id', 'submission_started': True}
            return original(intent, authorization)
        self.service.integrations.send = send
        batch = self.generate(3)
        for _ in range(4):
            self.tick()
        current = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual([row['status'] for row in current['parts']], ['accepted', 'unknown', 'planned'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(current['status'], 'waiting_receipt')
        self.service.set_reception(False, False)
        self.tick()
        current = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual(current['parts'][2]['status'], 'cancelled')
        operation_id = current['parts'][1]['operation_ids'][-1]
        self.service.integrations.reconcile_operation = lambda oid: {'status': 'accepted', 'message_id': 'pending-original-id',
            'submission_started': True, 'timestamp_ms': now_ms()}
        self.service.reconcile_operation(operation_id)
        self.tick()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.service.repo.get('reply_batch', batch['batch_id'])['status'], 'stopped')

    def test_new_buyer_message_cancels_remaining_parts_and_restart_does_not_resume(self):
        batch = self.generate(3)
        self.tick()
        self.add_message('buyer-2', 'Actually, please wait.')
        self.tick()
        current = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual([row['status'] for row in current['parts']], ['accepted', 'cancelled', 'cancelled'])
        self.service.close()
        self.service = ReceptionService(Path(self.temporary.name))
        current = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual(current['status'], 'stopped')
        self.assertEqual(len(self.service.repo.list('operation', conversation_id=self.owner['conversation_id'])), 1)

    def test_transient_retries_are_per_part_and_never_reset_after_new_jobs(self):
        original = self.service.integrations.send
        calls = []
        def send(intent, authorization):
            calls.append(copy.deepcopy(intent))
            if intent.get('part_index') == 1:
                return {'status': 'rejected', 'submission_started': False, 'error_code': 'timeout', 'stage': 'cli'}
            return original(intent, authorization)
        self.service.integrations.send = send
        batch = self.generate(3)
        self.tick()
        for attempt in range(3):
            self.tick()
            current = self.service.repo.get('reply_batch', batch['batch_id'])
            part = current['parts'][1]
            self.assertEqual(len(part['operation_ids']), attempt + 1, current)
            if attempt < 2:
                self.assertEqual(current['status'], 'waiting_retry')
                operation = self.service.repo.get('operation', part['operation_ids'][-1])
                operation['updated_at'] -= 20000
                self.service.repo.put('operation', operation['operation_id'], operation)
                child = self.service.repo.get('draft', part['draft_id'])
                child['retry_after'] = 0
                self.service.repo.put('draft', child['draft_id'], child)
        for _ in range(2):
            self.tick()
        current = self.service.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual(current['status'], 'stopped')
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({row['part_id'] for row in calls[1:]}), 1)
        self.assertEqual(current['parts'][2]['status'], 'cancelled')

    def test_combined_display_separators_do_not_consume_ai_body_budget(self):
        self.service._auto_send = False
        self.service.integrations.generate = lambda context, request_id, **kw: {
            'action': 'ask', 'reply_messages': [{'body': 'a' * 600, 'reply_sources': ['buyer-1']},
                                             {'body': 'b' * 600, 'reply_sources': ['buyer-1']}]}
        case = self.service.repo.get('case', self.case_id)
        ack = self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']})
        self.idle()
        job = self.service.repo.get('job', ack['job_id'])
        self.assertEqual(job['status'], 'succeeded', job)
        draft = self.service.repo.get('draft', job['result']['draft_id'])
        self.assertEqual(len(draft['body']), 1202)
        self.assertEqual(sum(len(row['body']) for row in draft['reply_messages']), 1200)

    def test_social_turn_keeps_grounded_unrecorded_purchase_and_window_evidence(self):
        self.service._auto_send = False
        self.add_message('buyer-thanks', 'Thanks!')
        self.service.integrations.generate = lambda context, request_id, **kw: {
            'action': 'ask', 'reply_messages': [{'body': 'You’re welcome.', 'reply_sources': ['buyer-thanks']}],
            'requirement_changes': [{'action': 'create', 'case_ref': 'new:1', 'trigger_evidence_ids': ['buyer-1'],
                'set_fields': {'product': {'value': '蓝色眼镜', 'evidence_ids': ['buyer-1'],
                    'source_excerpts': {'buyer-1': 'blue glasses'}},
                    'specifications': {'value': '黑色，ABS，10厘米', 'evidence_ids': ['buyer-1'],
                        'source_excerpts': {'buyer-1': 'black, ABS, 10 cm'}}}}]}
        case = self.service.repo.get('case', self.case_id)
        self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']})
        self.idle()
        saved = self.service.repo.get('case', self.case_id)
        self.assertEqual(saved['requirements']['product']['value'], '蓝色眼镜')
        revision = saved['requirements_revision']
        for index in range(55):
            self.add_message('older-window-' + str(index), 'Synthetic informational history.', role='seller')
        context = self.service.get_context(self.case_id)
        self.assertNotIn('buyer-1', [row['message_id'] for row in context['recent_messages']])
        self.assertIn('buyer-1', [row['message_id'] for row in context['historical_evidence']])
        self.assertEqual(context['case_catalog'][0]['requirements']['specifications']['value'], '黑色，ABS，10厘米')
        with self.service._lock, self.service.repo.transaction():
            owner = self.service.repo.get('conversation', self.owner['conversation_id'])
            self.service._apply_requirement_output(owner, saved, {}, context, 'unchanged-window')
        self.assertEqual(self.service.repo.get('case', self.case_id)['requirements_revision'], revision)


class NativeReplyBatchTests(unittest.TestCase):
    read = native_fixtures.NativeChatTests.read

    def setUp(self):
        native_fixtures.NativeChatTests.setUp(self)
        self.bodies = ['First message.', 'Second message.', 'Third message.']
        self.hashes = [hashlib.sha256(body.encode()).hexdigest() for body in self.bodies]

    def intent(self, index, *, operation=None, automatic=False, batch='fixed-batch'):
        base = (automatic_fixtures.NativeAutomaticChatTests.intent(self, operation or 'batch-op-' + str(index))
                if automatic else native_fixtures.NativeChatTests.intent(self, 'batch-request' + (f':part:{index}' if index else ''), self.bodies[index]))
        base.update(reply=self.bodies[index], operation_id=operation or 'batch-op-' + str(index), batch_id=batch,
                    part_id='fixed-part-' + str(index), part_index=index, batch_message_hashes=self.hashes,
                    batch_root_request_id='batch-request', client_request_id='batch-request' + (f':part:{index}' if index else ''))
        return base

    def test_automatic_batch_preserves_original_source_claim_and_checks_prefix(self):
        self.assertEqual(self.native.send(self.intent(1, automatic=True), lambda: True)['status'], 'rejected')
        self.assertEqual(self.calls, [])
        for index in range(3):
            result = self.native.send(self.intent(index, automatic=True), lambda: True)
            self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(sum(row[0] == 'send_msg' for row in self.calls), 3)
        self.assertEqual(self.native.send(self.intent(0, operation='replacement', automatic=True, batch='new-batch'), lambda: True)['status'], 'rejected')
        self.assertEqual(sum(row[0] == 'send_msg' for row in self.calls), 3)

    def test_final_continuation_batch_can_follow_ack_and_its_own_first_receipt(self):
        seller, buyer, cid = native_fixtures.SELLER, native_fixtures.BUYER, native_fixtures.CID
        ack = {'seller_id': seller, 'buyer_id': buyer, 'conversation_id': cid, 'reply': 'Got your message.',
               'purpose': 'continuity', 'continuity_phase': 'ack', 'source_message_id': 'new-buyer-message',
               'source_timestamp_ms': 1793982896000, 'source_text_sha256': 'a' * 64}
        self.native.ledger.claim('original-ack', 'original-ack-key', 'send', ack)
        self.native.ledger.finish('original-ack', {'status': 'accepted', 'message_id': 'ack-message',
            'submission_started': True, 'timestamp_ms': 1793982896100})
        previous_mid, previous_body = 'ack-message', ack['reply']
        for index in range(2):
            intent = self.intent(index, automatic=True)
            intent.update(purpose='continuity', continuity_phase='final', source_timestamp_ms=1793982896000,
                source_text_sha256='a' * 64, latest_message_id=previous_mid, latest_message_role='seller',
                latest_text_sha256=hashlib.sha256(previous_body.encode()).hexdigest())
            result = self.native.send_chat(intent, lambda: True)
            self.assertEqual(result['status'], 'accepted', result)
            previous_mid, previous_body = result['message_id'], self.bodies[index]
        self.assertEqual(sum(row[0] == 'send_msg' for row in self.calls), 2)

    def test_manual_batch_cannot_change_manifest_or_skip_unknown(self):
        self.assertEqual(self.native.send_chat(self.intent(0), lambda: True)['status'], 'accepted')
        changed = self.intent(1)
        changed['batch_message_hashes'] = ['f' * 64, *self.hashes[1:]]
        self.assertEqual(self.native.send_chat(changed, lambda: True)['status'], 'rejected')
        self.visible = False
        self.assertEqual(self.native.send_chat(self.intent(1), lambda: True)['status'], 'unknown')
        self.assertEqual(self.native.send_chat(self.intent(2), lambda: True)['status'], 'rejected')
        self.assertEqual(sum(row[0] == 'send_msg' for row in self.calls), 2)


if __name__ == '__main__':
    unittest.main()
