"""Real service/SQLite/worker/demo transport continuity; all messages are synthetic."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest

from reception_service.errors import ServiceError
from reception_service.repository import now_ms
from reception_service.service import ReceptionService


class ContinuityEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='arw22-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = ReceptionService(self.root)
        self.addCleanup(self.service.close)
        self.service.settings.update(merge_seconds=0, poll_seconds=3600)
        self.service._next_poll = time.monotonic() + 3600
        self.cid = 'demo-emma'
        self.calls = []

    def wait(self, check, message='condition did not finish', timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = check()
            if value:
                return value
            time.sleep(.02)
        self.fail(message + ': ' + repr([(j['kind'], j['status'], j.get('message')) for j in self.service.list_jobs()[:8]]))

    def output(self, context, reply='Thanks for the details. I will continue helping with your request.', **changes):
        source = next(m for m in reversed(context['recent_messages']) if m['role'] == 'buyer')
        result = dict(action='ask', facts={}, question_keys=[], reply=reply, summary=context['case']['summary'],
                      missing=[], conflicts=[], procurement_key=context['case']['procurement_key'] or 'main',
                      new_case=False, reply_sources=[source['message_id']])
        result.update(changes)
        return result

    def install(self, behavior=None):
        def generate(context, request_id, *, on_call_start=None):
            if on_call_start:
                on_call_start()
            self.calls.append(copy.deepcopy(context))
            return behavior(context, len(self.calls)) if behavior else self.output(context)
        self.service.integrations.generate = generate

    def enable(self):
        self.service.set_scope('selected', [self.cid], 1)
        self.service.set_reception(True, auto_send=True)
        self.service._next_poll = time.monotonic() + 3600

    def accepted(self, phase=None):
        return [o for o in self.service.repo.list('operation', conversation_id=self.cid)
                if o['status'] == 'accepted' and (phase is None or o.get('intent', {}).get('continuity_phase') == phase)]

    def assistance(self, kind='verify_followup'):
        with self.service._lock, self.service.repo.transaction():
            conversation = self.service.repo.get('conversation', self.cid)
            case = self.service.repo.get('case', conversation['case_id'])
            return self.service._create_assistance(case, self.service._latest_buyer(self.cid), None, kind, [])

    def save(self, task, result='This item is not currently in stock.', public=True, schedule=True, mutation='save-result'):
        return self.service.save_assistance_result(task_id=task['task_id'], expected_revision=task['revision'],
                    result=result, result_public=public, schedule_followup=schedule, client_mutation_id=mutation)

    def task(self, task):
        return self.service.repo.get('assistance_task', task['task_id'])

    def test_quality_human_and_bad_fact_do_not_block_real_automatic_transport(self):
        def behavior(context, attempt):
            source = next(m for m in reversed(context['recent_messages']) if m['role'] == 'buyer')
            return self.output(context, reply='We have this in stock and can ship for $17 tomorrow.', action='human',
                       facts={'product': {'value': source['text'], 'evidence_ids': [source['message_id']]},
                              'quantity': {'value': 'NEVER_IN_SOURCE_123', 'evidence_ids': [source['message_id']]}})
        self.install(behavior)
        self.enable()
        self.wait(lambda: self.accepted(), 'quality warning prevented automatic send')
        detail = self.service.get_conversation(self.cid)
        self.assertFalse(detail['conversation']['human_owned'])
        self.assertNotEqual(detail['case']['state'], 'needs_human')
        self.assertNotIn('quantity', detail['case']['facts'])
        self.assertTrue(detail['draft']['validation']['warnings'])
        self.assertTrue(detail['assistance_tasks'])
        self.assertEqual(detail['draft']['status'], 'accepted')

    def test_timeout_acknowledges_once_then_generates_and_sends_final(self):
        def behavior(context, attempt):
            if attempt == 1:
                raise ServiceError('timeout', 'Synthetic timeout', stage='analysis')
            self.assertEqual(context['continuation']['phase'], 'final')
            self.assertEqual(context['recent_messages'][-1]['role'], 'seller')
            return self.output(context, reply='Here is the complete response to your original message.')
        self.install(behavior)
        self.enable()
        self.wait(lambda: self.accepted('final'), 'final response never followed acknowledgement')
        self.assertEqual(len(self.accepted('ack')), 1)
        self.assertEqual(len(self.accepted('final')), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.service._ai_budget_used, 2)
        self.assertEqual(self.service._budget_used, 2)
        time.sleep(.25)
        self.assertEqual(len(self.service.repo.list('operation', conversation_id=self.cid)), 2)

    def test_explicit_generation_format_failure_retries_without_global_reception(self):
        def behavior(context, attempt):
            if attempt == 1:
                raise ServiceError('answer_non_json', 'Synthetic incomplete JSON', stage='answer')
            return self.output(context)
        self.install(behavior)
        case = self.service.get_conversation(self.cid)['case']
        self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']})
        self.wait(lambda: self.service.get_conversation(self.cid)['draft'], 'manual requested generation did not recover')
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.service.get_snapshot()['reception']['running'])
        self.assertEqual(self.service.repo.list('operation'), [])
        self.assertEqual(self.service._ai_budget_used, 2)

    def test_public_result_follows_seller_reply_private_result_stays_internal_and_save_is_idempotent(self):
        self.install()
        self.enable()
        self.wait(lambda: self.accepted())
        internal = self.assistance('model_request')
        self.save(internal, 'PRIVATE_SYNTHETIC_NOTE', public=False, schedule=False, mutation='internal')
        public = self.assistance()
        response = self.save(public)
        self.assertTrue(response['followup_id'])
        self.assertNotEqual(response['followup_status'], 'followed_up')
        self.assertEqual(self.save(public), response)
        self.wait(lambda: self.task(public)['followup_status'] == 'followed_up', 'public result never followed up')
        messages = self.service._messages(self.cid)
        self.assertEqual(sum(m['role'] == 'seller' and m['text'] == 'This item is not currently in stock.' for m in messages), 1)
        self.assertFalse(any('PRIVATE_SYNTHETIC_NOTE' in m['text'] for m in messages if m['role'] == 'seller'))
        case = self.service.get_conversation(self.cid)['case']
        self.assertNotIn('PRIVATE_SYNTHETIC_NOTE', repr(self.service.get_context(case['case_id'])))
        other = self.service.get_conversation('demo-sofia')['case']
        self.assertNotIn('This item is not currently in stock.', repr(self.service.get_context(other['case_id'])))
        self.assertFalse(self.service.get_conversation(self.cid)['conversation']['human_owned'])
        self.assertIsNone(self.service.repo.get('chat_draft', self.cid))
        current = self.task(public)
        repeated = self.save(current, mutation='new-save-id-same-verified-result')
        self.assertEqual(repeated['followup_id'], response['followup_id'])
        time.sleep(.2)
        self.assertEqual(len(self.accepted('assistance')), 1)

    def test_takeover_waits_and_explicit_customer_resume_allows_followup(self):
        self.install()
        self.enable()
        self.wait(lambda: self.accepted())
        self.service.begin_manual_edit(self.cid)
        task = self.assistance()
        response = self.save(task)
        self.assertEqual(response['followup_status'], 'waiting_manual')
        count = len(self.accepted())
        time.sleep(.25)
        self.assertEqual(len(self.accepted()), count)
        self.service.takeover(self.cid, False)
        self.wait(lambda: self.task(task)['followup_status'] == 'followed_up')
        self.assertEqual(len(self.accepted()), count + 1)

    def test_unknown_followup_only_reconciles_original_operation(self):
        self.install()
        self.enable()
        self.wait(lambda: self.accepted())
        sends, reads = [], []
        permitted = threading.Event()
        def send(intent, authorization):
            self.assertTrue(authorization())
            sends.append(copy.deepcopy(intent))
            return dict(status='unknown', message_id='synthetic-exact-receipt', submission_started=True)
        def reconcile(operation_id):
            reads.append(operation_id)
            if permitted.is_set():
                return dict(status='accepted', message_id='synthetic-exact-receipt', submission_started=True, timestamp_ms=now_ms())
            return dict(status='unknown', submission_started=True)
        self.service.integrations.send_chat = send
        self.service.integrations.reconcile_operation = reconcile
        task = self.assistance()
        response = self.save(task)
        self.wait(lambda: self.task(task)['followup_status'] == 'waiting_reconciliation')
        self.wait(lambda: reads)
        self.assertEqual(self.save(task), response)
        self.assertEqual(len(sends), 1)
        permitted.set()
        with self.service._lock:
            operation = next(o for o in self.service.repo.list('operation') if o['status'] == 'unknown')
            operation['next_reconcile_at'] = 0
            self.service.repo.put('operation', operation['operation_id'], operation)
        self.wait(lambda: self.task(task)['followup_status'] == 'followed_up')
        self.assertEqual(len(sends), 1)
        self.assertEqual(set(reads), {operation['operation_id']})

    def test_new_message_during_generation_replans_and_never_sends_old_answer(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def behavior(context, attempt):
            if attempt == 1:
                entered.set()
                self.assertTrue(release.wait(5))
                return self.output(context, reply='OLD_SYNTHETIC_RESPONSE')
            return self.output(context, reply='UPDATED_SYNTHETIC_RESPONSE')
        self.install(behavior)
        self.enable()
        self.assertTrue(entered.wait(3))
        self.service.inject_demo(self.cid, 'Updated question: please confirm the new destination.')
        release.set()
        self.wait(lambda: self.accepted())
        messages = self.service._messages(self.cid)
        self.assertTrue(any(m['role'] == 'seller' and m['text'] == 'UPDATED_SYNTHETIC_RESPONSE' for m in messages))
        self.assertFalse(any(m['role'] == 'seller' and m['text'] == 'OLD_SYNTHETIC_RESPONSE' for m in messages))
        self.assertGreaterEqual(len(self.calls), 2)

    def test_new_buyer_message_before_ordinary_submission_replans_after_definite_rejection(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.service.integrations.send
        attempts = []
        def send(intent, authorization):
            attempts.append(copy.deepcopy(intent))
            if len(attempts) == 1:
                entered.set()
                self.assertTrue(release.wait(5))
                self.assertFalse(authorization())
            return original(intent, authorization)
        self.service.integrations.send = send
        self.install(lambda context, attempt: self.output(context, reply='OLD_UNSUBMITTED_ANSWER' if attempt == 1 else 'LATEST_BUYER_ANSWER'))
        self.enable()
        self.assertTrue(entered.wait(3))
        self.service.inject_demo(self.cid, 'Please answer this updated question about delivery to Paris.')
        release.set()
        self.wait(lambda: self.accepted(), 'unsubmitted old draft blocked the latest buyer question')
        old = next(o for o in self.service.repo.list('operation') if o['source_message_id'] == attempts[0]['source_message_id'])
        self.assertEqual(old['status'], 'rejected')
        self.assertIs(old['submission_started'], False)
        self.assertEqual(self.service.repo.get('draft', old['draft_id'])['status'], 'stale')
        replies = [m['text'] for m in self.service._messages(self.cid) if m['role'] == 'seller']
        self.assertEqual(replies, ['LATEST_BUYER_ANSWER'])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(self.calls), 2)

    def test_new_buyer_message_before_final_submission_cancels_old_plan_and_replans(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.service.integrations.send_chat
        finals = []
        def generate(context, attempt):
            if attempt == 1:
                raise ServiceError('timeout', 'Synthetic timeout', stage='analysis')
            return self.output(context, reply='OLD_UNSUBMITTED_FINAL' if attempt == 2 else 'LATEST_AFTER_ACK_ANSWER')
        def send(intent, authorization):
            if intent.get('continuity_phase') == 'final':
                finals.append(copy.deepcopy(intent))
                entered.set()
                self.assertTrue(release.wait(5))
                self.assertFalse(authorization())
            return original(intent, authorization)
        self.service.integrations.send_chat = send
        self.install(generate)
        self.enable()
        self.assertTrue(entered.wait(5))
        self.service.inject_demo(self.cid, 'A newer question: please clarify the packaging material.')
        release.set()
        self.wait(lambda: any(m['text'] == 'LATEST_AFTER_ACK_ANSWER' for m in self.service._messages(self.cid)),
                  'unsubmitted final draft blocked the latest buyer question')
        plan = next(p for p in self.service.repo.list('continuity_plan') if p['phase'] == 'final')
        operation = self.service.repo.get('operation', plan['operation_id'])
        self.assertEqual(plan['status'], 'cancelled')
        self.assertEqual(operation['status'], 'rejected')
        self.assertIs(operation['submission_started'], False)
        self.assertEqual(self.service.repo.get('draft', plan['draft_id'])['status'], 'stale')
        self.assertEqual(len(self.accepted('ack')), 1)
        self.assertEqual(self.accepted('final'), [])
        self.assertEqual(len(finals), 1)
        self.assertFalse(any(m['text'] == 'OLD_UNSUBMITTED_FINAL' for m in self.service._messages(self.cid)))
        self.assertEqual(len(self.calls), 3)

    def test_verified_result_during_unsubmitted_final_keeps_request_fence_and_replans(self):
        from unittest.mock import patch
        from reception_service.integrations.accio_native.chat import _claim_continuity
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        claims = []
        def claim(gateway, intent):
            _claim_continuity(gateway, intent)
            if intent.get('continuity_phase') == 'final':
                claims.append(copy.deepcopy(intent))
                if len(claims) == 1:
                    entered.set()
                    self.assertTrue(release.wait(5))
        def generate(context, attempt):
            if attempt == 1:
                raise ServiceError('timeout', 'Synthetic timeout', stage='analysis')
            if attempt == 3:
                self.assertIn('VERIFIED_CONTEXT_UPDATE', repr(context['knowledge']))
            return self.output(context, reply='OLD_FINAL_BASIS' if attempt == 2 else 'UPDATED_VERIFIED_FINAL')
        self.install(generate)
        with patch('reception_service.integrations.accio_native.chat._claim_continuity', side_effect=claim):
            self.enable()
            self.assertTrue(entered.wait(5))
            task = self.assistance()
            self.save(task, result='VERIFIED_CONTEXT_UPDATE', schedule=False)
            release.set()
            self.wait(lambda: any(j['kind'] == 'automatic_send' and j['status'] == 'failed'
                                  for j in self.service.list_jobs()), 'same-source retry was not blocked')
        self.assertEqual(len(claims), 1)
        self.assertEqual(self.service.integrations.ledger.get(claims[0]['operation_id'])[2]['status'], 'rejected')
        self.assertIs(self.service.integrations.ledger.get(claims[0]['operation_id'])[2]['submission_started'], False)
        self.assertEqual(len(self.accepted('final')), 0)
        replies = [m['text'] for m in self.service._messages(self.cid) if m['role'] == 'seller']
        self.assertNotIn('UPDATED_VERIFIED_FINAL', replies)
        self.assertNotIn('OLD_FINAL_BASIS', replies)
        self.assertFalse(self.service.get_conversation(self.cid)['conversation']['human_owned'])

    def test_pause_during_generation_fences_late_result_and_counts_attempt(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def behavior(context, attempt):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.output(context)
        self.install(behavior)
        self.enable()
        self.assertTrue(entered.wait(3))
        self.service.set_reception(False)
        release.set()
        self.wait(lambda: not any(j['status'] in ('queued', 'running') for j in self.service.list_jobs()))
        self.assertEqual(self.accepted(), [])
        self.assertEqual(self.service._ai_budget_used, 1)
        self.assertFalse(self.service.get_snapshot()['reception']['running'])

    def test_over_original_hundred_limit_and_usage_survives_restart_without_authorization(self):
        self.install()
        with self.service._lock, self.service.repo.transaction():
            self.service._ai_budget_used = 150
            self.service._budget_used = 150
        self.enable()
        self.wait(lambda: self.accepted())
        self.assertEqual(self.service._ai_budget_used, 151)
        self.assertEqual(self.service._budget_used, 151)
        self.service.close()
        reopened = ReceptionService(self.root)
        self.addCleanup(reopened.close)
        snapshot = reopened.get_snapshot()
        self.assertEqual(snapshot['reception']['budget']['ai_used'], 151)
        self.assertEqual(snapshot['reception']['budget']['used'], 151)
        self.assertIsNone(snapshot['reception']['budget']['limit'])
        self.assertFalse(snapshot['reception']['running'])
        self.assertFalse(snapshot['reception']['auto_send'])

    def test_waiting_quote_keeps_chatting_and_new_purchase_preserves_old_case(self):
        def initial(context, attempt):
            source = next(m for m in reversed(context['recent_messages']) if m['role'] == 'buyer')
            return self.output(context, facts={key: {'value': source['text'], 'evidence_ids': [source['message_id']]}
                                               for key in context['required_fields']}, action='wait_quote',
                               new_case=True, procurement_key='initial-synthetic-purchase')
        self.service.inject_demo(self.cid, 'I need 500 pieces of ceramic mugs with a custom logo, delivery to Berlin.')
        self.install(initial)
        self.enable()
        self.wait(lambda: self.accepted())
        previous = self.service.get_conversation(self.cid)['case']
        self.assertEqual(previous['state'], 'waiting_quote')
        self.install()
        self.service.inject_demo(self.cid, 'Can you confirm the delivery options?')
        self.wait(lambda: len(self.accepted()) >= 2)
        self.assertEqual(self.service.get_conversation(self.cid)['case']['state'], 'waiting_quote')
        self.service.takeover(self.cid, True)
        self.assertEqual(self.service.get_conversation(self.cid)['case']['state'], 'waiting_quote')
        self.assertTrue(any(c['case_id'] == previous['case_id'] for c in self.service.get_snapshot()['handoff_cases']))
        self.service.takeover(self.cid, False)
        case_count = len(self.service.get_conversation(self.cid)['cases'])
        self.install(lambda context, attempt: self.output(context, new_case=True, procurement_key='second-synthetic-purchase'))
        self.service.inject_demo(self.cid, 'I also want to start a separate purchase for another product.')
        self.wait(lambda: len(self.service.get_conversation(self.cid)['cases']) > case_count)
        self.assertEqual(self.service.repo.get('case', previous['case_id'])['state'], 'waiting_quote')

    def test_schema_failure_recovers_and_discarded_fact_has_a_quality_record(self):
        from reception_service.integrations.analysis import validate_analysis
        def behavior(context, attempt):
            if attempt == 1:
                raise ServiceError('schema_facts', 'Synthetic facts type error', stage='schema')
            raw = self.output(context, facts={'quantity': {'value': 'NOT_IN_BUYER_SOURCE',
                                     'evidence_ids': [context['recent_messages'][-1]['message_id']]}})
            return validate_analysis(raw, context)
        self.install(behavior)
        self.enable()
        self.wait(lambda: any(j['status'] == 'deferred' for j in self.service.list_jobs()))
        status = next(c for c in self.service.get_snapshot()['conversations'] if c['conversation_id'] == self.cid)['reception_status']
        self.assertIn('重试', status['reason'])
        self.wait(lambda: self.accepted())
        detail = self.service.get_conversation(self.cid)
        self.assertNotIn('quantity', detail['case']['facts'])
        self.assertTrue(any(w.get('field') == 'quantity' for w in detail['draft']['validation']['warnings']))
        self.assertTrue(any(any(w.get('field') == 'quantity' for w in j.get('warnings', [])) for j in self.service.list_jobs()))

    def test_final_definite_rejection_obeys_plan_backoff_and_retries_same_body(self):
        def behavior(context, attempt):
            if attempt == 1:
                raise ServiceError('timeout', 'Synthetic timeout', stage='analysis')
            return self.output(context, reply='FINAL_SYNTHETIC_RESPONSE')
        self.install(behavior)
        original = self.service.integrations.send_chat
        finals = []
        def send(intent, authorization):
            if intent.get('continuity_phase') == 'final':
                finals.append(time.monotonic())
                if len(finals) == 1:
                    return dict(status='rejected', submission_started=False, error_code='backend_timeout', stage='cli')
            return original(intent, authorization)
        self.service.integrations.send_chat = send
        self.enable()
        self.wait(lambda: finals)
        time.sleep(.4)
        self.assertEqual(len(finals), 1, 'normal scheduler bypassed plan backoff')
        with self.service._lock:
            plan = next(p for p in self.service.repo.list('continuity_plan') if p['phase'] == 'final')
            self.assertGreater(plan['next_retry_at'], now_ms())
            plan['next_retry_at'] = 0
            self.service.repo.put('continuity_plan', plan['plan_id'], plan)
            operation = self.service.repo.get('operation', plan['operation_id'])
            operation['updated_at'] -= 5001
            self.service.repo.put('operation', operation['operation_id'], operation)
        self.wait(lambda: self.accepted('final'))
        self.assertEqual(len(finals), 2)
        self.assertEqual(len(self.calls), 2)

    def test_ordinary_definite_rejection_retries_after_backoff_without_regenerating(self):
        self.install()
        original = self.service.integrations.send
        checks = []
        def send(intent, authorization):
            checks.append(True)
            if len(checks) == 1:
                return dict(status='rejected', submission_started=False, error_code='backend_timeout', stage='cli')
            return original(intent, authorization)
        self.service.integrations.send = send
        self.enable()
        self.wait(lambda: any(o['status'] == 'rejected' for o in self.service.repo.list('operation')))
        time.sleep(.25)
        self.assertEqual(len(self.service.repo.list('operation')), 1)
        with self.service._lock:
            draft = self.service.get_conversation(self.cid)['draft']
            self.assertGreater(draft['retry_after'], now_ms())
            draft['retry_after'] = 0
            self.service.repo.put('draft', draft['draft_id'], draft)
            operation = self.service.repo.list('operation')[0]
            operation['updated_at'] -= 5001
            self.service.repo.put('operation', operation['operation_id'], operation)
        self.wait(lambda: self.accepted())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.service._budget_used, 2)

    def test_new_question_is_answered_before_older_verification_followup(self):
        self.install()
        self.enable()
        self.wait(lambda: self.accepted())
        task = self.assistance()
        self.install(lambda context, attempt: self.output(context, reply='CURRENT_QUESTION_ANSWER'))
        time.sleep(.02)
        with self.service._lock:
            self.service.inject_demo(self.cid, 'A new question: can you clarify the packaging options?')
            self.save(task)
        self.wait(lambda: self.task(task)['followup_status'] == 'followed_up')
        replies = [m['text'] for m in self.service._messages(self.cid) if m['role'] == 'seller']
        self.assertLess(replies.index('CURRENT_QUESTION_ANSWER'), replies.index('This item is not currently in stock.'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
