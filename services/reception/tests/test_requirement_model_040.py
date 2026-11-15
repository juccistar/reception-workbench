"""Exercise third-party model results through both real generation paths."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test_support'))
from reception_test_support.service import create_synthetic_service
from reception_service.integrations.live import LiveIntegration


class StubModel:
    def __init__(self):
        self.output = {}
        self.calls = 0

    def generate(self, purpose, instructions, payload, **kwargs):
        self.calls += 1
        if kwargs.get('on_call_start'):
            kwargs['on_call_start']()
        return {'output': copy.deepcopy(self.output), 'model_snapshot': {'stub': True}}


class RequirementModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = create_synthetic_service(Path(self.temp.name))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)
        self.owner = {'conversation_id': 'model-customer', 'buyer_id': 'model-buyer',
                      'seller_id': 'demo-seller', 'customer_name': 'Test'}
        self.message('model-message', 'I need blue glasses, 500 pairs.')
        self.case_id = self.service.get_conversation(self.owner['conversation_id'])['case']['case_id']
        self.model = StubModel()
        target = patch('reception_service.integrations.live.target')
        target.start()
        self.addCleanup(target.stop)
        live = LiveIntegration.__new__(LiveIntegration)
        live.settings = {}
        live._query_state = threading.local()
        live._model_router = self.model
        live.resolve_identity = lambda seller_id: None
        self.service.integrations.generate = live.generate
        self.service.integrations.generate_manual = live.generate_manual

    def message(self, mid, text):
        self.service._ingest(self.owner, [{**self.owner, 'message_id': mid, 'role': 'buyer',
            'text': text, 'timestamp_ms': int(time.time() * 1000), 'attachments': []}])

    def wait(self, ack):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.repo.get('job', ack['job_id'])
            if job['status'] not in ('queued', 'running'):
                self.assertEqual(job['status'], 'succeeded', job)
                return
            threading.Event().wait(.01)
        self.fail('job timed out')

    def test_auto_then_manual_apply_grounded_chinese_deltas(self):
        mid = 'model-message'
        self.model.output = {'action': 'ask', 'reply_messages': [{'body': 'Noted.', 'reply_sources': [mid]}],
            'requirement_changes': [{'action': 'create', 'case_ref': 'new:1', 'trigger_evidence_ids': [mid],
                'set_fields': {'product': {'value': '蓝色眼镜', 'evidence_ids': [mid],
                    'source_excerpts': {mid: 'blue glasses'}}}}], 'reply_case_refs': ['new:1']}
        case = self.service.repo.get('case', self.case_id)
        self.wait(self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']}))
        case = self.service.repo.get('case', self.case_id)
        self.assertEqual(case['requirements']['product']['value'], '蓝色眼镜')
        self.assertEqual(len(case['requirements']), 13)
        self.message('model-message-2', 'Change to 800 pairs.')
        mid = 'model-message-2'
        self.model.output = {'reply_messages': [{'body': 'Noted.', 'reply_sources': [mid]}],
            'requirement_changes': [{'action': 'update', 'case_ref': 'c1', 'trigger_evidence_ids': [mid],
                'set_fields': {'quantity': {'value': '800副', 'evidence_ids': [mid],
                    'source_excerpts': {mid: '800 pairs'}}}}]}
        self.wait(self.service.call('generate_manual_suggestion', {'conversation_id': self.owner['conversation_id'],
            'case_id': self.case_id, 'client_request_id': 'manual-model'}))
        case = self.service.repo.get('case', self.case_id)
        self.assertEqual(case['requirements']['quantity']['value'], '800副')
        self.assertEqual(case['requirements']['product']['value'], '蓝色眼镜')
        self.assertEqual(case['requirements_revision'], 2)
        self.assertEqual(self.model.calls, 2)
