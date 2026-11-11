"""Ordinary conversation through the actual local automatic-reception pipeline."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.conversation_style import social_intent
from reception_service.repository import now_ms
from reception_service.service import ReceptionService

COMPLETE_PURCHASE = 'Product: ceramic mugs; quantity 25 pieces; Color: white; ship to Canada.'


class SocialReceptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.schedule = ReceptionService._schedule
        self.scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)
        self.service = ReceptionService(Path(self.temp.name))
        self.addCleanup(self.service.close)
        self.service.settings['merge_seconds'] = 0
        self.service.set_scope('incoming', [], 10)
        self.start = self.service.set_reception(True, True)['reception']['started_at_ms']
        self.service._next_poll = time.monotonic() + 1000
        self.cid = 'social-reception-buyer'
        self.sequence = 0

    def idle(self):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if not any(job['status'] in ('queued', 'running') for job in self.service.list_jobs()):
                return
            threading.Event().wait(0.005)
        self.fail('local conversation queue did not complete')

    def ingest(self, text, attachments=None):
        self.sequence += 1
        info = self.service.repo.get('conversation', self.cid) or {'conversation_id': self.cid, 'buyer_id': 'social-buyer', 'customer_name': 'Synthetic buyer'}
        self.service._ingest(info, [{'seller_id': self.service.seller_id, 'buyer_id': info['buyer_id'],
            'conversation_id': self.cid, 'message_id': 'social-message-' + str(self.sequence), 'role': 'buyer',
            'text': text, 'timestamp_ms': max(now_ms(), self.start + self.sequence, (info.get('last_timestamp') or 0) + 1),
            'attachments': attachments or [], 'own': False}], full=True, has_more=False)

    def tick(self):
        self.schedule(self.service)
        self.idle()

    def detail(self):
        return self.service.get_conversation(self.cid)

    def sent(self):
        return [job for job in self.service.list_jobs() if job['kind'] == 'automatic_send' and job['status'] == 'succeeded']

    def test_social_hints_do_not_hide_a_purchase_or_stop_request(self):
        for text in ('Hi', 'Hello, how are you?', "I'm fine, thanks!", '你好', '在吗？', '谢谢', 'Have a nice weekend!', 'Bonjour'):
            self.assertIsNotNone(social_intent(text), text)
        for text in ('Hi, I need 500 mugs', 'Thanks! Ship 500 pieces to Canada', '你好，价格多少',
                     'How are you? Please cancel my order', 'Please stop contacting me', 'Hello, I need a human'):
            self.assertIsNone(social_intent(text), text)

    def test_greeting_sends_without_forcing_a_purchase_question_or_empty_registration(self):
        self.ingest('Hello!')
        self.tick()
        detail = self.detail()
        self.assertEqual(detail['draft']['status'], 'accepted', detail)
        self.assertIn('hello', detail['draft']['body'].lower())
        self.assertNotIn('quantity', detail['draft']['body'].lower())
        self.assertEqual(detail['case']['facts'], {})
        self.assertEqual(detail['draft']['question_keys'], [])
        self.assertEqual(detail['case']['questions'], {})
        self.assertEqual(self.service.repo.list('outbox'), [])
        self.assertEqual(len(self.sent()), 1)

    def test_greeting_wellbeing_thanks_wishes_and_farewell_can_run_automatically(self):
        for text in ('Hi', 'How are you?', 'Thank you!', 'Have a nice weekend!', 'Bye'):
            self.ingest(text)
            self.tick()
            self.assertEqual(self.detail()['draft']['status'], 'accepted', text)
        self.assertEqual(len(self.sent()), 5)
        self.assertEqual(self.detail()['case']['facts'], {})
        self.assertEqual(self.service.repo.list('outbox'), [])
        self.assertEqual(self.service._budget()['ai_used'], 5)
        self.tick()
        self.assertEqual(len(self.sent()), 5)

    def test_waiting_quote_can_chat_without_reopening_or_overwriting_the_purchase(self):
        self.ingest(COMPLETE_PURCHASE)
        self.tick()
        original = copy.deepcopy(self.detail()['case'])
        self.assertEqual(original['state'], 'waiting_quote')
        self.ingest('Hello, how are you?')
        self.tick()
        current = self.detail()
        self.assertEqual(current['draft']['status'], 'accepted')
        for key in ('case_id', 'facts', 'summary', 'procurement_key', 'missing', 'questions'):
            self.assertEqual(current['case'][key], original[key], key)
        self.assertEqual(current['case']['state'], 'waiting_quote')
        self.assertEqual(len(current['cases']), 1)
        self.assertEqual(len(self.sent()), 2)
        self.assertNotIn('quantity', current['draft']['body'].lower())

    def test_waiting_quote_terminal_acknowledgments_do_not_start_a_reply_loop(self):
        self.ingest(COMPLETE_PURCHASE)
        self.tick()
        used = self.service._budget()['ai_used']
        for text in ('Thank you!', 'OK', '👍', 'Bye'):
            self.ingest(text)
            self.tick()
        self.assertEqual(self.service._budget()['ai_used'], used)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.detail()['case']['state'], 'waiting_quote')

    def test_greeting_followed_by_actual_purchase_still_records_requirements(self):
        self.ingest('Hi! ' + COMPLETE_PURCHASE)
        self.tick()
        detail = self.detail()
        self.assertEqual(detail['draft']['status'], 'accepted')
        self.assertEqual(detail['case']['state'], 'waiting_quote')
        self.assertIn('ceramic mugs', detail['case']['facts']['product']['value'])
        self.assertEqual(detail['case']['facts']['quantity']['value'], '25 pieces')
        self.assertTrue(self.service.repo.list('outbox'))

    def test_image_attached_to_greeting_still_enters_requirement_clarification(self):
        self.ingest('Hi', attachments=[{'kind': 'image', 'url': 'https://example.invalid/product.png'}])
        self.tick()
        draft = self.detail()['draft']
        self.assertFalse(draft['social_reply'])
        self.assertIn('product', draft['question_keys'])

    def test_natural_model_body_is_preserved_and_empty_chat_sources_are_bound(self):
        from reception_service.integrations.analysis import validate_analysis
        body = 'Good morning! Glad you stopped by. How has your day been?'
        def generate(context, request_id):
            return validate_analysis(dict(action='ask', facts={}, question_keys=[], reply=body,
                summary='', missing=[], conflicts=[], procurement_key='main', new_case=False,
                reply_sources=[]), context)
        self.ingest('Hello!')
        with patch.object(self.service.integrations, 'generate', side_effect=generate):
            self.tick()
        draft = self.detail()['draft']
        self.assertEqual(draft['status'], 'accepted')
        self.assertEqual(draft['body'], body)
        self.assertEqual(draft['validation']['sources'], ['social-message-1'])

    def test_social_message_keeps_commercial_quality_warning_and_continues(self):
        from reception_service.integrations.analysis import validate_analysis
        def generate(context, request_id):
            return validate_analysis(dict(action='ask', facts={}, question_keys=[],
                reply='Hello! We offer free shipping and will ship tomorrow.', summary='',
                missing=[], conflicts=[], procurement_key='main', new_case=False,
                reply_sources=['social-message-1']), context)
        self.ingest('Hello!')
        with patch.object(self.service.integrations, 'generate', side_effect=generate):
            self.tick()
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.detail()['case']['state'], 'clarifying')
        self.assertTrue(self.detail()['draft']['validation']['ready'])
        self.assertTrue(self.detail()['draft']['validation']['warnings'])

    def test_existing_purchase_registration_keeps_a_current_version_after_chat(self):
        self.ingest('Product: ceramic mugs; quantity 25 pieces')
        self.tick()
        self.ingest('Hello!')
        self.tick()
        case = self.detail()['case']
        self.assertTrue(any(row['case_id'] == case['case_id'] and row['case_revision'] == case['revision']
                            for row in self.service.repo.list('outbox')))
        self.assertIn('product', case['facts'])

    def test_human_takeover_and_stop_contact_still_override_small_talk(self):
        self.ingest('Hi')
        self.service.begin_manual_edit(self.cid)
        self.tick()
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.service._budget()['ai_used'], 0)
        self.service.takeover(self.cid, False)
        self.ingest('Please stop contacting me')
        self.tick()
        self.assertEqual(self.detail()['case']['state'], 'closed')
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.service._budget()['ai_used'], 0)


if __name__ == '__main__':
    unittest.main()
