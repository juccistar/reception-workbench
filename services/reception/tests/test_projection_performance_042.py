"""Projection comparisons on disposable old and new workspace schemas."""
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reception_service.repository import Repository, encoded
from reception_service.reception_state import ReceptionProjection
from reception_test_support.service import create_synthetic_service


class ProjectionComparison(unittest.TestCase):
    def test_message_facts_match_full_history_on_old_and_new_schemas(self):
        rng = random.Random(42)
        for old in (False, True):
            with self.subTest(old=old), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'workspace.sqlite3'
                repo = Repository(path, 'seller')
                if old:
                    repo.db.execute('DROP INDEX IF EXISTS message_cursor_idx')
                    # Drop the optional cursor index by its actual schema name.
                    for (name,) in repo.db.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql LIKE '%timestamp_ms%'").fetchall():
                        repo.db.execute('DROP INDEX "' + name.replace('"', '""') + '"')
                    repo.close()
                    repo = Repository(path, 'seller')
                    self.assertIsNone(repo.db.execute("SELECT name FROM sqlite_master WHERE name='entities_message_cursor'").fetchone())
                try:
                    for cycle in range(80):
                        cid = 'c' + str(cycle % 5)
                        mid = '%04d' % cycle
                        message = {'conversation_id': cid, 'message_id': mid,
                                   'role': rng.choice(['buyer', 'seller', 'system']),
                                   'timestamp_ms': rng.choice([None, -1, 0, 1, 10])}
                        repo.put('message', encoded([cid, mid]), message)
                        rows = sorted(repo.list('message', conversation_id=cid),
                                      key=lambda m: (m['timestamp_ms'] if m['timestamp_ms'] is not None else -1, m['message_id']))
                        buyers = [m for m in rows if m['role'] == 'buyer']
                        sellers = [m for m in rows if m['role'] == 'seller']
                        latest, undated = repo.automatic_source_facts(cid)
                        self.assertEqual(latest, buyers[-1] if buyers else None)
                        self.assertEqual(undated, any(m['timestamp_ms'] is None for m in buyers))
                        for accepted in (None, buyers[-1]['message_id'] if buyers else None, 'other'):
                            expected = bool(buyers) and (any(m['timestamp_ms'] is None for m in buyers)
                                or not sellers or sellers[-1]['timestamp_ms'] is None
                                or any(m['timestamp_ms'] is not None and m['timestamp_ms'] > sellers[-1]['timestamp_ms'] for m in buyers))
                            if buyers and accepted == buyers[-1]['message_id']:
                                expected = False
                            b, undated, latest, bid, s, last = repo.reply_pending_facts(cid)
                            actual = bool(b) and (undated or not s or last is None or latest is not None and latest > last)
                            if b and accepted == bid:
                                actual = False
                            self.assertEqual(bool(expected), bool(actual), (old, cycle, accepted, rows))
                finally:
                    repo.close()

    def test_projection_matches_legacy_after_state_and_message_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = create_synthetic_service(Path(tmp))
            try:
                conversation = service.repo.list('conversation')[0]
                cid = conversation['conversation_id']
                def legacy(projection, conversation):
                    messages = service._messages(conversation['conversation_id'])
                    buyer = [m for m in messages if m.get('role') == 'buyer']
                    seller = [m for m in messages if m.get('role') == 'seller']
                    result = bool(buyer) and (any(m.get('timestamp_ms') is None for m in buyer) or not seller
                        or seller[-1].get('timestamp_ms') is None or any(m.get('timestamp_ms') is not None
                        and m['timestamp_ms'] > seller[-1]['timestamp_ms'] for m in buyer))
                    if buyer and any(o.get('status') == 'accepted' and o.get('source_message_id') == buyer[-1]['message_id']
                                     for o in projection.operations[cid]):
                        result = False
                    return bool(result)
                for cycle in range(4):
                    conversation['human_owned'] = cycle == 1
                    service.repo.put('conversation', cid, conversation)
                    service._running = True
                    service._frozen_scope = {'seller_id': service.seller_id, 'mode': 'selected',
                                              'conversation_ids': [cid] if cycle != 2 else []}
                    if cycle == 3:
                        service.repo.put('operation', 'synthetic-unknown', {'operation_id': 'synthetic-unknown',
                            'conversation_id': cid, 'status': 'unknown'})
                    message = {'seller_id': service.seller_id, 'buyer_id': conversation['buyer_id'],
                               'conversation_id': cid, 'message_id': 'new-%d' % cycle, 'role': 'buyer',
                               'timestamp_ms': 9999999999999 + cycle, 'text': 'new', 'attachments': []}
                    service.repo.put('message', encoded([cid, message['message_id']]), message)
                    projection = ReceptionProjection(service)
                    with patch('reception_service.reception_state.now_ms', return_value=projection.stamp):
                        current = service._summary(conversation, projection)
                        with patch.object(ReceptionProjection, 'reply_pending', legacy):
                            previous = service._summary(conversation, projection)
                    self.assertEqual(current, previous)
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
