"""Receipt queries must not block the JSON-lines caller's pause path."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from reception_service.repository import now_ms
from reception_service.service import ReceptionService


class ReconcileJobTests(unittest.TestCase):
    def test_slow_authority_query_is_queued_and_pause_remains_responsive(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ReceptionService(Path(folder), 'demo')
            started = threading.Event()
            release = threading.Event()
            try:
                detail = service.get_conversation('demo-sofia')
                case = detail['case']
                draft = service.save_draft(case['case_id'], 'Thank you. I will review your request.', None, 'synthetic-save')
                draft['status'] = 'unknown'
                service.repo.put('draft', draft['draft_id'], draft)
                operation_id = 'synthetic-blocked-receipt-query'
                service.repo.put('operation', operation_id, {
                    'operation_id': operation_id, 'case_id': case['case_id'], 'draft_id': draft['draft_id'],
                    'case_revision': case['revision'], 'draft_revision': draft['revision'],
                    'conversation_id': case['conversation_id'], 'source_message_id': draft['source_message_id'],
                    'status': 'unknown', 'submission_started': True, 'message_id': None,
                    'message': 'Synthetic original send outcome unknown', 'updated_at': now_ms(),
                })
                def reconcile(_operation_id):
                    started.set()
                    if not release.wait(3):
                        raise AssertionError('Synthetic authority query was never released')
                    return {'status': 'unknown', 'message': 'No authoritative receipt'}
                service.integrations.reconcile_operation = reconcile
                before = time.monotonic()
                ack = service.call('reconcile_operation', {'operation_id': operation_id})
                self.assertIn('job_id', ack)
                self.assertLess(time.monotonic() - before, 0.5)
                self.assertTrue(started.wait(1))
                before = time.monotonic()
                paused = service.call('set_reception', {'enabled': False, 'auto_send': False})
                self.assertFalse(paused['reception']['running'])
                self.assertLess(time.monotonic() - before, 0.5)
                release.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = service.repo.get('job', ack['job_id'])
                    if job['status'] not in ('queued', 'running'):
                        break
                    threading.Event().wait(0.01)
                self.assertNotIn(job['status'], ('queued', 'running'))
                self.assertEqual(service.repo.get('operation', operation_id)['status'], 'unknown')
            finally:
                release.set()
                service.close()


if __name__ == '__main__':
    unittest.main()
