import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.integrations.live import LiveIntegration
from reception_service.service import ReceptionService


class RecoveryTests(unittest.TestCase):
    def test_assistance_account_gate_and_cancellation(self):
        facade = LiveIntegration.__new__(LiveIntegration)
        facade.native = SimpleNamespace(cli='cli', data_dir=Path('.'), process=None)
        facade.settings = {'ai': {'agent_id': 'synthetic'}}
        facade.resolve_identity = lambda _: None
        cancel = threading.Event()
        calls = []

        class Runner:
            def __init__(self, *args, **kwargs):
                self.last_diagnostics = {'result_source': 'completed_record'}

            def run(self, args, **kwargs):
                calls.append(kwargs)
                if not kwargs['recover_own_results']:
                    raise ServiceError('timeout', 'synthetic')
                if 'items' in kwargs['input'] and 'source' in kwargs['input']:
                    answer = {'items': [{'key': 'key', 'text': 'translated'}]}
                else:
                    answer = {'reply': 'Hello'}
                return {'answer': json.dumps(answer)}

        def account(value):
            return {'cli': 'account-a', 'same': 'account-a', 'other': 'account-b'}.get(value)

        with patch('reception_service.integrations.live.DesktopQueryRunner', Runner), \
             patch('reception_service.integrations.live.account_path', side_effect=account), \
             patch.dict('os.environ', {'ACCIO_MCP_SCRIPT': 'same', 'PHOENIX_PLUGIN_CLI_WORKCTL_PATH': ''}):
            result = facade.translate([{'key': 'key', 'text': 'source'}], 'id', cancel_event=cancel)
            self.assertEqual(result['items'][0]['text'], 'translated')
            context = {'seller_id': 'seller', 'buyer_id': 'buyer', 'conversation_id': 'seller#buyer'}
            with patch('reception_service.integrations.live.target'):
                self.assertEqual(facade.generate_manual(context, 'id', cancel_event=cancel)['reply'], 'Hello')
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(call['recover_own_results'] and call['cancel_event'] is cancel for call in calls))
            with patch.dict('os.environ', {'PHOENIX_PLUGIN_CLI_WORKCTL_PATH': 'other'}):
                with self.assertRaises(ServiceError):
                    facade.translate([{'key': 'key', 'text': 'source'}], 'id', cancel_event=cancel)
            self.assertFalse(calls[-1]['recover_own_results'])
            cancel.set()
            with self.assertRaises(ServiceError) as caught:
                facade.translate([{'key': 'key', 'text': 'source'}], 'id', cancel_event=cancel)
            self.assertEqual(caught.exception.code, 'cancelled')
            self.assertEqual(len(calls), 3)

    def test_late_cancellation_does_not_hide_a_structured_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReceptionService(Path(directory))
            try:
                cid = service.repo.list('conversation')[0]['conversation_id']
                message = next(m for m in service._messages(cid) if m.get('text'))
                item = {'kind': 'message', 'source_id': message['message_id']}
                for index, code in enumerate(('timeout', 'answer_non_json', 'cancelled')):
                    class Facade:
                        def translate(self, items, request_id, *, on_call_start, cancel_event):
                            cancel_event.set()
                            raise ServiceError(code, 'Synthetic failure', stage='answer')
                    service._translation_facade = Facade()
                    job_id = service.request_translations(cid, [item], 'late-cancel-' + str(index))['job_id']
                    for _ in range(300):
                        job = service.repo.get('job', job_id)
                        if job['status'] not in ('queued', 'running'):
                            break
                        time.sleep(.01)
                    self.assertEqual(job['status'], 'cancelled' if code == 'cancelled' else 'failed')
                    self.assertEqual(job['error_code'], code)
            finally:
                service.close()

    def test_translation_failure_metadata_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReceptionService(Path(directory))
            try:
                cid = service.repo.list('conversation')[0]['conversation_id']
                message = next(m for m in service._messages(cid) if m.get('text'))
                item = {'kind': 'message', 'source_id': message['message_id']}
                for index, code in enumerate(('timeout', 'answer_non_json')):
                    class Facade:
                        def translate(self, items, request_id, *, on_call_start, cancel_event):
                            self.last_ai_diagnostics = {'result_source': 'completed_record', 'cli_timed_out': True,
                                                        'stdout': 'PRIVATE', 'recovery_status': 'completed'}
                            raise ServiceError(code, 'PRIVATE', stage='answer',
                                               diagnostics={'elapsed_ms': 12, 'code': 'PRIVATE', 'stderr': 'PRIVATE'})
                    service._translation_facade = Facade()
                    job_id = service.request_translations(cid, [item], 'request-' + str(index))['job_id']
                    for _ in range(300):
                        job = service.repo.get('job', job_id)
                        if job['status'] not in ('queued', 'running'):
                            break
                        time.sleep(.01)
                    self.assertEqual(job['status'], 'failed')
                    self.assertEqual(job['error_code'], code)
                    self.assertEqual(job['diagnostics']['stage'], 'answer')
                    self.assertEqual(job['diagnostics']['elapsed_ms'], 12)
                    self.assertEqual(job['diagnostics']['result_source'], 'completed_record')
                    self.assertNotIn('PRIVATE', json.dumps(job))
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
