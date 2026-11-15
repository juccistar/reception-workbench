"""Exercise incoming through the actual demo facade rather than MockFacade."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test_support'))
from reception_test_support.demo import DemoIntegration
from reception_test_support.service import create_synthetic_service


class DemoIncomingTests(unittest.TestCase):
    def intent(self):
        return {'seller_id': 'demo-seller', 'buyer_id': 'buyer-test', 'conversation_id': 'demo-incoming',
                'source_message_id': 'new-1', 'draft_id': 'draft-1', 'case_id': 'case-1',
                'operation_id': 'operation-1', 'reply': 'Thank you.', 'send_mode': 'automatic',
                'budget_reserved': True, 'source_timestamp_ms': 2000,
                'frozen_scope': {'mode': 'incoming', 'seller_id': 'demo-seller', 'started_at_ms': 1000, 'conversation_ids': []}}

    def test_valid_demo_incoming_uses_the_same_scope_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            facade = DemoIntegration(Path(temporary), {})
            result = facade.send(self.intent(), lambda: True)
            self.assertEqual(result['status'], 'accepted')
            self.assertTrue(result['simulated'])

    def test_demo_incoming_rejects_wrong_account_time_and_revocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            facade = DemoIntegration(Path(temporary), {})
            for index, changes in enumerate(({'source_timestamp_ms': 999}, {'source_timestamp_ms': None},
                    {'source_timestamp_ms': True}, {'frozen_scope': {**self.intent()['frozen_scope'], 'seller_id': 'other'}},
                    {'budget_reserved': False})):
                intent = {**self.intent(), **changes, 'operation_id': f'negative-{index}'}
                self.assertEqual(facade.send(intent, lambda: True)['status'], 'rejected')
            self.assertEqual(facade.send(self.intent(), lambda: False)['status'], 'rejected')

    def test_real_core_and_demo_facade_auto_reply_to_a_new_complete_purchase(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = create_synthetic_service(Path(temporary))
            try:
                service.save_settings(merge_seconds=0)
                service.set_scope('incoming', [], 5)
                state = service.set_reception(True, True)
                # Let the initial activity scan finish before introducing the new message.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    jobs = service.list_jobs()
                    if any(j['kind'] == 'sync_messages' and j['status'] == 'succeeded' for j in jobs):
                        break
                    threading.Event().wait(0.01)
                detail = service.get_conversation('demo-noah')
                message = {'seller_id': 'demo-seller', 'buyer_id': 'buyer-noah', 'conversation_id': 'demo-noah',
                           'message_id': 'fresh-noah', 'role': 'buyer', 'timestamp_ms': state['reception']['started_at_ms'] + 5,
                           'text': 'Product: ceramic mugs; quantity 25 pieces; Color: white; ship to Canada.',
                           'attachments': [], 'own': False}
                with service._lock:
                    service.integrations.messages.append(copy.deepcopy(message))
                    service._ingest(detail['conversation'], [message])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    current = service.get_conversation('demo-noah')
                    if current['draft'] and current['draft']['status'] in ('accepted', 'rejected', 'unknown'):
                        break
                    threading.Event().wait(0.01)
                self.assertEqual(current['draft']['status'], 'accepted', current)
                self.assertEqual(current['case']['state'], 'waiting_quote')
                budget = service.get_snapshot()['reception']['budget']
                self.assertEqual((budget['ai_used'], budget['used']), (1, 1))
                self.assertEqual([j['source_message_id'] for j in service.list_jobs() if j['kind'] == 'generate_draft'], ['fresh-noah'])
            finally:
                service.close()
