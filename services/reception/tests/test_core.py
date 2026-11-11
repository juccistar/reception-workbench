"""Targeted core tests; temporary synthetic data and mock facades only."""
from __future__ import annotations

import copy
import importlib
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reception_service.errors import ServiceError
from reception_service.repository import Repository, encoded, now_ms
from reception_service.service import ReceptionService


class MockFacade:
    def __init__(self):
        self.seller = 'demo-seller'
        self.runtime_ok = True
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_generate = False
        self.block_send = False
        self.block_read = False
        self.send_calls = 0
        self.generate_calls = 0
        self.revoked = 0
        self.observed_authorization = None
        self.send_result = {'status': 'accepted', 'message_id': 'accepted-1', 'submission_started': True}
        self.registration_result = {'status': 'failed'}
        self.registration_calls = []
        self.pages = []
        self.read_calls = []
        self.output = None

    def capabilities(self):
        return {'simulated': True}

    def runtime_status(self):
        return {'ok': self.runtime_ok, 'missing_variables': [], 'message': 'synthetic', 'simulated': False}

    def resolve_identity(self, expected_seller_id=''):
        if expected_seller_id and expected_seller_id != self.seller:
            raise ServiceError('seller_mismatch', 'synthetic identity mismatch')
        return self.seller

    def read_recent_conversations(self, seller_id, before_ms=None, count=20):
        if self.block_read:
            self.started.set()
            self.release.wait(5)
        return {'items': [], 'has_more': False, 'next_cursor': None}

    def read_message_page(self, seller_id, buyer_id, conversation_id, before_ms=None, count=100):
        self.read_calls.append((conversation_id, before_ms, count))
        return self.pages.pop(0) if self.pages else {'messages': [], 'has_more': False, 'next_cursor': None}

    def generate(self, context, request_id):
        self.generate_calls += 1
        if self.block_generate:
            self.started.set()
            self.release.wait(5)
        if self.output is not None:
            return copy.deepcopy(self.output)
        source = context['recent_messages'][-1]['message_id']
        return {'action': 'ask', 'facts': {}, 'question_keys': ['quantity'],
                'reply': "I've noted your request. Our team will verify sample charges and preparation time. Could you share the quantity?",
                'summary': 'Synthetic context', 'missing': ['quantity'], 'conflicts': [], 'procurement_key': 'main',
                'new_case': False, 'reply_sources': [source]}

    def preview_send(self, intent):
        return {'status': 'passed', 'submission_started': False}

    def send(self, intent, authorization):
        self.send_calls += 1
        if self.block_send:
            self.started.set()
            self.release.wait(5)
        self.observed_authorization = authorization()
        if not self.observed_authorization:
            return {'status': 'rejected', 'message_id': None, 'submission_started': False}
        return self.send_result.copy()

    def register(self, case, previous, operation_id, *, authorization=None):
        if authorization is not None and not authorization():
            return {'status': 'rejected', 'submission_started': False}
        self.registration_calls.append(operation_id)
        if isinstance(self.registration_result, Exception):
            raise self.registration_result
        return self.registration_result.copy()

    def check_registration(self):
        return {'status': 'passed', 'message': 'synthetic'}

    def revoke(self):
        self.revoked += 1


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.facade = MockFacade()
        self.patch = patch('reception_service.service.build_integrations', return_value=self.facade)
        self.patch.start()
        self.service = ReceptionService(self.root)
        self.cid = 'demo-emma'
        self.case_id = self.service.get_conversation(self.cid)['case']['case_id']

    def tearDown(self):
        self.facade.release.set()
        self.service.close()
        self.patch.stop()
        self.temp.cleanup()

    def wait_job(self, ack):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            job = next(j for j in self.service.list_jobs() if j['job_id'] == ack['job_id'])
            if job['status'] not in ('queued', 'running'):
                return job
            threading.Event().wait(0.005)
        self.fail('job did not complete')

    def generate(self):
        case = self.service.get_conversation(self.cid)['case']
        job = self.wait_job(self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']}))
        self.assertEqual(job['status'], 'succeeded', job)
        return self.service.get_conversation(self.cid)['draft']

    def manual(self, body='Thank you. Could you share your preferred quantity?'):
        detail = self.service.get_conversation(self.cid)
        previous = detail['draft']
        return self.service.save_draft(self.case_id, body, previous['revision'] if previous else None, 'manual-' + str(now_ms()))

    def start(self):
        self.service.set_reception(True, False)
        self.service._next_poll = time.monotonic() + 100

    def test_demo_seed_and_safe_initial_permissions(self):
        snapshot = self.service.get_snapshot()
        self.assertEqual(len(snapshot['conversations']), 3)
        self.assertFalse(snapshot['reception']['running'])
        self.assertFalse(snapshot['reception']['auto_send'])
        self.assertEqual(snapshot['settings']['workctl_path'], '')
        self.assertFalse(snapshot['settings']['feishu']['write_enabled'])
        self.assertTrue(all(check['status'] == 'not_checked' for check in snapshot['checks'].values()))

    def test_context_is_customer_local_and_drafts_are_not_history(self):
        draft = self.manual('DRAFT UNIQUE BODY')
        context = self.service.get_context(self.case_id)
        self.assertTrue(all(m['conversation_id'] == self.cid for m in context['recent_messages']))
        self.assertNotIn(draft['body'], str(context))
        self.assertEqual(context['historical_evidence'], [])

    def test_recent_twenty_retains_old_fact_evidence(self):
        conversation = self.service.repo.get('conversation', self.cid)
        stamp = now_ms()
        messages = [self.message(f'm-{i}', f'Follow-up {i}', stamp + i) for i in range(25)]
        self.service._ingest(conversation, messages)
        context = self.service.get_context(self.case_id)
        self.assertEqual(len(context['recent_messages']), 20)
        self.assertIn('demo-emma-message-1', [m['message_id'] for m in context['historical_evidence']])
        self.assertEqual(self.service.get_messages(self.cid, limit=10)['has_more'], True)
        recent = self.service.get_messages(self.cid, limit=10)['items']
        older = self.service.get_messages(self.cid, before_id=recent[0]['message_id'], limit=10)['items']
        self.assertFalse({m['message_id'] for m in older} & {m['message_id'] for m in recent})

    def message(self, mid, text, stamp=None):
        return {'message_id': mid, 'seller_id': self.service.seller_id, 'buyer_id': 'buyer-emma',
                'conversation_id': self.cid, 'role': 'buyer', 'text': text,
                'timestamp_ms': stamp if stamp is not None else now_ms(), 'own': False, 'attachments': []}

    def test_raw_dedup_and_revision_history(self):
        conversation = self.service.repo.get('conversation', self.cid)
        message = self.service.get_messages(self.cid)['items'][0]
        self.service._ingest(conversation, [message])
        self.assertEqual(self.service.repo.get('conversation', self.cid)['revision'], conversation['revision'])
        message['text'] += ' Updated.'
        self.service._ingest(conversation, [message])
        history = self.service.repo.history('message', encoded([self.cid, message['message_id']]))
        self.assertEqual(len(history), 2)
        self.assertNotEqual(history[0]['text'], history[1]['text'])
        self.assertEqual(self.service.get_conversation(self.cid)['case']['facts'], {})

    def test_overlap_does_not_erase_unchanged_fact_evidence(self):
        conversation = self.service.repo.get('conversation', self.cid)
        old = self.service.get_messages(self.cid)['items'][0]
        self.service._ingest(conversation, [old, self.message('new-message', 'Thanks')])
        self.assertIn('product', self.service.get_conversation(self.cid)['case']['facts'])

    def test_save_idempotence_and_revision_conflict(self):
        params = {'case_id': self.case_id, 'body': 'My edited reply', 'expected_revision': None, 'client_mutation_id': 'm1'}
        first = self.service.call('save_draft', params)
        self.assertEqual(first, self.service.call('save_draft', params))
        second = self.service.save_draft(self.case_id, 'New edited reply', first['revision'], 'm2')
        self.assertEqual(first['draft_id'], second['draft_id'])
        self.assertEqual(second['revision'], first['revision'] + 1)
        self.assertEqual(len(self.service.repo.list('draft')), 1)
        with self.assertRaises(ServiceError):
            self.service.save_draft(self.case_id, 'Lost edit', first['revision'], 'm3')
        with self.assertRaises(ServiceError):
            self.service.save_draft(self.case_id, 'Wrong idempotency', second['revision'], 'm2')

    def test_natural_body_preserved_and_check_history_independent(self):
        draft = self.generate()
        self.assertTrue(draft['body'].startswith("I've noted your request."))
        self.assertTrue(draft['validation']['ready'])
        first = self.wait_job(self.service.call('check_ai'))
        second = self.wait_job(self.service.call('check_ai'))
        self.assertNotEqual(first['job_id'], second['job_id'])
        self.assertEqual(first['status'], 'succeeded')
        self.assertEqual(self.service.get_snapshot()['checks']['ai']['status'], 'demo')
        self.assertFalse(self.service.get_snapshot()['reception']['running'])

    def test_fake_fact_and_foreign_reply_source_are_discarded(self):
        source = self.service.get_messages(self.cid)['items'][-1]['message_id']
        self.facade.output = {'action': 'ask', 'facts': {'quantity': {'value': '9000', 'evidence_ids': [source]}},
                              'reply': 'Okay', 'reply_sources': [source]}
        case = self.service.get_conversation(self.cid)['case']
        job = self.wait_job(self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']}))
        self.assertEqual(job['status'], 'succeeded', job)
        detail = self.service.get_conversation(self.cid)
        self.assertNotEqual(detail['case']['facts'].get('quantity', {}).get('value'), '9000')
        self.assertEqual(detail['draft']['body'], 'Okay')
        self.assertNotEqual(detail['case']['state'], 'human_owned')
        self.facade.output = {'action': 'ask', 'facts': {}, 'reply': 'Okay', 'reply_sources': ['demo-sofia-message-1']}
        self.service.inject_demo(self.cid, 'Please clarify the details.')
        case = self.service.get_conversation(self.cid)['case']
        from reception_service.integrations.analysis import validate_analysis
        with patch.object(self.facade, 'generate', side_effect=lambda context, request_id: validate_analysis(self.facade.output, context)):
            job = self.wait_job(self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']}))
        self.assertEqual(job['status'], 'succeeded', job)
        draft = self.service.get_conversation(self.cid)['draft']
        self.assertNotIn('demo-sofia-message-1', draft['validation']['sources'])

    def test_buyer_request_does_not_approve_commercial_promise(self):
        self.service.inject_demo(self.cid, 'Can you offer free shipping?')
        draft = self.manual('We offer free shipping.')
        self.assertTrue(draft['validation']['ready'])
        self.assertTrue(draft['validation']['warnings'])
        self.assertEqual(draft['body'], 'We offer free shipping.')

    def test_policy_change_and_expiry_revoke_draft(self):
        draft = self.generate()
        policy = self.service.get_snapshot()['policies'][0]
        self.service.save_policy(**{k: policy[k] for k in ('policy_id', 'kind', 'title', 'product_scope', 'confirmed', 'public')},
                                 content='Updated demo policy.', expected_revision=policy['revision'])
        self.assertEqual(self.service.get_conversation(self.cid)['draft']['status'], 'stale')
        self.start()
        with self.assertRaises(ServiceError):
            self.service.prepare_send(draft['draft_id'], draft['revision'])

    def test_new_message_and_takeover_revoke_confirmations(self):
        draft = self.generate()
        self.start()
        confirmation = self.service.prepare_send(draft['draft_id'], draft['revision'])
        self.service.inject_demo(self.cid, 'Please use a blue logo.')
        self.assertEqual(self.service.get_conversation(self.cid)['draft']['status'], 'stale')
        job = self.wait_job(self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']}))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.facade.send_calls, 0)
        self.service.takeover(self.cid, True)
        detail = self.service.get_conversation(self.cid)
        self.assertTrue(detail['conversation']['human_owned'])
        self.assertTrue(self.service.repo.get('conversation', self.cid)['manual_chat'])
        self.assertEqual(detail['case']['state'], 'clarifying')

    def test_background_generate_does_not_block_query_save_or_pause(self):
        self.facade.block_generate = True
        case = self.service.get_conversation(self.cid)['case']
        ack = self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']})
        self.assertTrue(self.facade.started.wait(1))
        begin = time.monotonic()
        self.service.get_snapshot()
        manual = self.manual('This is my manual edit')
        self.service.set_reception(False)
        self.assertLess(time.monotonic() - begin, 0.5)
        self.facade.release.set()
        job = self.wait_job(ack)
        self.assertEqual(job['status'], 'stale')
        self.assertEqual(self.service.get_conversation(self.cid)['draft']['body'], manual['body'])

    def test_preexisting_manual_draft_never_overwritten_by_ai(self):
        manual = self.manual('Handwritten exact reply')
        case = self.service.get_conversation(self.cid)['case']
        job = self.wait_job(self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']}))
        self.assertEqual(job['status'], 'stale')
        self.assertEqual(self.service.get_conversation(self.cid)['draft']['body'], manual['body'])
        self.assertEqual(len(self.service.repo.list('ai_result')), 1)

    def test_pause_revokes_native_final_check_without_waiting_for_io(self):
        draft = self.generate()
        self.start()
        confirmation = self.service.prepare_send(draft['draft_id'], draft['revision'])
        self.facade.block_send = True
        ack = self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']})
        self.assertTrue(self.facade.started.wait(1))
        before = time.monotonic()
        self.service.set_reception(False)
        self.assertLess(time.monotonic() - before, 0.5)
        self.facade.release.set()
        self.wait_job(ack)
        self.assertIs(self.facade.observed_authorization, False)
        self.assertEqual(self.service.get_conversation(self.cid)['draft']['status'], 'rejected')

    def test_takeover_revokes_inflight_native_final_check(self):
        draft = self.generate()
        self.start()
        confirmation = self.service.prepare_send(draft['draft_id'], draft['revision'])
        self.facade.block_send = True
        ack = self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']})
        self.assertTrue(self.facade.started.wait(1))
        self.service.takeover(self.cid, True)
        self.facade.release.set()
        self.wait_job(ack)
        self.assertIs(self.facade.observed_authorization, False)

    def test_shutdown_immediately_revokes_inflight_authorization(self):
        draft = self.generate()
        self.start()
        confirmation = self.service.prepare_send(draft['draft_id'], draft['revision'])
        self.facade.block_send = True
        ack = self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']})
        self.assertTrue(self.facade.started.wait(1))
        self.assertEqual(self.service.call('shutdown'), {'status': 'closing'})
        self.facade.release.set()
        self.wait_job(ack)
        self.assertIs(self.facade.observed_authorization, False)

    def test_native_snapshot_rejection_preserves_safe_reason_stage_and_code(self):
        draft = self.generate()
        self.start()
        confirmation = self.service.prepare_send(draft['draft_id'], draft['revision'])
        self.facade.send_result = dict(status='rejected', message_id='', submission_started=False,
            error_code='snapshot_time_unverified', stage='snapshot', message='PRIVATE_RAW_REASON',
            diagnostics={'code': 'snapshot_time_unverified', 'stage': 'snapshot', 'body': 'PRIVATE_BODY'})
        job = self.wait_job(self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']}))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error']['code'], 'snapshot_time_unverified')
        self.assertEqual(job['stage'], 'snapshot')
        self.assertIn('时区', job['message'])
        self.assertNotIn('PRIVATE', str(job))
        operation = self.service.repo.get('operation', job['result']['operation_id'])
        self.assertEqual(operation['error_code'], 'snapshot_time_unverified')
        self.assertEqual(operation['stage'], 'snapshot')
        self.assertFalse(operation['submission_started'])

    def test_preview_rejection_retains_snapshot_diagnostics_without_submission(self):
        draft = self.generate()
        self.facade.preview_send = lambda intent: dict(status='failed', submission_started=False,
            error_code='snapshot_identity_mismatch', stage='snapshot', diagnostics={'stage': 'snapshot'})
        job = self.wait_job(self.service.call('preview_send', {'draft_id': draft['draft_id'], 'expected_revision': draft['revision']}))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error']['code'], 'snapshot_identity_mismatch')
        self.assertEqual(job['stage'], 'snapshot')
        self.assertIn('身份', job['message'])
        self.assertEqual(self.facade.send_calls, 0)
        self.assertEqual(self.service.repo.list('operation'), [])

    def test_unknown_send_never_retried_and_persists(self):
        draft = self.generate()
        self.start()
        confirmation = self.service.prepare_send(draft['draft_id'], draft['revision'])
        self.facade.send_result = {'status': 'unknown', 'message_id': None, 'submission_started': True}
        job = self.wait_job(self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']}))
        self.assertEqual(job['status'], 'unknown')
        operation_id = job['result']['operation_id']
        self.assertEqual(self.service.reconcile_operation(operation_id)['status'], 'unknown')
        with self.assertRaises(ServiceError):
            self.service.prepare_send(draft['draft_id'], draft['revision'])
        self.service.close()
        self.service = ReceptionService(self.root)
        self.assertEqual(self.service.get_conversation(self.cid)['draft']['status'], 'unknown')
        self.assertEqual(self.service.reconcile_operation(operation_id)['status'], 'unknown')
        self.assertFalse(self.service.get_snapshot()['reception']['running'])
        self.assertEqual(self.facade.send_calls, 1)

    def test_accepted_reply_enters_history_only_after_receipt(self):
        draft = self.generate()
        self.assertFalse(any(m['text'] == draft['body'] for m in self.service.get_messages(self.cid)['items']))
        self.start()
        confirmation = self.service.prepare_send(draft['draft_id'], draft['revision'])
        job = self.wait_job(self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']}))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertTrue(any(m['text'] == draft['body'] for m in self.service.get_messages(self.cid)['items']))
        duplicate = self.wait_job(self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']}))
        self.assertEqual(duplicate['status'], 'failed')
        self.assertEqual(self.facade.send_calls, 1)

    def test_outbox_failure_does_not_rollback_local_case_or_reply(self):
        draft = self.generate()
        self.assertEqual(len(self.service.list_outbox()), 1)
        operation_id = self.service.list_outbox()[0]['operation_id']
        job = self.wait_job(self.service.call('run_outbox'))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.service.list_outbox()[0]['status'], 'failed')
        self.assertEqual(self.service.get_conversation(self.cid)['draft']['body'], draft['body'])
        self.assertEqual(self.facade.registration_calls, [operation_id])
        self.facade.registration_result = {'status': 'succeeded', 'record_id': 'synthetic-record'}
        self.assertEqual(self.wait_job(self.service.call('run_outbox'))['status'], 'succeeded')
        self.assertEqual(self.facade.registration_calls, [operation_id, operation_id])

    def test_history_pagination_and_stalled_cursor(self):
        stamp = now_ms() - 10_000_000
        self.facade.pages = [
            {'messages': [self.message('old-1', 'Older evidence', stamp)], 'has_more': True, 'next_cursor': stamp},
            {'messages': [self.message('old-2', 'Oldest evidence', stamp - 1000)], 'has_more': False, 'next_cursor': None},
        ]
        job = self.wait_job(self.service.call('fill_history', {'conversation_id': self.cid}))
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(self.service.get_conversation(self.cid)['history']['status'], 'complete')
        self.facade.pages = [{'messages': [], 'has_more': True, 'next_cursor': stamp},
                             {'messages': [], 'has_more': True, 'next_cursor': stamp}]
        self.assertEqual(self.wait_job(self.service.call('fill_history', {'conversation_id': self.cid}))['status'], 'failed')
        self.assertEqual(self.service.get_conversation(self.cid)['history']['status'], 'failed')

    def sync_gap_pages(self):
        old = self.service.get_messages(self.cid)['items'][0]
        messages = [self.message(f'burst-{i}', f'Synthetic new message {i}', old['timestamp_ms'] + i)
                    for i in range(1, 102)]
        return old, messages, [
            {'messages': list(reversed(messages[1:])), 'has_more': True, 'next_cursor': messages[1]['timestamp_ms']},
            {'messages': [messages[0], old], 'has_more': False, 'next_cursor': None},
        ]

    def test_sync_complete_then_101_new_messages_bridges_gap_and_revisions(self):
        draft = self.manual()
        old, messages, pages = self.sync_gap_pages()
        pages[1]['messages'][1] = {**old, 'text': 'Corrected original request'}
        self.assertEqual(self.service.get_conversation(self.cid)['history']['status'], 'complete')
        self.facade.pages = pages.copy()
        job = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(job['result']['changed_count'], 1)
        self.assertEqual([call[1] for call in self.facade.read_calls], [None, messages[1]['timestamp_ms']])
        self.assertEqual({m['message_id'] for m in self.service._messages(self.cid)},
                         {old['message_id']} | {m['message_id'] for m in messages})
        detail = self.service.get_conversation(self.cid)
        self.assertEqual(detail['history']['status'], 'complete')
        self.assertIsNone(detail['history']['next_cursor'])
        self.assertEqual(detail['case']['facts'], {})
        self.assertEqual(detail['draft']['draft_id'], draft['draft_id'])
        self.assertEqual(detail['draft']['status'], 'stale')
        self.assertEqual(len(self.service.repo.history('message', encoded([self.cid, old['message_id']]))), 2)
        revision = self.service.repo.get('conversation', self.cid)['revision']
        self.facade.pages = [pages[0]]
        repeated = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
        self.assertEqual(repeated['status'], 'succeeded', repeated)
        self.assertEqual(repeated['result']['changed_count'], 0)
        self.assertEqual(len(self.facade.read_calls), 3)
        self.assertEqual(self.service.repo.get('conversation', self.cid)['revision'], revision)
        self.assertEqual(self.service.get_conversation(self.cid)['history']['status'], 'complete')

    def test_sync_overlap_applies_revision_and_preserves_older_partial_cursor(self):
        old = self.service.get_messages(self.cid)['items'][0]
        history = self.service.repo.get('coverage', self.cid)
        history.update(status='partial', next_cursor=old['timestamp_ms'] - 1000)
        self.service.repo.put('coverage', self.cid, history)
        self.facade.pages = [{'messages': [{**old, 'text': 'Corrected request'}],
                              'has_more': True, 'next_cursor': old['timestamp_ms']}]
        job = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
        self.assertEqual(job['status'], 'succeeded', job)
        actual = self.service.get_conversation(self.cid)
        self.assertEqual(actual['messages'][0]['text'], 'Corrected request')
        self.assertEqual(actual['history']['status'], 'partial')
        self.assertEqual(actual['history']['next_cursor'], history['next_cursor'])
        self.assertEqual(len(self.facade.read_calls), 1)

    def test_sync_page_budget_resumes_gap_after_restart(self):
        old, messages, pages = self.sync_gap_pages()
        self.facade.pages = pages.copy()
        with patch('reception_service.service.SYNC_PAGE_LIMIT', 1):
            first = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
        self.assertEqual(first['status'], 'failed', first)
        history = self.service.get_conversation(self.cid)['history']
        self.assertEqual(history['status'], 'partial')
        self.assertEqual(history['next_cursor'], messages[1]['timestamp_ms'])
        self.assertNotIn(messages[0]['message_id'], {m['message_id'] for m in self.service._messages(self.cid)})
        self.service.close()
        self.service = ReceptionService(self.root)
        resumed = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
        self.assertEqual(resumed['status'], 'succeeded', resumed)
        self.assertEqual([call[1] for call in self.facade.read_calls], [None, messages[1]['timestamp_ms']])
        self.assertEqual(len(self.service._messages(self.cid)), 102)
        self.assertEqual(self.service.get_conversation(self.cid)['history']['status'], 'complete')
        self.assertEqual(self.service.repo.get('sync_gap', self.cid), {})

    def test_fill_history_can_finish_sync_gap_without_leaving_stale_checkpoint(self):
        old, messages, pages = self.sync_gap_pages()
        self.facade.pages = pages.copy()
        with patch('reception_service.service.SYNC_PAGE_LIMIT', 1):
            self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
        filled = self.wait_job(self.service.call('fill_history', {'conversation_id': self.cid}))
        self.assertEqual(filled['status'], 'succeeded', filled)
        self.assertEqual(self.facade.read_calls[-1][1], messages[1]['timestamp_ms'])
        self.assertEqual(self.service.get_conversation(self.cid)['history']['status'], 'complete')
        self.assertEqual(self.service.repo.get('sync_gap', self.cid), {})
        self.facade.pages = [pages[0]]
        synced = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
        self.assertEqual(synced['status'], 'succeeded', synced)
        self.assertIsNone(self.facade.read_calls[-1][1])
        self.assertEqual(self.service.get_conversation(self.cid)['history']['status'], 'complete')

    def test_fill_history_budget_remains_partial_and_continues_at_saved_cursor(self):
        stamp = self.service.get_messages(self.cid)['items'][0]['timestamp_ms'] - 1000
        self.facade.pages = [
            {'messages': [self.message('older-1', 'Synthetic older message', stamp)],
             'has_more': True, 'next_cursor': stamp},
            {'messages': [self.message('older-2', 'Synthetic oldest message', stamp - 1000)],
             'has_more': False, 'next_cursor': None},
        ]
        with patch('reception_service.service.HISTORY_PAGE_LIMIT', 1):
            limited = self.wait_job(self.service.call('fill_history', {'conversation_id': self.cid}))
        self.assertEqual(limited['status'], 'failed', limited)
        self.assertEqual(limited['error']['code'], 'history_limit')
        history = self.service.get_conversation(self.cid)['history']
        self.assertEqual(history['status'], 'partial')
        self.assertEqual(history['next_cursor'], stamp)
        resumed = self.wait_job(self.service.call('fill_history', {'conversation_id': self.cid}))
        self.assertEqual(resumed['status'], 'succeeded', resumed)
        self.assertEqual(self.facade.read_calls[-1][1], stamp)
        self.assertEqual(self.service.get_conversation(self.cid)['history']['status'], 'complete')

    def test_sync_rejects_non_integer_nonpositive_and_terminal_cursors(self):
        for more, cursor in [(True, True), (True, '123'), (True, 0), (True, -1), (True, None),
                             (False, 123), ('false', None)]:
            with self.subTest(more=more, cursor=cursor):
                self.facade.pages = [{'messages': [self.message('invalid-page', 'Must not ingest')],
                                      'has_more': more, 'next_cursor': cursor}]
                job = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
                self.assertEqual(job['status'], 'failed', job)
                self.assertEqual(job['error']['code'], 'history_cursor_stalled')
                self.assertNotEqual(self.service.get_conversation(self.cid)['history']['status'], 'complete')
                self.assertIsNone(self.service.repo.get('message', encoded([self.cid, 'invalid-page'])))

    def test_sync_stalled_cursor_keeps_last_good_page_and_retries_from_it(self):
        old = self.service.get_messages(self.cid)['items'][0]
        for bad_cursor in (old['timestamp_ms'] + 1000, old['timestamp_ms'] + 2000):
            with self.subTest(cursor=bad_cursor):
                # Each attempt starts with a fresh complete baseline.
                self.service.repo.put('sync_gap', self.cid, {})
                history = self.service.repo.get('coverage', self.cid)
                history.update(status='complete', next_cursor=None)
                self.service.repo.put('coverage', self.cid, history)
                first = self.message(f'new-{bad_cursor}', 'Synthetic recent', old['timestamp_ms'] + 1000)
                self.facade.pages = [
                    {'messages': [first], 'has_more': True, 'next_cursor': first['timestamp_ms']},
                    {'messages': [], 'has_more': True, 'next_cursor': bad_cursor},
                ]
                failed = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
                self.assertEqual(failed['status'], 'failed', failed)
                self.assertEqual(failed['error']['code'], 'history_cursor_stalled')
                self.assertEqual(self.service.get_conversation(self.cid)['history']['next_cursor'], first['timestamp_ms'])
                self.facade.pages = [{'messages': [old], 'has_more': False, 'next_cursor': None}]
                resumed = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
                self.assertEqual(resumed['status'], 'succeeded', resumed)
                self.assertEqual(self.facade.read_calls[-1][1], first['timestamp_ms'])

    def queue_case_versions(self, *revisions):
        case = self.service.repo.get('case', self.case_id)
        with self.service.repo.transaction():
            for revision in revisions:
                self.service._queue_outbox({**case, 'revision': revision, 'summary': f'Synthetic revision {revision}'})
        return {o['case_revision']: o for o in self.service.list_outbox() if o['case_id'] == self.case_id}

    def test_outbox_old_failure_then_new_success_never_resubmits_old_snapshot(self):
        items = self.queue_case_versions(1, 2)
        calls = []
        remote = {}

        def register(case, previous, operation_id, *, authorization=None):
            if authorization is not None and not authorization():
                return {'status': 'rejected', 'submission_started': False}
            calls.append((operation_id, case['revision'], copy.deepcopy(previous)))
            if case['revision'] == 1 and len(calls) == 1:
                raise ServiceError('synthetic_pre_submit_failure', 'Synthetic failure before submission', stage='outbox')
            remote.update(copy.deepcopy(case))
            return {'status': 'succeeded', 'record_id': 'synthetic-record', 'number': 'SYN-2'}

        with patch.object(self.facade, 'register', side_effect=register):
            first = self.wait_job(self.service.call('run_outbox'))
            self.assertEqual(first['status'], 'failed', first)
            self.assertEqual(remote['revision'], 2)
            registration = self.service.repo.get('registration', self.case_id)
            self.assertEqual(registration['case_revision'], 2)
            self.assertEqual(registration['operation_id'], items[2]['operation_id'])
            for _ in range(2):
                rerun = self.wait_job(self.service.call('run_outbox'))
                self.assertEqual(rerun['status'], 'succeeded', rerun)
            self.assertEqual([call[1] for call in calls], [1, 2])
            self.assertEqual(remote['summary'], 'Synthetic revision 2')
            self.assertEqual(self.service.repo.get('registration', self.case_id), registration)
            self.assertEqual(self.service.repo.get('outbox', items[1]['operation_id'])['status'], 'superseded')

    def test_outbox_skips_old_snapshot_using_registration_or_successful_outbox(self):
        items = self.queue_case_versions(1, 2)
        higher = items[2]
        higher['status'] = 'succeeded'
        self.service.repo.put('outbox', higher['operation_id'], higher)
        job = self.wait_job(self.service.call('run_outbox'))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(self.facade.registration_calls, [])
        self.assertEqual(self.service.repo.get('outbox', items[1]['operation_id'])['status'], 'superseded')
        self.service.repo.put('registration', self.case_id, {'record_id': 'synthetic-record', 'case_revision': 5})
        newer = self.queue_case_versions(3)[3]
        rerun = self.wait_job(self.service.call('run_outbox'))
        self.assertEqual(rerun['status'], 'succeeded', rerun)
        self.assertEqual(self.facade.registration_calls, [])
        self.assertEqual(self.service.repo.get('outbox', newer['operation_id'])['status'], 'superseded')
        self.assertEqual(self.service.repo.get('registration', self.case_id)['case_revision'], 5)

    def test_outbox_unknown_blocks_newer_versions_and_other_cases_can_progress(self):
        items = self.queue_case_versions(1, 2)
        self.facade.registration_result = {'status': 'unknown'}
        first = self.wait_job(self.service.call('run_outbox'))
        self.assertEqual(first['status'], 'failed', first)
        self.assertEqual(self.facade.registration_calls, [items[1]['operation_id']])
        self.assertEqual(self.service.repo.get('outbox', items[1]['operation_id'])['status'], 'unknown')
        self.assertEqual(self.service.repo.get('outbox', items[2]['operation_id'])['status'], 'pending')
        other = self.service.get_conversation('demo-sofia')['case']
        self.service._queue_outbox(other)
        self.facade.registration_result = {'status': 'succeeded', 'record_id': 'synthetic-other-record'}
        second = self.wait_job(self.service.call('run_outbox'))
        self.assertEqual(second['status'], 'failed', second)
        self.assertEqual(len(self.facade.registration_calls), 2)
        self.assertIsNone(self.service.repo.get('registration', self.case_id))
        self.assertEqual(self.service.repo.get('registration', other['case_id'])['case_revision'], other['revision'])
        self.assertEqual(self.service.repo.get('outbox', items[2]['operation_id'])['status'], 'pending')

    def test_outbox_registration_revision_cannot_drop_while_submission_is_in_flight(self):
        self.queue_case_versions(1)
        higher = {'record_id': 'synthetic-new-record', 'number': 'SYN-5',
                  'operation_id': 'synthetic-new-operation', 'case_revision': 5}

        def register(case, previous, operation_id, *, authorization=None):
            if authorization is not None and not authorization():
                return {'status': 'rejected', 'submission_started': False}
            self.service.repo.put('registration', self.case_id, higher)
            return {'status': 'succeeded', 'record_id': 'synthetic-old-record'}

        with patch.object(self.facade, 'register', side_effect=register):
            job = self.wait_job(self.service.call('run_outbox'))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(self.service.repo.get('registration', self.case_id), higher)

    def test_live_unverified_is_empty_and_no_seller_directory_opened(self):
        self.service.close()
        self.facade.runtime_ok = False
        self.service = ReceptionService(self.root, 'live', seller_id='real-expected')
        snapshot = self.service.get_snapshot()
        self.assertEqual(snapshot['conversations'], [])
        self.assertFalse(snapshot['app']['account_verified'])
        self.assertEqual(snapshot['app']['seller_id'], '')
        self.assertFalse((self.root / 'live' / 'sellers').exists())
        job = self.wait_job(self.service.call('check_connection'))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.service.get_snapshot()['checks']['runtime']['status'], 'blocked')
        self.assertEqual(self.service.get_snapshot()['conversations'], [])
        self.assertFalse((self.root / 'live' / 'sellers').exists())

    def test_live_identity_activates_only_matching_seller_and_never_seeds(self):
        self.service.close()
        self.facade.seller = '10001'
        self.service = ReceptionService(self.root, 'live', seller_id='10001')
        job = self.wait_job(self.service.call('check_connection'))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(self.service.get_snapshot()['app']['seller_id'], '10001')
        self.assertEqual(self.service.get_snapshot()['conversations'], [])
        original = self.service.repo.path
        self.facade.seller = '10002'
        job = self.wait_job(self.service.call('check_connection'))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.service.repo.path, original)
        self.assertEqual(self.service.get_snapshot()['app']['seller_id'], '10001')

    def test_inflight_operation_and_outbox_recover_unknown(self):
        draft = self.generate()
        self.service.repo.put('operation', 'crashed-send', {'operation_id': 'crashed-send', 'draft_id': draft['draft_id'],
            'case_id': self.case_id, 'conversation_id': self.cid, 'source_message_id': draft['source_message_id'], 'status': 'sending'})
        item = self.service.list_outbox()[0]
        item['status'] = 'syncing'
        self.service.repo.put('outbox', item['operation_id'], item)
        self.service.close()
        self.service = ReceptionService(self.root)
        self.assertEqual(self.service.reconcile_operation('crashed-send')['status'], 'unknown')
        self.assertEqual(self.service.list_outbox()[0]['status'], 'unknown')
        self.assertEqual(self.service.get_conversation(self.cid)['draft']['status'], 'unknown')

    def test_selected_case_is_read_only_and_cross_conversation_is_rejected(self):
        conversation = self.service.repo.get('conversation', self.cid)
        with self.service.repo.transaction():
            second = self.service._new_case(conversation, 'second-procurement')
        selected_draft = self.service.save_draft(second['case_id'], 'Separate procurement reply', None, 'second-case-save')
        selected = self.service.call('get_conversation', {'conversation_id': self.cid, 'case_id': second['case_id']})
        self.assertEqual(selected['case']['case_id'], second['case_id'])
        self.assertEqual(selected['draft']['draft_id'], selected_draft['draft_id'])
        self.assertEqual(self.service.repo.get('conversation', self.cid)['case_id'], self.case_id)
        self.assertEqual(self.service.get_conversation(self.cid)['case']['case_id'], self.case_id)
        sofia_case = self.service.get_conversation('demo-sofia')['case']['case_id']
        with self.assertRaises(ServiceError) as caught:
            self.service.call('get_conversation', {'conversation_id': self.cid, 'case_id': sofia_case})
        self.assertEqual(caught.exception.code, 'case_conversation_mismatch')

    def test_settings_are_validated_and_saving_revokes_auto_send(self):
        self.service.set_scope('selected', [self.cid], 2)
        self.service.set_reception(True, True)
        self.service.save_settings(poll_seconds=25, required_fields=['quantity'])
        snapshot = self.service.get_snapshot()
        self.assertFalse(snapshot['reception']['auto_send'])
        self.assertFalse(snapshot['reception']['running'])
        self.assertIn('product', snapshot['settings']['required_fields'])
        with self.assertRaises(ServiceError):
            self.service.save_settings(secret='not allowed')
        with self.assertRaises(ServiceError):
            self.service.save_settings(feishu={'app_secret': 'not allowed'})


if __name__ == '__main__':
    unittest.main()
