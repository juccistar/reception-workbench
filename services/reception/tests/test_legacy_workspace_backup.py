import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reception_service.repository import Repository
from reception_service.integrations.accio_native.vendor.adapters.common import Ledger
from reception_service.storage_guard import StorageError
from reception_service.workspace_backup import backup_workspace, restore_workspace
from reception_service.legacy_workspace_backup import LegacyShutdownEvidence, export_legacy_workspace


@unittest.skipUnless(os.name == 'nt', 'Windows sharing enforcement required')
class LegacyBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        Repository(self.root / 'live/control.sqlite3').close()
        Repository(self.root / 'live/sellers/a/workspace.sqlite3', 'a').close()
        Ledger(self.root / 'live/adapters-live.sqlite3').initialize()
        self.receipt = self.root / 'live/workctl-state/chat-analysis/send-receipts/unbound.json'
        self.receipt.parent.mkdir(parents=True)
        self.receipt.write_text('{}')
        os.utime(self.receipt, ns=(1700000000123456789, 1700000000123456789))
        self.evidence = LegacyShutdownEvidence('verified owning process list empty', 'verified no running operations')

    def tearDown(self):
        self.temp.cleanup()

    def export(self, name='backup'):
        return export_legacy_workspace(self.root, self.base / name, evidence=self.evidence)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file() and p.name != '.reception-workspace.lock'}

    def test_closed_wal_and_roundtrip_preserve_source(self):
        path = self.root / 'live/sellers/a/workspace.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute("INSERT INTO entities VALUES ('operation','wal-only','{\"status\":\"unknown\"}')")
            db.commit()
            copies = {suffix: Path(str(path) + suffix).read_bytes() for suffix in ('', '-wal', '-shm')}
        for suffix, contents in copies.items():
            Path(str(path) + suffix).write_bytes(contents)
        before = self.snapshot()
        backup = self.export()
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / '.reception-clean-shutdown').exists())
        restored = restore_workspace(backup, self.base / 'restored')
        repo = Repository(restored / 'live/sellers/a/workspace.sqlite3', 'a')
        self.assertEqual(repo.get('operation', 'wal-only')['status'], 'unknown')
        repo.close()
        self.assertEqual((restored / self.receipt.relative_to(self.root)).stat().st_mtime_ns, self.receipt.stat().st_mtime_ns)

    def test_historical_absolute_and_windows_relative_roundtrip(self):
        nested = self.receipt.parent / 'nested' / 'receipt.json'
        nested.parent.mkdir()
        nested.write_text('{}')
        repo = Repository(self.root / 'live/sellers/a/workspace.sqlite3', 'a')
        with repo.transaction():
            repo.put('operation', 'x', {'native_receipt_ref': str(nested)})
            repo.put('operation', 'x', {'native_receipt_ref': r'nested\receipt.json',
                                        'receipt_baseline': {r'nested\receipt.json': {}}})
        before = repo.history('operation', 'x')
        repo.close()
        backup = self.export()
        restored = restore_workspace(backup, self.base / 'restored')
        repo = Repository(restored / 'live/sellers/a/workspace.sqlite3', 'a')
        self.assertEqual(before, repo.history('operation', 'x'))
        with repo.transaction():
            repo.put('operation', 'new', {'native_receipt_ref': str(restored / nested.relative_to(self.root))})
        repo.close()
        second = backup_workspace(restored, self.base / 'second-backup')
        restored_again = restore_workspace(second, self.base / 'restored-again')
        third = backup_workspace(restored_again, self.base / 'third-backup')
        restore_workspace(third, self.base / 'third-restore')

    def test_unsafe_and_missing_references_rejected(self):
        refs = [r'..\unbound.json', r'nested\..\unbound.json',
                str(self.receipt.parent.parent / 'outside.json'),
                str(self.receipt.parent / 'missing.json'),
                r'nested\missing.json', r'C:\outside\unbound.json',
                r'C:unbound.json', r'\unbound.json']
        for index, ref in enumerate(refs):
            with self.subTest(ref=ref):
                repo = Repository(self.root / 'live/sellers/a/workspace.sqlite3', 'a')
                with repo.transaction():
                    repo.put('operation', 'bad', {'native_receipt_ref': ref})
                repo.close()
                with self.assertRaises(StorageError):
                    self.export('bad-' + str(index))
                with closing(sqlite3.connect(self.root / 'live/sellers/a/workspace.sqlite3')) as db:
                    db.execute("DELETE FROM entities WHERE id='bad'")
                    db.execute("DELETE FROM revisions WHERE id='bad'")
                    db.commit()

    def test_writer_and_missing_evidence_rejected(self):
        path = self.root / 'live/control.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(StorageError): self.export()
            db.rollback()
        with self.assertRaises(StorageError):
            export_legacy_workspace(self.root, self.base / 'no-evidence', evidence=LegacyShutdownEvidence('', 'checked'))

    def test_future_and_missing_ledger_or_receipt_rejected(self):
        path = self.root / 'live/control.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA user_version=99')
        before = self.snapshot()
        with self.assertRaises(StorageError): self.export('future')
        self.assertEqual(before, self.snapshot())
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA user_version=2')
        repo = Repository(self.root / 'live/sellers/a/workspace.sqlite3', 'a')
        with repo.transaction(): repo.put('operation', 'x', {'native_receipt_ref': 'missing.json'})
        repo.close()
        with self.assertRaises(StorageError): self.export('missing-ref')
        (self.root / 'live/adapters-live.sqlite3').unlink()
        with self.assertRaises(StorageError): self.export('missing-ledger')

    def test_tamper_and_nonempty_target_rejected(self):
        target = self.base / 'occupied'
        target.mkdir()
        (target / 'x').write_text('x')
        with self.assertRaises(StorageError): self.export('occupied')
        backup = self.export()
        manifest = json.loads((backup / 'manifest.json').read_text())
        (backup / manifest['files'][0]['path']).write_bytes(b'tampered')
        with self.assertRaises(StorageError): restore_workspace(backup, self.base / 'bad')
