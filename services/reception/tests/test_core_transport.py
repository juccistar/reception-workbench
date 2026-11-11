"""Local demo and JSON-lines checks, with isolated temporary storage."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reception_service.service import ReceptionService


class LocalDemoTests(unittest.TestCase):
    def wait_job(self, service, ack):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = next(j for j in service.list_jobs() if j['job_id'] == ack['job_id'])
            if job['status'] not in ('queued', 'running'):
                return job
            threading.Event().wait(0.005)
        self.fail('demo job did not complete')

    def test_real_local_demo_preserves_sample_logo_body_and_marks_simulation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReceptionService(Path(directory))
            try:
                connection = self.wait_job(service, service.call('check_connection'))
                self.assertEqual(connection['status'], 'succeeded', connection)
                detail = service.get_conversation('demo-emma')
                ack = service.call('generate_draft', {'case_id': detail['case']['case_id'], 'expected_case_revision': detail['case']['revision']})
                job = self.wait_job(service, ack)
                self.assertEqual(job['status'], 'succeeded', job)
                detail = service.get_conversation('demo-emma')
                self.assertIn('sample', detail['draft']['body'].lower())
                self.assertIn('logo', detail['draft']['body'].lower())
                self.assertIn('charge', detail['draft']['body'].lower())
                self.assertNotIn('do you want', detail['draft']['body'].lower())
                self.assertTrue(detail['draft']['validation']['ready'], detail['draft'])
                self.assertNotEqual(detail['case']['state'], 'needs_human')
                self.assertTrue(all(check['status'] == 'demo' for name, check in service.get_snapshot()['checks'].items()
                                    if name in ('runtime', 'identity', 'messages')))
                preview = self.wait_job(service, service.call('preview_send', {'draft_id': detail['draft']['draft_id'], 'expected_revision': detail['draft']['revision']}))
                self.assertEqual(preview['status'], 'succeeded', preview)
                self.assertEqual(service.get_snapshot()['checks']['send_preview']['status'], 'demo')
            finally:
                service.close()

    def test_demo_state_restart_and_no_fake_feishu_success(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReceptionService(Path(directory))
            service.inject_demo('demo-emma', '合成的新消息\n第二行')
            check = self.wait_job(service, service.call('check_feishu'))
            self.assertEqual(check['status'], 'failed')
            self.assertNotEqual(service.get_snapshot()['checks']['feishu']['status'], 'passed')
            service.close()
            service = ReceptionService(Path(directory))
            try:
                self.assertEqual(service.get_messages('demo-emma')['items'][-1]['text'], '合成的新消息\n第二行')
                self.assertEqual(len(service.get_snapshot()['conversations']), 3)
            finally:
                service.close()

    def test_utf8_json_lines_and_eof_close_without_production_data(self):
        with tempfile.TemporaryDirectory() as directory:
            run_path = Path(__file__).resolve().parents[1] / 'run.py'
            requests = [
                {'id': 1, 'method': 'get_snapshot'},
                {'id': 2, 'method': 'inject_demo', 'params': {'conversation_id': 'demo-emma', 'text': '合成客户消息\nLine two'}},
                {'id': 3, 'method': 'get_messages', 'params': {'conversation_id': 'demo-emma'}},
                {'id': 4, 'method': 'not_a_method'},
            ]
            result = subprocess.run([sys.executable, '-I', '-u', str(run_path), '--mode', 'demo', '--data-dir', directory],
                                    input='\n'.join(json.dumps(r, ensure_ascii=False) for r in requests) + '\n',
                                    text=True, encoding='utf-8', capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in result.stdout.splitlines()]
            responses = {record['id']: record for record in records if 'id' in record}
            self.assertEqual(len(responses), 4)
            self.assertFalse(responses[1]['result']['reception']['auto_send'])
            self.assertEqual(responses[3]['result']['items'][-1]['text'], '合成客户消息\nLine two')
            self.assertEqual(responses[4]['error']['code'], 'unknown_method')
            self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
