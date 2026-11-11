import hashlib
import json
import os
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from reception_service.launch_ownership import inspect_launch_ownership


class LaunchOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='launch-owner-test-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def seller_store(self, seller='12345', folder=None):
        directory = self.root / 'live' / 'sellers' / (folder or hashlib.sha256(seller.encode()).hexdigest())
        directory.mkdir(parents=True)
        database = directory / 'workspace.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE entities(kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id))')
            db.execute('INSERT INTO entities VALUES(?,?,?)', ('meta', 'seller', json.dumps({'seller_id': seller})))
            # The route must not decode or expose any customer row.
            db.execute('INSERT INTO entities VALUES(?,?,?)', ('message', 'private', 'PRIVATE_NOT_JSON_NOT_TO_BE_READ'))
        db.close()
        return database

    def test_zero_byte_workspace_is_unknown_with_safe_reason(self):
        directory = self.root / 'live' / 'sellers' / hashlib.sha256(b'12345').hexdigest()
        directory.mkdir(parents=True)
        (directory / 'workspace.sqlite3').touch()
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'unknown', 'reason': 'zero_byte_workspace'})

    @unittest.skipUnless(os.name == 'nt', 'Windows extended metadata paths')
    def test_legacy_long_zero_byte_workspace_is_classified_without_modification(self):
        from reception_service.launch_ownership import _io_path
        root = self.root / ('x' * 150)
        workspace = root / 'live/sellers' / hashlib.sha256(b'12345').hexdigest() / 'workspace.sqlite3'
        self.assertGreater(len(str(workspace) + '-journal') + 1, 260)
        try:
            _io_path(workspace.parent).mkdir(parents=True)
            _io_path(workspace).touch()
            self.assertEqual(inspect_launch_ownership(root), {'status': 'unknown', 'reason': 'zero_byte_workspace'})
            self.assertEqual(_io_path(workspace).stat().st_size, 0)
            self.assertEqual({p.name for p in _io_path(workspace.parent).iterdir()}, {'workspace.sqlite3'})
        finally:
            shutil.rmtree(_io_path(root))

    def test_reads_only_dedicated_seller_metadata_and_keeps_source_bytes(self):
        database = self.seller_store()
        before = database.read_bytes()
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'owned', 'sellerId': '12345'})
        self.assertEqual(database.read_bytes(), before)
        self.assertFalse(Path(str(database) + '-wal').exists())
        self.assertFalse(Path(str(database) + '-shm').exists())

    def test_mismatched_hash_missing_metadata_or_multiple_sellers_is_not_unique_owner(self):
        self.seller_store(folder='a' * 64)
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'unknown'})
        with tempfile.TemporaryDirectory() as other:
            old = self.root
            self.root = Path(other)
            self.seller_store('12345')
            self.seller_store('67890')
            self.assertEqual(inspect_launch_ownership(self.root), {'status': 'multiple'})
            self.root = old

    def test_current_wal_identity_is_read_from_private_copy_without_source_mutation(self):
        database = self.seller_store()
        db = sqlite3.connect(database)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('UPDATE entities SET data=? WHERE kind=? AND id=?', (json.dumps({'seller_id': '12345'}), 'meta', 'seller'))
            db.commit()
            before = {file.name: file.read_bytes() for file in database.parent.iterdir()}
            self.assertEqual(inspect_launch_ownership(self.root), {'status': 'owned', 'sellerId': '12345'})
            self.assertEqual({file.name: file.read_bytes() for file in database.parent.iterdir()}, before)
        finally:
            db.close()

    def test_demo_is_not_live_owner_and_unknown_root_is_preserved(self):
        (self.root / 'demo').mkdir()
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'empty'})
        (self.root / 'unknown.txt').write_text('PRIVATE', encoding='utf-8')
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'unknown'})
        self.assertEqual((self.root / 'unknown.txt').read_text(), 'PRIVATE')

    def unbound_stores(self):
        live = self.root / 'live'
        live.mkdir(exist_ok=True)
        (live / 'workctl-state/chat-analysis/send-receipts').mkdir(parents=True)
        with sqlite3.connect(live / 'control.sqlite3') as connection:
            connection.execute('CREATE TABLE entities(kind TEXT,id TEXT,data TEXT)')
            connection.execute('CREATE TABLE revisions(kind TEXT,id TEXT,data TEXT)')
            connection.execute('INSERT INTO entities VALUES(?,?,?)', ('meta', 'seller', '{"seller_id":""}'))
        connection.close()
        with sqlite3.connect(live / 'adapters-live.sqlite3') as connection:
            for table in ('operations', 'attempt_history', 'retry_grants'):
                connection.execute('CREATE TABLE ' + table + '(data TEXT)')
        connection.close()
        return live

    def test_empty_unbound_stores_are_allowed_but_residual_history_is_not(self):
        live = self.unbound_stores()
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'empty'})
        ledger = live / 'adapters-live.sqlite3'
        for table in ('operations', 'attempt_history', 'retry_grants'):
            with self.subTest(table=table):
                with sqlite3.connect(ledger) as connection:
                    connection.execute('INSERT INTO ' + table + ' VALUES(?)', ('PRIVATE_NOT_JSON',))
                connection.close()
                before = ledger.read_bytes()
                self.assertEqual(inspect_launch_ownership(self.root), {'status': 'unknown'})
                self.assertEqual(ledger.read_bytes(), before)
                with sqlite3.connect(ledger) as connection:
                    connection.execute('DELETE FROM ' + table)
                connection.close()
        with sqlite3.connect(live / 'control.sqlite3') as connection:
            connection.execute('INSERT INTO entities VALUES(?,?,?)', ('message', 'private', 'PRIVATE_NOT_JSON'))
        connection.close()
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'unknown'})

    def test_revision_only_business_history_cannot_be_classified_as_empty(self):
        live = self.unbound_stores()
        control = live / 'control.sqlite3'
        with sqlite3.connect(control) as connection:
            connection.execute('INSERT INTO revisions VALUES(?,?,?)', ('operation', 'original-unknown', '{"status":"unknown"}'))
        connection.close()
        before = control.read_bytes()
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'unknown'})
        self.assertEqual(control.read_bytes(), before)

    def test_unknown_root_files_and_orphan_sqlite_sidecars_cannot_be_empty(self):
        live = self.root / 'live'
        live.mkdir()
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'empty'})
        unknown = self.root / 'unclassified-history.bin'
        unknown.write_bytes(b'PRESERVE_UNKNOWN_HISTORY')
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'unknown'})
        self.assertEqual(unknown.read_bytes(), b'PRESERVE_UNKNOWN_HISTORY')
        unknown.unlink()
        for name in ('control.sqlite3-wal', 'adapters-live.sqlite3-shm'):
            with self.subTest(name=name):
                orphan = live / name
                orphan.write_bytes(b'PRESERVE_ORPHAN')
                self.assertEqual(inspect_launch_ownership(self.root), {'status': 'unknown'})
                self.assertEqual(orphan.read_bytes(), b'PRESERVE_ORPHAN')
                orphan.unlink()

    def test_missing_unbound_seller_metadata_is_not_an_empty_store(self):
        live = self.unbound_stores()
        with sqlite3.connect(live / 'control.sqlite3') as connection:
            connection.execute('DELETE FROM entities')
        connection.close()
        self.assertEqual(inspect_launch_ownership(self.root), {'status': 'unknown'})

    def test_cli_owner_branch_does_not_construct_reception_service(self):
        from reception_service.__main__ import main
        self.seller_store()
        with patch.object(sys, 'argv', ['service', '--inspect-launch-owner', '--data-dir', str(self.root)]), patch('reception_service.__main__.ReceptionService', side_effect=AssertionError('Business service must not be constructed')):
            with patch('sys.stdout') as output:
                main()
                emitted = ''.join(call.args[0] for call in output.write.call_args_list)
        self.assertEqual(json.loads(emitted), {'status': 'owned', 'sellerId': '12345'})
        self.assertNotIn('PRIVATE', emitted)


if __name__ == '__main__':
    unittest.main()
