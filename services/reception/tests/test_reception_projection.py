"""Synthetic, offline tests of the per-customer read model."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reception_service.reception_state import ReceptionProjection
from reception_service.repository import encoded, now_ms
from reception_service.service import ReceptionService


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        scheduler = patch.object(ReceptionService, '_schedule')
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.s = ReceptionService(Path(self.tmp.name))
        self.addCleanup(self.s.close)
        self.c = self.s.repo.list('conversation')[0]
        self.cid = self.c['conversation_id']
        self.case = self.s.repo.get('case', self.c['case_id'])
        self.s._running = True
        self.s._frozen_scope = {'seller_id': self.s.seller_id, 'mode': 'selected', 'conversation_ids': [self.cid]}
        self.s.settings['merge_seconds'] = 0
        self.s._scope['max_auto_replies'] = 10
        self.c['last_buyer_at'] = now_ms() - 10000
        self.s.repo.put('conversation', self.cid, self.c)
        self.message = {'message_id': 'synthetic', 'conversation_id': self.cid, 'buyer_id': self.c['buyer_id'],
                        'seller_id': self.s.seller_id, 'role': 'buyer', 'timestamp_ms': now_ms() + 1000,
                        'text': 'Please quote 25 widgets', 'attachments': []}
        self.s.repo.put('message', encoded([self.cid, 'synthetic']), self.message)

    def status(self):
        return ReceptionProjection(self.s).status(self.c)

    def codes(self):
        return {item['code'] for item in self.status()['blockers']}

    def put_job(self, kind, status='queued', **kw):
        job = {'job_id': kind, 'kind': kind, 'status': status, 'conversation_id': self.cid,
               'case_id': self.case['case_id'], 'automatic': True, 'created_at': now_ms(), **kw}
        self.s.repo.put('job', job['job_id'], job)
        return job

    def test_scope_pause_ownership_and_closed(self):
        self.assertEqual(self.status()['state'], 'ready')
        self.s._scope['mode'] = 'all'
        self.s._frozen_scope['conversation_ids'] = []
        self.assertIn('out_of_scope', self.codes())
        self.assertEqual(self.status()['owner'], 'none')
        self.s._frozen_scope['mode'] = 'incoming'
        self.s._started_at_ms = self.message['timestamp_ms'] - 1
        self.assertNotIn('out_of_scope', self.codes())
        self.s._running = False
        self.assertIn('paused', self.codes())
        self.s._running = True
        self.s._manual_only = True
        self.assertIn('paused', self.codes())
        self.s._manual_only = False
        self.case['state'] = 'closed'
        self.s.repo.put('case', self.case['case_id'], self.case)
        self.assertIn('closed', self.codes())
        self.assertEqual(self.status()['owner'], 'none')
        self.c['human_owned'] = True
        self.assertEqual(self.status()['owner'], 'human')
        self.assertIn('human_owned', self.codes())

    def test_operations_and_activity_coexist_across_cases(self):
        self.put_job('confirm_send', automatic=False)
        self.s.repo.put('operation', 'old', {'operation_id': 'old', 'conversation_id': self.cid,
                                             'case_id': 'previous', 'status': 'unknown'})
        self.s.repo.put('operation', 'pending', {'operation_id': 'pending', 'conversation_id': self.cid,
                                                 'case_id': 'previous', 'status': 'prepared'})
        status = self.status()
        self.assertEqual(status['activity']['kind'], 'confirm_send')
        self.assertEqual({b['code'] for b in status['blockers']} & {'unknown', 'send_pending'}, {'unknown', 'send_pending'})

    def test_usage_never_creates_quota_blockers(self):
        self.s._auto_send = True
        self.s._scope['max_auto_replies'] = 2
        self.s._ai_budget_used = 1
        self.s._budget_used = 1
        self.put_job('generate_draft', external_started=False)
        self.put_job('automatic_send')
        self.assertFalse({'ai_quota', 'send_quota'} & self.codes())
        self.put_job('generate_draft', external_started=True)
        self.put_job('automatic_send', status='running')
        self.s.repo.put('operation', 'sending', {'operation_id': 'sending', 'conversation_id': self.cid,
                                                  'case_id': self.case['case_id'], 'status': 'sending'})
        self.assertNotIn('ai_quota', self.codes())
        self.assertNotIn('send_quota', self.codes())
        self.s._budget_used = 2
        self.assertNotIn('send_quota', self.codes())

    def test_drafts_generation_merge_and_waiting(self):
        draft = {'draft_id': 'd', 'conversation_id': self.cid, 'case_id': 'old', 'status': 'pending',
                 'origin': 'manual', 'source_message_id': 'old'}
        self.s.repo.put('draft', 'd', draft)
        self.assertEqual(self.status()['state'], 'ready')
        draft['case_id'] = self.case['case_id']
        self.s.repo.put('draft', 'd', draft)
        self.assertIn('draft_review', self.codes())
        draft['status'] = 'accepted'
        draft['source_message_id'] = 'synthetic'
        self.s.repo.put('draft', 'd', draft)
        self.assertEqual(self.status()['state'], 'handled')
        self.c['last_buyer_at'] = now_ms()
        self.s.settings['merge_seconds'] = 60
        self.assertIn('merge', self.codes())
        self.s.settings['merge_seconds'] = 0
        self.s._frozen_scope['mode'] = 'incoming'
        self.s._scope['mode'] = 'incoming'
        self.s._started_at_ms = self.message['timestamp_ms'] + 1
        self.assertEqual(self.status()['state'], 'waiting')

    def test_capabilities_body_and_post_quote(self):
        self.s.mode = 'live'
        self.s._started_at_ms = self.message['timestamp_ms'] - 1
        self.s._auto_send = True
        self.s.checks['send_preview']['status'] = 'failed'
        self.assertTrue({'capability', 'ai_capability'} <= self.codes())
        self.assertNotIn('send_capability', self.codes())
        self.s.checks['messages']['status'] = self.s.checks['ai']['status'] = 'passed'
        self.s.checks['send_preview']['status'] = 'not_checked'
        self.s.repo.put('message_sync_task', self.cid, {'pending': True, 'activity_required': True})
        self.assertIn('body_pending', self.codes())
        self.s.repo.put('message_sync_task', self.cid, {'pending': False})
        self.case['state'] = 'waiting_quote'
        self.case['facts'] = {'product': {'evidence_ids': ['synthetic']}}
        self.s.repo.put('case', self.case['case_id'], self.case)
        self.assertEqual(self.status()['state'], 'handled')

    def test_generation_attempts_and_waiting_activity(self):
        job = self.put_job('generate_draft', status='cancelled', source_message_id='synthetic', external_started=False)
        self.assertEqual(self.status()['state'], 'ready')
        job['external_started'] = True
        self.s.repo.put('job', job['job_id'], job)
        self.assertEqual(self.status()['state'], 'ready')
        self.s.mode = 'live'
        self.s._started_at_ms = self.message['timestamp_ms'] + 1
        self.s.checks['messages']['status'] = self.s.checks['ai']['status'] = 'passed'
        self.s.repo.put('message_activity', self.cid, {'version': 2, 'latest_timestamp': 123})
        self.s.repo.put('message_sync_task', self.cid, {'pending': True, 'wait_reason': 'waiting_activity',
                        'no_body_confirmed': True, 'empty_version': 2, 'empty_target': 123})
        self.assertEqual(self.status()['state'], 'waiting')
        self.assertNotIn('body_pending', self.codes())

    def test_send_receipt_charged_before_job_finishes(self):
        self.s._auto_send = True
        self.s._scope['max_auto_replies'] = 2
        self.s._budget_used = 1
        self.put_job('automatic_send', status='running', draft_id='d', started_at=100)
        self.s.repo.put('operation', 'receipt', {'operation_id': 'receipt', 'draft_id': 'd',
                        'conversation_id': self.cid, 'case_id': self.case['case_id'],
                        'status': 'accepted', 'updated_at': 101})
        self.assertNotIn('send_quota', self.codes())
        self.s.repo.put('operation', 'receipt', {'operation_id': 'receipt', 'draft_id': 'd',
                        'conversation_id': self.cid, 'case_id': self.case['case_id'],
                        'status': 'accepted', 'updated_at': 99})
        self.assertNotIn('send_quota', self.codes())

    def test_reads_do_not_write_or_enqueue_and_batch_lists(self):
        before = self.s.repo.db.total_changes
        state = copy.deepcopy((self.s._scope, self.s._frozen_scope, self.s._budget(), self.s._queue.qsize()))
        original = self.s.repo.list
        with patch.object(self.s.repo, 'list', wraps=original) as listing, patch.object(self.s, '_enqueue') as enqueue:
            self.s.get_snapshot()
            for kind in ('job', 'draft', 'operation'):
                calls = [c for c in listing.call_args_list if c.args[0] == kind and c.kwargs]
                self.assertEqual(calls, [], (kind, calls))
            self.s.get_conversation(self.cid)
            enqueue.assert_not_called()
        self.assertEqual(self.s.repo.db.total_changes, before)
        self.assertEqual((self.s._scope, self.s._frozen_scope, self.s._budget(), self.s._queue.qsize()), state)

    def test_enqueue_confirmation_attribution(self):
        self.s.repo.put('draft', 'd', {'draft_id': 'd', 'case_id': self.case['case_id'], 'conversation_id': self.cid})
        self.s.repo.put('confirmation', 'confirm', {'confirmation_id': 'confirm', 'draft_id': 'd', 'conversation_id': self.cid})
        with patch.object(self.s._queue, 'put'):
            ack = self.s._enqueue('confirm_send', {'confirmation_id': 'confirm'})
        job = self.s.repo.get('job', ack['job_id'])
        self.assertEqual((job['conversation_id'], job['case_id']), (self.cid, self.case['case_id']))
        self.assertEqual(self.status()['activity']['job_id'], ack['job_id'])


if __name__ == '__main__':
    unittest.main()
