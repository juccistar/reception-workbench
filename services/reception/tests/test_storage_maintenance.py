from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reception_service.repository import Repository
from reception_service.storage_guard import StorageError
from reception_service.storage_maintenance import run_maintenance
from reception_service.workspace_lock import WorkspaceLock
from reception_service.workspace_backup import backup_workspace, restore_workspace
from reception_service.integrations.accio_native.vendor.adapters.common import Ledger, AdapterError
from reception_service.integrations.accio_native.vendor.adapters.send_receipts import _file


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'root'
        Repository(self.root / 'live/control.sqlite3').close()
        Repository(self.root / 'live/sellers/a/workspace.sqlite3', 'seller').close()
        ledger = Ledger(self.root / 'live/adapters-live.sqlite3')
        ledger.initialize()
        with closing(ledger._db()) as db, db:
            db.execute("INSERT INTO operations VALUES ('a','key','send','{}','{\"status\":\"unknown\"}')")
        self.receipt = self.root / 'live/workctl-state/chat-analysis/send-receipts/a.json'
        self.receipt.parent.mkdir(parents=True)
        self.receipt.write_text('{"test":true}', encoding='utf8')
        with WorkspaceLock(self.root) as lock:
            lock.mark_clean_shutdown()

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrate_and_stale_restore(self):
        old = backup_workspace(self.root, self.base / 'old')
        with closing(sqlite3.connect(self.root / 'live/adapters-live.sqlite3')) as db, db:
            db.execute("INSERT INTO operations VALUES ('b','key2','send','{}','{\"status\":\"accepted\"}')")
        with self.assertRaises(StorageError):
            run_maintenance('restore', str(self.root), str(self.base / 'refused'), str(old), str(self.base))
        self.assertFalse((self.base / 'refused').exists())
        result = run_maintenance('migrate', str(self.root), str(self.base / 'moved'), backup_dir=str(self.base))
        self.assertTrue(Path(result['backup']).is_dir())
        with closing(sqlite3.connect(self.base / 'moved/live/adapters-live.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0], 2)
        self.assertTrue(self.root.is_dir())

    def test_old_receipt_path_after_source_deleted_and_tampered(self):
        reference = str(self.receipt)
        backup = backup_workspace(self.root, self.base / 'backup')
        restored = restore_workspace(backup, self.base / 'restored')
        shutil.rmtree(self.root)
        gateway = SimpleNamespace(data_dir=restored / 'live')
        self.assertEqual(_file(gateway, reference).read_text(), '{"test":true}')
        (restored / self.receipt.relative_to(self.root)).write_text('{}', encoding='utf8')
        with self.assertRaises(AdapterError):
            _file(gateway, reference)

    def test_fresh_custom_roots_require_explicit_allow(self):
        for root in (self.base / 'new', self.base / 'host-only'):
            if root.name == 'host-only':
                (root / 'host').mkdir(parents=True)
            with self.assertRaises(StorageError):
                run_maintenance('prepare', str(root))
            self.assertTrue(run_maintenance('prepare', str(root), allow_new=True)['new'])
        (self.base / 'host-only' / 'other').mkdir()
        with self.assertRaises(StorageError):
            run_maintenance('prepare', str(self.base / 'host-only'), allow_new=True)

    def test_dirty_prepare_preserves_absence_of_clean_marker(self):
        marker = self.root / '.reception-clean-shutdown'
        marker.unlink()
        result = run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        self.assertFalse(result['upgrade_required'])
        self.assertFalse(marker.exists())
        with self.assertRaises(StorageError):
            run_maintenance('migrate', str(self.root), str(self.base / 'moved'), backup_dir=str(self.base))
        self.assertFalse(marker.exists())
        with closing(sqlite3.connect(self.root / 'live/control.sqlite3')) as db, db:
            db.execute('PRAGMA user_version=1')
        with self.assertRaisesRegex(StorageError, 'previous version'):
            run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        self.assertFalse(marker.exists())

    def test_open_checks_history_and_grants(self):
        archived = backup_workspace(self.root, self.base / 'old')
        older = restore_workspace(archived, self.base / 'older')
        with closing(sqlite3.connect(self.root / 'live/adapters-live.sqlite3')) as db, db:
            db.execute("INSERT INTO attempt_history(operation_id,result,stamp) VALUES ('a','unknown',1)")
        with self.assertRaises(StorageError):
            run_maintenance('open', str(self.root), str(older), backup_dir=str(self.base))
        with self.assertRaises(StorageError):
            run_maintenance('restore', str(self.root), str(self.base / 'refused'), str(archived), str(self.base))
        fresh = restore_workspace(backup_workspace(self.root, self.base / 'current-backup'), self.base / 'current-copy')
        with closing(sqlite3.connect(fresh / 'live/adapters-live.sqlite3')) as db, db:
            db.execute("INSERT INTO retry_grants VALUES ('a')")
        with self.assertRaises(StorageError):
            run_maintenance('open', str(self.root), str(fresh), backup_dir=str(self.base))
        with closing(sqlite3.connect(fresh / 'live/adapters-live.sqlite3')) as db, db:
            db.execute('DELETE FROM retry_grants')
        result = run_maintenance('open', str(self.root), str(fresh), backup_dir=str(self.base))
        self.assertEqual(result['data_root'], str(fresh))
        self.assertTrue(Path(result['backup']).is_dir())
        self.assertEqual(run_maintenance('open', str(fresh))['data_root'], str(fresh))

    def test_migration_retains_entities_revisions_and_receipt_reference(self):
        seller = Repository(self.root / 'live/sellers/a/workspace.sqlite3', 'seller')
        image = {'attachments': [{'kind': 'image', 'url': 'https://example.invalid/a.png'}],
                 'native_receipt_ref': str(self.receipt)}
        seller.put('message', 'image-1', image, history=True)
        seller.close()
        result = run_maintenance('migrate', str(self.root), str(self.base / 'moved'), backup_dir=str(self.base))
        moved = Path(result['data_root'])
        shutil.rmtree(self.root)
        with closing(sqlite3.connect(moved / 'live/sellers/a/workspace.sqlite3')) as db:
            self.assertEqual(json.loads(db.execute("SELECT data FROM entities WHERE id='image-1'").fetchone()[0]), image)
            self.assertEqual(json.loads(db.execute("SELECT data FROM revisions WHERE id='image-1'").fetchone()[0]), image)
        with closing(sqlite3.connect(moved / 'live/adapters-live.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT operation_id,result FROM operations').fetchone(), ('a', '{"status":"unknown"}'))
        self.assertTrue(_file(SimpleNamespace(data_dir=moved / 'live'), str(self.receipt)).is_file())

    def test_future_schema_and_collision_refused(self):
        with closing(sqlite3.connect(self.root / 'live/control.sqlite3')) as db, db:
            db.execute('PRAGMA user_version=999')
        with self.assertRaises(StorageError):
            run_maintenance('prepare', str(self.root))
        with closing(sqlite3.connect(self.root / 'live/control.sqlite3')) as db, db:
            db.execute('PRAGMA user_version=2')
        target = self.base / 'collision'
        target.mkdir()
        (target / 'keep').write_text('keep')
        with self.assertRaises(StorageError):
            run_maintenance('migrate', str(self.root), str(target), backup_dir=str(self.base))
        self.assertEqual((target / 'keep').read_text(), 'keep')

    def test_source_change_after_backup_refuses_publication(self):
        def backup_then_change(root, destination):
            result = backup_workspace(root, destination)
            with closing(sqlite3.connect(self.root / 'live/adapters-live.sqlite3')) as db, db:
                db.execute("INSERT INTO operations VALUES ('b','key2','send','{}','{}')")
            return result
        with patch('reception_service.storage_maintenance.backup_workspace', side_effect=backup_then_change):
            with self.assertRaisesRegex(StorageError, 'changed after backup'):
                run_maintenance('migrate', str(self.root), str(self.base / 'moved'), backup_dir=str(self.base))
        self.assertFalse((self.base / 'moved').exists())

    def test_provenance_tampering_and_link_refused(self):
        restored = restore_workspace(backup_workspace(self.root, self.base / 'backup'), self.base / 'restored')
        gateway = SimpleNamespace(data_dir=restored / 'live')
        provenance = restored / '.receipt-roots.json'
        data = json.loads(provenance.read_text())
        data['receipt_roots'] = [str(self.base / 'forged')]
        provenance.write_text(json.dumps(data))
        with self.assertRaises(AdapterError):
            _file(gateway, str(self.receipt))
        link = self.base / 'link'
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symlinks is unavailable')
        with self.assertRaises((StorageError, ValueError)):
            run_maintenance('inspect', str(link))

    def test_busy_and_missing_refused(self):
        with WorkspaceLock(self.root):
            with self.assertRaises((StorageError, OSError)):
                run_maintenance('inspect', str(self.root))
        with self.assertRaises(StorageError):
            run_maintenance('inspect', str(self.base / 'missing'))
        self.assertFalse((self.base / 'missing').exists())

if __name__ == '__main__':
    unittest.main()
