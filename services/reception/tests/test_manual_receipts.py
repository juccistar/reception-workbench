"""Receipt and final registration authorization regressions; synthetic mocks only."""
from __future__ import annotations

import copy
import threading
from unittest.mock import patch
import unittest

import test_manual_reception as manual_fixture
from reception_service.errors import ServiceError
from reception_service.repository import new_id, now_ms


class ReceiptFixture(unittest.TestCase):
    # Reuse fixture helpers without inheriting and rerunning its test methods.
    setUp = manual_fixture.ManualReceptionTests.setUp
    tearDown = manual_fixture.ManualReceptionTests.tearDown
    wait_job = manual_fixture.ManualReceptionTests.wait_job
    ingest = manual_fixture.ManualReceptionTests.ingest
    manual = manual_fixture.ManualReceptionTests.manual
    start_manual = manual_fixture.ManualReceptionTests.start_manual
    start_assisted = manual_fixture.ManualReceptionTests.start_assisted
    prepare = manual_fixture.ManualReceptionTests.prepare
    confirm = manual_fixture.ManualReceptionTests.confirm

    def unknown_send(self):
        self.start_manual()
        draft = self.manual()
        self.facade.send_result = {'status': 'unknown', 'message_id': None, 'submission_started': True}
        result = self.confirm(self.prepare(draft))
        self.assertEqual(result['status'], 'unknown', result)
        return draft, result['result']['operation_id']

    def reconcile(self, operation_id, result):
        with patch.object(self.facade, 'reconcile_operation', return_value=result, create=True) as reconcile:
            receipt = self.service.reconcile_operation(operation_id)
        reconcile.assert_called_once_with(operation_id)
        return receipt

    def queue_registration(self, revision=None):
        case = self.service.repo.get('case', self.case_id)
        if revision is not None:
            case['revision'] = revision
        self.service._queue_outbox(case)
        return self.service.repo.list('outbox', case_id=self.case_id)[-1]


