"""Current-only business notes and source fences, using isolated Demo databases."""
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.context import validate_reply
from reception_service.errors import ServiceError
from reception_service.repository import Repository, encoded
from reception_service.service import ReceptionService


class BusinessSupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.schedule = patch.object(ReceptionService, '_schedule', return_value=None)
        self.schedule.start()
        self.addCleanup(self.schedule.stop)
        self.service = ReceptionService(Path(self.temp.name))
        self.addCleanup(lambda: self.service.close())
        self.cid = self.service.repo.list('conversation')[0]['conversation_id']

    def wait(self, job_id):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            job = self.service.repo.get('job', job_id)
            if job['status'] not in ('queued', 'running'):
                return job
            time.sleep(.01)
        self.fail('Business note task timed out')

    def generate(self):
        s = self.service
        s.set_scope('selected', [self.cid], None)
        s.set_reception(True, auto_send=False)
        case = s.get_conversation(self.cid)['case']
        return s.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']})

    def test_current_note_is_unconditionally_in_automatic_context_and_is_a_seller_source(self):
        s = self.service
        before = s.repo.get('conversation', self.cid)
        s.save_conversation_supplement(self.cid, 'Price is 14.5 per piece, shipping not included.', None, 'save')
        context = s.get_context(before['case_id'])
        note = context['supplement']
        self.assertEqual(note['body'], 'Price is 14.5 per piece, shipping not included.')
        self.assertTrue(note['source_id'].startswith('business_note_'))
        validation = validate_reply(note['body'], [note['source_id']], context)
        self.assertTrue(validation['ready'])
        self.assertEqual(validation['sources'], [note['source_id']])
        self.assertNotIn('warnings', validation)
        self.assertEqual(before, s.repo.get('conversation', self.cid))
        self.assertNotIn('public', note)

    def test_overwrite_and_clear_keep_only_one_current_body_without_history_or_dedup_copies(self):
        s = self.service
        first = s.save_conversation_supplement(self.cid, 'OLD-NOTE-SENTINEL', None, 'first')
        second = s.save_conversation_supplement(self.cid, 'Latest note', first['revision'], 'second')
        self.assertEqual(s.repo.history('conversation_supplement', self.cid), [])
        records = s.repo.list('conversation_supplement') + s.repo.list('supplement_mutation')
        self.assertNotIn('OLD-NOTE-SENTINEL', str(records))
        self.assertEqual(len(s.repo.list('conversation_supplement')), 1)
        with self.assertRaises(ServiceError):
            s.save_conversation_supplement(self.cid, 'OLD-NOTE-SENTINEL', None, 'first')
        cleared = s.save_conversation_supplement(self.cid, '', second['revision'], 'clear')
        self.assertEqual(s.get_context(s.repo.get('conversation', self.cid)['case_id'])['supplement']['body'], '')
        s.close()
        self.service = ReceptionService(Path(self.temp.name))
        self.assertEqual(self.service.get_conversation(self.cid)['supplement'], cleared)

    def test_legacy_migration_removes_only_note_bodies_and_preserves_latest_and_receipts(self):
        s = self.service
        s.close()
        repo = Repository(Path(self.temp.name) / 'demo/workspace.sqlite3', 'demo-seller')
        with repo.transaction():
            old = {'conversation_id': self.cid, 'body': 'LEGACY-OLD-BODY', 'revision': 1, 'updated_at': 1}
            latest = dict(old, body='Latest business terms', revision=2)
            repo.put('conversation_supplement', self.cid, old, history=True)
            repo.put('conversation_supplement', self.cid, latest, history=True)
            request = {'conversation_id': self.cid, 'body': old['body'], 'expected_revision': None}
            repo.put('supplement_mutation', 'legacy', {'request': request, 'result': old})
            repo.put('operation', 'receipt', {'operation_id': 'receipt', 'status': 'unknown', 'case_id': 'retained', 'kind': 'send'})
            repo.db.execute("DELETE FROM entities WHERE kind='meta' AND id='supplement_storage'")
        repo.compact_supplements()
        self.assertEqual(repo.get('conversation_supplement', self.cid), latest)
        self.assertEqual(repo.history('conversation_supplement', self.cid), [])
        dedup = repo.get('supplement_mutation', 'legacy')
        self.assertEqual(dedup['request_hash'], hashlib.sha256(encoded(request).encode()).hexdigest())
        self.assertNotIn('LEGACY-OLD-BODY', str(dedup))
        self.assertEqual(repo.get('operation', 'receipt')['status'], 'unknown')
        repo.close()

    def test_changed_note_discards_inflight_old_generation(self):
        s = self.service
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(context, request_id, *, on_call_start=None, photo_inputs=None):
            if on_call_start:
                on_call_start()
            started.set()
            release.wait(3)
            return {'action': 'ask', 'reply': 'OLD-REPLY-SENTINEL', 'facts': {}, 'question_keys': [],
                    'conflicts': [], 'summary': '', 'new_case': False,
                    'procurement_key': context['case']['procurement_key'],
                    'reply_sources': [context['recent_messages'][-1]['message_id']]}
        with patch.object(s.integrations, 'generate', side_effect=generate):
            task = self.generate()
            self.assertTrue(started.wait(2))
            s.save_conversation_supplement(self.cid, 'New price 15 per piece.', None, 'new-price')
            release.set()
            job = self.wait(task['job_id'])
            self.assertIn(job['status'], ('stale', 'deferred', 'cancelled'))
            if job['status'] == 'deferred':
                self.assertEqual(job['retry_status'], 'replan')
        self.assertFalse(any(d['status'] == 'pending' and d['body'] == 'OLD-REPLY-SENTINEL' for d in s.repo.list('draft')))

    def test_note_change_invalidates_pending_reply_without_touching_manual_chat(self):
        s = self.service
        first = s.save_conversation_supplement(self.cid, '14.5 per piece.', None, 'price')
        task = self.generate()
        self.assertEqual(self.wait(task['job_id'])['status'], 'succeeded')
        draft = s.get_conversation(self.cid)['draft']
        self.assertTrue(draft['supplement_fingerprint'])
        s.save_conversation_supplement(self.cid, '15 per piece.', first['revision'], 'price-updated')
        with self.assertRaises(ServiceError):
            s._validate_send(draft['draft_id'], draft['revision'])
        self.assertIsNone(s.repo.get('chat_draft', self.cid))
        self.assertFalse(s.repo.get('conversation', self.cid)['human_owned'])


if __name__ == '__main__':
    unittest.main()
