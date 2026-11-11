import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.repository import Repository, encoded
from reception_service.service import ReceptionService
from reception_service.storage_guard import StorageError
from reception_service.workspace_lock import WorkspaceLock, MARKER, track_native_child, release_native_child
from reception_service import unread


class ReadingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = ReceptionService(self.root)
        self.info = {'conversation_id': 'reading-customer', 'buyer_id': 'reading-buyer', 'customer_name': 'Synthetic Reader'}
        self.cid = self.info['conversation_id']
        self.ingest([self.message('base', 100)])

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def message(self, mid, stamp, role='buyer', text='synthetic text'):
        return dict(message_id=mid, timestamp_ms=stamp, role=role, text=text, attachments=[],
                    conversation_id=self.cid, seller_id='demo-seller', buyer_id=self.info['buyer_id'])

    def ingest(self, messages, **options):
        self.service._ingest(self.info, messages, **options)

    def detail(self):
        return self.service.get_conversation(self.cid)

    def count(self):
        return self.detail()['conversation']['unread_count']

    def test_batch_duplicates_corrections_sellers_and_reads_have_separate_semantics(self):
        batch = [self.message(mid, 101) for mid in ('a', 'b', 'c')]
        self.ingest(batch)
        self.assertEqual(self.count(), 3)
        self.ingest(batch)
        self.ingest([self.message('b', 101, text='corrected')])
        self.ingest([self.message('seller', 102, role='seller')])
        self.assertEqual(self.count(), 3)
        detail = self.detail()
        case_before = detail['case']
        source_before = self.service._source_baseline.copy()
        self.service.mark_messages_read(self.cid, 'b', detail['reading']['observed_sequence'])
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.detail()['case'], case_before)
        self.assertEqual(self.service._source_baseline, source_before)
        self.assertFalse(self.detail()['conversation']['human_owned'])

    def test_ack_race_same_timestamp_and_reverse_ack(self):
        self.ingest([self.message('b', 101), self.message('d', 101)])
        seq = self.detail()['reading']['observed_sequence']
        self.ingest([self.message('a', 101)])  # chronologically earlier but received later
        self.service.mark_messages_read(self.cid, 'd', seq)
        self.assertEqual(self.count(), 1)
        self.service.mark_messages_read(self.cid, 'b', seq)
        self.assertEqual(self.count(), 1)
        current = self.detail()['reading']['observed_sequence']
        self.service.mark_messages_read(self.cid, 'd', current)
        self.assertEqual(self.count(), 0)

    def test_correction_during_ack_does_not_clear_changed_boundary_or_hidden_message(self):
        self.ingest([self.message('a', 101), self.message('b', 102)])
        seq = self.detail()['reading']['observed_sequence']
        self.ingest([self.message('a', 104, text='moved by correction')])
        with self.assertRaises(ServiceError):
            self.service.mark_messages_read(self.cid, 'a', seq)
        self.service.mark_messages_read(self.cid, 'b', seq)
        self.assertEqual(self.count(), 1)
        self.assertFalse(self.service.repo.get('unread_receipt', encoded([self.cid, 'a']))['read'])

    def test_future_seller_boundary_and_invalid_sequences_rejected_without_writes(self):
        self.ingest([self.message('a', 101)])
        seq = self.detail()['reading']['observed_sequence']
        self.ingest([self.message('seller', 105, role='seller')])
        for boundary, observed in [('seller', seq), ('missing', seq), ('a', -1), ('a', True), ('a', 99999)]:
            with self.subTest(boundary=boundary, observed=observed):
                before = list(self.service.repo.db.execute('SELECT kind,id,data FROM entities ORDER BY kind,id'))
                with self.assertRaises(ServiceError):
                    self.service.mark_messages_read(self.cid, boundary, observed)
                self.assertEqual(list(self.service.repo.db.execute('SELECT kind,id,data FROM entities ORDER BY kind,id')), before)

    def test_history_pages_dont_flood_and_newer_messages_are_not_lost_in_history_fill(self):
        self.ingest([self.message('old1', 99), self.message('old2', 98)], history=True)
        self.assertEqual(self.count(), 0)
        self.ingest([self.message('new1', 102)], history=True)
        self.ingest([self.message('new2', 101), self.message('old3', 97)], history=True)
        self.assertEqual(self.count(), 2)
        unread.finish_sync(self.service.repo, self.cid)
        # Explicit conservative fallback: an unknown old/equal timestamp is history.
        self.ingest([self.message('late-history', 100), self.message('ambiguous-equal', 102)])
        self.assertEqual(self.count(), 2)

    def test_confirmed_empty_history_receives_first_new_message(self):
        empty = dict(self.info, conversation_id='empty-customer')
        self.service._ingest(empty, [], full=True, has_more=False)
        self.service._park_empty_history(empty['conversation_id'], {'empty_target': 100, 'empty_version': 1})
        self.service._ingest(empty, [dict(self.message('first-new', 101), conversation_id=empty['conversation_id'])])
        self.assertEqual(self.service.get_conversation(empty['conversation_id'])['conversation']['unread_count'], 1)

    def test_reading_survives_restart_and_authority_does_not(self):
        self.ingest([self.message('a', 101), self.message('b', 102)])
        seq = self.detail()['reading']['observed_sequence']
        self.service.mark_messages_read(self.cid, 'a', seq)
        self.service.close()
        self.assertTrue((self.root / MARKER).is_file())
        self.service = ReceptionService(self.root)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.detail()['reading']['observed_sequence'], seq)
        self.assertFalse(self.service.get_snapshot()['reception']['running'])
        self.assertFalse((self.root / MARKER).exists())

    def test_duplicate_ids_in_one_batch_follow_final_validated_role(self):
        self.ingest([self.message('seller-final', 101), self.message('seller-final', 101, role='seller')])
        self.assertEqual(self.count(), 0)
        self.ingest([self.message('buyer-final', 102, role='seller'), self.message('buyer-final', 102)])
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.service.repo.get('message', encoded([self.cid, 'buyer-final']))['role'], 'buyer')

    def test_invalid_ingest_rolls_back_observation_and_count(self):
        before = self.detail()
        with self.assertRaises(ServiceError):
            self.ingest([self.message('a', 101), dict(self.message('foreign', 102), buyer_id='another')])
        self.assertEqual(self.detail()['reading'], before['reading'])
        self.assertEqual(self.count(), 0)
        self.assertIsNone(self.service.repo.get('message', encoded([self.cid, 'a'])))


