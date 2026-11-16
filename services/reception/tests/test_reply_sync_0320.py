"""Fixed-live-mode regressions using only synthetic data and the demo transport."""
import copy
import unittest
from unittest.mock import patch

import test_reply_batches_0319 as fixtures
from reception_service.repository import now_ms
from reception_service.send_gate import evaluate_authorization
from reception_service.send_retry_policy import decision


class ReplySyncTests(unittest.TestCase):
    add_message = fixtures.ReplyBatchServiceTests.add_message
    idle = fixtures.ReplyBatchServiceTests.idle
    generate = fixtures.ReplyBatchServiceTests.generate
    tick = fixtures.ReplyBatchServiceTests.tick

    def setUp(self):
        fixtures.ReplyBatchServiceTests.setUp(self)
        self.service.mode = 'live'
        # These legacy fixtures use a synthetic model, independent of user configuration.
        for name, result in (('_freeze_reception_model', {'purpose': 'reception'}),
                             ('_assert_reception_model', None), ('_verify_model_result', None)):
            stub = patch.object(self.service, name, return_value=result)
            stub.start()
            self.addCleanup(stub.stop)
        self.service._started_at_ms = 0
        self.service._source_baseline = {}
        for name in ('messages', 'ai', 'send_preview'):
            self.service.checks[name]['status'] = 'passed'
        # The normal scheduler is disabled by the fixture. Drive the ordinary
        # sync dispatcher explicitly so the wait is observable deterministically.
        self.dispatcher = patch.object(self.service, '_dispatch_message_syncs')
        self.dispatcher.start()
        self.addCleanup(self.dispatcher.stop)

    def pending(self):
        cid = self.owner['conversation_id']
        old = self.service.repo.get('message_activity', cid, {})
        self.service.repo.put('message_activity', cid, {'conversation_id': cid,
            'version': old.get('version', 0) + 1, 'latest_timestamp': now_ms(), 'activity_clock': 'conversation'})
        self.service.repo.put('message_sync_task', cid, {'conversation_id': cid, 'pending': True, 'activity_required': True})
        return cid

    def caught_up(self):
        cid = self.owner['conversation_id']
        activity = self.service.repo.get('message_activity', cid)
        token = self.service._read_token(self.service.integrations)
        latest = max(r['timestamp_ms'] for r in self.service._messages(cid))
        # Weak activity needs the same two completed reads as production.
        for _ in range(2):
            self.service._complete_message_sync(cid, activity['latest_timestamp'], activity['version'], latest,
                'synthetic-sync', token, activity_clock='conversation', changed=False)
        self.assertFalse(self.service._message_sync_pending(cid))

    def batch(self, frozen):
        return self.service.repo.get('reply_batch', frozen['batch_id'])

    def install_pending_rejection(self, *, code='source_sync_pending'):
        original = self.service.integrations.send
        calls = []
        def send(intent, authorization):
            calls.append(copy.deepcopy(intent))
            if intent.get('part_index') != 1 or len(calls) > 2:
                return original(intent, authorization)
            before = evaluate_authorization(authorization, 'before_claim')
            self.assertTrue(before.allowed)
            self.pending()
            denied = evaluate_authorization(authorization, 'after_token')
            self.assertEqual(denied.reason, 'source_sync_pending')
            self.assertFalse(denied.allowed)
            self.assertTrue(self.service._draft_basis_current(self.service.repo.get('draft', intent['draft_id'])))
            return {'status': 'rejected', 'submission_started': False, 'message_id': '', 'error_code': code,
                'stage': 'send', 'diagnostics': {'code': code, 'stage': 'send', **denied.diagnostics('after_token')}}
        self.service.integrations.send = send
        return calls

    def test_complete_history_incremental_display_change_keeps_three_part_batch(self):
        cid = self.owner['conversation_id']
        self.service._ingest(self.owner, [], full=True, has_more=False)
        batch = self.generate(3)
        ingest = self.service._ingest
        def ingest_with_full_refresh(info, messages, **kwargs):
            changed = ingest(info, messages, **kwargs)
            ingest(info, [], full=True, has_more=False)
            return changed
        # Model a completed full refresh before the accepted echo freezes its new basis.
        with patch.object(self.service, '_ingest', side_effect=ingest_with_full_refresh):
            self.tick()
        first = self.batch(batch)
        self.assertEqual(first['parts'][0]['status'], 'accepted')
        self.assertTrue(first['parts'][0]['message_id'])
        before = self.service.repo.get('coverage', cid)
        self.assertEqual((before['status'], before['message']), ('complete', '历史读取完成'))
        self.assertFalse(self.service._ingest(self.owner, []))
        after = self.service.repo.get('coverage', cid)
        self.assertEqual({k: v for k, v in before.items() if k not in ('message', 'updated_at')},
                         {k: v for k, v in after.items() if k not in ('message', 'updated_at')})
        self.assertEqual(after['message'], '已读取部分消息')
        self.tick()
        self.tick()
        final = self.batch(batch)
        self.assertEqual(final['status'], 'accepted', final)
        self.assertEqual([part['status'] for part in final['parts']], ['accepted'] * 3)
        self.assertEqual(len({part['message_id'] for part in final['parts']}), 3)
        self.tick()
        self.assertEqual(len(self.service.repo.list('operation', conversation_id=cid)), 3)

    def test_echo_before_receipt_with_complete_history_keeps_three_parts(self):
        self.service._ingest(self.owner, [], full=True, has_more=False)
        original = self.service.integrations.send
        calls = []
        def send(intent, authorization):
            calls.append(intent['part_index'])
            result = original(intent, authorization)
            if intent['part_index'] == 0:
                self.service._ingest(self.owner, [{
                    'message_id': result['message_id'], 'seller_id': self.service.seller_id,
                    'buyer_id': self.owner['buyer_id'], 'conversation_id': self.owner['conversation_id'],
                    'role': 'seller', 'text': intent['reply'], 'timestamp_ms': result['timestamp_ms'],
                    'attachments': [], 'own': True}])
                self.service._ingest(self.owner, [], full=True, has_more=False)
            return result
        self.service.integrations.send = send
        batch = self.generate(3)
        for _ in range(3):
            self.tick()
        self.assertEqual(self.batch(batch)['status'], 'accepted', self.batch(batch))
        self.assertEqual(calls, [0, 1, 2])

    def test_echo_before_receipt_with_changed_body_or_attachment_stops_tail(self):
        for change in ('body', 'attachment'):
            with self.subTest(change=change):
                if change != 'body':
                    self.doCleanups()
                    self.setUp()
                original = self.service.integrations.send
                def send(intent, authorization):
                    result = original(intent, authorization)
                    if intent['part_index'] == 0:
                        self.service._ingest(self.owner, [{
                            'message_id': result['message_id'], 'seller_id': self.service.seller_id,
                            'buyer_id': self.owner['buyer_id'], 'conversation_id': self.owner['conversation_id'],
                            'role': 'seller', 'text': 'different' if change == 'body' else intent['reply'],
                            'timestamp_ms': result['timestamp_ms'],
                            'attachments': [{'name': 'other.png'}] if change == 'attachment' else [], 'own': True}])
                    return result
                self.service.integrations.send = send
                batch = self.generate(3)
                self.tick()
                self.tick()
                self.assertEqual(self.batch(batch)['status'], 'stopped')
                self.assertEqual(len(self.service.repo.list('operation', conversation_id=self.owner['conversation_id'])), 1)

    def test_coverage_semantic_changes_stop_unsent_tail(self):
        for key, value in (('status', 'partial'), ('next_cursor', 'older'),
                           ('earliest_ms', 1), ('latest_ms', 2)):
            with self.subTest(key=key):
                if key != 'status':
                    self.doCleanups()
                    self.setUp()
                self.service._ingest(self.owner, [], full=True, has_more=False)
                batch = self.generate(3)
                self.tick()
                cid = self.owner['conversation_id']
                current = self.service.repo.get('coverage', cid)
                current[key] = value
                self.service.repo.put('coverage', cid, current)
                self.tick()
                self.assertEqual(self.batch(batch)['status'], 'stopped')
                self.tick()
                self.assertEqual(len(self.service.repo.list('operation', conversation_id=cid)), 1)

    def _unknown_first_send(self, *, early_echo=False):
        original = self.service.integrations.send
        calls, receipts = [], []
        def send(intent, authorization):
            calls.append(copy.deepcopy(intent))
            result = original(intent, authorization)
            if intent['part_index'] == 0:
                receipts.append(copy.deepcopy(result))
                if early_echo:
                    self.service._ingest(self.owner, [{**self.owner, 'message_id': result['message_id'],
                        'role': 'seller', 'text': intent['reply'], 'timestamp_ms': result['timestamp_ms'],
                        'attachments': [], 'own': True}])
                return {'status': 'unknown', 'submission_started': True, 'message_id': None}
            return result
        self.service.integrations.send = send
        return calls, receipts

    def test_original_receipt_self_echo_waits_then_continues_exactly_once(self):
        calls, receipts = self._unknown_first_send(early_echo=True)
        batch = self.generate(3)
        self.tick()
        operation_id = self.batch(batch)['parts'][0]['operation_ids'][0]
        self.service.integrations.reconcile_operation = lambda _id: {'status': 'unknown'}
        for _ in range(2):
            self.tick()
            self.service.reconcile_operation(operation_id)
        waiting = self.batch(batch)
        self.assertFalse(waiting.get('stopped', False), waiting)
        self.assertEqual(waiting['status'], 'waiting_receipt')
        self.assertEqual(len(calls), 1)
        self.service.integrations.reconcile_operation = lambda _id: copy.deepcopy(receipts[0])
        self.service.reconcile_operation(operation_id)
        self.tick()
        self.tick()
        self.assertEqual(self.batch(batch)['status'], 'accepted', self.batch(batch))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.service.repo.list('operation', conversation_id=self.owner['conversation_id'])), 3)
        self.service.reconcile_operation(operation_id)
        self.tick()
        self.assertEqual(len(calls), 3)

    def test_original_receipt_self_echo_with_changed_basis_stops_tail(self):
        for change in ('buyer', 'body', 'attachment', 'supplement', 'case', 'authorization', 'coverage', 'legacy'):
            with self.subTest(change=change):
                if change != 'buyer':
                    self.doCleanups()
                    self.setUp()
                calls, receipts = self._unknown_first_send(early_echo=True)
                batch = self.generate(3)
                self.tick()
                cid = self.owner['conversation_id']
                operation_id = self.batch(batch)['parts'][0]['operation_ids'][0]
                if change == 'buyer':
                    self.add_message('new-buyer', 'Please change the quantity.')
                elif change in ('body', 'attachment'):
                    from reception_service.repository import encoded
                    key = encoded([cid, receipts[0]['message_id']])
                    row = self.service.repo.get('message', key)
                    if change == 'body':
                        row['text'] = 'a different reply'
                    else:
                        row['attachments'] = [{'name': 'extra.png'}]
                    self.service._ingest(self.owner, [row])
                elif change == 'supplement':
                    self.service.repo.put('conversation_supplement', cid,
                        {'conversation_id': cid, 'body': 'A new business condition', 'revision': 1, 'updated_at': now_ms()})
                elif change == 'case':
                    case = self.service.repo.get('case', self.case_id)
                    case['revision'] += 1
                    self.service.repo.put('case', self.case_id, case)
                elif change == 'authorization':
                    self.service.set_reception(False, False)
                elif change == 'coverage':
                    coverage = self.service.repo.get('coverage', cid)
                    coverage['status'] = 'complete'
                    self.service.repo.put('coverage', cid, coverage)
                else:
                    operation = self.service.repo.get('operation', operation_id)
                    operation.pop('batch_send_basis', None)
                    self.service.repo.put('operation', operation_id, operation)
                self.tick()
                self.service.integrations.reconcile_operation = lambda _id: copy.deepcopy(receipts[0])
                self.service.reconcile_operation(operation_id)
                self.tick()
                self.assertTrue(self.batch(batch)['stopped'], self.batch(batch))
                self.assertEqual(len(calls), 1)

    def test_original_receipt_self_echo_does_not_adopt_same_body_manual_id(self):
        calls, receipts = self._unknown_first_send()
        batch = self.generate(3)
        self.tick()
        self.add_message('manual-same-body', calls[0]['reply'], role='seller')
        self.tick()
        self.assertEqual(len(calls), 1)
        operation_id = self.batch(batch)['parts'][0]['operation_ids'][0]
        self.service.integrations.reconcile_operation = lambda _id: copy.deepcopy(receipts[0])
        self.service.reconcile_operation(operation_id)
        self.tick()
        self.assertTrue(self.batch(batch)['stopped'])
        self.assertEqual(len(calls), 1)

    def test_reconciled_without_time_self_echo_waits_for_exact_platform_message(self):
        calls, receipts = self._unknown_first_send()
        batch = self.generate(3)
        self.tick()
        operation_id = self.batch(batch)['parts'][0]['operation_ids'][0]
        receipt = {k: v for k, v in receipts[0].items() if k != 'timestamp_ms'}
        self.service.integrations.reconcile_operation = lambda _id: copy.deepcopy(receipt)
        self.service.reconcile_operation(operation_id)
        self.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.batch(batch)['status'], 'waiting_sync')
        self.service._ingest(self.owner, [{**self.owner, 'message_id': receipt['message_id'],
            'role': 'seller', 'text': calls[0]['reply'], 'timestamp_ms': receipts[0]['timestamp_ms'],
            'attachments': [], 'own': True}])
        self.tick()
        self.tick()
        self.assertEqual(self.batch(batch)['status'], 'accepted', self.batch(batch))
        self.assertEqual(len(calls), 3)

    def test_direct_without_time_self_echo_does_not_invent_platform_timestamp(self):
        original = self.service.integrations.send
        calls, receipts = [], []
        def send(intent, authorization):
            calls.append(copy.deepcopy(intent))
            result = original(intent, authorization)
            if intent['part_index'] == 0:
                receipts.append(copy.deepcopy(result))
                result = {k: v for k, v in result.items() if k != 'timestamp_ms'}
            return result
        self.service.integrations.send = send
        batch = self.generate(3)
        self.tick()
        from reception_service.repository import encoded
        self.assertIsNone(self.service.repo.get('message', encoded([self.owner['conversation_id'], receipts[0]['message_id']])))
        self.tick()
        self.assertEqual(len(calls), 1)
        self.service._ingest(self.owner, [{**self.owner, 'message_id': receipts[0]['message_id'],
            'role': 'seller', 'text': calls[0]['reply'], 'timestamp_ms': receipts[0]['timestamp_ms'],
            'attachments': [], 'own': True}])
        self.tick()
        self.tick()
        self.assertEqual(self.batch(batch)['status'], 'accepted', self.batch(batch))
        self.assertEqual(len(calls), 3)

    def test_missing_time_self_echo_timeout_and_late_sync_never_resume(self):
        calls, receipts = self._unknown_first_send()
        batch = self.generate(3)
        self.tick()
        operation_id = self.batch(batch)['parts'][0]['operation_ids'][0]
        self.service.integrations.reconcile_operation = lambda _id: {
            k: v for k, v in receipts[0].items() if k != 'timestamp_ms'}
        self.service.reconcile_operation(operation_id)
        waiting = self.batch(batch)
        waiting['parts'][1]['sync_wait_deadline_at'] = now_ms() - 1
        self.service.repo.put('reply_batch', waiting['batch_id'], waiting)
        self.tick()
        self.assertTrue(self.batch(batch)['stopped'])
        self.service._ingest(self.owner, [{**self.owner, 'message_id': receipts[0]['message_id'],
            'role': 'seller', 'text': calls[0]['reply'], 'timestamp_ms': receipts[0]['timestamp_ms'],
            'attachments': [], 'own': True}])
        self.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual([p['status'] for p in self.batch(batch)['parts']], ['accepted', 'cancelled', 'cancelled'])

    def test_unknown_self_echo_restart_keeps_receipt_and_stops_tail(self):
        calls, receipts = self._unknown_first_send(early_echo=True)
        batch = self.generate(3)
        self.tick()
        operation_id = self.batch(batch)['parts'][0]['operation_ids'][0]
        with self.service._lock, self.service.repo.transaction():
            self.service._recover_reply_batches()
        self.service.integrations.reconcile_operation = lambda _id: copy.deepcopy(receipts[0])
        self.service.reconcile_operation(operation_id)
        self.tick()
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.batch(batch)['stopped'])
        self.assertEqual(self.batch(batch)['parts'][0]['status'], 'accepted')

    def test_invalid_accepted_id_self_echo_remains_unknown_without_replay(self):
        for mid in (' ', 123):
            with self.subTest(message_id=mid):
                if mid != ' ':
                    self.doCleanups()
                    self.setUp()
                calls = []
                def send(intent, authorization):
                    calls.append(copy.deepcopy(intent))
                    return {'status': 'accepted', 'submission_started': True, 'message_id': mid}
                self.service.integrations.send = send
                batch = self.generate(3)
                self.tick()
                self.tick()
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.batch(batch)['parts'][0]['status'], 'unknown')
                self.assertEqual(self.batch(batch)['status'], 'waiting_receipt')

    def test_token_interval_pending_waits_then_only_second_part_submits(self):
        calls = self.install_pending_rejection()
        batch = self.generate()
        self.tick()
        self.tick()
        waiting = self.batch(batch)
        self.assertEqual(waiting['status'], 'waiting_sync', self.service.list_jobs())
        self.assertEqual([p['status'] for p in waiting['parts']], ['accepted', 'waiting_sync'])
        first_id = waiting['parts'][0]['message_id']
        second_op = self.service.repo.get('operation', waiting['parts'][1]['operation_ids'][0])
        self.assertIs(second_op['submission_started'], False)
        self.assertEqual(second_op['diagnostics']['gate_checkpoint'], 'after_token')
        self.assertTrue(self.service._conversation_unresolved(self.owner['conversation_id']))
        for _ in range(3):
            self.tick()
        self.assertEqual(len(calls), 2)
        self.caught_up()
        self.tick()
        final = self.batch(batch)
        self.assertEqual(final['status'], 'accepted', final)
        self.assertEqual(final['parts'][0]['message_id'], first_id)
        self.assertEqual([r['part_index'] for r in calls], [0, 1, 1])
        self.assertEqual(len(final['parts'][1]['operation_ids']), 2)
        self.assertEqual(len({r['batch_id'] for r in calls}), 1)
        self.assertEqual(len({r['part_id'] for r in calls[1:]}), 1)
        self.assertTrue(all(j['status'] not in ('queued', 'running') for j in self.service.repo.list('job')))

    def test_progress_projects_current_retry_and_receipts_without_bodies(self):
        frozen = self.generate()
        self.tick()
        batch = self.batch(frozen)
        child = self.service.repo.get('draft', batch['parts'][1]['draft_id'])
        child['retry_after'] = now_ms() + 5000
        self.service.repo.put('draft', child['draft_id'], child)
        batch.update(status='waiting_retry', message='等待重试', last_gate_reason='rate_limit')
        batch['parts'][1]['status'] = 'rejected'
        self.service.repo.put('reply_batch', batch['batch_id'], batch)
        progress = self.service.get_conversation(self.owner['conversation_id'])['reply_batches'][0]
        self.assertEqual((progress['origin'], progress['channel'], progress['phase']), ('automatic', 'international', 'reply'))
        self.assertEqual(progress['conversation_id'], self.owner['conversation_id'])
        self.assertEqual(progress['created_at'], batch['created_at'])
        self.assertEqual(progress['updated_at'], batch['updated_at'])
        self.assertEqual(progress['message'], '等待重试')
        self.assertEqual(progress['last_gate_reason'], 'rate_limit')
        self.assertEqual(progress['retry_at'], child['retry_after'])
        self.assertEqual(progress['parts'], [
            {'part_index': 0, 'status': 'accepted', 'message_id': batch['parts'][0]['message_id']},
            {'part_index': 1, 'status': 'rejected', 'message_id': None}])
        self.assertNotIn('body', str(progress))
        batch['status'] = 'stopped'
        self.service.repo.put('reply_batch', batch['batch_id'], batch)
        stopped = self.service.get_conversation(self.owner['conversation_id'])['reply_batches'][0]
        self.assertNotIn('retry_at', stopped)

    def test_prequeue_wait_creates_no_operation_and_keeps_first_deadline(self):
        batch = self.generate()
        self.pending()
        self.tick()
        first = self.batch(batch)
        self.assertEqual(first['status'], 'waiting_sync')
        self.assertEqual(self.service.repo.list('operation', conversation_id=self.owner['conversation_id']), [])
        self.pending()
        self.tick()
        self.assertEqual(self.batch(batch)['sync_wait_deadline_at'], first['sync_wait_deadline_at'])
        self.caught_up()
        self.tick()
        self.tick()
        self.assertEqual(self.batch(batch)['status'], 'accepted')

    def test_queued_job_adopts_only_proven_self_echo_before_wait(self):
        batch = self.generate()
        self.tick()
        current = self.batch(batch)
        child = self.service.repo.get('draft', current['parts'][1]['draft_id'])
        child['status'] = 'pending'
        self.service.repo.put('draft', child['draft_id'], child)
        self.add_message(current['parts'][0]['message_id'], current['parts'][0]['body'], role='seller')
        self.pending()
        result = self.service._task_automatic_send({'draft_id': child['draft_id'], 'expected_revision': child['revision']},
            'synthetic-queued-job', self.service._epoch, self.service.integrations)
        self.assertEqual(result['_job_status'], 'deferred')
        self.assertEqual(self.batch(batch)['status'], 'waiting_sync')
        self.assertEqual(len(self.service.repo.list('operation', conversation_id=self.owner['conversation_id'])), 1)
        self.caught_up()
        self.tick()
        self.assertEqual(self.batch(batch)['status'], 'accepted')

    def test_queued_job_defers_before_claim_without_counting_attempt(self):
        batch = self.generate()
        part = batch['parts'][0]
        child = self.service.repo.get('draft', part['draft_id'])
        child['status'] = 'pending'
        self.service.repo.put('draft', child['draft_id'], child)
        self.pending()
        result = self.service._task_automatic_send({'draft_id': child['draft_id'], 'expected_revision': child['revision']},
            'synthetic-job', self.service._epoch, self.service.integrations)
        self.assertEqual(result['_job_status'], 'deferred')
        self.assertEqual(self.batch(batch)['status'], 'waiting_sync')
        self.assertFalse(self.service.repo.list('operation', conversation_id=self.owner['conversation_id']))

    def test_accepted_receipt_pending_preserves_tail_and_exact_echo_metadata(self):
        original = self.service.integrations.send
        calls = []
        def send(intent, authorization):
            calls.append(intent['part_index'])
            result = original(intent, authorization)
            if intent['part_index'] == 0:
                self.pending()
            return result
        self.service.integrations.send = send
        batch = self.generate()
        self.tick()
        waiting = self.batch(batch)
        self.assertEqual(waiting['status'], 'waiting_sync', waiting)
        mid = waiting['parts'][0]['message_id']
        # The exact native receipt can refine the locally inserted timestamp,
        # while role, body, attachments, target and chronological order stay fixed.
        self.add_message(mid, waiting['parts'][0]['body'], role='seller')
        self.caught_up()
        self.tick()
        final = self.batch(batch)
        self.assertEqual(final['status'], 'accepted', final)
        self.assertEqual(final['accepted_self_sync']['message_ids'], [mid])
        self.assertEqual(calls, [0, 1])

    def test_new_buyer_or_other_manual_id_or_business_change_stops_waiting_tail(self):
        for change in ('buyer', 'manual', 'supplement', 'policy'):
            with self.subTest(change=change):
                if change != 'buyer':
                    self.doCleanups()
                    self.setUp()
                calls = self.install_pending_rejection()
                batch = self.generate()
                self.tick()
                self.tick()
                if change == 'buyer':
                    self.add_message('new-source', 'Please stop and wait for my update.')
                elif change == 'manual':
                    self.add_message('different-platform-id', self.batch(batch)['parts'][0]['body'], role='seller')
                elif change == 'supplement':
                    cid = self.owner['conversation_id']
                    self.service.repo.put('conversation_supplement', cid, {'conversation_id': cid,
                        'body': 'New business condition', 'revision': 1, 'updated_at': now_ms()})
                else:
                    policy = self.service.repo.list('policy')[0]
                    policy.update(revision=policy['revision'] + 1, content='Changed approved terms')
                    self.service.repo.put('policy', policy['policy_id'], policy)
                self.caught_up()
                self.tick()
                self.assertEqual(self.batch(batch)['status'], 'stopped')
                self.assertEqual(len(calls), 2)

    def test_deadline_expires_without_creating_another_attempt(self):
        self.install_pending_rejection()
        batch = self.generate()
        self.tick()
        self.tick()
        waiting = self.batch(batch)
        waiting['parts'][1]['sync_wait_deadline_at'] = now_ms() - 1
        self.service.repo.put('reply_batch', batch['batch_id'], waiting)
        self.tick()
        final = self.batch(batch)
        self.assertEqual(final['status'], 'stopped')
        self.assertIn('60秒', final['message'])
        self.assertEqual(len(final['parts'][1]['operation_ids']), 1)

    def test_legacy_auth_or_token_failure_never_becomes_recoverable(self):
        for code in ('auth', 'communication_token_missing'):
            with self.subTest(code=code):
                if code != 'auth':
                    self.doCleanups()
                    self.setUp()
                calls = self.install_pending_rejection(code=code)
                batch = self.generate()
                self.tick()
                self.tick()
                self.assertEqual(self.batch(batch)['status'], 'stopped')
                self.caught_up()
                self.tick()
                self.assertEqual(len(calls), 2)

    def test_unknown_with_sync_diagnostics_keeps_original_receipt_fence(self):
        original = self.service.integrations.send
        calls = []
        def send(intent, authorization):
            calls.append(intent['part_index'])
            if intent['part_index'] == 0:
                return original(intent, authorization)
            self.pending()
            return {'status': 'unknown', 'submission_started': True, 'message_id': 'exact-pending-id',
                'error_code': 'source_sync_pending', 'stage': 'send',
                'diagnostics': {'gate_reason': 'source_sync_pending', 'gate_checkpoint': 'before_submit'}}
        self.service.integrations.send = send
        batch = self.generate(3)
        self.tick()
        self.tick()
        self.caught_up()
        self.tick()
        current = self.batch(batch)
        self.assertEqual(current['status'], 'waiting_receipt')
        self.assertEqual([p['status'] for p in current['parts']], ['accepted', 'unknown', 'planned'])
        self.assertEqual(calls, [0, 1])
        self.assertTrue(self.service._conversation_unresolved(self.owner['conversation_id']))

    def test_wait_does_not_block_other_customer_manual_chat(self):
        self.install_pending_rejection()
        batch = self.generate()
        self.tick()
        self.tick()
        other = next(c['conversation_id'] for c in self.service.repo.list('conversation')
                     if c['conversation_id'] != self.owner['conversation_id'])
        draft = self.service.save_chat_draft(other, 'A separate manual message.', None, 'other-edit')
        self.service.call('send_chat', {'conversation_id': other, 'draft_id': draft['draft_id'],
            'expected_revision': draft['revision'], 'client_request_id': 'other-send'})
        self.idle()
        self.assertEqual(self.service.repo.get('chat_draft', other)['status'], 'accepted')
        self.assertEqual(self.batch(batch)['status'], 'waiting_sync')

    def test_takeover_cancels_unsent_wait_but_preserves_receipts(self):
        self.install_pending_rejection()
        batch = self.generate()
        self.tick()
        self.tick()
        self.service.begin_manual_edit(self.owner['conversation_id'])
        self.assertEqual(self.batch(batch)['status'], 'stopped')
        self.assertFalse(self.service._conversation_unresolved(self.owner['conversation_id']))
        self.assertEqual(self.batch(batch)['parts'][0]['status'], 'accepted')

    def test_final_and_supplement_batches_resume_after_accepted_pending(self):
        for phase in ('final', 'supplement'):
            with self.subTest(phase=phase):
                if phase != 'final':
                    self.doCleanups()
                    self.setUp()
                s = self.service
                self.add_message('original-ack', 'We will check the details.', role='seller')
                ack_intent = {'seller_id': s.seller_id, 'buyer_id': self.owner['buyer_id'],
                    'conversation_id': self.owner['conversation_id'], 'reply': 'We will check the details.',
                    'purpose': 'continuity', 'continuity_phase': 'ack', 'source_message_id': 'buyer-1'}
                s.integrations.ledger.claim('synthetic-native-ack', 'synthetic-ack-key', 'send', ack_intent)
                s.integrations.ledger.finish('synthetic-native-ack', {'status': 'accepted',
                    'message_id': 'original-ack', 'submission_started': True, 'timestamp_ms': now_ms()})
                if phase == 'final':
                    from reception_service.continuity import text_hash
                    source = s._latest_buyer(self.owner['conversation_id'])
                    s.repo.put('operation', 'prior-ack', {'operation_id': 'prior-ack', 'purpose': 'continuity',
                        'kind': 'send', 'status': 'accepted', 'message_id': 'original-ack',
                        'source_message_id': source['message_id'], 'seller_id': s.seller_id,
                        'buyer_id': self.owner['buyer_id'], 'conversation_id': self.owner['conversation_id'],
                        'case_id': self.case_id, 'updated_at': now_ms(), 'intent': {
                            'continuity_phase': 'ack', 'source_text_sha256': text_hash(source['text']),
                            'reply': 'We will check the details.'}})
                else:
                    s.save_conversation_supplement(self.owner['conversation_id'], 'Freight can be checked.', None, 'sync-note')
                    work = s.repo.get('supplement_followup', self.owner['conversation_id'])
                    work.update(status='pending', due_at=0)
                    s.repo.put('supplement_followup', self.owner['conversation_id'], work)
                s.integrations.generate = lambda context, request_id, **kw: {'action': 'ask', 'reply_messages': [
                    {'body': body, 'reply_sources': ['buyer-1']} for body in ('Noted.', 'What is your postcode?')]}
                case = s.repo.get('case', self.case_id)
                s._enqueue('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision'],
                    **({'supplement_followup': True} if phase == 'supplement' else {})}, automatic=True)
                self.idle()
                plans = s.repo.list('continuity_plan', conversation_id=self.owner['conversation_id'])
                if not s.repo.list('reply_batch', conversation_id=self.owner['conversation_id']):
                    self.assertTrue(plans, s.list_jobs())
                    s._queue_continuity_plan({'plan_id': plans[-1]['plan_id']})
                    self.idle()
                batch = s.repo.list('reply_batch', conversation_id=self.owner['conversation_id'])[0]
                self.assertEqual(batch['phase'], phase)
                original = s.integrations.send_chat
                calls = []
                def send(intent, authorization):
                    calls.append(intent['part_index'])
                    result = original(intent, authorization)
                    if intent['part_index'] == 0:
                        self.pending()
                    return result
                s.integrations.send_chat = send
                self.tick()
                self.assertEqual(self.batch(batch)['status'], 'waiting_sync', s.list_jobs())
                self.caught_up()
                self.tick()
                self.assertEqual(self.batch(batch)['status'], 'accepted', s.list_jobs())
                self.assertEqual(calls, [0, 1])
                if phase == 'supplement':
                    self.assertEqual(s.repo.get('supplement_followup', self.owner['conversation_id'])['status'], 'complete')

    def test_restart_stops_waiting_tail_and_keeps_rejected_operation(self):
        from pathlib import Path
        from reception_service.service import ReceptionService
        self.install_pending_rejection()
        batch = self.generate()
        self.tick()
        self.tick()
        rejected = self.batch(batch)['parts'][1]['operation_ids'][0]
        self.service.close()
        self.service = ReceptionService(Path(self.temporary.name))
        self.assertEqual(self.batch(batch)['status'], 'stopped')
        self.assertEqual(self.service.repo.get('operation', rejected)['status'], 'rejected')
        self.assertFalse(self.service._auto_send)

    def test_three_sync_rejections_keep_part_budget(self):
        original = self.service.integrations.send
        calls = []
        def send(intent, authorization):
            calls.append(intent['part_index'])
            if intent['part_index'] == 0:
                return original(intent, authorization)
            self.pending()
            denied = evaluate_authorization(authorization, 'before_submit')
            self.assertEqual(denied.reason, 'source_sync_pending')
            return {'status': 'rejected', 'submission_started': False, 'error_code': denied.reason,
                'stage': 'send', 'diagnostics': denied.diagnostics('before_submit')}
        self.service.integrations.send = send
        batch = self.generate()
        self.tick()
        for index in range(3):
            self.tick()
            current = self.batch(batch)
            self.assertEqual(len(current['parts'][1]['operation_ids']), index + 1)
            if index < 2:
                self.assertEqual(current['status'], 'waiting_sync')
                self.caught_up()
        self.assertEqual(self.batch(batch)['status'], 'stopped')
        self.assertEqual(calls, [0, 1, 1, 1])
        part = self.batch(batch)['parts'][1]
        self.assertEqual(decision(self.service.repo, self.service.seller_id, self.owner['conversation_id'],
            batch['source_message_id'], part_id=part['part_id']), ('exhausted', 3, None))


if __name__ == '__main__':
    unittest.main()
