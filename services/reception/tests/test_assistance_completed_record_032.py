"""Exercise real own-record correlation with a silent synthetic CLI boundary."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reception_service.integrations.live import LiveIntegration
from reception_service.workspace_lock import native_children_stopped


class CompletedRecordIntegrationTests(unittest.TestCase):
    def test_translation_and_manual_recover_exact_records_without_second_call(self):
        with tempfile.TemporaryDirectory(prefix='arw032-recovery-') as directory:
            root = Path(directory)
            account = root / 'accounts' / 'synthetic-account'
            folder = account / 'conversations' / 'dm'
            folder.mkdir(parents=True)
            cli, mcp = account / 'workctl.exe', account / 'mcp.js'
            cli.touch()
            mcp.touch()
            facade = LiveIntegration.__new__(LiveIntegration)
            facade._query_state = threading.local()
            facade._visual_workspace_identity = None
            facade.native = SimpleNamespace(cli=str(cli), data_dir=root / 'state', process=None)
            facade.settings = {'ai': {'agent_id': 'synthetic-agent'}}
            facade.resolve_identity = lambda seller: seller
            calls, stopped = [], []
            answer = {'items': [{'key': 'source-1', 'text': '你好'}]}

            class SilentOwnedProcess:
                def __init__(self, args, inheritance, prompt):
                    self.proc = SimpleNamespace(returncode=None)
                    calls.append(list(args))
                    name = args[args.index('--conversation-name') + 1]
                    agent = args[args.index('--agent-id') + 1]
                    cid = 'record-' + str(len(calls))
                    (folder / (cid + '.json')).write_text(json.dumps({'id': cid, 'name': name, 'agentId': agent}), encoding='utf-8')
                    rows = [dict(role='req', status='finished', conversationId=cid, agentId=agent,
                                 requestId='request-1', messageId='message-1', content=prompt),
                            dict(role='res', status='finished', conversationId=cid, agentId=agent,
                                 clientRequestId='request-1', branchParentId='message-1', content=json.dumps(answer))]
                    (folder / (cid + '.message_1.jsonl')).write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')

                def poll(self):
                    return self.proc.returncode

                def finished(self):
                    return False

                def output(self):
                    return '', ''

                def stop(self):
                    self.proc.returncode = 1
                    stopped.append(True)

            with patch.dict('os.environ', {'ACCIO_MCP_SCRIPT': str(mcp), 'PHOENIX_PLUGIN_CLI_WORKCTL_PATH': str(cli)}), \
                 patch('reception_service.integrations.accio_native.vendor.adapters.owned_process.OwnedProcess', SilentOwnedProcess), \
                 patch('reception_service.integrations.accio_native.vendor.adapters.desktop_query._POLL_SECONDS', .01):
                cancel = threading.Event()
                result = facade.translate([{'key': 'source-1', 'text': 'Hello'}], 'synthetic-translation', cancel_event=cancel)
                self.assertEqual(result, answer)
                self.assertEqual(len(calls), 1)
                self.assertEqual(facade.last_ai_diagnostics['result_source'], 'completed_record')
                self.assertEqual(facade.last_ai_diagnostics['recovery_status'], 'completed')
                self.assertTrue(facade.last_ai_diagnostics['recovery_enabled'])
                self.assertFalse(facade.last_ai_diagnostics['stdout_present'])
                answer = {'reply': 'Thank you for waiting.'}
                result = facade.generate_manual({'seller_id': '10001', 'buyer_id': '20002',
                                                  'conversation_id': '10001-20002#icbu', 'case_id': None,
                                                  'recent_messages': [], 'supplement': ''},
                                                 'synthetic-manual', cancel_event=cancel)
                self.assertEqual(result['reply'], answer['reply'])
                self.assertEqual(result['verified_image_reads'], [])
                self.assertEqual(result['reply_sources'], [])
                self.assertEqual(len(calls), 2)
                self.assertNotEqual(calls[0][calls[0].index('--conversation-name') + 1],
                                    calls[1][calls[1].index('--conversation-name') + 1])
                self.assertEqual(facade.last_ai_diagnostics['result_source'], 'completed_record')
            self.assertEqual(len(stopped), 2)
            self.assertTrue(native_children_stopped())


if __name__ == '__main__':
    unittest.main()
