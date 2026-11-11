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
from reception_service.storage_guard import LEDGER_SCHEMA, StorageError, preflight_workspace
from reception_service.storage_maintenance import run_maintenance
from reception_service.workspace_backup import RECEIPTS
from reception_service.workspace_lock import WorkspaceLock


@unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics required')
class WalRecoveryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'root'
        for name in ('demo/control.sqlite3', 'demo/workspace.sqlite3'):
            Repository(self.root / name).close()
        self.marker = self.root / '.reception-clean-shutdown'
        with WorkspaceLock(self.root) as lock:
            lock.mark_clean_shutdown()

    def tearDown(self):
        self.temp.cleanup()

    def wal(self, name, statements, checkpoint=False):
        path = self.root / name
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            for sql, params in statements:
                db.execute(sql, params)
            if checkpoint:
                self.assertEqual(db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()[0], 0)
            main = path.read_bytes()
            wal = Path(str(path) + '-wal').read_bytes()
        path.write_bytes(main)
        Path(str(path) + '-wal').write_bytes(wal)
        return main, wal

    def ledger(self, name='demo/integrations-demo.sqlite3'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            for sql in LEDGER_SCHEMA:
                db.execute(sql)
            db.execute('PRAGMA user_version=1')
            db.commit()
        return path

    def prepare(self):
        return run_maintenance('prepare', str(self.root), backup_dir=str(self.base))

    def test_multi_database_checkpoint_failure_keeps_raw_evidence_and_logical_state(self):
        ledger = self.ledger()
        state = [('INSERT INTO operations VALUES (?,?,?,?,?)', ('op', 'key', 'send', '{}', '{"status":"unknown"}')),
                 ('INSERT INTO attempt_history(operation_id,result,stamp) VALUES (?,?,?)', ('op', 'unknown', 1)),
                 ('INSERT INTO retry_grants VALUES (?)', ('op',))]
        ledger_main, ledger_wal = self.wal('demo/integrations-demo.sqlite3', state)
        repo_main, repo_wal = self.wal('demo/workspace.sqlite3', [("INSERT INTO entities VALUES (?,?,?)", ('test', 'w', '{}'))])
        real_connect = sqlite3.connect
        class ConnectionProxy:
            def __init__(self, db): self.db = db
            def execute(self, sql, *args):
                if sql == 'PRAGMA wal_checkpoint(TRUNCATE)':
                    raise sqlite3.OperationalError('injected second checkpoint failure')
                return self.db.execute(sql, *args)
            def close(self): self.db.close()
        def connect(database, *args, **kwargs):
            db = real_connect(database, *args, **kwargs)
            if isinstance(database, str) and 'workspace.sqlite3?mode=rw' in database:
                return ConnectionProxy(db)
            return db
        with patch('reception_service.storage_recovery.sqlite3.connect', side_effect=connect):
            with self.assertRaises(StorageError) as error:
                self.prepare()
        self.assertEqual(error.exception.code, 'storage_recovery_incomplete')
        self.assertFalse(self.marker.exists())
        backup = next(self.base.glob('reception-raw-recovery-*'))
        manifest = json.loads((backup / 'raw-manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'], 'checkpoint_in_progress')
        self.assertEqual(len(manifest['checkpoints']), 1)
        for name, main, wal in [('demo/integrations-demo.sqlite3', ledger_main, ledger_wal), ('demo/workspace.sqlite3', repo_main, repo_wal)]:
            self.assertEqual((backup / name).read_bytes(), main)
            self.assertEqual((backup / (name + '-wal')).read_bytes(), wal)
        with closing(sqlite3.connect(ledger)) as db:
            self.assertEqual(db.execute('SELECT result FROM operations').fetchall(), [('{"status":"unknown"}',)])
            self.assertEqual(db.execute('SELECT result FROM attempt_history').fetchall(), [('unknown',)])
            self.assertEqual(db.execute('SELECT operation_id FROM retry_grants').fetchall(), [('op',)])
        with closing(sqlite3.connect(self.root / 'demo/workspace.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT id FROM entities WHERE id='w'").fetchall(), [('w',)])

    def test_missing_receipt_referenced_only_by_wal_does_not_mark_clean(self):
        Repository(self.root / 'live/control.sqlite3').close()
        self.ledger('live/adapters-live.sqlite3')
        (self.root / RECEIPTS).mkdir(parents=True)
        name = 'live/control.sqlite3'
        main, wal = self.wal(name, [('INSERT INTO entities VALUES (?,?,?)', ('operation', 'op', '{"native_receipt_ref":"missing.json"}'))])
        with self.assertRaises(StorageError):
            self.prepare()
        self.assertFalse(self.marker.exists())
        backup = next(self.base.glob('reception-raw-recovery-*'))
        self.assertEqual((backup / name).read_bytes(), main)
        self.assertEqual((backup / (name + '-wal')).read_bytes(), wal)
        with closing(sqlite3.connect(self.root / name)) as db:
            self.assertIn('missing.json', db.execute("SELECT data FROM entities WHERE id='op'").fetchone()[0])

    def test_valid_receipt_and_ledger_data_only_in_wal_survive_recovery(self):
        Repository(self.root / 'live/control.sqlite3').close()
        ledger = self.ledger('live/adapters-live.sqlite3')
        receipt = self.root / RECEIPTS / 'receipt.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{}', encoding='utf-8')
        name = 'live/adapters-live.sqlite3'
        main, wal = self.wal(name, [
            ('INSERT INTO operations VALUES (?,?,?,?,?)', ('op', 'key', 'send', '{}', '{"status":"unknown","native_receipt_ref":"receipt.json"}')),
            ('INSERT INTO attempt_history(operation_id,result,stamp) VALUES (?,?,?)', ('op', 'unknown', 1)),
            ('INSERT INTO retry_grants VALUES (?)', ('op',))])
        preflight_workspace(self.root)
        self.assertEqual((self.root / name).read_bytes(), main)
        self.assertEqual((self.root / (name + '-wal')).read_bytes(), wal)
        self.assertTrue(self.marker.exists())
        result = self.prepare()
        backup = Path(result['recovery_backup'])
        self.assertEqual((backup / (name + '-wal')).read_bytes(), wal)
        self.assertTrue(self.marker.exists())
        with closing(sqlite3.connect(ledger)) as db:
            self.assertEqual(json.loads(db.execute('SELECT result FROM operations').fetchone()[0]),
                             {'status': 'unknown', 'native_receipt_ref': 'receipt.json'})
            self.assertEqual(db.execute('SELECT result FROM attempt_history').fetchall(), [('unknown',)])
            self.assertEqual(db.execute('SELECT operation_id FROM retry_grants').fetchall(), [('op',)])
        self.assertEqual(receipt.read_text(encoding='utf-8'), '{}')

    def test_nonempty_checkpointed_wal_is_recovered(self):
        name = 'demo/workspace.sqlite3'
        _, wal = self.wal(name, [('INSERT INTO entities VALUES (?,?,?)', ('test', 'checkpointed', '{}'))], checkpoint=True)
        self.assertGreater(len(wal), 0)
        result = self.prepare()
        self.assertEqual((Path(result['recovery_backup']) / (name + '-wal')).read_bytes(), wal)
        self.assertTrue(self.marker.exists())
        with closing(sqlite3.connect(self.root / name)) as db:
            self.assertEqual(db.execute("SELECT id FROM entities WHERE id='checkpointed'").fetchall(), [('checkpointed',)])

    def test_dirty_old_schema_with_wal_refuses_upgrade_without_touching_files(self):
        name = 'demo/workspace.sqlite3'
        main, wal = self.wal(name, [('PRAGMA user_version=1', ()), ('INSERT INTO entities VALUES (?,?,?)', ('test', 'old', '{}'))])
        self.marker.unlink()
        with self.assertRaises(StorageError):
            self.prepare()
        self.assertFalse(self.marker.exists())
        self.assertEqual((self.root / name).read_bytes(), main)
        self.assertEqual((self.root / (name + '-wal')).read_bytes(), wal)
        self.assertFalse(list(self.base.glob('reception-raw-recovery-*')))

    def test_preflight_and_dirty_prepare_leave_wal_and_marker_untouched(self):
        name = 'demo/workspace.sqlite3'
        main, wal = self.wal(name, [('INSERT INTO entities VALUES (?,?,?)', ('test', 'dirty', '{}'))])
        self.marker.unlink()
        preflight_workspace(self.root)
        result = self.prepare()
        self.assertIsNone(result['recovery_backup'])
        self.assertFalse(self.marker.exists())
        self.assertEqual((self.root / name).read_bytes(), main)
        self.assertEqual((self.root / (name + '-wal')).read_bytes(), wal)
