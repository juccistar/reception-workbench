"""Manual reception permissions with temporary SQLite data and mock integrations only."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_core import MockFacade
from reception_service.errors import ServiceError
from reception_service.repository import new_id, now_ms
from reception_service.service import ReceptionService


class ManualFacade(MockFacade):
    def __init__(self):
        super().__init__()
        self.seller = '10001'
        self.previews = []
        self.intents = []
        self.recent_reads = 0

    def read_recent_conversations(self, seller_id, before_ms=None, count=20):
        self.recent_reads += 1
        return super().read_recent_conversations(seller_id, before_ms, count)

    def preview_send(self, intent):
        self.previews.append(copy.deepcopy(intent))
        return super().preview_send(intent)

    def send(self, intent, authorization):
        self.intents.append(copy.deepcopy(intent))
        return super().send(intent, authorization)


class ManualReceptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.facade = ManualFacade()
        self.factory = patch('reception_service.service.build_integrations', return_value=self.facade)
        self.factory.start()
        self.schedule = ReceptionService._schedule
        self.scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        self.scheduler.start()
        self.service = ReceptionService(self.root, 'live', seller_id=self.facade.seller)
        self.assertEqual(self.wait_job(self.service.call('check_connection'))['status'], 'succeeded')
        self.cid = '10001:10002'
        self.case_id = self.ingest()['case_id']
        self.service.settings['merge_seconds'] = 0

    def tearDown(self):
        self.facade.release.set()
        self.service.close()
        self.scheduler.stop()
        self.factory.stop()
        self.temp.cleanup()

    def wait_job(self, ack):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.repo.get('job', ack['job_id'])
            if job and job['status'] not in ('queued', 'running'):
                return job
            threading.Event().wait(0.005)
        self.fail('mock job did not complete')

    def idle(self):
        for job in self.service.list_jobs():
            self.wait_job(job)

    def ingest(self, mid='synthetic-source', text='I am interested in ceramic mugs.', stamp=None):
        info = {'conversation_id': self.cid, 'buyer_id': '10002', 'customer_name': 'Synthetic customer'}
        self.service._ingest(info, [{'message_id': mid, 'seller_id': self.facade.seller,
            'buyer_id': info['buyer_id'], 'conversation_id': self.cid, 'role': 'buyer', 'own': False,
            'text': text, 'timestamp_ms': now_ms() if stamp is None else stamp, 'attachments': []}],
            has_more=False, full=True)
        return self.service.get_conversation(self.cid)['case']

    def manual(self, body='Thank you. Could you share your preferred quantity?'):
        previous = self.service.get_conversation(self.cid)['draft']
        return self.service.save_draft(self.case_id, body,
            previous['revision'] if previous else None, new_id('synthetic-mutation'))

    def start_manual(self):
        snapshot = self.service.set_reception(True, manual_only=True)
        self.service._next_poll = time.monotonic() + 100
        return snapshot

    def start_assisted(self, auto=False):
        for name in ('ai', 'send_preview'):
            self.service.checks[name]['status'] = 'passed'
        self.service.set_scope('incoming', [], 5)
        snapshot = self.service.set_reception(True, auto_send=auto)
        self.service._next_poll = time.monotonic() + 100
        return snapshot

    def prepare(self, draft):
        return self.service.prepare_send(draft['draft_id'], draft['revision'])

    def confirm(self, confirmation):
        return self.wait_job(self.service.call('confirm_send', {
            'confirmation_id': confirmation['confirmation_id']}))

    def preview(self, draft):
        return self.wait_job(self.service.call('preview_send', {
            'draft_id': draft['draft_id'], 'expected_revision': draft['revision']}))

    def generation_params(self):
        case = self.service.get_conversation(self.cid)['case']
        return {'case_id': case['case_id'], 'expected_case_revision': case['revision']}

    def test_failed_live_ai_does_not_block_manual_preview_and_acceptance(self):
        def fail_ai(context, request_id):
            self.facade.generate_calls += 1
            raise ServiceError('synthetic_ai_failed', 'Synthetic AI failed')
        with patch.object(self.facade, 'generate', side_effect=fail_ai):
            checked = self.wait_job(self.service.call('check_ai'))
        self.assertEqual(checked['status'], 'failed')
        self.assertEqual(self.service.checks['ai']['status'], 'failed')
        self.assertEqual(self.service.checks['send_preview']['status'], 'not_checked')
        snapshot = self.start_manual()['reception']
        self.assertTrue(snapshot['running'])
        self.assertTrue(snapshot['manual_only'])
        self.assertFalse(snapshot['auto_send'])
        self.assertIsNone(snapshot['started_at_ms'])
        self.assertIsNone(self.service._authorized_scope)
        self.assertIsNone(self.service._frozen_scope)
        draft = self.manual()
        self.assertTrue(draft['validation']['ready'], draft)
        self.assertEqual(self.preview(draft)['status'], 'succeeded')
        result = self.confirm(self.prepare(draft))
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['result']['status'], 'accepted')
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertTrue(self.facade.observed_authorization)
        self.assertEqual(self.facade.previews[0]['send_mode'], 'manual')
        self.assertEqual(self.facade.intents[0]['send_mode'], 'manual')
        self.assertNotIn('frozen_scope', self.facade.intents[0])
        self.assertNotIn('budget_reserved', self.facade.intents[0])
        self.assertEqual(self.service._budget()['used'], 0)
        self.assertEqual(self.service._budget()['ai_used'], 0)
        self.assertTrue(any(m['own'] and m['text'] == draft['body']
                            for m in self.service.get_messages(self.cid)['items']))

    def test_manual_requires_verified_identity_and_successful_read(self):
        for status in ('not_checked', 'failed', 'blocked', 'running', 'demo'):
            with self.subTest(read=status):
                self.service.checks['messages']['status'] = status
                with self.assertRaises(ServiceError) as caught:
                    self.service.set_reception(True, manual_only=True)
                self.assertEqual(caught.exception.code, 'connection_required')
        self.service.checks['messages']['status'] = 'passed'
        self.service.account_verified = False
        with self.assertRaises(ServiceError):
            self.service.set_reception(True, manual_only=True)
        self.assertFalse(self.service.get_snapshot()['reception']['running'])

    def test_manual_rejects_auto_send_and_non_boolean_flags(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.assertRaises(ServiceError):
                self.service.set_reception(enabled, auto_send=True, manual_only=True)
        for key in ('enabled', 'auto_send', 'manual_only'):
            params = {'enabled': True, 'auto_send': False, 'manual_only': True, key: 1}
            with self.subTest(flag=key), self.assertRaises(ServiceError):
                self.service.call('set_reception', params)

    def test_manual_schedules_reads_only_even_with_pending_outbox(self):
        self.start_manual()
        self.service.settings['feishu'].update(enabled=True, write_enabled=True)
        self.service._queue_outbox(self.service.repo.get('case', self.case_id))
        monitor_once = self.service._monitor_once
        with patch.object(self.service, '_monitor_once', return_value=None):
            self.service._next_probe = 0
            baseline = self.facade.recent_reads
            monitor_once()
            self.schedule(self.service)
            self.idle()
            monitor_once()  # The next read waits for the probe interval.
            self.schedule(self.service)
            self.idle()
        self.assertEqual(self.facade.recent_reads, baseline + 1)
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.send_calls, 0)
        self.assertEqual(self.facade.registration_calls, [])
        self.assertFalse(any(j['kind'] in ('generate_draft', 'automatic_send', 'run_outbox')
                             for j in self.service.list_jobs()))
        for kind, params in [('generate_draft', self.generation_params()), ('run_outbox', {})]:
            with self.subTest(kind=kind), self.assertRaises(ServiceError) as caught:
                self.service.call(kind, params)
            self.assertEqual(caught.exception.code, 'manual_only')
        draft = self.manual()
        with self.assertRaises(ServiceError):
            self.service._enqueue('automatic_send', {'draft_id': draft['draft_id'],
                'expected_revision': draft['revision']}, automatic=True)

    def test_sync_reads_new_messages_and_invalidates_manual_confirmation(self):
        self.start_manual()
        draft = self.manual()
        confirmation = self.prepare(draft)
        self.facade.pages = [{'messages': [{'message_id': 'synthetic-new', 'seller_id': self.facade.seller,
            'buyer_id': '10002', 'conversation_id': self.cid, 'role': 'buyer', 'own': False,
            'text': 'Please use a blue logo.', 'timestamp_ms': now_ms() + 1, 'attachments': []}],
            'has_more': False, 'next_cursor': None}]
        job = self.wait_job(self.service.call('sync_messages', {'conversation_id': self.cid}))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(self.service.get_conversation(self.cid)['draft']['status'], 'stale')
        self.assertEqual(self.confirm(confirmation)['status'], 'failed')
        self.assertEqual(self.facade.send_calls, 0)
        rebound = self.manual(draft['body'])
        self.assertEqual(rebound['source_message_id'], 'synthetic-new')
        self.assertGreater(rebound['revision'], draft['revision'])
        self.assertEqual(self.preview(rebound)['status'], 'succeeded')
        self.assertEqual(self.facade.generate_calls, 0)

    def test_takeover_invalidates_old_confirmation_but_rebound_manual_can_send(self):
        self.start_manual()
        draft = self.manual()
        confirmation = self.prepare(draft)
        self.service.takeover(self.cid, True)
        self.assertEqual(self.confirm(confirmation)['status'], 'failed')
        stale = self.service.get_conversation(self.cid)['draft']
        self.assertEqual(stale['status'], 'stale')
        rebound = self.manual(draft['body'])
        self.assertEqual(rebound['draft_id'], draft['draft_id'])
        self.assertGreater(rebound['revision'], draft['revision'])
        self.assertEqual(rebound['status'], 'pending')
        self.assertEqual(self.preview(rebound)['status'], 'succeeded')
        self.assertEqual(self.confirm(self.prepare(rebound))['status'], 'succeeded')
        detail = self.service.get_conversation(self.cid)
        self.assertTrue(detail['conversation']['human_owned'])
        self.assertTrue(self.service.repo.get('conversation', self.cid)['manual_chat'])
        self.assertEqual(detail['case']['state'], 'clarifying')
        self.assertTrue(self.facade.observed_authorization)
        self.assertEqual(self.service._budget()['used'], 0)

    def test_assisted_mode_also_allows_manual_send_after_takeover(self):
        self.start_assisted(auto=True)
        self.service.takeover(self.cid, True)
        draft = self.manual()
        self.assertEqual(self.preview(draft)['status'], 'succeeded')
        self.assertEqual(self.confirm(self.prepare(draft))['status'], 'succeeded')
        self.assertEqual(self.service._budget()['used'], 0)
        self.assertEqual(self.facade.intents[0]['send_mode'], 'manual')

    def test_mode_switch_revokes_confirmations_and_preserves_spent_budgets(self):
        old_start = self.start_assisted(auto=True)['reception']['started_at_ms']
        self.service._budget_used = 2
        self.service._ai_budget_used = 3
        draft = self.manual()
        confirmation = self.prepare(draft)
        old_epoch = self.service._epoch
        self.start_manual()
        self.assertGreater(self.service._epoch, old_epoch)
        self.assertIsNone(self.service._authorized_scope)
        self.assertIsNone(self.service._frozen_scope)
        self.assertIsNone(self.service._started_at_ms)
        self.assertEqual(self.service._source_baseline, {})
        self.assertEqual(self.confirm(confirmation)['status'], 'failed')
        manual_confirmation = self.prepare(draft)
        with patch('reception_service.service.now_ms', return_value=old_start + 100):
            resumed = self.service.set_reception(True, False)
        self.assertFalse(resumed['reception']['manual_only'])
        self.assertEqual(resumed['reception']['started_at_ms'], old_start + 100)
        self.assertEqual(self.confirm(manual_confirmation)['status'], 'failed')
        self.assertEqual(self.service._budget()['used'], 2)
        self.assertEqual(self.service._budget()['ai_used'], 3)
        self.assertEqual(self.facade.send_calls, 0)

    def test_pausing_blocks_prepare_and_invalidates_confirmation(self):
        self.start_manual()
        draft = self.manual()
        confirmation = self.prepare(draft)
        self.service.set_reception(False, manual_only=True)
        with self.assertRaises(ServiceError) as caught:
            self.prepare(draft)
        self.assertEqual(caught.exception.code, 'reception_paused')
        self.start_manual()
        self.assertEqual(self.confirm(confirmation)['status'], 'failed')
        self.assertEqual(self.facade.send_calls, 0)

    def test_old_queued_manual_and_automatic_ai_never_execute_in_manual_mode(self):
        self.start_assisted(auto=True)
        self.ingest(mid='new-authorized-source', stamp=self.service._started_at_ms + 1)
        self.facade.block_read = True
        read = self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(1))
        queued = [self.service._enqueue('generate_draft', self.generation_params(), automatic=True),
                  self.service.call('generate_draft', self.generation_params())]
        self.service.settings['feishu'].update(enabled=True, write_enabled=True)
        self.service._queue_outbox(self.service.repo.get('case', self.case_id))
        queued.append(self.service.call('run_outbox'))
        self.start_manual()
        self.facade.release.set()
        self.wait_job(read)
        for ack in queued:
            self.assertEqual(self.wait_job(ack)['status'], 'cancelled')
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.registration_calls, [])
        self.assertEqual(self.service._budget()['ai_used'], 0)
        self.assertIsNone(self.service.get_conversation(self.cid)['draft'])

    def test_running_ai_result_cannot_modify_manual_mode_or_trigger_send(self):
        self.start_assisted(auto=True)
        self.ingest(mid='new-authorized-source', stamp=self.service._started_at_ms + 1)
        self.facade.block_generate = True
        ack = self.service._enqueue('generate_draft', self.generation_params(), automatic=True)
        self.assertTrue(self.facade.started.wait(1))
        case_before = self.service.get_conversation(self.cid)['case']
        self.start_manual()
        draft = self.manual('Thank you. I will review your request.')
        self.facade.release.set()
        job = self.wait_job(ack)
        self.assertEqual(job['status'], 'stale', job)
        self.assertEqual(self.service.get_conversation(self.cid)['case'], case_before)
        self.assertEqual(self.service.get_conversation(self.cid)['draft'], draft)
        self.assertEqual(self.service.repo.get('ai_result', ack['job_id'])['status'], 'stale')
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertEqual(self.facade.send_calls, 0)
        self.assertEqual(self.service.list_outbox(), [])

    def test_failed_running_ai_cannot_change_manual_case_state(self):
        self.facade.block_generate = True
        def fail(context, request_id):
            self.facade.started.set()
            self.facade.release.wait(5)
            raise ServiceError('synthetic_ai_failure', 'Synthetic failure')
        with patch.object(self.facade, 'generate', side_effect=fail):
            ack = self.service.call('generate_draft', self.generation_params())
            self.assertTrue(self.facade.started.wait(1))
            before = self.service.get_conversation(self.cid)['case']
            self.start_manual()
            self.facade.release.set()
            self.assertEqual(self.wait_job(ack)['status'], 'failed')
        self.assertEqual(self.service.get_conversation(self.cid)['case'], before)

    def test_explicit_ai_check_pauses_manual_mode_and_uses_synthetic_context(self):
        self.start_manual()
        draft = self.manual()
        confirmation = self.prepare(draft)
        with patch.object(self.facade, 'generate', wraps=self.facade.generate) as generate:
            checked = self.wait_job(self.service.call('check_ai'))
        self.assertEqual(checked['status'], 'succeeded', checked)
        self.assertFalse(self.service.get_snapshot()['reception']['running'])
        self.assertEqual(self.confirm(confirmation)['status'], 'failed')
        context = generate.call_args.args[0]
        self.assertNotEqual(context['case']['case_id'], self.case_id)
        self.assertNotIn(self.cid, str(context))
        self.assertEqual(self.service.get_conversation(self.cid)['draft'], draft)

    def test_queued_ai_check_is_revoked_when_manual_reception_resumes(self):
        self.facade.block_read = True
        read = self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(1))
        checked = self.service.call('check_ai')
        self.start_manual()
        self.facade.release.set()
        self.wait_job(read)
        self.assertEqual(self.wait_job(checked)['status'], 'cancelled')
        self.assertEqual(self.facade.generate_calls, 0)

    def test_stopped_customer_cannot_be_reopened_by_takeover_or_resaving(self):
        self.start_manual()
        self.ingest(mid='stop-source', text='Please stop contacting me.', stamp=now_ms() + 1)
        self.service.takeover(self.cid, True)
        self.assertEqual(self.service.get_conversation(self.cid)['case']['state'], 'closed')
        draft = self.manual('Thank you for your message.')
        self.assertEqual(self.preview(draft)['status'], 'failed')
        with self.assertRaises(ServiceError):
            self.prepare(draft)
        self.service.takeover(self.cid, False)
        self.assertEqual(self.service.get_conversation(self.cid)['case']['state'], 'closed')
        self.assertEqual(self.facade.send_calls, 0)
        self.assertEqual(self.facade.previews, [])

    def test_final_manual_authorization_rechecks_pause_mode_takeover_and_new_messages(self):
        for change in ('pause', 'mode', 'takeover', 'new_message', 'stop', 'closed', 'shutdown'):
            with self.subTest(change=change):
                self.cid = new_id('synthetic-conversation')
                self.case_id = self.ingest()['case_id']
                self.start_manual()
                self.service.takeover(self.cid, False)
                self.ingest(mid=new_id('source'), stamp=now_ms() + 1)
                draft = self.manual('Thank you. I will review your request.')
                confirmation = self.prepare(draft)
                self.facade.started.clear()
                self.facade.release.clear()
                self.facade.block_send = True
                ack = self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']})
                self.assertTrue(self.facade.started.wait(1))
                if change == 'pause':
                    self.service.set_reception(False, manual_only=True)
                elif change == 'mode':
                    for name in ('ai', 'send_preview'):
                        self.service.checks[name]['status'] = 'passed'
                    self.service.set_reception(True, False)
                elif change == 'takeover':
                    self.service.takeover(self.cid, True)
                elif change == 'new_message':
                    self.ingest(mid=new_id('changed'), text='Please use a blue logo.', stamp=now_ms() + 2)
                elif change == 'stop':
                    self.ingest(mid=new_id('stop'), text='Do not contact me.', stamp=now_ms() + 2)
                elif change == 'closed':
                    case = self.service.repo.get('case', self.case_id)
                    case['state'] = 'closed'
                    self.service.repo.put('case', self.case_id, case)
                else:
                    self.service.call('shutdown')
                self.facade.release.set()
                result = self.wait_job(ack)
                self.assertEqual(result['status'], 'failed', result)
                self.assertIs(self.facade.observed_authorization, False)

    def test_unknown_send_blocks_resave_reprepare_and_replay(self):
        self.start_manual()
        self.service.takeover(self.cid, True)
        draft = self.manual()
        confirmation = self.prepare(draft)
        self.facade.send_result = {'status': 'unknown', 'message_id': None, 'submission_started': True}
        result = self.confirm(confirmation)
        self.assertEqual(result['status'], 'unknown', result)
        operation = result['result']['operation_id']
        self.assertEqual(self.service.reconcile_operation(operation)['status'], 'unknown')
        with self.assertRaises(ServiceError):
            self.manual('A different manual reply')
        with self.assertRaises(ServiceError):
            self.prepare(draft)
        self.assertEqual(self.confirm(confirmation)['status'], 'failed')
        self.assertEqual(self.facade.send_calls, 1)

    def test_accepted_source_remains_deduplicated_after_edit_and_reconfirmation(self):
        self.start_manual()
        draft = self.manual()
        confirmation = self.prepare(draft)
        self.assertEqual(self.confirm(confirmation)['status'], 'succeeded')
        self.assertEqual(self.confirm(confirmation)['status'], 'failed')
        edited = self.manual('Thank you. I will review your request.')
        self.assertEqual(self.preview(edited)['status'], 'failed')
        with self.assertRaises(ServiceError) as caught:
            self.prepare(edited)
        self.assertEqual(caught.exception.code, 'duplicate_send')
        self.assertEqual(self.facade.send_calls, 1)

    def test_policy_version_and_body_validation_still_block_manual_sends(self):
        self.start_manual()
        draft = self.manual('We offer free shipping.')
        self.assertTrue(draft['validation']['ready'])
        self.assertTrue(draft['validation']['warnings'])
        self.assertEqual(draft['body'], 'We offer free shipping.')
        self.assertTrue(self.prepare(draft)['confirmation_id'])
        draft = self.manual()
        confirmation = self.prepare(draft)
        self.service.save_policy(kind='policy', title='Synthetic policy', content='Costs must be checked.',
            product_scope=[], confirmed=True, public=True)
        self.assertEqual(self.confirm(confirmation)['status'], 'failed')
        with self.assertRaises(ServiceError):
            self.prepare(draft)
        rebound = self.manual(draft['body'])
        self.assertEqual(rebound['status'], 'pending')
        self.assertEqual(self.preview(rebound)['status'], 'succeeded')

    def test_final_callback_rechecks_policy_expiry(self):
        self.start_manual()
        policy = self.service.save_policy(kind='policy', title='Synthetic policy', content='Costs must be checked.',
            product_scope=[], confirmed=True, public=True, valid_until=now_ms() + 60_000)
        draft = self.manual()
        self.facade.block_send = True
        confirmation = self.prepare(draft)
        ack = self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']})
        self.assertTrue(self.facade.started.wait(1))
        with patch('reception_service.context.now_ms', return_value=policy['valid_until'] + 1):
            self.facade.release.set()
            result = self.wait_job(ack)
        self.assertEqual(result['status'], 'failed', result)
        self.assertIs(self.facade.observed_authorization, False)

    def test_automatic_paths_still_block_takeover_and_manual_mode(self):
        self.start_assisted(auto=True)
        self.ingest(mid='new-automatic-source', stamp=self.service._started_at_ms + 1)
        self.service.takeover(self.cid, True)
        draft = self.manual()
        self.assertEqual(self.wait_job(self.service._enqueue('automatic_send', {
            'draft_id': draft['draft_id'], 'expected_revision': draft['revision']}, automatic=True))['status'], 'failed')
        generated = self.wait_job(self.service.call('generate_draft', self.generation_params()))
        self.assertEqual(generated['status'], 'failed')
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.send_calls, 0)
        self.start_manual()
        with self.assertRaises(ServiceError):
            self.service._validate_send(draft['draft_id'], draft['revision'])
        with self.assertRaises(ServiceError):
            self.service._perform_send(draft, self.service.repo.get('case', self.case_id),
                self.service._epoch, self.facade, automatic=True)
        self.assertEqual(self.facade.send_calls, 0)

    def test_automatic_start_requires_ai_without_a_customer_draft_preview(self):
        self.service.set_scope('selected', [self.cid], 5)
        for auto in (False, True):
            self.service.checks['ai']['status'] = 'failed'
            self.service.checks['send_preview']['status'] = 'passed'
            with self.subTest(auto=auto), self.assertRaises(ServiceError) as caught:
                self.service.set_reception(True, auto_send=auto)
            self.assertEqual(caught.exception.code, 'checks_required')
            self.service.checks['ai']['status'] = 'passed'
            self.service.checks['send_preview']['status'] = 'not_checked'
            self.assertTrue(self.service.set_reception(True, auto_send=auto)['reception']['running'])
            self.service.set_reception(False)

    def test_pause_with_only_enabled_preserves_manual_mode_and_blocks_idle_outbox(self):
        self.start_manual()
        self.service.settings['feishu'].update(enabled=True, write_enabled=True)
        self.service._queue_outbox(self.service.repo.get('case', self.case_id))
        snapshot = self.service.call('set_reception', {'enabled': False})
        self.assertFalse(snapshot['reception']['running'])
        self.assertTrue(snapshot['reception']['manual_only'])
        self.service._next_outbox = 0
        self.schedule(self.service)
        self.idle()
        self.assertEqual(self.facade.registration_calls, [])
        self.assertEqual(self.facade.generate_calls, 0)
        with self.assertRaises(ServiceError):
            self.service.call('run_outbox')
        # Even an explicit paused flag cannot select a different running mode.
        self.service.set_reception(False, manual_only=False)
        self.assertTrue(self.service.get_snapshot()['reception']['manual_only'])
        self.start_assisted()
        self.assertFalse(self.service.get_snapshot()['reception']['manual_only'])

    def test_same_body_after_accepted_or_rejected_binds_to_new_buyer_source(self):
        self.start_manual()
        for status in ('accepted', 'rejected'):
            with self.subTest(receipt=status):
                self.facade.send_result = {'status': status, 'message_id': new_id('receipt') if status == 'accepted' else None,
                                           'submission_started': status == 'accepted'}
                draft = self.manual()
                result = self.confirm(self.prepare(draft))
                self.assertEqual(result['result']['status'], status)
                self.ingest(mid=new_id('new-source'), stamp=now_ms() + 10)
                rebound = self.manual(draft['body'])
                self.assertNotEqual(rebound['draft_id'], draft['draft_id'])
                self.assertEqual(rebound['status'], 'pending')
                self.assertNotEqual(rebound['source_message_id'], draft['source_message_id'])
                self.assertEqual(self.preview(rebound)['status'], 'succeeded')
                self.assertEqual(self.manual(rebound['body']), rebound)

    def test_same_body_pending_rebinds_when_case_conversation_or_policy_version_changes(self):
        self.start_manual()
        draft = self.manual()
        self.assertEqual(self.manual(draft['body']), draft)
        for change in ('case', 'conversation', 'policy'):
            with self.subTest(change=change):
                if change == 'case':
                    case = self.service.repo.get('case', self.case_id)
                    case['revision'] += 1
                    self.service.repo.put('case', self.case_id, case)
                elif change == 'conversation':
                    conversation = self.service.repo.get('conversation', self.cid)
                    conversation['revision'] += 1
                    self.service.repo.put('conversation', self.cid, conversation)
                else:
                    # Simulate expiry without the usual eager draft invalidation.
                    policy = {'policy_id': 'synthetic-applicable', 'kind': 'policy', 'title': 'Synthetic',
                              'content': 'Costs must be checked.', 'product_scope': [], 'confirmed': True,
                              'public': True, 'revision': 1, 'updated_at': now_ms(), 'valid_until': None}
                    self.service.repo.put('policy', policy['policy_id'], policy)
                rebound = self.manual(draft['body'])
                self.assertEqual(rebound['status'], 'pending')
                self.assertGreater(rebound['revision'], draft['revision'])
                self.assertEqual(rebound['source_case_revision'], self.service.repo.get('case', self.case_id)['revision'])
                self.assertEqual(rebound['source_conversation_revision'], self.service.repo.get('conversation', self.cid)['revision'])
                draft = rebound

    def test_unknown_send_still_blocks_same_body_after_new_source_arrives(self):
        self.start_manual()
        draft = self.manual()
        self.facade.send_result = {'status': 'unknown', 'message_id': None, 'submission_started': True}
        self.assertEqual(self.confirm(self.prepare(draft))['status'], 'unknown')
        self.ingest(mid='synthetic-after-unknown', stamp=now_ms() + 10)
        with self.assertRaises(ServiceError) as caught:
            self.manual(draft['body'])
        self.assertEqual(caught.exception.code, 'send_unresolved')
        self.assertEqual(len(self.service.repo.list('draft', case_id=self.case_id)), 1)

    def test_restart_does_not_restore_manual_or_automatic_permissions(self):
        self.start_manual()
        self.service.close()
        self.service = ReceptionService(self.root, 'live', seller_id=self.facade.seller)
        snapshot = self.service.get_snapshot()['reception']
        self.assertFalse(snapshot['running'])
        self.assertFalse(snapshot['auto_send'])
        self.assertFalse(snapshot['manual_only'])
        self.assertIsNone(snapshot['started_at_ms'])
        self.assertIsNone(self.service._authorized_scope)
        self.assertFalse(self.service.account_verified)


if __name__ == '__main__':
    unittest.main()
