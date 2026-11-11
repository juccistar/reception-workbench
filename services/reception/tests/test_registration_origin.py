"""Immutable inquiry-message binding through the real core/outbox boundary."""
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reception_service.service import ReceptionService

class InquiryOriginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ReceptionService(Path(self.temp.name))

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def message(self, cid, mid, stamp):
        return {'seller_id': 'demo-seller', 'buyer_id': 'buyer-' + cid, 'conversation_id': cid,
                'message_id': mid, 'role': 'buyer', 'text': 'Product: ceramic mugs',
                'timestamp_ms': stamp, 'attachments': [], 'own': False}

    def test_partial_history_does_not_guess_original_time_and_completion_binds_once(self):
        info = {'conversation_id': 'origin-test', 'buyer_id': 'buyer-origin-test', 'customer_name': 'Synthetic'}
        self.service._ingest(info, [self.message('origin-test', 'newer', 2000)], has_more=True, next_cursor=2000)
        case = self.service.get_conversation('origin-test')['case']
        self.assertNotIn('_inquiry_source_id', self.service._registration_snapshot(case))
        self.service._ingest(info, [self.message('origin-test', 'original', 1000)], has_more=False, full=True)
        self.assertEqual(self.service._registration_snapshot(case)['_inquiry_source_id'], 'original')
        self.service._ingest(info, [self.message('origin-test', 'subsequent-import', 500)], has_more=False, full=True)
        self.assertEqual(self.service._registration_snapshot(case)['_inquiry_source_id'], 'original')
        self.assertNotIn('_inquiry_source_id', self.service.get_conversation('origin-test')['case'])

    def test_explicit_new_purchase_uses_its_trigger_not_earliest_conversation_message(self):
        detail = self.service.get_conversation('demo-emma')
        message = dict(detail['messages'][0], message_id='new-purchase-trigger', timestamp_ms=detail['messages'][0]['timestamp_ms'] + 5000)
        self.service._ingest(detail['conversation'], [message])
        case = self.service._new_case(detail['conversation'], 'second-purchase', source_message_id='new-purchase-trigger')
        self.assertEqual(self.service._registration_snapshot(case)['_inquiry_source_id'], 'new-purchase-trigger')
        wrong = self.service._new_case(detail['conversation'], 'invalid', source_message_id='demo-sofia-message-1')
        self.assertNotIn('_inquiry_source_id', self.service._registration_snapshot(wrong))

    def test_outbox_passes_immutable_origin_separately_from_mutable_fact_evidence(self):
        detail = self.service.get_conversation('demo-emma')
        case = detail['case']
        received = []
        def register(snapshot, previous, operation, *, authorization=None):
            if authorization is not None and not authorization():
                return {'status': 'rejected', 'submission_started': False}
            received.append(snapshot)
            return {'status': 'confirmed', 'record_id': 'synthetic-record'}
        self.service.integrations.register = register
        with self.service._lock, self.service.repo.transaction():
            self.service._queue_outbox(case)
        ack = self.service.call('run_outbox')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = next(j for j in self.service.list_jobs() if j['job_id'] == ack['job_id'])
            if job['status'] not in ('queued', 'running'):
                break
            threading.Event().wait(0.005)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(received[0]['_inquiry_source_id'], 'demo-emma-message-1')
        self.assertNotIn('inquiry_timestamp_ms', received[0])
