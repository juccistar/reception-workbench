"""Synthetic-only recovery behavior and refusal regression checks."""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from reception_service import profile_recovery as module
from reception_service.repository import Repository
from reception_service.workspace_lock import WorkspaceLock
from reception_service.integrations.accio_native.vendor.adapters.common import Ledger

@unittest.skipUnless(os.name == 'nt', 'Windows kernel lock behavior')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rp-')
        self.base = Path(self.temp.name)
        self.registry = self.base / 'r'
        self.seller = '987654321'
        self.key = hashlib.sha256(self.seller.encode()).hexdigest()
        self.state = self.registry / 'profiles' / self.key / 'state'
        self.root = self.base / ('x' * 145)
        self.target = self.base / 't'
        self.backups = self.base / 'b'
        self.state.mkdir(parents=True)
        Repository(self.root / 'live/control.sqlite3').close()
        Ledger(self.root / 'live/adapters-live.sqlite3').initialize()
        workspace = self.root / 'live/sellers' / self.key / 'workspace.sqlite3'
        module.io_path(workspace.parent).mkdir(parents=True)
        module.io_path(workspace).touch()
        self.receipts = self.root / 'live/workctl-state/chat-analysis/send-receipts'
        self.receipts.mkdir(parents=True)
        with WorkspaceLock(self.root) as lock:
            lock.mark_clean_shutdown()
        self.other = self.base / 'other'
        self.other.mkdir()
        (self.other / 'sentinel').write_bytes(b'unrelated synthetic data')
        other_seller = '123456789'
        other_key = hashlib.sha256(other_seller.encode()).hexdigest()
        other_state = self.registry / 'profiles' / other_key / 'state'
        other_state.mkdir(parents=True)
        (other_state / 'storage.json').write_text(json.dumps({'version':1,'dataRoot':str(self.other)}),encoding='utf-8')
        record={'sellerId':self.seller,'stateDirectory':str(self.state),'initialDataRoot':str(self.root),'status':'created'}
        other_record={'sellerId':other_seller,'stateDirectory':str(other_state),'initialDataRoot':str(self.other),'status':'verified'}
        (self.registry / 'merchants.json').write_text(json.dumps({'format':1,'profiles':{self.seller:record,other_seller:other_record}}),encoding='utf-8')
        (self.state / 'storage.json').write_text(json.dumps({'version':1,'dataRoot':str(self.root)}),encoding='utf-8')
        (self.state / 'profile.json').write_text(json.dumps({'format':1,'sellerId':self.seller,'dataRoot':str(self.root),'stateDirectory':str(self.state)}),encoding='utf-8')
        self.metadata={p:p.read_bytes() for p in [self.registry/'merchants.json',self.state/'storage.json',self.state/'profile.json']}

    def tearDown(self):
        shutil.rmtree(module.io_path(self.base))
        self.temp.cleanup()

    def source_hashes(self):
        return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}

    def call(self, **kwargs):
        kwargs.pop('apply', None)
        return module._recover(self.registry,self.seller,self.target,self.backups,**kwargs)

    def assert_metadata_unchanged(self):
        for path,content in self.metadata.items():
            self.assertEqual(path.read_bytes(),content)
        self.assertEqual((self.other/'sentinel').read_bytes(),b'unrelated synthetic data')

    def test_recovery_preserves_source_other_profile_and_retired_root(self):
        before=self.source_hashes()
        result=self.call(apply=True)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(module.inspect_launch_ownership(self.target),{'status':'empty'})
        self.assertFalse((self.target/'live/sellers').exists())
        self.assertEqual(self.source_hashes(),before)
        data=json.loads((self.registry/'merchants.json').read_text(encoding='utf-8'))
        self.assertEqual(data['profiles'][self.seller]['status'],'created')
        self.assertEqual(data['profiles'][self.seller]['initialDataRoot'],str(self.target))
        self.assertEqual(data['profiles']['123456789'],json.loads(self.metadata[self.registry/'merchants.json'])['profiles']['123456789'])
        storage=json.loads((self.state/'storage.json').read_text(encoding='utf-8'))
        self.assertEqual(storage['previousRoot'],str(self.root))
        self.assertEqual((self.other/'sentinel').read_bytes(),b'unrelated synthetic data')
        journal=json.loads((Path(result['backup'])/'recovery-transaction.json').read_text(encoding='utf-8'))
        self.assertEqual(journal['status'],'completed')

    def test_short_zero_byte_workspace_is_refused(self):
        root = self.base / 'short'
        workspace = root / 'live' / 'sellers' / self.key / 'workspace.sqlite3'
        workspace.parent.mkdir(parents=True)
        workspace.touch()
        with self.assertRaisesRegex(module.ProfileRecoveryError, 'source_path_not_legacy_too_long'):
            module.qualification(root, self.key)
        self.assertEqual(workspace.stat().st_size, 0)

    def test_target_cannot_enter_other_merchant_root(self):
        with self.assertRaisesRegex(module.ProfileRecoveryError, 'target_conflicts_with_registered_root'):
            module.recover_created_profile(self.registry, self.seller, self.other / 'new', self.backups)
        self.assertEqual({p.name for p in self.other.iterdir()}, {'sentinel'})
        self.assert_metadata_unchanged()

    def test_verified_profile_is_refused(self):
        path = self.registry / 'merchants.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['profiles'][self.seller]['status'] = 'verified'
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(module.ProfileRecoveryError, 'not_created_profile'):
            self.call()
        self.assertFalse(self.target.exists())

    def test_unknown_live_file_is_refused(self):
        (self.root / 'live' / 'unknown').write_bytes(b'evidence')
        with self.assertRaises(module.ProfileRecoveryError):
            self.call()
        self.assertFalse(self.target.exists())
        self.assert_metadata_unchanged()

    def test_process_exit_after_replace_before_progress_restores(self):
        fixture = self.base / 'fixture.json'
        fixture.write_text(json.dumps({'registry':str(self.registry),'seller':self.seller,'target':str(self.target),'backups':str(self.backups),'state':str(self.state)}), encoding='utf-8')
        code = '''import json,os,sys\nfrom pathlib import Path\nfrom reception_service import profile_recovery as m\nf=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))\noriginal=m.atomic_bytes\ndef interrupt(path,data):\n original(path,data)\n if path==Path(f["state"])/"storage.json": os._exit(77)\nm.atomic_bytes=interrupt\nm.recover_created_profile(f["registry"],f["seller"],f["target"],f["backups"])\n'''
        child = subprocess.run([sys.executable, '-c', code, str(fixture)], capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 77, child.stderr.decode(errors='replace'))
        journal = next(self.backups.glob('*/recovery-transaction.json'))
        self.assertEqual(module.restore_journal(journal)['status'], 'restored_metadata')
        self.assert_metadata_unchanged()

    def test_ledger_history_refused(self):
        with closing(sqlite3.connect(self.root/'live/adapters-live.sqlite3')) as db,db:
            db.execute("INSERT INTO operations VALUES ('synthetic-operation','key','send','{}','{}')")
        with self.assertRaises(module.Refused):
            self.call(apply=True)
        self.assertFalse(self.target.exists())
        self.assert_metadata_unchanged()

    def test_nonempty_seller_store_refused(self):
        module.io_path(self.root/'live/sellers'/self.key/'workspace.sqlite3').write_bytes(b'synthetic nonempty')
        with self.assertRaises(module.Refused):
            self.call(apply=True)
        self.assertFalse(self.target.exists())
        self.assert_metadata_unchanged()

    def test_seller_sidecar_refused(self):
        module.io_path(self.root/'live/sellers'/self.key/'workspace.sqlite3-wal').write_bytes(b'synthetic WAL evidence')
        with self.assertRaises(module.Refused):
            self.call(apply=True)
        self.assertFalse(self.target.exists())
        self.assert_metadata_unchanged()

    def test_receipt_history_refused(self):
        module.io_path(self.receipts/'synthetic.json').write_bytes(b'{}')
        with self.assertRaises(module.Refused):
            self.call(apply=True)
        self.assertFalse(self.target.exists())
        self.assert_metadata_unchanged()

    def test_partial_metadata_commit_rolls_back(self):
        before=self.source_hashes()
        with self.assertRaisesRegex(module.Refused,'synthetic_commit_failure'):
            self.call(apply=True,fail_after=2)
        self.assert_metadata_unchanged()
        self.assertEqual(self.source_hashes(),before)
        journal=next(self.backups.glob('*/recovery-transaction.json'))
        self.assertEqual(json.loads(journal.read_text(encoding='utf-8'))['status'],'rolled_back')
        self.assertEqual(module.inspect_launch_ownership(self.target),{'status':'empty'})

    def test_open_database_writer_blocks_recovery(self):
        with closing(sqlite3.connect(self.root/'live/control.sqlite3')):
            with self.assertRaises(module.Refused):
                self.call(apply=True)
        self.assertFalse(self.target.exists())
        self.assert_metadata_unchanged()

    def test_backup_cannot_enter_other_merchant_root(self):
        with self.assertRaisesRegex(module.Refused,'backup_conflicts_with_registered_root'):
            module._recover(self.registry,self.seller,self.target,self.other)
        self.assertFalse(self.target.exists())
        self.assertEqual({p.name for p in self.other.iterdir()},{'sentinel'})
        self.assert_metadata_unchanged()

    def test_storage_mutex_blocks_concurrent_configuration_writer(self):
        observed=[]
        original=module.qualification
        def contender():
            try:
                with module.storage_mutex(self.state,timeout_ms=50):
                    observed.append('unexpected_access')
            except module.Refused:
                observed.append('blocked')
        def check(root,key):
            original(root,key)
            thread=threading.Thread(target=contender)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        with patch.object(module,'qualification',side_effect=check):
            self.assertEqual(self.call()['status'],'completed')
        self.assertEqual(observed,['blocked'])

    def test_journal_does_not_rollback_activated_target(self):
        atomic = module.atomic_bytes
        def fail_one_rollback(path, data):
            if path == self.state/'storage.json' and data == self.metadata[path]:
                raise OSError('synthetic rollback failure')
            return atomic(path, data)
        with patch.object(module, 'atomic_bytes', side_effect=fail_one_rollback):
            with self.assertRaises(module.PendingRecovery):
                self.call(fail_after=2)
        with closing(sqlite3.connect(self.target/'live/adapters-live.sqlite3')) as db, db:
            db.execute("INSERT INTO operations VALUES ('synthetic-operation','key','send','{}','{}')")
        before = (self.state/'storage.json').read_bytes()
        journal = next(self.backups.glob('*/recovery-transaction.json'))
        with self.assertRaisesRegex(module.Refused, 'target_has_been_activated_or_changed'):
            module.restore_journal(journal)
        self.assertEqual((self.state/'storage.json').read_bytes(), before)

    def test_rollback_io_failure_continues_and_journal_restores(self):
        atomic=module.atomic_bytes
        def fail_one_rollback(path,data):
            if path==self.state/'storage.json' and data==self.metadata[path]:
                raise OSError('synthetic rollback IO failure')
            return atomic(path,data)
        with patch.object(module,'atomic_bytes',side_effect=fail_one_rollback):
            with self.assertRaises(module.PendingRecovery):
                self.call(apply=True,fail_after=2)
        self.assertEqual((self.state/'profile.json').read_bytes(),self.metadata[self.state/'profile.json'])
        journal=next(self.backups.glob('*/recovery-transaction.json'))
        self.assertEqual(json.loads(journal.read_text(encoding='utf-8'))['status'],'rollback_requires_review')
        self.assertEqual(module.restore_journal(journal)['status'],'restored_metadata')
        self.assert_metadata_unchanged()

if __name__=='__main__':
    unittest.main(verbosity=2)
