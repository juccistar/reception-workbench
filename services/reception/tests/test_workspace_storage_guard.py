import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reception_service.repository import Repository
from reception_service.storage_guard import StorageError, validate_database, preflight_workspace
from reception_service.workspace_lock import WorkspaceLock
from reception_service.workspace_backup import backup_workspace, restore_workspace
from reception_service.integrations.accio_native.vendor.adapters.common import Ledger

class WorkspaceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'root'
    def tearDown(self):
        self.temp.cleanup()
    def make_root(self):
        for name, seller in [('live/control.sqlite3', ''), ('live/sellers/abc/workspace.sqlite3', '123')]:
            repo = Repository(self.root / name, seller)
            if seller:
                with repo.transaction():
                    repo.put('operation', 'a', {'status': 'accepted'}, history=True)
                    repo.put('operation', 'b', {'status': 'unknown'}, history=True)
                    repo.put('read', '1', {'sequence': 7})
            repo.close()
        ledger = Ledger(self.root / 'live/adapters-live.sqlite3')
        ledger.initialize()
        with closing(ledger._db()) as db, db:
            db.execute("INSERT INTO operations VALUES ('a','key','send','{}','{\"status\":\"accepted\"}')")
            db.execute("INSERT INTO operations VALUES ('b','unknown-key','send','{}','{\"status\":\"unknown\"}')")
            db.execute("INSERT INTO retry_grants VALUES ('a')")
        receipt = self.root / 'live/workctl-state/chat-analysis/send-receipts/r.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"receipt":true}', encoding='utf8')
        os.utime(receipt, ns=(1700000000123456789, 1700000000123456789))
        with WorkspaceLock(self.root) as lock:
            lock.mark_clean_shutdown()
        return receipt
    def test_roundtrip(self):
        receipt = self.make_root()
        backup = backup_workspace(self.root, self.base / 'backup')
        restored = restore_workspace(backup, self.base / 'restored')
        self.assertEqual((restored / receipt.relative_to(self.root)).stat().st_mtime_ns, receipt.stat().st_mtime_ns)
        repo = Repository(restored / 'live/sellers/abc/workspace.sqlite3', '123')
        self.assertEqual(repo.get('operation', 'b')['status'], 'unknown')
        self.assertEqual(repo.history('operation', 'a')[0]['status'], 'accepted')
        self.assertEqual(repo.get('read', '1')['sequence'], 7)
        repo.close()
        ledger = Ledger(restored / 'live/adapters-live.sqlite3')
        self.assertEqual(ledger.get('a')[2]['status'], 'accepted')
        self.assertEqual(ledger.get('b')[2]['status'], 'unknown')
        with closing(ledger._db()) as db:
            self.assertEqual(db.execute("SELECT logical_key FROM operations WHERE operation_id='b'").fetchone()[0], 'unknown-key')
            self.assertEqual(db.execute('SELECT operation_id FROM retry_grants').fetchone()[0], 'a')
    def test_demo_ledger_is_guarded_and_restored(self):
        demo = self.root / 'demo'
        Repository(demo / 'control.sqlite3').close()
        Repository(demo / 'workspace.sqlite3', 'demo-seller').close()
        ledger_path = demo / 'integrations-demo.sqlite3'
        with closing(Ledger(ledger_path)._db()) as db, db:
            db.execute("INSERT INTO operations VALUES ('demo-op','demo-key','send','{}','{\"status\":\"unknown\"}')")
        with WorkspaceLock(self.root) as lock:
            lock.mark_clean_shutdown()
        backup = backup_workspace(self.root, self.base / 'demo-backup')
        restored = restore_workspace(backup, self.base / 'demo-restored')
        self.assertEqual(Ledger(restored / 'demo/integrations-demo.sqlite3').get('demo-op')[2]['status'], 'unknown')
        with closing(sqlite3.connect(ledger_path)) as db, db:
            db.execute('PRAGMA user_version=999')
        before = ledger_path.read_bytes()
        with self.assertRaises(StorageError):
            preflight_workspace(self.root)
        self.assertEqual(ledger_path.read_bytes(), before)

    def test_unknown_version_and_schema_unchanged(self):
        self.make_root()
        dbpath = self.root / 'live/sellers/abc/workspace.sqlite3'
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('PRAGMA user_version=99')
        before = dbpath.read_bytes()
        with self.assertRaises(StorageError): preflight_workspace(self.root)
        with self.assertRaises(StorageError): Repository(dbpath, '123')
        self.assertEqual(before, dbpath.read_bytes())
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('PRAGMA user_version=0')
        before = dbpath.read_bytes()
        with self.assertRaises(StorageError): Repository(dbpath, '123')
        self.assertEqual(before, dbpath.read_bytes())
        ledger = self.root / 'live/adapters-live.sqlite3'
        with closing(sqlite3.connect(ledger)) as db, db:
            db.execute('DROP TABLE retry_grants')
            db.execute('PRAGMA user_version=0')
        before = ledger.read_bytes()
        with self.assertRaises(StorageError): validate_database(ledger, 'ledger')
        self.assertEqual(before, ledger.read_bytes())
    def test_wal_preflight_preserves_source_and_checks_future_version(self):
        self.make_root()
        p = self.root / 'live/sellers/abc/workspace.sqlite3'
        db = sqlite3.connect(p)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute("INSERT INTO entities VALUES ('operation','wal-only','{\"status\":\"unknown\"}')")
            db.commit()
            def snapshot():
                paths = [p, Path(str(p) + '-wal'), Path(str(p) + '-shm')]
                return {str(x): x.read_bytes() for x in paths if x.exists()}
            before = snapshot()
            self.assertTrue(Path(str(p) + '-wal').stat().st_size)
            with WorkspaceLock(self.root):
                preflight_workspace(self.root)
            self.assertEqual(before, snapshot())
            recovered = Repository(p, '123')
            self.assertEqual(recovered.get('operation', 'wal-only')['status'], 'unknown')
            recovered.close()
            # Repository.close now checkpoints; create a new residual frame to test backup refusal.
            db.execute("INSERT INTO entities VALUES ('operation','second','{}')")
            db.commit()
            with WorkspaceLock(self.root) as lock:
                lock.mark_clean_shutdown()
            with self.assertRaises(StorageError): backup_workspace(self.root, self.base / 'wal-backup')
            db.execute('PRAGMA user_version=99')
            db.commit()
            before = snapshot()
            with WorkspaceLock(self.root):
                with self.assertRaises(StorageError): preflight_workspace(self.root)
            self.assertEqual(before, snapshot())
        finally:
            db.close()

    def test_migration_rolls_back_on_seller_mismatch(self):
        self.make_root()
        p = self.root / 'live/sellers/abc/workspace.sqlite3'
        with closing(sqlite3.connect(p)) as db, db: db.execute('PRAGMA user_version=1')
        with self.assertRaises(Exception): Repository(p, '456')
        with patch.object(Repository, 'put', side_effect=RuntimeError('injected failure after version update')):
            with self.assertRaises(RuntimeError): Repository(p, '123')
        with closing(sqlite3.connect(p)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
    def test_reject_active_missing_and_corruption(self):
        receipt = self.make_root()
        with WorkspaceLock(self.root):
            with self.assertRaises((OSError, StorageError)): backup_workspace(self.root, self.base / 'busy')
        with self.assertRaises(StorageError): backup_workspace(self.root, self.base / 'unclean')
        with WorkspaceLock(self.root) as lock: lock.mark_clean_shutdown()
        receipt.unlink()
        (self.root / 'live/adapters-live.sqlite3').unlink()
        with self.assertRaises(StorageError): backup_workspace(self.root, self.base / 'missing')
    def test_missing_referenced_receipt_and_legacy_upgrade(self):
        self.make_root()
        path = self.root / 'live/sellers/abc/workspace.sqlite3'
        repo = Repository(path, '123')
        with repo.transaction():
            repo.put('operation', 'missing', {'native_receipt_ref': 'missing.json'})
        repo.close()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA user_version=1')
        repo = Repository(path, '123')
        self.assertEqual(repo.get('operation', 'a')['status'], 'accepted')
        repo.close()
        self.assertEqual(validate_database(path, 'repository'), 2)
        with self.assertRaises(StorageError): backup_workspace(self.root, self.base / 'missing-receipt')

    def test_restore_rejects_lost_receipt_timestamp_precision(self):
        self.make_root()
        backup = backup_workspace(self.root, self.base / 'backup')
        with patch('reception_service.workspace_backup.os.utime'):
            with self.assertRaises(StorageError):
                restore_workspace(backup, self.base / 'imprecise')
        self.assertFalse((self.base / 'imprecise').exists())

    def test_symlink_rejected(self):
        self.make_root()
        link = self.root / 'live/workctl-state/chat-analysis/send-receipts/link.json'
        try:
            link.symlink_to(self.base / 'outside.json')
        except OSError:
            self.skipTest('Symlink creation unavailable')
        with self.assertRaises(ValueError): backup_workspace(self.root, self.base / 'linked')

    def test_manifest_hash_and_traversal(self):
        self.make_root()
        backup = backup_workspace(self.root, self.base / 'backup')
        manifest_path = backup / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
        manifest['files'][0]['sha256'] = 'bad'
        manifest_path.write_text(json.dumps(manifest), encoding='utf8')
        with self.assertRaises(StorageError): restore_workspace(backup, self.base / 'bad')
        manifest['files'][0]['path'] = '../escape'
        manifest_path.write_text(json.dumps(manifest), encoding='utf8')
        with self.assertRaises(StorageError): restore_workspace(backup, self.base / 'escape')

if __name__ == '__main__': unittest.main()
