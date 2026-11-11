from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reception_service.repository import Repository
from reception_service.storage_guard import StorageError
from reception_service.storage_maintenance import run_maintenance
from reception_service.workspace_lock import WorkspaceLock


@unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics required')
class WalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'root'
        Repository(self.root / 'demo/control.sqlite3').close()
        Repository(self.root / 'demo/workspace.sqlite3').close()
        with WorkspaceLock(self.root) as lock:
            lock.mark_clean_shutdown()

    def tearDown(self):
        self.temp.cleanup()

    def residual_wal(self):
        path = self.root / 'demo/workspace.sqlite3'
        db = sqlite3.connect(path, isolation_level=None)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("INSERT INTO entities VALUES ('test','one','{\"value\":1}')")
        main = path.read_bytes()
        wal = Path(str(path) + '-wal').read_bytes()
        db.close()
        path.write_bytes(main)
        Path(str(path) + '-wal').write_bytes(wal)
        return path, wal

    def test_clean_residual_wal_is_backed_up_and_checkpointed(self):
        path, wal = self.residual_wal()
        result = run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        backup = Path(result['recovery_backup'])
        self.assertEqual((backup / 'demo/workspace.sqlite3-wal').read_bytes(), wal)
        self.assertEqual(json.loads((backup / 'raw-manifest.json').read_text())['status'], 'checkpoint_verified')
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(json.loads(db.execute("SELECT data FROM entities WHERE id='one'").fetchone()[0]), {'value': 1})
        self.assertTrue((self.root / '.reception-clean-shutdown').exists())

    def test_backup_failure_preserves_clean_and_wal(self):
        path, wal = self.residual_wal()
        with patch('reception_service.storage_recovery._hash', side_effect=OSError('injected')):
            with self.assertRaises(StorageError) as error:
                run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        self.assertEqual(error.exception.code, 'storage_backup_failed')
        self.assertEqual(Path(str(path) + '-wal').read_bytes(), wal)
        self.assertTrue((self.root / '.reception-clean-shutdown').exists())

    def test_open_connection_refuses_without_modifying_wal(self):
        path, wal = self.residual_wal()
        with closing(sqlite3.connect(path)) as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(StorageError):
                run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
            self.assertEqual(Path(str(path) + '-wal').read_bytes(), wal)
            db.rollback()

    def test_failure_after_checkpoint_retains_snapshot_and_dirty_marker(self):
        path, wal = self.residual_wal()
        with patch('reception_service.storage_recovery.preflight_workspace', side_effect=StorageError('injected')):
            with self.assertRaises(StorageError):
                run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        self.assertFalse((self.root / '.reception-clean-shutdown').exists())
        backup = next(self.base.glob('reception-raw-recovery-*'))
        self.assertEqual((backup / 'demo/workspace.sqlite3-wal').read_bytes(), wal)
        with closing(sqlite3.connect(path)) as db:
            self.assertIsNotNone(db.execute("SELECT data FROM entities WHERE id='one'").fetchone())

    def test_busy_checkpoint_is_reported_and_connection_closed(self):
        path = self.root / 'demo/workspace.sqlite3'
        repository = Repository(path)
        reader = sqlite3.connect(path, isolation_level=None)
        try:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM entities').fetchall()
            repository.put('test', 'busy', {'value': 1})
            with self.assertRaises(StorageError) as error:
                repository.close()
            self.assertEqual(error.exception.code, 'storage_busy')
            with self.assertRaises(sqlite3.ProgrammingError):
                repository.db.execute('SELECT 1')
        finally:
            reader.close()

    def test_dirty_current_wal_is_not_recovered_or_marked_clean(self):
        path, wal = self.residual_wal()
        (self.root / '.reception-clean-shutdown').unlink()
        result = run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        self.assertFalse(result['upgrade_required'])
        self.assertIsNone(result['recovery_backup'])
        self.assertEqual(Path(str(path) + '-wal').read_bytes(), wal)
        self.assertFalse((self.root / '.reception-clean-shutdown').exists())

    def test_long_sqlite_path_rejected_before_creating_directories(self):
        path = self.base / ('a' * 120) / ('b' * 120) / 'store.sqlite3'
        with self.assertRaises(StorageError) as error:
            Repository(path)
        self.assertEqual(error.exception.code, 'storage_path_too_long')
        self.assertFalse(path.parent.exists())

    def test_service_closes_other_repository_after_failure_without_clean_marker(self):
        from reception_service.service import ReceptionService
        service = ReceptionService(self.base / 'service', 'demo')
        real_close = service.repo.close
        def fail_close():
            real_close()
            raise StorageError('injected close failure')
        with patch.object(service.repo, 'close', side_effect=fail_close), patch.object(service._control, 'close', wraps=service._control.close) as other:
            with self.assertRaises(StorageError):
                service.close()
            other.assert_called_once()
        self.assertFalse((self.base / 'service/.reception-clean-shutdown').exists())

    def test_memory_close(self):
        Repository(None).close()
