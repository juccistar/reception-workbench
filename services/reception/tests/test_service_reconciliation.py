"""Business receipt recovery with temporary SQLite workspaces and mock facades only."""
from __future__ import annotations

import copy
import threading
import time
import unittest
from unittest.mock import patch

import test_manual_chat as chat_fixture
import test_manual_reception as manual_fixture
from reception_service.errors import ServiceError
from reception_service.repository import encoded, now_ms


class ServiceReconciliationTests(unittest.TestCase):
    setUp = manual_fixture.ManualReceptionTests.setUp
    tearDown = manual_fixture.ManualReceptionTests.tearDown
    wait_job = manual_fixture.ManualReceptionTests.wait_job
    ingest = manual_fixture.ManualReceptionTests.ingest
    start_assisted = manual_fixture.ManualReceptionTests.start_assisted

    def idle(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(j['status'] in ('queued', 'running') for j in self.service.repo.list('job')):
                return
            threading.Event().wait(0.005)
        self.fail('Synthetic reception queue did not finish')

    def unknown_automatic(self, *, social=False):
        self.start_assisted(auto=True)
        stamp = max(now_ms(), self.service._started_at_ms + 1)
        self.ingest(mid='synthetic-first-source', text='Hello!' if social else 'I need ceramic mugs.', stamp=stamp)
        self.facade.output = {
            'action': 'ask', 'facts': {}, 'question_keys': [] if social else ['quantity'],
            'reply': 'Hello! How are you?' if social else 'Thank you. What quantity do you need?',
            'summary': '', 'missing': [], 'conflicts': [], 'procurement_key': 'main',
            'new_case': False, 'reply_sources': ['synthetic-first-source'],
        }
        self.facade.send_result = {'status': 'unknown', 'message_id': None, 'submission_started': True}
        self.schedule(self.service)
        self.idle()
        job = next(j for j in self.service.repo.list('job') if j['kind'] == 'automatic_send')
        self.assertEqual(job['status'], 'unknown', job)
        operation = self.service.repo.get('operation', job['result']['operation_id'])
        draft = self.service.repo.get('draft', operation['draft_id'])
        self.assertEqual(draft['status'], 'unknown')
        self.assertEqual(self.facade.send_calls, 1)
        return operation, draft, job, stamp

    def check(self, operation_id, result):
        with patch.object(self.facade, 'reconcile_operation', return_value=result, create=True) as lookup:
            job = self.wait_job(self.service.call('reconcile_operation', {'operation_id': operation_id}))
        lookup.assert_called_once_with(operation_id)
        return job

    @staticmethod
    def accepted(stamp=None, mid='synthetic-authoritative-message'):
        receipt = {'status': 'accepted', 'message_id': mid, 'submission_started': True}
        if stamp is not None:
            receipt['timestamp_ms'] = stamp
        return receipt

    def test_automatic_recovery_preserves_newer_buyer_order_and_allows_next_reply(self):
        operation, draft, original_job, source_stamp = self.unknown_automatic()
        self.ingest(mid='synthetic-next-source', text='Please use blue mugs.', stamp=source_stamp + 200)
        self.schedule(self.service)
        self.idle()
        self.assertEqual(self.facade.generate_calls, 1)  # Unknown blocks the newer source.
        before_case = self.service.repo.get('case', self.case_id)
        before_outbox = self.service.repo.list('outbox')
        before_budget = self.service._budget()
        receipt = self.accepted(source_stamp + 100, mid='platform-ID-with-Exact-Case:01')
        checked = self.check(operation['operation_id'], receipt)
        self.assertEqual(checked['status'], 'succeeded', checked)
        self.assertEqual(checked['result']['status'], 'accepted')
        saved = self.service.repo.get('operation', operation['operation_id'])
        self.assertEqual(saved['status'], 'accepted')
        self.assertEqual(saved['message_id'], receipt['message_id'])
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'accepted')
        repaired_job = self.service.repo.get('job', original_job['job_id'])
        self.assertEqual(repaired_job['status'], 'succeeded')
        self.assertEqual(repaired_job['result']['status'], 'accepted')
        self.assertEqual(repaired_job['result']['operation_id'], operation['operation_id'])
        self.assertEqual(repaired_job['result']['message_id'], receipt['message_id'])
        seller = self.service.repo.get('message', encoded([self.cid, receipt['message_id']]))
        self.assertEqual(seller, {
            'message_id': receipt['message_id'], 'seller_id': self.facade.seller, 'buyer_id': '10002',
            'conversation_id': self.cid, 'role': 'seller', 'text': draft['body'],
            'timestamp_ms': source_stamp + 100, 'attachments': [], 'own': True,
        })
        messages = self.service._messages(self.cid)
        ids = [m['message_id'] for m in messages]
        self.assertLess(ids.index('synthetic-first-source'), ids.index(receipt['message_id']))
        self.assertLess(ids.index(receipt['message_id']), ids.index('synthetic-next-source'))
        conversation = self.service.repo.get('conversation', self.cid)
        self.assertEqual(conversation['last_timestamp'], source_stamp + 200)
        self.assertEqual(conversation['last_message'], 'Please use blue mugs.')
        self.assertEqual(self.service.repo.get('case', self.case_id), before_case)
        self.assertEqual(self.service.repo.list('outbox'), before_outbox)
        self.assertEqual(self.service._budget(), before_budget)
        self.assertEqual(self.facade.send_calls, 1)  # Reconciliation never resends.
        self.assertEqual(self.facade.registration_calls, [])

        self.facade.output['reply_sources'] = ['synthetic-next-source']
        self.facade.send_result = self.accepted(source_stamp + 300, mid='synthetic-next-reply')
        self.schedule(self.service)
        self.idle()
        jobs = self.service.repo.list('job')
        generated = [j for j in jobs if j['kind'] == 'generate_draft']
        self.assertEqual(len(generated), 2)
        self.assertEqual(generated[-1]['source_message_id'], 'synthetic-next-source')
        self.assertEqual(generated[-1]['status'], 'succeeded')
        sent = [j for j in jobs if j['kind'] == 'automatic_send']
        self.assertEqual(sent[-1]['status'], 'succeeded', sent[-1])
        self.assertEqual(self.facade.send_calls, 2)

    def test_repeated_accepted_reconciliation_is_idempotent(self):
        operation, draft, original_job, stamp = self.unknown_automatic()
        before_case = self.service.repo.get('case', self.case_id)
        before_outbox = self.service.repo.list('outbox')
        result = self.accepted(stamp + 1)
        self.check(operation['operation_id'], result)
        entities = {kind: self.service.repo.list(kind) for kind in ('operation', 'draft', 'case', 'message', 'outbox')}
        original_history = self.service.repo.history('job', original_job['job_id'])
        with patch.object(self.facade, 'reconcile_operation', create=True) as lookup:
            for _ in range(3):
                self.assertEqual(self.service.reconcile_operation(operation['operation_id'])['status'], 'accepted')
            checked = self.wait_job(self.service.call('reconcile_operation', {'operation_id': operation['operation_id']}))
            self.assertEqual(checked['status'], 'succeeded')
        lookup.assert_not_called()
        for kind, rows in entities.items():
            self.assertEqual(self.service.repo.list(kind), rows, kind)
        self.assertEqual(self.service.repo.history('job', original_job['job_id']), original_history)
        self.assertEqual(self.service.repo.get('case', self.case_id), before_case)
        self.assertEqual(self.service.repo.list('outbox'), before_outbox)
        self.assertEqual(len(self.service.repo.history('message', encoded([self.cid, result['message_id']]))), 1)
        self.assertEqual(self.facade.send_calls, 1)

    def test_unconfirmed_reconciliation_jobs_stay_unknown_and_keep_all_fences(self):
        operation, draft, original_job, stamp = self.unknown_automatic()
        invalid = [None, {}, {'status': 'unknown'},
                   {'status': 'accepted', 'submission_started': True, 'timestamp_ms': stamp + 1},
                   {'status': 'accepted', 'message_id': 123, 'submission_started': True},
                   {'status': 'accepted', 'message_id': '  ', 'submission_started': True},
                   {'status': 'accepted', 'message_id': 'synthetic-id', 'submission_started': 1},
                   {'status': 'accepted', 'message_id': 'synthetic-id', 'submission_started': False},
                   {'status': 'rejected'}, {'status': 'rejected', 'submission_started': True},
                   {'status': 'rejected', 'submission_started': 0}]
        before_case = self.service.repo.get('case', self.case_id)
        before_messages = self.service._messages(self.cid)
        for result in invalid:
            with self.subTest(result=result):
                checked = self.check(operation['operation_id'], result)
                self.assertEqual(checked['status'], 'unknown', checked)
                self.assertEqual(checked['result']['status'], 'unknown')
                self.assertEqual(self.service.repo.get('operation', operation['operation_id'])['status'], 'unknown')
                self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'unknown')
                self.assertEqual(self.service.repo.get('job', original_job['job_id'])['status'], 'unknown')
                with self.assertRaises(ServiceError):
                    self.service.save_draft(self.case_id, 'Another reply', draft['revision'], 'synthetic-resave')
        with patch.object(self.facade, 'reconcile_operation', side_effect=RuntimeError('Synthetic unavailable receipt'), create=True):
            checked = self.wait_job(self.service.call('reconcile_operation', {'operation_id': operation['operation_id']}))
        self.assertEqual(checked['status'], 'unknown')
        self.assertEqual(self.service._messages(self.cid), before_messages)
        self.assertEqual(self.service.repo.get('case', self.case_id), before_case)
        self.schedule(self.service)
        self.idle()
        self.assertEqual(self.facade.send_calls, 1)
        self.assertEqual(self.facade.generate_calls, 1)

    def test_acceptance_without_platform_time_does_not_invent_history(self):
        operation, draft, original_job, _ = self.unknown_automatic()
        before = self.service._messages(self.cid)
        checked = self.check(operation['operation_id'], self.accepted())
        self.assertEqual(checked['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'accepted')
        self.assertEqual(self.service.repo.get('job', original_job['job_id'])['status'], 'succeeded')
        self.assertEqual(self.service._messages(self.cid), before)

    def test_invalid_platform_time_does_not_become_local_time(self):
        operation, _, _, _ = self.unknown_automatic()
        before = self.service._messages(self.cid)
        self.check(operation['operation_id'], {**self.accepted(), 'timestamp_ms': True})
        self.assertEqual(self.service._messages(self.cid), before)

    def test_existing_platform_history_is_preserved(self):
        operation, draft, _, stamp = self.unknown_automatic()
        result = self.accepted(stamp + 20)
        message = {'message_id': result['message_id'], 'seller_id': self.facade.seller, 'buyer_id': '10002',
                   'conversation_id': self.cid, 'role': 'seller', 'text': draft['body'],
                   'timestamp_ms': stamp + 10, 'attachments': [{'kind': 'image', 'url': 'https://example.invalid/item.png'}],
                   'own': True}
        self.service._ingest(self.service.repo.get('conversation', self.cid), [message])
        key = encoded([self.cid, message['message_id']])
        history = self.service.repo.history('message', key)
        conversation = self.service.repo.get('conversation', self.cid)
        self.check(operation['operation_id'], result)
        self.assertEqual(self.service.repo.get('message', key), message)
        self.assertEqual(self.service.repo.history('message', key), history)
        self.assertEqual(self.service.repo.get('conversation', self.cid), conversation)

    def test_only_the_original_draft_and_send_job_can_be_repaired(self):
        operation, draft, original_job, stamp = self.unknown_automatic()
        changed = {**draft, 'revision': draft['revision'] + 1, 'status': 'pending', 'body': 'A newer draft body'}
        self.service.repo.put('draft', draft['draft_id'], changed)
        unrelated = copy.deepcopy(original_job)
        unrelated.update(job_id='synthetic-unrelated-job', result={**original_job['result'], 'operation_id': 'another-operation'})
        self.service.repo.put('job', unrelated['job_id'], unrelated)
        conversation = self.service.repo.get('conversation', self.cid)
        new_case = self.service._new_case(conversation, 'synthetic-new-purchase')
        conversation['case_id'] = new_case['case_id']
        self.service.repo.put('conversation', self.cid, conversation)
        new_draft = self.service.save_draft(new_case['case_id'], 'Thank you. I will review your request.', None, 'synthetic-new-case-draft')
        self.check(operation['operation_id'], self.accepted(stamp + 1))
        self.assertEqual(self.service.repo.get('draft', draft['draft_id']), changed)
        self.assertEqual(self.service.repo.get('draft', new_draft['draft_id']), new_draft)
        self.assertEqual(self.service.repo.get('case', new_case['case_id']), new_case)
        self.assertEqual(self.service.repo.get('job', unrelated['job_id']), unrelated)
        self.assertEqual(self.service.repo.get('job', original_job['job_id'])['status'], 'succeeded')
        self.assertFalse(any(m['text'] == changed['body'] for m in self.service._messages(self.cid)))

    def test_social_reconciliation_preserves_procurement_state_questions_and_registration(self):
        operation, draft, _, stamp = self.unknown_automatic(social=True)
        self.assertTrue(draft['social_reply'])
        case = self.service.repo.get('case', self.case_id)
        case.update(state='waiting_quote', questions={'quantity': 2})
        self.service.repo.put('case', self.case_id, case)
        self.service._queue_outbox(case)
        before_outbox = self.service.repo.list('outbox')
        self.check(operation['operation_id'], self.accepted(stamp + 1))
        self.assertEqual(self.service.repo.get('case', self.case_id), case)
        self.assertEqual(self.service.repo.list('outbox'), before_outbox)
        self.assertEqual(self.facade.registration_calls, [])

    def test_explicit_rejection_repairs_only_original_statuses_without_a_resend(self):
        operation, draft, original_job, _ = self.unknown_automatic()
        before_case = self.service.repo.get('case', self.case_id)
        before_messages = self.service._messages(self.cid)
        checked = self.check(operation['operation_id'], {'status': 'rejected', 'submission_started': False})
        self.assertEqual(checked['status'], 'failed')
        self.assertEqual(checked['result']['status'], 'rejected')
        self.assertEqual(self.service.repo.get('operation', operation['operation_id'])['status'], 'rejected')
        self.assertIs(self.service.repo.get('operation', operation['operation_id'])['submission_started'], False)
        self.assertEqual(self.service.repo.get('draft', draft['draft_id'])['status'], 'rejected')
        self.assertEqual(self.service.repo.get('job', original_job['job_id'])['status'], 'failed')
        self.assertEqual(self.service.repo.get('case', self.case_id), before_case)
        self.assertEqual(self.service._messages(self.cid), before_messages)
        self.assertEqual(self.facade.send_calls, 1)

    def test_registration_query_job_reports_unknown_until_authority_confirms(self):
        self.service._queue_outbox(self.service.repo.get('case', self.case_id))
        item = self.service.repo.list('outbox')[0]
        item['status'] = 'unknown'
        self.service.repo.put('outbox', item['operation_id'], item)
        for result in ({'status': 'unknown'}, {'status': 'failed'}, {'status': 'accepted'}):
            with self.subTest(result=result):
                checked = self.check(item['operation_id'], result)
                self.assertEqual(checked['status'], 'unknown')
                self.assertEqual(checked['result']['status'], 'unknown')
        checked = self.check(item['operation_id'], {'status': 'rejected', 'submission_started': False})
        self.assertEqual(checked['status'], 'failed')
        checked = self.check(item['operation_id'], {'status': 'accepted', 'record_id': 'synthetic-record'})
        self.assertEqual(checked['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('registration', self.case_id)['record_id'], 'synthetic-record')
        self.assertEqual(self.facade.registration_calls, [])


class ManualChatReconciliationTests(unittest.TestCase):
    setUp = chat_fixture.ManualChatTests.setUp
    tearDown = chat_fixture.ManualChatTests.tearDown
    wait_job = chat_fixture.ManualChatTests.wait_job
    ingest = chat_fixture.ManualChatTests.ingest
    save = chat_fixture.ManualChatTests.save
    params = chat_fixture.ManualChatTests.params
    send = chat_fixture.ManualChatTests.send

    def test_chat_recovery_repairs_bound_job_and_retains_normal_manual_chat(self):
        self.facade.chat_result = {'status': 'unknown', 'submission_started': True}
        draft = self.save('A handwritten greeting.')
        original = self.send(draft)
        self.assertEqual(original['status'], 'unknown')
        operation_id = original['result']['operation_id']
        # An explicit operation.job_id takes precedence over a copied result.
        unrelated = copy.deepcopy(original)
        unrelated['job_id'] = 'synthetic-copied-chat-job'
        self.service.repo.put('job', unrelated['job_id'], unrelated)
        stamp = now_ms() + 10
        self.ingest(mid='synthetic-later-chat-source', text='A later buyer message', stamp=stamp + 10)
        before_case = self.service.repo.get('case', self.case_id)
        before_outbox = self.service.repo.list('outbox')
        receipt = {'status': 'accepted', 'message_id': 'synthetic-original-chat-id',
                   'submission_started': True, 'timestamp_ms': stamp}
        with patch.object(self.facade, 'reconcile_operation', return_value=receipt, create=True):
            checked = self.wait_job(self.service.call('reconcile_operation', {'operation_id': operation_id}))
        self.assertEqual(checked['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('job', original['job_id'])['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('job', unrelated['job_id']), unrelated)
        self.assertEqual(self.service.repo.get('chat_draft', self.cid)['status'], 'accepted')
        seller = self.service.repo.get('message', encoded([self.cid, receipt['message_id']]))
        self.assertEqual(seller['timestamp_ms'], stamp)
        self.assertEqual(seller['text'], draft['body'])
        self.assertEqual(self.service.repo.get('conversation', self.cid)['last_timestamp'], stamp + 10)
        self.assertEqual(self.service.repo.get('case', self.case_id), before_case)
        self.assertEqual(self.service.repo.list('outbox'), before_outbox)
        self.assertEqual(len(self.facade.chat_intents), 1)
        self.facade.chat_result = {**receipt, 'message_id': 'synthetic-next-chat-id', 'timestamp_ms': stamp + 20}
        self.assertEqual(self.send(self.save('A new handwritten response.'))['status'], 'succeeded')
        self.assertEqual(len(self.facade.chat_intents), 2)
        self.assertEqual(self.service.repo.get('case', self.case_id), before_case)


if __name__ == '__main__':
    unittest.main()
