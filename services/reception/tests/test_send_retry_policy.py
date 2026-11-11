import unittest

from reception_service.send_retry_policy import decision


class MemoryRepo:
    def __init__(self):
        self.rows = {'job': [], 'operation': [], 'continuity_plan': []}

    def list(self, kind, **filters):
        return [r for r in self.rows[kind] if all(r.get(k) == v for k, v in filters.items())]

    def get(self, kind, key):
        return next((r for r in self.rows[kind] if r.get('plan_id') == key), None)

    def attempt(self, source='source', code='backend_timeout', phase='reply', status='rejected', stamp=1000):
        number = len(self.rows['operation']) + 1
        job_id = str(number)
        self.rows['job'].append(dict(job_id=job_id, conversation_id='c', automatic=True))
        self.rows['operation'].append(dict(job_id=job_id, kind='send', seller_id='s', conversation_id='c',
                                          source_message_id=source, status=status, submission_started=False,
                                          error_code=code, updated_at=stamp,
                                          purpose='continuity' if phase != 'reply' else None,
                                          intent={'continuity_phase': phase}))


class SendRetryPolicyTest(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepo()

    def get(self, source='source', phase='reply'):
        return decision(self.repo, 's', 'c', source, phase)

    def test_transient_backoff_and_limit_survive_new_drafts(self):
        self.repo.attempt()
        self.assertEqual(self.get(), ('waiting', 1, 6000))
        self.repo.attempt(stamp=8000)
        self.assertEqual(self.get(), ('waiting', 2, 23000))
        self.repo.attempt(stamp=25000)
        self.assertEqual(self.get(), ('exhausted', 3, None))
        self.assertEqual(self.get('new_source'), ('ready', 0, None))

    def test_unclassified_and_legacy_attempts_are_blocked(self):
        for _ in range(9):
            self.repo.attempt(code='adapter_error')
        self.assertEqual(self.get(), ('exhausted', 9, None))
        self.assertEqual(self.get('new_source'), ('ready', 0, None))

    def test_unknown_and_continuation_are_fenced(self):
        self.repo.attempt(phase='final', status='unknown')
        self.assertEqual(self.get(phase='final'), ('blocked', 1, None))
        self.assertEqual(self.get(), ('ready', 0, None))

    def test_guard_diagnostics_are_coded_and_sanitized(self):
        from reception_service.integrations.live import LiveIntegration
        result = LiveIntegration._send_diagnostics(dict(error_code='ready_fields_invalid', stage='snapshot',
            diagnostics={'reply': 'secret', 'readyToken': 'secret', 'exit_code': 2}))
        self.assertEqual(result['error_code'], 'ready_fields_invalid')
        self.assertEqual(result['stage'], 'snapshot')
        self.assertEqual(result['diagnostics'], {'exit_code': 2, 'code': 'ready_fields_invalid', 'stage': 'snapshot'})

    def test_failed_presend_job_blocks_regenerated_draft(self):
        self.repo.rows['job'].append(dict(job_id='failed', conversation_id='c', automatic=True,
                                          kind='automatic_send', source_message_id='source', status='failed'))
        self.assertEqual(self.get(), ('blocked', 0, None))


if __name__ == '__main__':
    unittest.main()
