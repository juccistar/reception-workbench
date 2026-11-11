import tempfile
import unittest
from pathlib import Path

from reception_service.errors import ServiceError
from reception_service.service import ReceptionService


class ContinuityServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ReceptionService(Path(self.temp.name))
        self.addCleanup(self.service.close)
        self.conversation = self.service.repo.list('conversation')[0]
        self.case = self.service.repo.get('case', self.conversation['case_id'])

    def test_unlimited_scope_and_usage_projection(self):
        self.service.set_scope('all', [], 100)
        self.service._budget_used = 150
        self.service._ai_budget_used = 200
        snapshot = self.service.get_snapshot()
        self.assertIsNone(snapshot['reception']['scope']['max_auto_replies'])
        self.assertEqual(snapshot['reception']['budget'], {'limit': None, 'used': 150, 'remaining': None,
                         'ai_used': 200, 'ai_remaining': None, 'unlimited': True})

    def test_assistance_result_is_cas_and_idempotent(self):
        with self.service._lock, self.service.repo.transaction():
            task = self.service._create_assistance(self.case, None, None, 'model_request', [])
        args = dict(task_id=task['task_id'], expected_revision=1, result='核实结果',
                    result_public=False, schedule_followup=False, client_mutation_id='synthetic-1')
        first = self.service.save_assistance_result(**args)
        self.assertEqual(first, self.service.save_assistance_result(**args))
        self.assertEqual(first['task']['revision'], 2)
        self.assertFalse(self.service.repo.get('conversation', self.conversation['conversation_id'])['human_owned'])
        with self.assertRaises(ServiceError):
            self.service.save_assistance_result(**dict(args, result='不同结果'))
        with self.assertRaises(ServiceError):
            self.service.save_assistance_result(**dict(args, client_mutation_id='synthetic-2'))
        self.assertEqual(self.service.list_assistance_tasks(conversation_id=self.conversation['conversation_id'])['items'][0]['status'], 'resolved')

    def test_retry_waits_until_due_and_does_not_block_other_sources(self):
        source = {'message_id': 'synthetic-source'}
        job = {'job_id': 'synthetic-job', 'kind': 'generate_draft', 'case_id': self.case['case_id'],
               'conversation_id': self.case['conversation_id'], 'source_message_id': source['message_id'],
               'source_case_revision': self.case['revision'], 'status': 'deferred', 'retry_at': 9999999999999,
               'created_at': 0}
        self.service.repo.put('job', job['job_id'], job)
        self.assertTrue(self.service._generation_attempted(self.case['conversation_id'], self.case, source))
        self.assertFalse(self.service._generation_attempted(self.case['conversation_id'], self.case, {'message_id': 'new-source'}))
        job['retry_at'] = 0
        self.service.repo.put('job', job['job_id'], job)
        self.assertFalse(self.service._generation_attempted(self.case['conversation_id'], self.case, source))
