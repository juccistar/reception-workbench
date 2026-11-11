"""Fresh live accounts have a complete empty ledger before any platform activity."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reception_service.service import ReceptionService
from reception_service.storage_maintenance import run_maintenance
from reception_service.storage_guard import StorageError
from reception_service.launch_ownership import inspect_launch_ownership
from reception_service.integrations.accio_native.vendor.adapters.common import Ledger, AdapterError


class LiveInitializationTests(unittest.TestCase):
    def test_fresh_live_without_any_native_activity_can_prepare_backup_and_reopen(self):
        with TemporaryDirectory(prefix='new-live-complete-') as temporary:
            root = Path(temporary) / 'workspace'
            with patch('subprocess.run', side_effect=AssertionError('No native call expected')):
                service = ReceptionService(root, 'live', '12345')
                self.assertFalse(service.get_snapshot()['app']['account_verified'])
                service.close()
            ledger = root / 'live/adapters-live.sqlite3'
            self.assertTrue(ledger.is_file())
            connection = sqlite3.connect(ledger)
            try:
                self.assertEqual(connection.execute('SELECT count(*) FROM operations').fetchone(), (0,))
            finally:
                connection.close()
            self.assertEqual(inspect_launch_ownership(root), {'status': 'empty'})
            self.assertEqual(run_maintenance('prepare', str(root))['status'], 'completed')
            backup = Path(temporary) / 'backup'
            self.assertEqual(run_maintenance('backup', str(root), str(backup))['status'], 'completed')
            reopened = ReceptionService(root, 'live', '12345')
            try:
                self.assertFalse(reopened.get_snapshot()['reception']['auto_send'])
            finally:
                reopened.close()
            receipt = root / 'live/workctl-state/chat-analysis/send-receipts/unknown.json'
            receipt.write_text('PRIVATE_UNKNOWN_RECEIPT', encoding='utf-8')
            self.assertEqual(inspect_launch_ownership(root), {'status': 'unknown'})
            self.assertEqual(receipt.read_text(encoding='utf-8'), 'PRIVATE_UNKNOWN_RECEIPT')

    def test_missing_ledger_in_existing_workspace_is_not_silently_recreated(self):
        with TemporaryDirectory(prefix='existing-live-missing-ledger-') as temporary:
            root = Path(temporary) / 'workspace'
            service = ReceptionService(root, 'live')
            service.close()
            ledger = root / 'live/adapters-live.sqlite3'
            established = Ledger(ledger)
            ledger.unlink()
            reopened = ReceptionService(root, 'live')
            with self.assertRaises(StorageError):
                reopened.close()
            self.assertFalse((root / '.reception-clean-shutdown').exists())
            self.assertFalse(ledger.exists())
            self.assertEqual(inspect_launch_ownership(root), {'status': 'unknown'})
            for store in (established, Ledger(ledger)):
                with self.assertRaises(AdapterError):
                    store.get('previous-operation')
            self.assertFalse(ledger.exists())
            with self.assertRaises(StorageError):
                run_maintenance('prepare', str(root))
            self.assertFalse(ledger.exists())


if __name__ == '__main__':
    unittest.main()
