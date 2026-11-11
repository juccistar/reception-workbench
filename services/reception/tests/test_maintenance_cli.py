"""Maintenance dispatch never constructs the business service or echoes raw errors."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from reception_service.__main__ import main
from reception_service.profile_recovery import ProfileRecoveryError
from reception_service.storage_guard import StorageError


class MaintenanceCliTests(unittest.TestCase):
    def invoke(self, args):
        status = 0
        with patch.object(sys, 'argv', ['service', *args]), patch('sys.stdout') as output, patch(
                'reception_service.__main__.ReceptionService', side_effect=AssertionError('business service constructed')):
            try:
                main()
            except SystemExit as exc:
                status = exc.code
            text = ''.join(call.args[0] for call in output.write.call_args_list)
        return status, json.loads(text)

    def test_recover_profile_dispatches_before_service_with_program_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = {'status': 'completed', 'action': 'recover-profile', 'data_root': str(root / 'data'),
                      'backup': str(root / 'backup'), 'source': str(root / 'old'), 'owner_written': False}
            args = ['--maintenance', 'recover-profile', '--source', str(root / 'registry'), '--seller-id', '12345',
                    '--destination', str(root / 'data'), '--backup-dir', str(root / 'backups')]
            with patch('reception_service.profile_recovery.recover_created_profile', return_value=result) as recover:
                status, emitted = self.invoke(args)
            self.assertEqual((status, emitted), (0, result))
            self.assertEqual(recover.call_args.args, (str(root / 'registry'), '12345', str(root / 'data'), str(root / 'backups')))
            self.assertEqual(recover.call_args.kwargs['program_directory'], Path(__file__).resolve().parents[3])

    def test_recover_profile_rejects_incomplete_or_unrelated_parameters(self):
        with patch('reception_service.profile_recovery.recover_created_profile') as recover:
            status, result = self.invoke(['--maintenance', 'recover-profile', '--seller-id', '12345'])
        self.assertEqual(status, 2)
        self.assertEqual(result['code'], 'invalid_arguments')
        recover.assert_not_called()

    def test_storage_failure_emits_known_code_and_fixed_message(self):
        with patch('reception_service.storage_maintenance.run_maintenance', side_effect=StorageError(
                'PRIVATE_NATIVE_EXCEPTION', code='storage_path_too_long')):
            status, result = self.invoke(['--maintenance', 'prepare', '--source', str(Path(tempfile.gettempdir()) / 'synthetic')])
        self.assertEqual(status, 1)
        self.assertEqual(result['code'], 'storage_path_too_long')
        self.assertIn('路径过长', result['message'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_profile_refusal_does_not_echo_exception_text(self):
        error = ProfileRecoveryError('PRIVATE_NATIVE_EXCEPTION')
        with tempfile.TemporaryDirectory() as temporary, patch(
                'reception_service.profile_recovery.recover_created_profile', side_effect=error):
            root = Path(temporary)
            status, result = self.invoke(['--maintenance', 'recover-profile', '--source', str(root / 'registry'),
                '--seller-id', '12345', '--destination', str(root / 'data'), '--backup-dir', str(root / 'backups')])
        self.assertEqual(status, 1)
        self.assertEqual(result['code'], 'recovery_failed')
        self.assertNotIn('PRIVATE', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