class WorkspaceLifecycleTests(unittest.TestCase):
    def test_active_service_excludes_second_instance_and_backup_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ReceptionService(root)
            try:
                with self.assertRaises(OSError):
                    ReceptionService(root)
                self.assertFalse((root / MARKER).exists())
            finally:
                service.close()
            with WorkspaceLock(root) as lock:
                self.assertTrue(lock.was_clean)

    def test_preflight_future_seller_does_not_mutate_control_or_seller(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / 'live/control.sqlite3'
            Repository(control).close()
            seller = root / 'live/sellers/synthetic/workspace.sqlite3'
            Repository(seller, 'synthetic').close()
            with closing(sqlite3.connect(seller)) as db, db:
                db.execute('PRAGMA user_version=999')
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (control, seller)}
            with self.assertRaises(StorageError):
                ReceptionService(root, mode='live')
            self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}, before)
            self.assertFalse((root / MARKER).exists())
            with WorkspaceLock(root):
                pass

    def test_future_schema_rejection_preserves_existing_clean_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ReceptionService(root).close()
            with closing(sqlite3.connect(root / 'demo/control.sqlite3')) as db, db:
                db.execute('PRAGMA user_version=999')
            marker = root / MARKER
            before = (marker.read_bytes(), marker.stat().st_mtime_ns)
            with self.assertRaises(StorageError):
                ReceptionService(root)
            self.assertEqual((marker.read_bytes(), marker.stat().st_mtime_ns), before)

    def test_failed_initialization_releases_lock_without_clean_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('reception_service.service.build_integrations', side_effect=RuntimeError('synthetic init failure')):
                with self.assertRaises(RuntimeError):
                    ReceptionService(root)
            with WorkspaceLock(root) as lock:
                self.assertFalse(lock.was_clean)

    def test_unreaped_native_child_prevents_clean_marker(self):
        import threading
        observed = threading.Event()
        class Child:
            exited = False
            def poll(self):
                observed.set()
                return 0 if self.exited else None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ReceptionService(root)
            child = Child()
            errors = []
            def close():
                try:
                    service.close()
                except Exception as error:
                    errors.append(error)
            track_native_child(child)
            closer = threading.Thread(target=close, daemon=True)
            try:
                closer.start()
                self.assertTrue(observed.wait(5))
                self.assertTrue(closer.is_alive())
                self.assertFalse((root / MARKER).exists())
                with self.assertRaises(OSError):
                    WorkspaceLock(root).acquire()
            finally:
                child.exited = True
                release_native_child(child)
                closer.join(10)
            self.assertFalse(closer.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue((root / MARKER).exists())


if __name__ == '__main__':
    unittest.main()
