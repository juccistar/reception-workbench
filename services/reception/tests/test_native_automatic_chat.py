"""Automatic replies use direct chat with no pre-submit platform history or Ready."""
from datetime import datetime, timedelta, timezone
import json
import unittest

import test_native_chat as fixtures
from reception_service.integrations.accio_native.gateway import NativeGateway
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError, digest
from reception_service.integrations.live import LiveIntegration

SELLER, BUYER, CID = fixtures.SELLER, fixtures.BUYER, fixtures.CID


class NativeAutomaticChatTests(unittest.TestCase):
    def setUp(self):
        fixtures.NativeChatTests.setUp(self)
        self.reads = []
        self.native._wc = lambda *a, **k: self.fail('Automatic replies must not run Ready, Seal or workflow send')

    def read(self, name, arguments):
        self.assertTrue(any(call == 'send_msg' for call, _ in self.calls), 'Platform messages were read before submit')
        self.reads.append(name)
        return fixtures.NativeChatTests.read(self, name, arguments)

    def intent(self, operation='automatic-one', **changes):
        stamp = int(datetime(2026, 11, 12, 5, 35, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
        value = dict(seller_id=SELLER, buyer_id=BUYER, conversation_id=CID, reply='Thank you for your inquiry.',
                     source_message_id='new-buyer-message', source_timestamp_ms=stamp, draft_id='draft-one',
                     case_id='case-one', case_version=1, revision=1, epoch=1, operation_id=operation,
                     send_mode='automatic', frozen_scope={'mode': 'selected', 'seller_id': SELLER,
                     'conversation_ids': [CID]}, budget_reserved=True, budget_sequence=1)
        value.update(changes)
        return value

    def send(self, intent=None, authorization=lambda: True):
        return self.native.send(intent or self.intent(), authorization)

    def seed_legacy(self, intent, result):
        key = 'send:' + digest([SELLER, CID, intent['source_message_id']])
        self.native.ledger.claim(intent['operation_id'], key, 'send', intent, conversation_fence=True)
        self.native.ledger.finish(intent['operation_id'], result)

    def test_early_morning_new_customer_submits_without_snapshot_or_history(self):
        result = self.send()
        self.assertEqual(result['status'], 'accepted')
        self.assertTrue(result['submission_started'])
        self.assertEqual([name for name, _ in self.calls], ['get_communication_chatToken', 'send_msg'])
        self.assertEqual(self.reads, ['query_conversation_msg'])
        request, receipt = self.native.ledger.get('automatic-one')[1:]
        self.assertEqual(request['send_mode'], 'automatic')
        self.assertEqual(request['purpose'], 'reception')
        self.assertEqual(receipt['message_id'], result['message_id'])
        self.assertNotIn('synthetic-memory-only-token', json.dumps((request, receipt)))

    def test_same_operation_and_same_source_are_not_submitted_twice(self):
        first = self.send()
        self.assertEqual(self.send(), first)
        self.assertEqual(self.send(self.intent('different-operation', case_id='different-case'))['status'], 'rejected')
        self.assertEqual(sum(name == 'send_msg' for name, _ in self.calls), 1)

    def test_unknown_fences_other_sources_and_reconciles_after_restart(self):
        self.visible = False
        intent = self.intent()
        self.assertEqual(self.send(intent)['status'], 'unknown')
        self.assertEqual(self.send(self.intent('second', source_message_id='another-source'))['status'], 'rejected')
        self.visible = True
        fresh = NativeGateway({}, self.root)
        fresh._identity = self.native._identity
        fresh._history = self.native._history
        fresh._read = self.read
        facade = object.__new__(LiveIntegration)
        facade.native = fresh
        self.assertEqual(facade.reconcile_operation(intent['operation_id'])['status'], 'accepted')
        self.assertEqual(sum(name == 'send_msg' for name, _ in self.calls), 1)

    def test_legacy_unknown_operation_returns_original_without_new_transport(self):
        intent = self.intent()
        previous = dict(status='unknown', message_id='', submission_started=True)
        self.seed_legacy(intent, previous)
        self.assertEqual(self.send(intent), previous)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.send(self.intent('second'))['status'], 'rejected')
        self.assertEqual(self.calls, [])

    def test_legacy_accepted_source_still_fences_new_operation(self):
        intent = self.intent()
        self.seed_legacy(intent, dict(status='accepted', message_id='old-platform-id', submission_started=True))
        self.assertEqual(self.send(self.intent('second'))['status'], 'rejected')
        self.assertEqual(self.calls, [])

    def test_definite_rejection_can_retry_without_removing_old_receipt(self):
        allowed = [True]
        self.after_token = lambda: allowed.__setitem__(0, False)
        self.assertEqual(self.send(authorization=lambda: allowed[0])['status'], 'rejected')
        self.after_token = None
        self.assertEqual(self.send(self.intent('second'))['status'], 'accepted')
        self.assertEqual(self.native.ledger.get('automatic-one')[2]['status'], 'rejected')
        self.assertEqual(sum(name == 'send_msg' for name, _ in self.calls), 1)

    def test_scope_and_reservation_are_required_before_token_or_send(self):
        for changes in ({'frozen_scope': None}, {'budget_reserved': False},
                        {'frozen_scope': {'mode': 'selected', 'conversation_ids': []}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.send(self.intent(**changes))['status'], 'rejected')
        self.assertEqual(self.calls, [])

    def test_global_revoke_and_service_local_changes_fence_submit(self):
        for change in ('pause', 'takeover', 'source', 'draft', 'policy', 'supplement', 'photo'):
            with self.subTest(change=change):
                allowed = [True]
                self.after_token = (self.native.revoke if change == 'pause'
                                    else lambda: allowed.__setitem__(0, False))
                result = self.send(self.intent('changed-' + change), lambda: allowed[0])
                self.assertEqual(result['status'], 'rejected')
                self.assertFalse(result['submission_started'])
        self.assertFalse(any(name == 'send_msg' for name, _ in self.calls))

    def test_timeout_after_submit_is_unknown_and_journal_precedes_platform_call(self):
        original = self.native._chat_call
        def call(name, arguments, *, before_submit=None):
            if name != 'send_msg':
                return original(name, arguments, before_submit=before_submit)
            before_submit()
            stored = self.native.ledger.get('automatic-one')[2]
            self.assertEqual(stored['status'], 'unknown')
            self.assertIs(stored['submission_started'], True)
            raise AdapterError('private platform error', code='timeout', stage='cli')
        self.native._chat_call = call
        result = self.send()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['error_code'], 'timeout')
        self.assertTrue(result['submission_started'])
        self.assertNotIn('private platform error', json.dumps(result))
        self.assertEqual(self.send(), result)

    def test_transient_final_receipt_write_failure_keeps_returned_message_id(self):
        original = self.native.ledger.finish
        failed = [False]
        def finish(operation, result):
            if result.get('status') == 'accepted' and not failed[0]:
                failed[0] = True
                raise OSError('synthetic final receipt write failure')
            return original(operation, result)
        self.native.ledger.finish = finish
        result = self.send()
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['message_id'])
        self.assertEqual(self.native.ledger.get('automatic-one')[2]['message_id'], result['message_id'])
        self.native.ledger.finish = original
        self.assertEqual(self.native.reconcile_chat('automatic-one')['status'], 'accepted')
        self.assertEqual(sum(name == 'send_msg' for name, _ in self.calls), 1)

    def test_token_timeout_preserves_definite_transient_error_for_retry_policy(self):
        def call(*args, **kwargs):
            raise AdapterError('private token error', code='timeout', stage='cli')
        self.native._chat_call = call
        result = self.send()
        self.assertEqual(result['status'], 'rejected')
        self.assertFalse(result['submission_started'])
        self.assertEqual(result['error_code'], 'timeout')
        self.assertNotIn('private token error', json.dumps(result))

    def test_target_source_and_body_constraints_are_kept(self):
        for changes in ({'buyer_id': '1234'}, {'source_message_id': ''}, {'source_timestamp_ms': 0},
                        {'revision': 0}, {'reply': ''}, {'reply': 'x' * 1201}):
            with self.subTest(changes=changes):
                self.assertEqual(self.send(self.intent(**changes))['status'], 'rejected')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
