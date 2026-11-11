"""Human chat uses the real gateway/ledger with synthetic native responses only."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reception_service.integrations.accio_native.gateway import NativeGateway
from reception_service.integrations.accio_native.chat import chat_text, call_chat_tool
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError

SELLER, BUYER = '2500002602126', '4500116156724'
CID = SELLER + '-' + BUYER + '#11011'


class NativeChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='synthetic-native-chat-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.native = NativeGateway({}, self.root)
        self.native._identity = lambda expected: (expected, 'synthetic-sha')
        self.native._history = lambda *args: self.fail('Human chat must not depend on buyer history')
        self.calls = []
        self.visible = True
        self.after_token = None
        self.fail_send = False
        self.body_override = None
        self.role = SELLER
        def call(name, arguments, *, before_submit=None):
            self.calls.append((name, deepcopy(arguments)))
            if name == 'get_communication_chatToken':
                if self.after_token:
                    self.after_token()
                return {'success': True, 'data': {'chatToken': 'synthetic-memory-only-token'}}
            if before_submit:
                before_submit()
            if self.fail_send:
                raise AdapterError('synthetic timeout')
            return {'success': True, 'data': {'messageID': 'synthetic-mid-' + str(len(self.calls)),
                                             'businessID': CID + '1793982896156'}}
        self.native._chat_call = call
        self.native._read = self.read

    def intent(self, request='request-one', body='Price is $25. Delivery is tomorrow.'):
        return dict(purpose='chat', send_mode='manual', seller_id=SELLER, buyer_id=BUYER,
                    conversation_id=CID, reply=body, client_request_id=request,
                    operation_id='operation-' + request, draft_id='draft-' + request, revision=1)

    def read(self, name, args):
        self.assertEqual(name, 'query_conversation_msg')
        self.assertEqual(args['conversationId'], CID)
        sent = [(name, value) for name, value in self.calls if name == 'send_msg']
        if not self.visible or not sent:
            rows = []
        else:
            mid = 'synthetic-mid-' + str(next(i + 1 for i, row in reversed(list(enumerate(self.calls))) if row[0] == 'send_msg'))
            rows = [dict(messageId=mid, conversationId=CID, senderAliId=self.role,
                         content=self.body_override if self.body_override is not None else sent[-1][1]['sendRequest']['content'],
                         timestamp=1793982896176)]
        return dict(conversationId=CID, sellerAliId=SELLER, contactAliId=BUYER, hasMore=False, messages=rows)

    def send(self, intent=None, authorization=lambda: True):
        return self.native.send_chat(intent or self.intent(), authorization)

    def test_human_commercial_text_needs_no_ai_or_buyer_source(self):
        result = self.send()
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['timestamp_ms'], 1793982896176)
        args = self.calls[-1][1]['sendRequest']
        self.assertEqual(args['receiverAliID'], [int(BUYER), int(SELLER)])
        self.assertTrue(args['safeFilter'])
        self.assertTrue(args['needCheckToken'])
        self.assertFalse(args['autoReply'])
        stored = self.native.ledger.get('operation-request-one')
        self.assertNotIn('synthetic-memory-only-token', json.dumps(stored))

    def test_same_request_does_not_submit_again(self):
        first = self.send()
        self.assertEqual(self.send(), first)
        self.assertEqual(len(self.calls), 2)

    def test_different_request_can_send_same_body_after_seller(self):
        self.assertEqual(self.send()['status'], 'accepted')
        self.assertEqual(self.send(self.intent('request-two'))['status'], 'accepted')
        self.assertEqual(len([row for row in self.calls if row[0] == 'send_msg']), 2)

    def test_reusing_operation_with_different_body_is_rejected(self):
        self.send()
        self.assertEqual(self.send(self.intent(body='different'))['status'], 'rejected')
        self.assertEqual(len(self.calls), 2)

    def test_unknown_fences_conversation_across_request_and_case(self):
        self.visible = False
        result = self.send()
        self.assertEqual(result['status'], 'unknown')
        other = self.intent('request-two')
        other['case_id'] = 'another-case'
        self.assertEqual(self.send(other)['status'], 'rejected')
        self.assertEqual(len(self.calls), 2)

    def test_readback_later_resolves_original_without_resubmit(self):
        self.visible = False
        unknown = self.send()
        self.assertTrue(unknown['message_id'])
        self.visible = True
        self.assertEqual(self.native.reconcile_chat('operation-request-one')['status'], 'accepted')
        self.assertEqual(len(self.calls), 2)

    def test_timeout_is_unknown_without_body_guess(self):
        self.fail_send = True
        self.assertEqual(self.send()['status'], 'unknown')
        self.assertEqual(self.native.reconcile_chat('operation-request-one')['status'], 'unknown')
        self.assertEqual(len(self.calls), 2)

    def test_wrong_readback_body_or_direction_remains_unknown(self):
        self.body_override = 'other body'
        self.assertEqual(self.send()['status'], 'unknown')
        self.body_override = None
        self.role = BUYER
        self.assertEqual(self.native.reconcile_chat('operation-request-one')['status'], 'unknown')

    def test_authorization_revoked_before_submission_is_definite(self):
        allowed = [True]
        self.after_token = lambda: allowed.__setitem__(0, False)
        result = self.send(authorization=lambda: allowed[0])
        self.assertEqual(result['status'], 'rejected')
        self.assertFalse(result['submission_started'])
        self.assertEqual(len(self.calls), 1)

    def test_global_automatic_revoke_does_not_cancel_human_intent(self):
        self.after_token = self.native.revoke
        self.assertEqual(self.send()['status'], 'accepted')

    def test_bad_target_is_rejected_before_tool_call(self):
        intent = self.intent()
        intent['buyer_id'] = '1234'
        self.assertEqual(self.send(intent)['status'], 'rejected')
        self.assertEqual(self.calls, [])

    def test_ordinary_body_capacity_is_independent_of_ai_1200_limit(self):
        self.assertEqual(chat_text('hello ' * 300), 'hello ' * 300)
        for body in ('', 'x\x00y', 'x\ud800', 'x' * 50001):
            with self.assertRaises(AdapterError):
                chat_text(body)

    def test_mcp_bridge_keeps_payload_off_os_argv_and_disk(self):
        node, script = self.root / 'node.exe', self.root / 'mcp.mjs'
        node.touch(); script.touch()
        self.native.require_runtime = lambda: None
        captured = []
        class SyntheticProcess:
            def run(_, args, **kwargs):
                captured.append((args, kwargs))
                return {'success': True, 'data': {}}
        self.native.process = SyntheticProcess()
        with patch.dict(os.environ, {'ACCIO_NODE_BIN': str(node), 'ACCIO_MCP_SCRIPT': str(script)}):
            call_chat_tool(self.native, 'send_msg', {'chatToken': 'memory-only-sentinel', 'content': 'body'})
        args, kwargs = captured[0]
        self.assertNotIn('memory-only-sentinel', ' '.join(args))
        self.assertIn('memory-only-sentinel', kwargs['input'])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['mcp.mjs', 'node.exe'])

    def test_real_node_bridge_delivers_stdin_to_cli_in_memory(self):
        import shutil
        from reception_service.integrations.accio_native.vendor.adapters.common import Process
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is unavailable')
        script = self.root / 'synthetic-mcp.mjs'
        script.write_text("const p=JSON.parse(process.argv[7]); process.stdout.write(JSON.stringify({success:true,"
                          "data:{name:process.argv[5],payload_matches:p.chatToken==='synthetic-bridge-sentinel'}}));",
                          encoding='utf-8')
        self.native.require_runtime = lambda: None
        self.native.process = Process(self.root)
        with patch.dict(os.environ, {'ACCIO_NODE_BIN': node, 'ACCIO_MCP_SCRIPT': str(script)}):
            result = call_chat_tool(self.native, 'send_msg', {'chatToken': 'synthetic-bridge-sentinel'})
        self.assertEqual(result, {'success': True, 'data': {'name': 'send_msg', 'payload_matches': True}})
        self.assertNotIn('synthetic-bridge-sentinel', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
