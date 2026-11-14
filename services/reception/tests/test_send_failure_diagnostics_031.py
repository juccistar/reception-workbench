import unittest

from reception_service.errors import ServiceError, safe_diagnostics
from reception_service.integrations.live import LiveIntegration
from reception_service.integrations.accio_native.vendor.diagnostics import process_diagnostics


class SendFailureDiagnostics031Tests(unittest.TestCase):
    def test_local_exception_class_survives_receipt_without_private_fields(self):
        receipt = LiveIntegration._send_result({
            'status': 'rejected', 'submission_started': False,
            'error_code': 'adapter_error', 'stage': 'adapter',
            'diagnostics': {'exception_class': 'JSONDecodeError',
                            'reply': 'PRIVATE_SYNTHETIC_REPLY',
                            'readyToken': 'PRIVATE_SYNTHETIC_TOKEN'},
        })
        self.assertEqual(receipt['status'], 'rejected')
        self.assertIs(receipt['submission_started'], False)
        self.assertEqual(receipt['diagnostics']['exception_class'], 'JSONDecodeError')
        self.assertEqual(receipt['error_code'], 'adapter_error')
        self.assertNotIn('PRIVATE_SYNTHETIC', str(receipt))
        error = ServiceError('adapter_error', '发送前检查失败', stage='adapter', diagnostics=receipt['diagnostics'])
        self.assertEqual(error.to_dict()['diagnostics']['exception_class'], 'JSONDecodeError')
        self.assertNotIn('PRIVATE_SYNTHETIC', str(error.to_dict()))

    def test_gate_facts_survive_live_filter_without_private_data(self):
        receipt = LiveIntegration._send_result({
            'status': 'rejected', 'submission_started': False,
            'error_code': 'source_sync_pending', 'stage': 'send',
            'diagnostics': {'gate_reason': 'source_sync_pending', 'gate_checkpoint': 'after_token',
                            'sync_target_version': 7, 'sync_target_timestamp': 42,
                            'authorization_epoch': 3, 'token_acquired': True,
                            'reply': 'PRIVATE', 'token': 'PRIVATE'},
        })
        self.assertEqual(receipt['diagnostics']['gate_checkpoint'], 'after_token')
        self.assertEqual(receipt['diagnostics']['sync_target_version'], 7)
        self.assertIs(receipt['diagnostics']['token_acquired'], True)
        self.assertNotIn('PRIVATE', str(receipt))
        self.assertIn('同步', receipt['message'])
        self.assertEqual(process_diagnostics({'gate_reason': 'PRIVATE', 'gate_checkpoint': 'PRIVATE',
                                              'sync_target_version': True}), {})

    def test_incomplete_rejection_never_becomes_retryable_pre_submission(self):
        for flag in ('missing', None, 0, '', True, 'false'):
            with self.subTest(flag=flag):
                raw = {'status': 'rejected', 'error_code': 'timeout', 'stage': 'cli'}
                if flag != 'missing':
                    raw['submission_started'] = flag
                receipt = LiveIntegration._send_result(raw)
                self.assertEqual(receipt['status'], 'unknown')
                self.assertIs(receipt['submission_started'], True)
        explicit = LiveIntegration._send_result({'status': 'rejected', 'submission_started': False,
                                                'error_code': 'timeout', 'stage': 'cli'})
        self.assertEqual(explicit['status'], 'rejected')
        self.assertIs(explicit['submission_started'], False)

    def test_arbitrary_class_or_error_text_is_not_diagnostic_metadata(self):
        for invalid in ('PRIVATE_SYNTHETIC_EXCEPTION_TEXT', {'text': 'private'}, ['JSONDecodeError']):
            with self.subTest(value=type(invalid).__name__):
                self.assertEqual(process_diagnostics({'exception_class': invalid, 'message': 'private'}), {})
                self.assertEqual(safe_diagnostics({'exception_class': invalid, 'message': 'private'}), {})


if __name__ == '__main__':
    unittest.main()
