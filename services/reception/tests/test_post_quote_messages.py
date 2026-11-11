"""Post-quotation source boundaries using temporary SQLite and local facades."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_core import MockFacade
from reception_service.errors import ServiceError
from reception_service.repository import now_ms
from reception_service.service import ReceptionService


COMPLETE_PURCHASE = 'Product: ceramic mugs; quantity 25 pieces; Color: white; ship to Canada.'
SEPARATE_PURCHASE = 'For a separate order I also need cotton bags, 1000 pieces.'


class PostQuoteMessagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.schedule = ReceptionService._schedule
        self.scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)
        self.service = ReceptionService(Path(self.temp.name))
        self.addCleanup(self.service.close)
        self.facade = self.service.integrations
        generate_patch = patch.object(self.facade, 'generate', wraps=self.facade.generate)
        self.generate = generate_patch.start()
        self.addCleanup(generate_patch.stop)
        send_patch = patch.object(self.facade, 'send', wraps=self.facade.send)
        self.send = send_patch.start()
        self.addCleanup(send_patch.stop)
        self.service.settings['merge_seconds'] = 0
        self.service.settings['required_fields'] = ['product', 'quantity', 'specifications', 'destination']
        self.service.set_scope('incoming', [], 5)
        self.start_ms = self.service.set_reception(True, True)['reception']['started_at_ms']
        self.service._next_poll = time.monotonic() + 1000
        self.cid = 'post-quote-buyer'
        self.sequence = 0

    def idle(self):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if not any(job['status'] in ('queued', 'running') for job in self.service.list_jobs()):
                return
            threading.Event().wait(0.005)
        self.fail('local worker queue did not finish')

    def tick(self):
        self.schedule(self.service)
        self.idle()

    def ingest(self, text, *, stamp=None, role='buyer'):
        self.sequence += 1
        info = self.service.repo.get('conversation', self.cid) or {
            'conversation_id': self.cid, 'buyer_id': 'synthetic-buyer', 'customer_name': 'Synthetic buyer'}
        message = {'seller_id': self.service.seller_id, 'buyer_id': info['buyer_id'],
                   'conversation_id': self.cid, 'message_id': 'post-quote-' + str(self.sequence),
                   'role': role, 'own': role == 'seller', 'text': text,
                   'timestamp_ms': max(now_ms(), self.start_ms + self.sequence, (info.get('last_timestamp') or 0) + 1)
                                   if stamp is None else stamp, 'attachments': []}
        self.service._ingest(info, [message], full=True)
        return message

    def detail(self):
        return self.service.get_conversation(self.cid)

    def manual_generate(self):
        case = self.detail()['case']
        ack = self.service.call('generate_draft', {
            'case_id': case['case_id'], 'expected_case_revision': case['revision']})
        self.idle()
        return self.service.repo.get('job', ack['job_id'])

    def waiting_quote(self):
        self.ingest(COMPLETE_PURCHASE)
        self.tick()
        detail = self.detail()
        self.assertEqual(detail['case']['state'], 'waiting_quote', detail)
        self.assertEqual(detail['draft']['status'], 'accepted', detail)
        return copy.deepcopy(detail['case'])

    def assert_new_purchase(self, original):
        detail = self.detail()
        self.assertEqual(len(detail['cases']), 2, detail)
        self.assertNotEqual(detail['case']['case_id'], original['case_id'])
        old = self.service.repo.get('case', original['case_id'])
        self.assertEqual(old['state'], 'waiting_quote')
        self.assertEqual(old['facts'], original['facts'])
        self.assertEqual(old['summary'], original['summary'])
        facts = detail['case']['facts']
        self.assertIn('cotton bags', facts['product']['value'])
        self.assertEqual(facts['quantity']['value'], '1000 pieces')
        self.assertNotIn('specifications', facts)
        self.assertNotIn('destination', facts)
        self.assertTrue(all(fact['evidence_ids'] == ['post-quote-2'] for fact in facts.values()))
        self.assertIn(detail['case']['state'], ('clarifying', 'waiting_customer'))
        return detail

    def test_new_separate_purchase_automatically_creates_second_case_and_keeps_original(self):
        original = self.waiting_quote()
        self.ingest(SEPARATE_PURCHASE)
        self.tick()
        detail = self.assert_new_purchase(original)
        self.assertEqual(detail['draft']['status'], 'accepted')
        self.assertEqual((self.generate.call_count, self.send.call_count), (2, 2))
        self.assertEqual(self.service._budget()['ai_used'], 2)
        self.assertEqual(self.service._budget()['used'], 2)
        self.assertEqual(self.service._started_at_ms, self.start_ms)
        self.tick()
        self.tick()
        self.assertEqual((self.generate.call_count, self.send.call_count), (2, 2))

    def test_manual_generate_can_analyze_new_purchase_while_waiting_quote(self):
        original = self.waiting_quote()
        self.service.set_reception(False)
        self.ingest(SEPARATE_PURCHASE)
        job = self.manual_generate()
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertTrue(job['external_started'])
        self.assert_new_purchase(original)
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(self.service._budget()['ai_used'], 2)
        self.assertEqual(self.send.call_count, 1)

    def test_no_new_source_never_reanalyzes_completed_quote_even_when_manually_requested(self):
        original = self.waiting_quote()
        self.tick()
        job = self.manual_generate()
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertFalse(job['external_started'])
        self.assertEqual(self.generate.call_count, 1)
        self.assertEqual(self.detail()['case'], original)
        self.tick()
        self.assertEqual(self.send.call_count, 1)

    def test_pure_thanks_and_acknowledgments_do_not_call_ai_or_reply(self):
        original = self.waiting_quote()
        for text in ('Thank you for the help.', 'Thanks!', 'OK, thank you.', 'Confirmed.', 'Got it.', '收到，谢谢。'):
            with self.subTest(text=text):
                self.ingest(text)
                self.tick()
                job = self.manual_generate()
                self.assertEqual(job['status'], 'succeeded', job)
                self.assertFalse(job['external_started'])
                self.assertEqual((self.generate.call_count, self.send.call_count), (1, 1))
                self.assertEqual(self.service._budget()['ai_used'], 1)
                self.assertEqual(self.detail()['case']['state'], 'waiting_quote')
                self.assertEqual(self.detail()['case']['facts'], original['facts'])

    def test_thanks_followed_by_new_purchase_is_substantive(self):
        original = self.waiting_quote()
        self.ingest('Thank you for the help. ' + SEPARATE_PURCHASE)
        self.tick()
        self.assert_new_purchase(original)
        self.assertEqual(self.generate.call_count, 2)

    def test_same_purchase_quantity_change_creates_assistance_without_takeover(self):
        original = self.waiting_quote()
        self.ingest('Please change the quantity to 50 pieces.')
        self.tick()
        detail = self.detail()
        self.assertEqual(len(detail['cases']), 1)
        self.assertEqual(detail['case']['case_id'], original['case_id'])
        self.assertNotEqual(detail['case']['state'], 'human_owned')
        self.assertEqual(detail['case']['facts']['quantity']['status'], 'conflict')
        self.assertTrue(any(task['kind'] == 'fact_conflict' for task in detail['assistance_tasks']))
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(self.send.call_count, 2)

    def test_accepted_manual_draft_does_not_block_subsequent_new_substantive_source(self):
        self.service.set_reception(False)
        self.ingest(COMPLETE_PURCHASE)
        case = self.detail()['case']
        draft = self.service.save_draft(case['case_id'], 'Thank you for the details.', None, 'manual-first')
        self.service.set_reception(True, False)
        confirm = self.service.prepare_send(draft['draft_id'], draft['revision'])
        ack = self.service.call('confirm_send', {'confirmation_id': confirm['confirmation_id']})
        self.idle()
        self.assertEqual(self.service.repo.get('job', ack['job_id'])['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'accepted')
        self.service._next_poll = time.monotonic() + 1000
        self.ingest('We also need logo printing.')
        self.tick()
        self.assertEqual(self.generate.call_count, 1)
        self.assertEqual(self.detail()['draft']['source_message_id'], 'post-quote-2')
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['body'], draft['body'])
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'accepted')

    def test_accepted_manual_response_completes_only_its_own_fresh_source(self):
        # This source arrives after authorization and has no generation job.
        self.ingest(COMPLETE_PURCHASE)
        case = self.detail()['case']
        draft = self.service.save_draft(case['case_id'], 'Thank you for the details.', None, 'fresh-manual')
        confirm = self.service.prepare_send(draft['draft_id'], draft['revision'])
        self.service.call('confirm_send', {'confirmation_id': confirm['confirmation_id']})
        self.idle()
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'accepted')
        self.tick()
        self.tick()
        self.assertEqual(self.generate.call_count, 0)
        self.ingest('Please add our logo to the mugs.')
        self.tick()
        self.assertEqual(self.generate.call_count, 1)
        self.assertEqual(self.detail()['draft']['source_message_id'], 'post-quote-2')

    def test_accepted_manual_quote_reply_does_not_block_new_purchase(self):
        self.ingest(COMPLETE_PURCHASE)
        self.service.set_reception(False)
        self.assertEqual(self.manual_generate()['status'], 'succeeded')
        detail = self.detail()
        draft = self.service.save_draft(detail['case']['case_id'], 'Thank you for the details.',
                                        detail['draft']['revision'], 'manual-quote')
        self.service.set_reception(True, False)
        self.service._next_poll = time.monotonic() + 1000
        confirm = self.service.prepare_send(draft['draft_id'], draft['revision'])
        self.service.call('confirm_send', {'confirmation_id': confirm['confirmation_id']})
        self.idle()
        original = self.detail()['case']
        self.assertEqual(original['state'], 'waiting_quote')
        self.assertEqual(self.detail()['draft']['status'], 'accepted')
        self.ingest(SEPARATE_PURCHASE)
        self.tick()
        self.assert_new_purchase(original)
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'accepted')

    def test_manual_generation_of_same_purchase_change_uses_existing_conflict_handling(self):
        original = self.waiting_quote()
        self.service.set_reception(False)
        self.ingest('Please change the quantity to 50 pieces.')
        job = self.manual_generate()
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertTrue(job['external_started'])
        self.assertEqual(self.detail()['case']['case_id'], original['case_id'])
        self.assertNotEqual(self.detail()['case']['state'], 'human_owned')
        self.assertEqual(self.detail()['case']['facts']['quantity']['status'], 'conflict')
        self.assertTrue(any(task['kind'] == 'fact_conflict' for task in self.detail()['assistance_tasks']))
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(self.send.call_count, 1)

    def test_default_required_fields_create_new_case_without_inheriting_old_facts(self):
        self.service.settings['required_fields'] = ['product', 'quantity', 'specifications', 'destination']
        original = self.waiting_quote()
        self.ingest(SEPARATE_PURCHASE)
        self.tick()
        detail = self.detail()
        self.assertEqual(len(detail['cases']), 2)
        self.assertEqual(self.service.repo.get('case', original['case_id'])['state'], 'waiting_quote')
        self.assertEqual(self.service.repo.get('case', original['case_id'])['facts'], original['facts'])
        self.assertEqual(detail['case']['facts']['quantity']['value'], '1000 pieces')
        self.assertNotIn('destination', detail['case']['facts'])
        self.assertEqual(set(detail['case']['missing']), {'specifications', 'destination'})
        self.assertEqual(detail['case']['state'], 'waiting_customer')
        self.assertEqual(detail['draft']['status'], 'accepted')
        self.assertEqual({case['case_id'] for case in self.service.get_snapshot()['handoff_cases']}, {original['case_id']})
        self.assertTrue(all(fact['evidence_ids'] == ['post-quote-2'] for fact in detail['case']['facts'].values()))
        self.assertEqual(self.generate.call_count, 2)

    def test_pending_and_stale_manual_drafts_remain_protected(self):
        for status in ('pending', 'stale'):
            with self.subTest(status=status):
                self.cid = 'protected-' + status
                self.ingest(COMPLETE_PURCHASE)
                case = self.detail()['case']
                draft = self.service.save_draft(case['case_id'], 'Keep my handwritten reply.', None, 'manual-' + status)
                self.ingest('Please add our logo to the mugs.')
                draft['status'] = status
                self.service.repo.put('draft', draft['draft_id'], draft)
                self.tick()
                self.assertEqual(self.generate.call_count, 0)
                job = self.manual_generate()
                self.assertEqual(job['status'], 'stale', job)
                saved = self.service.repo.get('draft', draft['draft_id'])
                self.assertEqual(saved['body'], draft['body'])
                self.assertEqual(saved['revision'], draft['revision'])
                self.assertEqual(saved['status'], status)
                self.generate.reset_mock()

    def test_post_quote_generation_failure_is_charged_once_without_automatic_retry(self):
        self.waiting_quote()
        self.ingest(SEPARATE_PURCHASE)
        with patch.object(self.facade, 'generate', side_effect=ServiceError('synthetic_failure', 'Synthetic failure')) as failed:
            self.tick()
            self.service.set_reception(False)
            self.service.set_reception(True, True)
            self.service._next_poll = time.monotonic() + 1000
            self.tick()
            self.tick()
            self.assertEqual(failed.call_count, 1)
        self.assertEqual(self.service._budget()['ai_used'], 2)
        self.assertEqual(self.service._started_at_ms, self.start_ms)
        self.assertEqual(self.send.call_count, 1)

    def test_new_post_quote_source_with_mock_analysis_can_create_case(self):
        original = self.waiting_quote()
        mock = MockFacade()
        mock.output = {'action': 'ask', 'facts': {
            'product': {'value': 'cotton bags', 'evidence_ids': ['post-quote-2']},
            'quantity': {'value': '1000 pieces', 'evidence_ids': ['post-quote-2']}},
            'question_keys': ['specifications'], 'reply': 'Could you share the key specifications?',
            'summary': 'cotton bags', 'missing': ['specifications', 'destination'], 'conflicts': [],
            'procurement_key': 'cotton-bags', 'new_case': True, 'reply_sources': ['post-quote-2']}
        self.service.integrations = mock
        self.ingest(SEPARATE_PURCHASE)
        self.tick()
        self.assert_new_purchase(original)
        self.assertEqual((mock.generate_calls, mock.send_calls), (1, 1))
        self.tick()
        self.assertEqual(mock.generate_calls, 1)


if __name__ == '__main__':
    unittest.main()
