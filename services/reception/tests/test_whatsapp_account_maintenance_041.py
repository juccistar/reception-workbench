import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from reception_service.storage_guard import StorageError
from reception_service.whatsapp_account_maintenance import backup_account, restore_account, verify_legacy_sources
from reception_service.whatsapp_account_service import AccountRepository
from reception_service.workspace_backup import _long_path


class AccountMaintenanceTests(unittest.TestCase):
    def test_backup_restore_and_tamper(self):
        with tempfile.TemporaryDirectory(dir=_long_path(tempfile.gettempdir())) as temp:
            base = Path(temp)
            root = base / 'root'
            repo = AccountRepository(root, 'workspace')
            op = {'operation_id': 'op', 'account_id': 'acct', 'status': 'unknown'}
            repo.put('operation', 'op', op)
            with self.assertRaises((OSError, StorageError)):
                backup_account(root, base / 'busy')
            repo.close()
            ledger = root / 'send-ledger'
            ledger.mkdir()
            (ledger / (hashlib.sha256(b'op').hexdigest() + '.jsonl')).write_text('intent\n', encoding='utf-8')
            archive = base / 'backup'
            backup_account(root, archive)
            result = restore_account(archive, base / 'restored')
            self.assertFalse(result['reception_enabled'])
            restored = AccountRepository(base / 'restored', 'workspace')
            self.assertEqual(restored.get('operation', 'op'), op)
            restored.close()
            (archive / 'send-ledger' / (hashlib.sha256(b'op').hexdigest() + '.jsonl')).write_text('changed', encoding='utf-8')
            with self.assertRaises(StorageError):
                restore_account(archive, base / 'invalid')

    def test_unclean_and_extra_profile_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=_long_path(tempfile.gettempdir())) as temp:
            base = Path(temp)
            repo = AccountRepository(base / 'root', 'workspace')
            repo.close()
            (base / 'root' / '.reception-clean-shutdown').unlink()
            with self.assertRaises(StorageError):
                backup_account(base / 'root', base / 'backup')
            repo = AccountRepository(base / 'root', 'workspace')
            repo.close()
            (base / 'root' / 'profile').mkdir()
            (base / 'root' / 'profile' / 'cookie').write_text('private')
            with self.assertRaises(StorageError):
                backup_account(base / 'root', base / 'backup')

    def test_legacy_exact_intent_and_missing_ledger(self):
        with tempfile.TemporaryDirectory(dir=_long_path(tempfile.gettempdir())) as temp:
            base = Path(temp)
            seller = 'seller'
            seller_hash = hashlib.sha256(seller.encode()).hexdigest()
            fixture = base / 'fixture'
            dbpath = fixture / 'live' / 'sellers' / seller_hash / 'workspace.sqlite3'
            dbpath.parent.mkdir(parents=True)
            op = {'operation_id': 'id1', 'seller_id': seller, 'account_id': 'acct', 'connection_epoch': 'epoch', 'chat_id': 'chat', 'body': 'body', 'status': 'unknown', 'submission_started': True}
            db = sqlite3.connect(dbpath)
            db.execute('CREATE TABLE entities(kind TEXT,id TEXT,data TEXT)')
            db.executemany('INSERT INTO entities VALUES (?,?,?)', [('meta', 'seller', json.dumps({'seller_id': seller})), ('wa_operation', 'id1', json.dumps(op))])
            db.commit(); db.close()
            (fixture / 'manifest.json').write_text(json.dumps({'files': []}))
            ledger = fixture / 'live' / 'whatsapp-ledgers' / seller_hash / 'send-ledger'
            ledger.mkdir(parents=True)
            ledger_file = ledger / (hashlib.sha256(b'id1').hexdigest() + '.jsonl')
            ledger_file.write_text(json.dumps({k: op[k] for k in ('operation_id','account_id','connection_epoch','chat_id','body')}) + '\n')
            source = {'source_id': 'source', 'merchant_id': 'merchant', 'seller_id': seller, 'data_root': str(base / 'merchant'), 'state_directory': str(base / 'state'), 'registry_directory': str(base / 'registry')}
            def fake_backup(root, destination):
                import shutil
                shutil.copytree(fixture, destination)
            with patch('reception_service.whatsapp_account_maintenance.backup_workspace', fake_backup), patch('reception_service.whatsapp_account_maintenance._validate_archive', return_value={'files': []}):
                packet = verify_legacy_sources('acct', [source], base / 'archives')
                self.assertEqual(packet['sources'][0]['status'], 'verified')
                self.assertEqual(packet['sources'][0]['pending_operations'], [op])
                ledger_file.unlink()
                blocked = verify_legacy_sources('acct', [source], base / 'other')
                self.assertEqual(blocked['sources'][0]['status'], 'blocked')
            self.assertEqual(verify_legacy_sources('acct', [], base / 'empty')['sources'], [])


if __name__ == '__main__':
    unittest.main()