class ManualReceiptTests(ReceiptFixture):
    def test_send_queue_contains_safe_receipt_metadata_and_preserves_registration_rows(self):
        draft, operation_id = self.unknown_send()
        operation = self.service.repo.get('operation', operation_id)
        self.assertEqual(operation['case_revision'], draft['source_case_revision'])
        self.assertEqual(operation['draft_revision'], draft['revision'])
        operation.update(raw_output='synthetic-private-output', intent={'reply': 'synthetic-private-body'})
        self.service.repo.put('operation', operation_id, operation)
        registration = self.queue_registration()
        items = self.service.list_outbox()
        send = next(item for item in items if item['operation_id'] == operation_id)
        self.assertEqual(send['kind'], 'send')
        self.assertEqual(send['status'], 'unknown')
        self.assertEqual(send['case_revision'], draft['source_case_revision'])
        self.assertEqual(send['draft_revision'], draft['revision'])
        self.assertNotIn('intent', send)
        self.assertNotIn('raw_output', send)
        self.assertNotIn('synthetic-private', str(send))
        self.assertIn(registration, items)
        self.assertEqual(self.service.get_snapshot()['outbox'], items)

    def test_accepted_reconciliation_updates_matching_draft_without_fabricating_history(self):
        draft, operation_id = self.unknown_send()
        before_messages = self.service.get_messages(self.cid)
        before_case = self.service.repo.get('case', self.case_id)
        receipt = self.reconcile(operation_id, {'status': 'accepted', 'message_id': 'synthetic-platform-id',
                                                'submission_started': True, 'raw': 'synthetic-private'})
        self.assertEqual(receipt['status'], 'accepted')
        self.assertEqual(receipt['message_id'], 'synthetic-platform-id')
        self.assertNotIn('raw', receipt)
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'accepted')
        self.assertEqual(self.service.get_messages(self.cid), before_messages)
        self.assertEqual(self.service.repo.get('case', self.case_id), before_case)
        edited = self.manual(draft['body'])
        with self.assertRaises(ServiceError) as caught:
            self.prepare(edited)
        self.assertEqual(caught.exception.code, 'duplicate_send')
        self.ingest(mid='new-source-after-receipt', stamp=now_ms() + 10)
        rebound = self.manual(draft['body'])
        self.assertEqual(rebound['status'], 'pending')
        self.prepare(rebound)
        self.assertEqual(self.facade.send_calls, 1)
        # A terminal accepted receipt remains terminal even if later lookup has no evidence.
        self.assertEqual(self.service.reconcile_operation(operation_id)['status'], 'accepted')

    def test_missing_or_invalid_acceptance_evidence_stays_unknown(self):
        draft, operation_id = self.unknown_send()
        invalid = ({'status': 'accepted', 'submission_started': True},
                   {'status': 'accepted', 'message_id': 'synthetic-id', 'submission_started': False},
                   {'status': 'accepted', 'message_id': 'synthetic-id', 'submission_started': 1},
                   {'status': 'accepted', 'message_id': 42, 'submission_started': True},
                   {'status': 'accepted', 'message_id': '  ', 'submission_started': True},
                   {'status': 'unknown'}, {'status': 'rejected'},
                   {'status': 'rejected', 'submission_started': True},
                   {'status': 'rejected', 'submission_started': 0},
                   {'status': 'succeeded', 'message_id': 'synthetic-id'}, None)
        for result in invalid:
            with self.subTest(result=result):
                receipt = self.reconcile(operation_id, result)
                self.assertEqual(receipt['status'], 'unknown')
                self.assertTrue(receipt['submission_started'])
                self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'unknown')
                with self.assertRaises(ServiceError):
                    self.manual(draft['body'])
        with patch.object(self.facade, 'reconcile_operation', side_effect=RuntimeError('synthetic'), create=True):
            self.assertEqual(self.service.reconcile_operation(operation_id)['status'], 'unknown')
        self.assertEqual(self.facade.send_calls, 1)

    def test_rejected_reconciliation_allows_explicit_new_pending_draft(self):
        draft, operation_id = self.unknown_send()
        receipt = self.reconcile(operation_id, {'status': 'rejected', 'submission_started': False})
        self.assertEqual(receipt['status'], 'rejected')
        self.assertIs(receipt['submission_started'], False)
        self.assertIs(self.service.repo.get('operation', operation_id)['submission_started'], False)
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'rejected')
        rebound = self.manual(draft['body'])
        self.assertNotEqual(rebound['draft_id'], draft['draft_id'])
        self.assertEqual(rebound['status'], 'pending')
        self.prepare(rebound)
        self.assertEqual(self.facade.send_calls, 1)

    def test_reconciliation_cannot_overwrite_new_draft_revision_or_active_case(self):
        draft, operation_id = self.unknown_send()
        changed = copy.deepcopy(draft)
        changed.update(revision=draft['revision'] + 1, body='Synthetic newer draft', status='pending')
        self.service.repo.put('draft', draft['draft_id'], changed)
        conversation = self.service.repo.get('conversation', self.cid)
        new_case = self.service._new_case(conversation, 'synthetic-new-purchase')
        conversation['case_id'] = new_case['case_id']
        self.service.repo.put('conversation', self.cid, conversation)
        new_draft = self.service.save_draft(new_case['case_id'], 'Thank you. I will review your request.', None, new_id('mutation'))
        receipt = self.reconcile(operation_id, {'status': 'accepted', 'message_id': 'synthetic-platform-id',
                                                'submission_started': True})
        self.assertEqual(receipt['status'], 'accepted')
        self.assertEqual(self.service.repo.get('draft', draft['draft_id']), changed)
        self.assertEqual(self.service.repo.get('draft', new_draft['draft_id']), new_draft)
        self.assertEqual(self.service.repo.get('case', new_case['case_id']), new_case)
        self.assertEqual(self.service.get_conversation(self.cid)['case']['case_id'], new_case['case_id'])

    def test_reconciliation_rejects_foreign_operation_owner_before_facade_lookup(self):
        _, operation_id = self.unknown_send()
        operation = self.service.repo.get('operation', operation_id)
        for field, value in (('seller_id', 'foreign-seller'), ('buyer_id', 'foreign-buyer'),
                             ('conversation_id', 'foreign-conversation'), ('source_message_id', 'foreign-source')):
            with self.subTest(field=field):
                self.service.repo.put('operation', operation_id, {**operation, field: value})
                with patch.object(self.facade, 'reconcile_operation', create=True) as reconcile:
                    with self.assertRaises(ServiceError):
                        self.service.reconcile_operation(operation_id)
                    reconcile.assert_not_called()
        self.service.repo.put('operation', operation_id, operation)
        self.service.account_verified = False
        with self.assertRaises(ServiceError):
            self.service.reconcile_operation(operation_id)

    def test_registration_reconciliation_resolves_unknown_without_rolling_back_newer_registration(self):
        older = self.queue_registration(1)
        newer = self.queue_registration(5)
        for item in (older, newer):
            item['status'] = 'unknown'
            self.service.repo.put('outbox', item['operation_id'], item)
        resolved = self.reconcile(newer['operation_id'], {'status': 'accepted', 'record_id': 'synthetic-record-5'})
        self.assertEqual(resolved['status'], 'succeeded')
        saved = self.service.repo.get('registration', self.case_id)
        self.assertEqual(saved['case_revision'], 5)
        resolved = self.reconcile(older['operation_id'], {'status': 'confirmed', 'record_id': 'synthetic-record-1'})
        self.assertEqual(resolved['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('registration', self.case_id), saved)
        self.assertNotIn('case_snapshot', resolved)
        self.assertEqual(self.facade.registration_calls, [])

    def test_registration_reconciliation_requires_record_evidence_and_handles_rejection(self):
        item = self.queue_registration()
        item['status'] = 'unknown'
        self.service.repo.put('outbox', item['operation_id'], item)
        for status in ('accepted', 'unknown', 'blocked', 'disabled', 'failed'):
            with self.subTest(status=status):
                self.assertEqual(self.reconcile(item['operation_id'], {'status': status})['status'], 'unknown')
        self.assertIsNone(self.service.repo.get('registration', self.case_id))
        self.assertEqual(self.reconcile(item['operation_id'], {'status': 'rejected'})['status'], 'unknown')
        self.assertEqual(self.reconcile(item['operation_id'], {'status': 'rejected', 'submission_started': True})['status'], 'unknown')
        self.assertEqual(self.reconcile(item['operation_id'], {'status': 'rejected', 'submission_started': False})['status'], 'failed')
        self.start_assisted()
        self.service.settings['feishu'].update(enabled=True, write_enabled=True)
        self.facade.registration_result = {'status': 'succeeded', 'record_id': 'synthetic-after-rejection'}
        self.assertEqual(self.wait_job(self.service.call('run_outbox'))['status'], 'succeeded')
        self.assertEqual(self.facade.registration_calls, [item['operation_id']])


class RegistrationAuthorizationTests(ReceiptFixture):
    def run_blocked_registration(self, change, expected_item_status='failed'):
        self.start_assisted()
        self.service.settings['feishu'].update(enabled=True, write_enabled=True)
        item = self.queue_registration()
        observed = []
        submitted = []
        entered, release = threading.Event(), threading.Event()

        def register(case, previous, operation_id, *, authorization=None):
            self.assertTrue(callable(authorization))
            self.assertNotIn('authorization', case)
            entered.set()
            self.assertTrue(release.wait(3))
            allowed = authorization()
            observed.append(allowed)
            if not allowed:
                return {'status': 'rejected', 'submission_started': False}
            submitted.append(operation_id)
            return {'status': 'succeeded', 'record_id': 'synthetic-record'}

        with patch.object(self.facade, 'register', side_effect=register):
            ack = self.service.call('run_outbox')
            self.assertTrue(entered.wait(1))
            try:
                change(item)
            finally:
                release.set()
            job = self.wait_job(ack)
        self.assertEqual(job['status'], 'failed', job)
        self.assertEqual(observed, [False])
        self.assertEqual(submitted, [])
        self.assertEqual(self.service.repo.get('outbox', item['operation_id'])['status'], expected_item_status)
        self.assertNotIn('authorization', str(self.service.repo.list('outbox')))

    def test_pause_revokes_registration_at_final_boundary(self):
        self.run_blocked_registration(lambda _: self.service.set_reception(False))

    def test_manual_switch_revokes_registration_at_final_boundary(self):
        self.run_blocked_registration(lambda _: self.start_manual())

    def test_current_write_setting_is_rechecked_at_final_boundary(self):
        self.run_blocked_registration(lambda _: self.service.settings['feishu'].update(write_enabled=False))

    def test_disabled_integration_is_rechecked_at_final_boundary(self):
        self.run_blocked_registration(lambda _: self.service.settings['feishu'].update(enabled=False))

    def test_closing_is_rechecked_at_final_boundary(self):
        self.run_blocked_registration(lambda _: setattr(self.service, '_closing', True))

    def test_stop_is_rechecked_at_final_boundary(self):
        self.run_blocked_registration(lambda _: self.service._stop.set())

    def test_current_item_state_is_rechecked_at_final_boundary(self):
        def change(item):
            item = {**item, 'status': 'superseded'}
            self.service.repo.put('outbox', item['operation_id'], item)
        self.run_blocked_registration(change, expected_item_status='superseded')

    def test_current_item_owner_is_rechecked_at_final_boundary(self):
        def change(item):
            case = self.service.repo.get('case', item['case_id'])
            case['buyer_id'] = 'synthetic-other-buyer'
            self.service.repo.put('case', case['case_id'], case)
        self.run_blocked_registration(change)

    def test_higher_successful_registration_is_rechecked_at_final_boundary(self):
        def change(item):
            self.service.repo.put('registration', item['case_id'], {
                'record_id': 'synthetic-newer-record', 'case_revision': item['case_revision'] + 1})
        self.run_blocked_registration(change)
        self.assertEqual(self.service.repo.get('registration', self.case_id)['record_id'], 'synthetic-newer-record')

    def test_higher_successful_outbox_item_is_rechecked_at_final_boundary(self):
        def change(item):
            higher = self.queue_registration(item['case_revision'] + 1)
            higher['status'] = 'succeeded'
            self.service.repo.put('outbox', higher['operation_id'], higher)
        self.run_blocked_registration(change)

    def test_callback_is_reusable_for_multiple_write_boundaries(self):
        self.start_assisted()
        self.service.settings['feishu'].update(enabled=True, write_enabled=True)
        item = self.queue_registration()
        observed = []
        def register(case, previous, operation_id, *, authorization=None):
            observed.append(authorization())
            self.start_manual()
            observed.append(authorization())
            return {'status': 'unknown'}  # A completed first write may need receipt reconciliation.
        with patch.object(self.facade, 'register', side_effect=register):
            job = self.wait_job(self.service.call('run_outbox'))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(observed, [True, False])
        self.assertEqual(self.service.repo.get('outbox', item['operation_id'])['status'], 'unknown')

    def test_actual_success_is_saved_after_permission_is_revoked(self):
        self.start_assisted()
        self.service.settings['feishu'].update(enabled=True, write_enabled=True)
        item = self.queue_registration()
        def register(case, previous, operation_id, *, authorization=None):
            self.assertTrue(authorization())
            self.start_manual()  # The synthetic platform submission already happened.
            return {'status': 'accepted', 'record_id': 'synthetic-submitted-record'}
        with patch.object(self.facade, 'register', side_effect=register):
            job = self.wait_job(self.service.call('run_outbox'))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertTrue(self.service.get_snapshot()['reception']['manual_only'])
        self.assertEqual(self.service.repo.get('outbox', item['operation_id'])['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('registration', self.case_id)['record_id'], 'synthetic-submitted-record')


if __name__ == '__main__':
    unittest.main()
