import tempfile
import unittest
from pathlib import Path

from reception_service import unread
from reception_service.errors import ServiceError
from reception_service.repository import Repository, encoded
from reception_service.reception_state import ReceptionProjection
from reception_service.service import ReceptionService


class UnreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.repo = Repository(self.path, 'seller')
        self.conversation = {'conversation_id': 'c', 'unread_count': 9}
        self.repo.put('conversation', 'c', self.conversation)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def batch(self, entries):
        new = []
        for mid, stamp, role in entries:
            message = {'conversation_id': 'c', 'message_id': mid, 'timestamp_ms': stamp, 'role': role}
            key = encoded(['c', mid])
            if not self.repo.get('message', key):
                new.append(message)
            self.repo.put('message', key, message)
        unread.ingest(self.repo, self.conversation, new)
        self.repo.put('conversation', 'c', self.conversation)

    def test_baseline_pages_race_and_restart(self):
        unread.initialize(self.repo, self.conversation)
        self.batch([('base', 100, 'buyer')])
        self.batch([('older', 90, 'buyer'), ('base', 100, 'buyer')])
        self.assertEqual(self.conversation['unread_count'], 0)
        self.batch([('a', 101, 'buyer'), ('b', 101, 'buyer'), ('c', 102, 'buyer'), ('seller', 103, 'seller')])
        self.assertEqual(self.conversation['unread_count'], 3)
        observed = unread.reading(self.repo, 'c')['observed_sequence']
        self.batch([('d', 101, 'buyer'), ('a', 101, 'buyer')])
        result = unread.acknowledge(self.repo, self.conversation, 'c', observed)
        self.assertEqual(result['unread_count'], 1)
        unread.acknowledge(self.repo, self.conversation, 'c', observed)
        self.assertEqual(self.conversation['unread_count'], 1)
        with self.assertRaises(ServiceError):
            unread.acknowledge(self.repo, self.conversation, 'foreign', observed)
        with self.assertRaises(ServiceError):
            unread.acknowledge(self.repo, self.conversation, 'c', 999)
        self.repo.close()
        self.repo = Repository(self.path, 'seller')
        self.assertEqual(unread.reading(self.repo, 'c')['observed_sequence'], 7)
        self.assertEqual(self.repo.get('conversation', 'c')['unread_count'], 1)

    def test_legacy_migration_preserves_messages(self):
        message = {'conversation_id': 'c', 'message_id': 'old', 'timestamp_ms': 50, 'role': 'buyer'}
        self.repo.put('message', encoded(['c', 'old']), message, history=True)
        unread.initialize(self.repo, self.conversation)
        self.assertEqual(self.repo.get('conversation', 'c')['unread_count'], 0)
        self.assertEqual(len(self.repo.history('message', encoded(['c', 'old']))), 1)
        self.batch([('history', 49, 'buyer'), ('new', 51, 'buyer')])
        self.assertEqual(self.conversation['unread_count'], 1)


class ProjectionTests(unittest.TestCase):
    def test_frozen_scope_unknown_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = ReceptionService(Path(temporary))
            try:
                with service._lock:
                    conversations = service.repo.list('conversation')
                    first, second = conversations[:2]
                    cid = first['conversation_id']
                    service._running = True
                    service._frozen_scope = {'seller_id': service.seller_id, 'mode': 'all',
                                             'conversation_ids': [cid]}
                    service._scope = {'mode': 'all', 'conversation_ids': [], 'max_auto_replies': 10}
                    service.repo.put('operation', 'unknown', {'operation_id': 'unknown',
                                     'conversation_id': cid, 'status': 'unknown'})
                    before = service.repo.db.total_changes
                    projection = ReceptionProjection(service)
                    blocked = projection.status(first)
                    other = projection.status(second)
                    self.assertIn('unknown', [b['code'] for b in blocked['blockers']])
                    self.assertIn('out_of_scope', [b['code'] for b in other['blockers']])
                    self.assertEqual(service.repo.db.total_changes, before)
                    service._running = False
                    self.assertIn('paused', [b['code'] for b in ReceptionProjection(service).status(first)['blockers']])
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
