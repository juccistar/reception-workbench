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
from reception_service.translation_provider import TranslationProviderError


class RecoveryTests(unittest.TestCase):
    def test_assistance_account_gate_and_cancellation(self):
        facade = LiveIntegration.__new__(LiveIntegration)
        facade._query_state = threading.local()
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
                answer = {'reply': 'Hello'}
                return {'answer': json.dumps(answer)}

        def account(value):
            return {'cli': 'account-a', 'same': 'account-a', 'other': 'account-b'}.get(value)

        with patch('reception_service.integrations.live.DesktopQueryRunner', Runner), \
             patch('reception_service.integrations.live.account_path', side_effect=account), \
             patch.dict('os.environ', {'ACCIO_MCP_SCRIPT': 'same', 'PHOENIX_PLUGIN_CLI_WORKCTL_PATH': ''}):
            context = {'seller_id': 'seller', 'buyer_id': 'buyer', 'conversation_id': 'seller#buyer'}
            with patch('reception_service.integrations.live.target'):
                self.assertEqual(facade.generate_manual(context, 'id', cancel_event=cancel)['reply'], 'Hello')
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0]['recover_own_results'] and calls[0]['cancel_event'] is cancel)
                with patch.object(facade, '_assistance_query', return_value={'reply': 'Hello'}) as query:
                    facade.generate_manual({**context, 'unsent_reply': {'body': '14.5 USD each, shipping excluded', 'revision': 2}},
                                           'photo-polish', cancel_event=cancel, photo_inputs=[{'message': {}, 'asset': {}}])
                    prompt = query.call_args.args[0]
                    self.assertIn('image_transport=read_compat', prompt)
                    self.assertIn('14.5', str(query.call_args.args[1]))
                    self.assertIn('价格、数量、币种、单位、运费、时间、型号、规格和商务承诺不擅自增删或改变', prompt)
                with patch.dict('os.environ', {'PHOENIX_PLUGIN_CLI_WORKCTL_PATH': 'other'}):
                    with self.assertRaises(ServiceError):
                        facade.generate_manual(context, 'different-identity', cancel_event=cancel)
                self.assertFalse(calls[-1]['recover_own_results'])
                cancel.set()
                with self.assertRaises(ServiceError) as caught:
                    facade.generate_manual(context, 'cancelled', cancel_event=cancel)
                self.assertEqual(caught.exception.code, 'cancelled')
                self.assertEqual(len(calls), 2)

    def test_late_cancellation_does_not_hide_a_structured_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReceptionService(Path(directory))
            try:
                cid = service.repo.list('conversation')[0]['conversation_id']
                message = next(m for m in service._messages(cid) if m.get('text'))
                item = {'kind': 'message', 'source_id': message['message_id']}
                for index, code in enumerate(('timeout', 'answer_non_json', 'cancelled')):
                    class Provider:
                        def translate(self, items, request_id, *, cancel_event):
                            cancel_event.set()
                            raise TranslationProviderError(code, 'Synthetic failure')
                    service._new_translation_provider = Provider
                    job_id = service.request_translations(cid, [item], 'late-cancel-' + str(index))['job_id']
                    for _ in range(300):
                        job = service.repo.get('job', job_id)
                        if job['status'] not in ('queued', 'running'):
                            break
                        time.sleep(.01)
                    self.assertEqual(job['status'], 'cancelled' if code == 'cancelled' else 'failed')
                    self.assertEqual(job['error_code'], code)
                    self.assertEqual(service.request_translations(cid, [item], 'late-cancel-' + str(index))['job_id'], job_id)
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
                    class Provider:
                        def translate(self, items, request_id, *, cancel_event):
                            raise TranslationProviderError(code, 'PRIVATE')
                    service._new_translation_provider = Provider
                    job_id = service.request_translations(cid, [item], 'request-' + str(index))['job_id']
                    for _ in range(300):
                        job = service.repo.get('job', job_id)
                        if job['status'] not in ('queued', 'running'):
                            break
                        time.sleep(.01)
                    self.assertEqual(job['status'], 'failed')
                    self.assertEqual(job['error_code'], code)
                    self.assertNotIn('diagnostics', job)
                    self.assertNotIn('PRIVATE', json.dumps(job))
                    self.assertEqual(service.request_translations(cid, [item], 'request-' + str(index))['job_id'], job_id)
                service._new_translation_provider = lambda: service.integrations
                recovered = service.request_translations(cid, [item], 'recovered-original-source')['job_id']
                for _ in range(300):
                    result = service.repo.get('job', recovered)
                    if result['status'] not in ('queued', 'running', 'deferred'):
                        break
                    time.sleep(.01)
                self.assertEqual(result['status'], 'succeeded')
                self.assertEqual(service.get_translations(cid, [item])['items'][0]['status'], 'ready')
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
