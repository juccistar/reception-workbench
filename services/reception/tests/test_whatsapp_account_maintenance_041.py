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

    def test_real_legacy_backup_import_and_restore_fence(self):
        from reception_service.repository import Repository
        from reception_service.workspace_lock import WorkspaceLock
        from reception_service.whatsapp_account_service import WhatsAppAccountService
        from reception_service.integrations.accio_native.vendor.adapters.common import Ledger

        from reception_service.workspace_backup import _normal_path
        with tempfile.TemporaryDirectory(dir=_long_path(tempfile.gettempdir())) as temp:
            base = _normal_path(temp)
            root = base / 'merchant'
            seller = 'synthetic-seller'
            seller_hash = hashlib.sha256(seller.encode()).hexdigest()
            Repository(root / 'live/control.sqlite3').close()
            dbpath = root / 'live/sellers' / seller_hash / 'workspace.sqlite3'
            repo = Repository(dbpath, seller)
            op = {'operation_id': 'old', 'seller_id': seller, 'account_id': 'acct', 'connection_epoch': 'old-epoch', 'chat_id': 'chat', 'body': 'synthetic', 'status': 'unknown', 'submission_started': True}
            repo.put('wa_operation', 'old', op)
            repo.close()
            Ledger(root / 'live/adapters-live.sqlite3').initialize()
            (root / 'live/workctl-state/chat-analysis/send-receipts').mkdir(parents=True)
            ledger = root / 'live/whatsapp-ledgers' / seller_hash / 'send-ledger' / (hashlib.sha256(b'old').hexdigest() + '.jsonl')
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({k: op[k] for k in ('operation_id', 'account_id', 'connection_epoch', 'chat_id', 'body')}) + '\n', encoding='utf-8')
            with WorkspaceLock(root) as lock:
                lock.mark_clean_shutdown()
            before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (dbpath, ledger)]
            source = {'source_id': 's', 'merchant_id': 'm', 'seller_id': seller, 'data_root': str(root), 'state_directory': str(base / 'state'), 'registry_directory': str(base / 'registry')}
            from reception_service.workspace_backup import backup_workspace
            from reception_service.whatsapp_account_maintenance import _legacy_operations
            snapshot = backup_workspace(root, base / 'independent-backup')
            self.assertEqual(_legacy_operations(snapshot, seller, 'acct'), [op])
            account_root = base / 'account'
            service = WhatsAppAccountService(account_root, 'workspace', lambda event: None)
            service._wa_set_account('acct', 'epoch', 'connected')
            packet = verify_legacy_sources('acct', [source], account_root / 'legacy-archives')
            self.assertEqual(packet['sources'][0]['status'], 'verified')
            self.assertEqual(packet['sources'][0]['pending_operations'], [op])
            again = verify_legacy_sources('acct', [source], account_root / 'legacy-archives')
            self.assertEqual(packet['manifest_hash'], again['manifest_hash'])
            self.assertEqual(service._wa_set_legacy_status(**packet)['pending_count'], 1)
            self.assertEqual(service._wa_set_legacy_status(**again)['pending_count'], 1)
            service.close()
            self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (dbpath, ledger)])
            backup_account(account_root, base / 'backup')
            restore_account(base / 'backup', base / 'restored')
            restored = WhatsAppAccountService(base / 'restored', 'workspace', lambda event: None)
            try:
                self.assertFalse(restored.enabled)
                restored._wa_set_account('acct', 'new-epoch', 'connected')
                self.assertEqual(restored.repo.get('operation', 'old')['status'], 'unknown')
                self.assertEqual(restored.get_whatsapp_state()['legacy']['pending_count'], 1)
                self.assertEqual(restored.repo.get('legacy_original', 'old'), op)
                restored._wa_ingest('acct', 'new-epoch', [{'id': 'message', 'chat_id': 'chat', 'body': 'hello', 'timestamp_ms': 1, 'is_history': False, 'is_group': False, 'from_me': False}])
                binding = restored.save_whatsapp_binding('chat', 'merchant', None, 0, 'acct', 'new-epoch')
                self.assertIsNone(binding['merchant_id'])
                self.assertEqual(binding['requested_binding']['merchant_id'], 'merchant')
            finally:
                restored.close()

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
