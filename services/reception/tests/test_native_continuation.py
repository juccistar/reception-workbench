"""Continuity uses the real native gateway and ledger with synthetic messages."""
from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reception_service.integrations.accio_native.gateway import NativeGateway

SELLER, BUYER = '2500002602126', '4500116156724'
CID = SELLER + '-' + BUYER + '#11011'

def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()

class NativeContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='synthetic-continuity-')
        self.addCleanup(self.tmp.cleanup)
        self.gateway = NativeGateway({}, Path(self.tmp.name))
        self.gateway._identity = lambda seller: (seller, 'identity')
        self.rows = [dict(messageId='buyer-1', senderAliId=BUYER, content='Question', timestamp=100)]
        self.calls = []
        self.after_token = None
        self.visible = True
        def body():
            return dict(conversationId=CID, sellerAliId=SELLER, contactAliId=BUYER, hasMore=False,
                        messages=deepcopy(self.rows if self.visible else self.rows[:1]))
        self.gateway._read = lambda name, args: body()
        self.gateway._history = lambda *args: self.fail('Continuations must not reread history before submission')
        def call(name, arguments, *, before_submit=None):
            self.calls.append(name)
            if name == 'get_communication_chatToken':
                if self.after_token:
                    self.after_token()
                return {'success': True, 'data': {'chatToken': 'synthetic-token'}}
            before_submit()
            mid = 'own-' + str(len(self.rows))
            self.rows.append(dict(messageId=mid, senderAliId=SELLER,
                                  content=arguments['sendRequest']['content'], timestamp=100 + len(self.rows)))
            return {'success': True, 'data': {'messageID': mid}}
        self.gateway._chat_call = call

    def intent(self, phase='ack', operation=None):
        latest = self.rows[-1]
        return dict(purpose='continuity', send_mode='automatic', continuity_phase=phase,
                    seller_id=SELLER, buyer_id=BUYER, conversation_id=CID, case_id='case',
                    draft_id='draft', revision=1, operation_id=operation or 'op-' + phase,
                    client_request_id='request-' + phase, reply='reply ' + phase,
                    source_message_id='buyer-1', source_timestamp_ms=100, source_text_sha256=sha('Question'),
                    latest_message_id=latest['messageId'], latest_message_role='buyer' if latest['senderAliId'] == BUYER else 'seller',
                    latest_text_sha256=sha(latest['content']), frozen_scope={'mode': 'all'}, budget_reserved=True)

    def send(self, intent, callback=lambda: True):
        return self.gateway.send_chat(intent, callback)

    def test_ack_final_and_idempotency(self):
        ack = self.intent()
        first = self.send(ack)
        self.assertEqual(first['status'], 'accepted')
        self.assertEqual(self.send(ack), first)
        final = self.intent('final')
        self.assertEqual(self.send(final)['status'], 'accepted')
        self.assertEqual(self.send(final)['status'], 'accepted')
        self.assertEqual(self.calls.count('send_msg'), 2)

    def test_unproved_seller_and_wrong_phase_are_rejected(self):
        self.rows.append(dict(messageId='external', senderAliId=SELLER, content='else', timestamp=101))
        for phase in ('final', 'assistance', 'supplement', 'ack'):
            self.assertEqual(self.send(self.intent(phase))['status'], 'rejected')
        self.assertEqual(self.calls, [])

    def test_revoke_and_local_callback_changes_block_submit(self):
        for change in ('revoke', 'callback'):
            with self.subTest(change=change):
                allowed = [True]
                def mutate():
                    if change == 'revoke': self.gateway.revoke()
                    else: allowed[0] = False
                self.after_token = mutate
                result = self.send(self.intent(operation='op-' + change), lambda: allowed[0])
                self.assertEqual(result['status'], 'rejected')
                self.assertFalse(result['submission_started'])
                self.rows = self.rows[:1]
                self.after_token = None
        self.assertEqual(len(self.rows), 1)  # No invocation passed the submit fence.

    def test_unknown_fences_and_reconciles_without_resend(self):
        self.visible = False
        intent = self.intent()
        self.assertEqual(self.send(intent)['status'], 'unknown')
        self.assertEqual(self.send(self.intent('assistance'))['status'], 'rejected')
        self.visible = True
        self.assertEqual(self.gateway.reconcile_chat(intent['operation_id'])['status'], 'accepted')
        self.assertEqual(self.calls.count('send_msg'), 1)

    def test_retry_after_definite_rejection_and_phase_fence(self):
        self.after_token = self.gateway.revoke
        original = self.intent()
        self.assertEqual(self.send(original)['status'], 'rejected')
        self.after_token = None
        retry = self.intent(operation='retry')
        self.assertEqual(self.send(retry)['status'], 'accepted')
        other = self.intent(operation='third')
        self.assertEqual(self.send(other)['status'], 'rejected')

    def test_final_requires_same_source_ack_and_assistance_accepts_own_manual(self):
        manual = dict(purpose='chat', send_mode='manual', seller_id=SELLER, buyer_id=BUYER,
                      conversation_id=CID, reply='human', operation_id='manual', client_request_id='manual')
        self.assertEqual(self.send(manual)['status'], 'accepted')
        self.assertEqual(self.send(self.intent('final'))['status'], 'rejected')
        self.assertEqual(self.send(self.intent('assistance'))['status'], 'accepted')

    def test_changed_intent_and_latest_buyer_rejected(self):
        ack = self.intent()
        self.assertEqual(self.send(ack)['status'], 'accepted')
        changed = dict(ack, reply='different')
        self.assertEqual(self.send(changed)['status'], 'rejected')
        self.rows.append(dict(messageId='buyer-2', senderAliId=BUYER, content='later', timestamp=200))
        self.assertEqual(self.send(self.intent('final'))['status'], 'rejected')
        self.assertEqual(self.calls.count('send_msg'), 1)

    def test_source_time_hash_and_scope_contracts(self):
        for key, value in [('source_timestamp_ms', 0), ('source_text_sha256', sha('wrong')),
                           ('frozen_scope', {'mode': 'selected', 'conversation_ids': []})]:
            intent = self.intent(operation='bad-' + key)
            intent[key] = value
            self.assertEqual(self.send(intent)['status'], 'rejected')
        self.assertFalse(self.calls)

    def own_manual_receipt(self):
        manual = dict(purpose='chat', send_mode='manual', seller_id=SELLER, buyer_id=BUYER,
                      conversation_id=CID, reply='We will check.', operation_id='manual', client_request_id='manual')
        self.assertEqual(self.send(manual)['status'], 'accepted')

    def test_supplement_uses_accepted_seller_receipt_and_is_idempotent(self):
        self.own_manual_receipt()
        intent = self.intent('supplement')
        self.assertEqual(self.send(intent)['status'], 'accepted')
        self.assertEqual(self.send(intent)['status'], 'accepted')
        self.assertEqual(self.calls.count('send_msg'), 2)
        # A new operation or edited body cannot replay the same stable issue request.
        changed = self.intent('supplement', operation='edited-note')
        changed['reply'] = 'An edited answer for the same issue.'
        self.assertEqual(self.send(changed)['status'], 'rejected')
        self.assertEqual(self.send(dict(intent, reply='changed frozen body'))['status'], 'rejected')
        self.assertEqual(self.calls.count('send_msg'), 2)

    def test_distinct_supplement_issue_requests_each_use_latest_exact_receipt(self):
        self.own_manual_receipt()
        for issue in ('price', 'freight'):
            intent = self.intent('supplement', operation='op-' + issue)
            intent.update(client_request_id='stable-issue-' + issue, reply='Answer for ' + issue)
            self.assertEqual(self.send(intent)['status'], 'accepted')
        self.assertEqual(self.calls.count('send_msg'), 3)

    def test_supplement_rejects_wrong_seller_body_or_receipt_time(self):
        self.own_manual_receipt()
        for key, value in [('latest_text_sha256', sha('not the accepted body')),
                           ('source_timestamp_ms', 999)]:
            intent = self.intent('supplement', operation='wrong-' + key)
            intent[key] = value
            self.assertEqual(self.send(intent)['status'], 'rejected')
        self.assertEqual(self.calls.count('send_msg'), 1)

    def test_supplement_unknown_blocks_other_issue_and_reconciles_original(self):
        self.own_manual_receipt()
        self.visible = False
        intent = self.intent('supplement')
        self.assertEqual(self.send(intent)['status'], 'unknown')
        other = self.intent('supplement', operation='other-issue')
        other['client_request_id'] = 'stable-other-issue'
        self.assertEqual(self.send(other)['status'], 'rejected')
        self.visible = True
        self.assertEqual(self.gateway.reconcile_chat(intent['operation_id'])['status'], 'accepted')
        self.assertEqual(self.calls.count('send_msg'), 2)

    def test_supplement_definite_rejection_can_retry_same_stable_issue(self):
        self.own_manual_receipt()
        self.after_token = self.gateway.revoke
        first = self.send(self.intent('supplement'))
        self.assertEqual(first['status'], 'rejected')
        self.assertIs(first['submission_started'], False)
        self.after_token = None
        self.assertEqual(self.send(self.intent('supplement', operation='retry-supplement'))['status'], 'accepted')
        self.assertEqual(self.calls.count('send_msg'), 2)

    def test_remote_only_activity_does_not_add_a_pre_submit_history_read(self):
        intent = self.intent()
        self.after_token = lambda: self.rows.append(dict(messageId='remote-new', senderAliId=BUYER,
                                                        content='not yet synchronized', timestamp=102))
        self.assertEqual(self.send(intent)['status'], 'accepted')
        self.assertEqual(self.calls, ['get_communication_chatToken', 'send_msg'])

if __name__ == '__main__':
    unittest.main()
