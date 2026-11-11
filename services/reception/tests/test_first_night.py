"""First-night boundaries; temporary SQLite workspaces and MockFacade only."""
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
from reception_service.integrations.analysis import prepare_context
from reception_service.integrations.accio_native.vendor.adapters.common import target
from reception_service.repository import encoded, now_ms
from reception_service.service import ReceptionService
from reception_service.synthetic_check import SYNTHETIC_BUYERS


class FirstNightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.facade = MockFacade()
        self.facade.seller = '10001'
        self.factory = patch('reception_service.service.build_integrations', return_value=self.facade)
        self.factory.start()
        # Drive scheduling explicitly, while exercising the actual worker queue.
        self.schedule = ReceptionService._schedule
        self.scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        self.scheduler.start()
        # These tests drive their own ticks. Keep the independent monitor from
        # racing the synthetic-only AI privacy guard on the shared repository.
        self.monitor = patch.object(ReceptionService, '_start_message_monitor', return_value=None)
        self.monitor.start()
        self.service = ReceptionService(self.root, 'live', seller_id=self.facade.seller)
        self.service._activate_seller(self.facade.seller)
        self.pass_checks()
        self.service.settings['merge_seconds'] = 0

    def tearDown(self):
        self.facade.release.set()
        self.service.close()
        self.scheduler.stop()
        self.monitor.stop()
        self.factory.stop()
        self.temp.cleanup()

    def pass_checks(self):
        for name in ('messages', 'ai', 'send_preview'):
            self.service.checks[name]['status'] = 'passed'

    def wait_job(self, ack):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.repo.get('job', ack['job_id'])
            if job and job['status'] not in ('queued', 'running'):
                return job
            threading.Event().wait(0.005)
        self.fail('synthetic job did not finish')

    def idle(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(j['status'] in ('queued', 'running') for j in self.service.list_jobs()):
                return
            threading.Event().wait(0.005)
        self.fail('synthetic queue did not finish')

    def ingest(self, cid='customer-a', mid='source-a', stamp=None):
        info = self.service.repo.get('conversation', cid) or {
            'conversation_id': cid, 'buyer_id': 'buyer-' + cid, 'customer_name': 'Synthetic customer'}
        message = {'seller_id': self.service.seller_id, 'buyer_id': info['buyer_id'], 'conversation_id': cid,
                   'message_id': mid, 'role': 'buyer', 'own': False, 'text': 'I am interested in ceramic mugs.',
                   'timestamp_ms': stamp, 'attachments': []}
        self.service._ingest(info, [message], full=True)
        return self.service.get_conversation(cid)['case']

    def start(self, mode='incoming', ids=None, limit=5, auto=False):
        self.service.set_scope(mode, ids or [], limit)
        snapshot = self.service.set_reception(True, auto)
        self.service._next_poll = time.monotonic() + 100
        return snapshot['reception']['started_at_ms']

    def tick(self):
        self.schedule(self.service)
        self.idle()

    def manual_generate(self, case):
        current = self.service.repo.get('case', case['case_id'])
        return self.wait_job(self.service.call('generate_draft', {
            'case_id': current['case_id'], 'expected_case_revision': current['revision']}))

    def generate_jobs(self):
        return [j for j in self.service.list_jobs() if j['kind'] == 'generate_draft']

    def test_snapshot_defaults_and_live_checks_required_even_without_auto_send(self):
        initial = self.service.get_snapshot()['reception']
        self.assertEqual(initial['scope']['mode'], 'selected')
        self.assertEqual(initial['scope']['conversation_ids'], [])
        self.assertIsNone(initial['started_at_ms'])
        self.assertFalse(initial['running'])
        self.assertFalse(initial['auto_send'])
        self.assertEqual(initial['budget']['ai_used'], 0)
        self.service.set_scope('incoming', [], 5)
        for name in ('messages', 'ai'):
            with self.subTest(check=name):
                self.service.checks[name]['status'] = 'not_checked'
                with self.assertRaises(ServiceError):
                    self.service.set_reception(True, False)
                self.service.checks[name]['status'] = 'passed'
        self.assertTrue(self.service.set_reception(True, False)['reception']['running'])

    def test_first_sync_24h_old_history_never_generates_in_any_live_scope(self):
        for mode in ('selected', 'all', 'incoming'):
            with self.subTest(mode=mode):
                self.service.set_reception(False)
                self.ingest(stamp=now_ms() - 86_400_000)
                start = self.start(mode, ['customer-a'])
                # The old message arrives for the first time *after* authorization.
                self.ingest('newly-discovered-' + mode, stamp=start - 86_400_000)
                self.tick()
                self.assertEqual(self.facade.generate_calls, 0)
                self.assertEqual(self.generate_jobs(), [])
                self.assertEqual(self.service.get_snapshot()['reception']['budget']['ai_used'], 0)

    def test_first_read_of_known_empty_conversation_does_not_reply_to_old_history(self):
        for mode in ('selected', 'all', 'incoming'):
            with self.subTest(mode=mode):
                cid = 'first-read-' + mode
                info = {'conversation_id': cid, 'buyer_id': 'buyer-' + cid, 'customer_name': 'Synthetic'}
                self.service._ingest(info, [])
                start = self.start(mode, [cid])
                message = {'message_id': 'first-read-old', 'seller_id': self.service.seller_id,
                           'buyer_id': info['buyer_id'], 'conversation_id': cid, 'role': 'buyer',
                           'text': 'I am interested in ceramic mugs.', 'timestamp_ms': start - 86_400_000,
                           'attachments': [], 'own': False}
                self.facade.pages = [{'messages': [message], 'has_more': False, 'next_cursor': None}]
                job = self.wait_job(self.service.call('sync_messages', {'conversation_id': cid}))
                self.assertEqual(job['status'], 'succeeded', job)
                self.assertIsNotNone(self.service.repo.get('conversation', cid)['last_buyer_at'])
                self.tick()
                self.assertEqual(self.facade.generate_calls, 0)

    def test_platform_timestamp_controls_existing_live_customer_in_draft_mode(self):
        self.ingest(stamp=now_ms() - 86_400_000)
        start = self.start('selected', ['customer-a'])
        self.ingest(mid='late-old-history', stamp=start - 1000)
        self.tick()
        self.assertEqual(self.facade.generate_calls, 0)
        case = self.ingest(mid='after-authorization', stamp=start + 1)
        self.tick()
        self.assertEqual(self.facade.generate_calls, 1)
        job = self.generate_jobs()[0]
        self.assertTrue(job['automatic'])
        self.assertTrue(job['external_started'])
        self.assertEqual(job['source_message_id'], 'after-authorization')
        self.assertEqual(job['source_case_revision'], case['revision'])
        self.assertEqual(self.facade.send_calls, 0)

    def test_incoming_starts_empty_and_accepts_new_customer_after_authorization(self):
        self.assertEqual(self.service.get_snapshot()['conversations'], [])
        start = self.start(auto=True)
        self.assertEqual(self.service._frozen_scope['conversation_ids'], [])
        self.assertEqual(self.service._frozen_scope['mode'], 'incoming')
        self.assertEqual(self.service._frozen_scope['seller_id'], '10001')
        self.assertEqual(self.service._frozen_scope['started_at_ms'], start)
        self.assertFalse(self.service._in_scope('unknown-customer'))
        self.ingest(stamp=start + 1)
        self.assertTrue(self.service._in_scope('customer-a'))
        self.tick()
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertEqual(self.facade.send_calls, 1)
        self.assertTrue(self.facade.observed_authorization)

    def test_selected_and_all_freeze_existing_ids_across_pause(self):
        for mode in ('selected', 'all'):
            with self.subTest(mode=mode):
                self.ingest('frozen-' + mode, stamp=now_ms() - 1000)
                start = self.start(mode, ['frozen-' + mode])
                cid = 'outside-' + mode
                self.ingest(cid, stamp=start + 10)
                self.assertFalse(self.service._in_scope(cid))
                self.service.set_reception(False)
                self.service.set_reception(True, False)
                self.assertFalse(self.service._in_scope(cid))
                self.assertEqual(self.service._started_at_ms, start)

    def test_demo_incoming_also_skips_seed_history_and_accepts_new_message(self):
        self.service.close()
        self.service = ReceptionService(self.root, 'demo')
        self.service.settings['merge_seconds'] = 0
        start = self.start()
        self.tick()
        self.assertEqual(self.facade.generate_calls, 0)
        self.ingest('demo-emma', 'demo-new', start + 1)
        self.tick()
        self.assertEqual(self.facade.generate_calls, 1)

    def test_missing_timestamp_blocks_automatic_generation_and_send(self):
        start = self.start(auto=True)
        self.ingest(stamp=start + 1)
        self.ingest(mid='undated-source', stamp=None)
        self.tick()
        self.assertFalse(self.service._in_scope('customer-a'))
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.send_calls, 0)

    def test_known_future_dated_source_is_baselined_at_start(self):
        self.ingest(stamp=now_ms() + 60_000)
        self.start('all')
        self.tick()
        self.assertEqual(self.facade.generate_calls, 0)

    def test_queued_cancelled_before_ai_resumes_at_same_start_without_charge(self):
        start = self.start()
        self.facade.block_read = True
        read = self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(1))
        self.ingest(stamp=start + 1)
        self.schedule(self.service)
        queued = self.generate_jobs()[0]
        self.service.set_reception(False)
        self.facade.release.set()
        self.wait_job(read)
        cancelled = self.wait_job(queued)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIs(cancelled['external_started'], False)
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.service._budget()['ai_used'], 0)
        self.facade.block_read = False
        resumed = self.service.set_reception(True, False)
        self.assertEqual(resumed['reception']['started_at_ms'], start)
        self.service._next_poll = time.monotonic() + 100
        self.tick()
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertEqual(len(self.generate_jobs()), 2)

    def test_provider_failure_after_start_is_charged_and_never_automatically_retried(self):
        start = self.start()
        self.ingest(stamp=start + 1)
        def fail(context, request_id):
            self.facade.generate_calls += 1
            raise ServiceError('synthetic_paid_failure', 'Synthetic provider failure')
        with patch.object(self.facade, 'generate', side_effect=fail):
            self.tick()
            first = self.generate_jobs()[0]
            self.assertEqual(first['status'], 'failed')
            self.assertTrue(first['external_started'])
            self.assertEqual(self.service._budget()['ai_used'], 1)
            self.service.set_reception(False)
            self.service.set_reception(True, False)
            self.service._next_poll = time.monotonic() + 100
            self.tick()
            self.tick()
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertEqual(len(self.generate_jobs()), 1)

    def test_paid_stale_response_replans_after_explicit_resume(self):
        start = self.start()
        self.ingest(stamp=start + 1)
        self.facade.block_generate = True
        self.schedule(self.service)
        self.assertTrue(self.facade.started.wait(1))
        self.assertTrue(self.generate_jobs()[0]['external_started'])
        self.service.set_reception(False)
        self.facade.release.set()
        self.idle()
        self.assertEqual(self.generate_jobs()[0]['status'], 'stale')
        self.service.set_reception(True, False)
        self.service._next_poll = time.monotonic() + 100
        self.tick()
        self.assertEqual(self.facade.generate_calls, 2)
        self.assertEqual(self.service._budget()['ai_used'], 2)
        self.assertEqual(self.facade.send_calls, 0)

    def test_auto_and_manual_generations_continue_beyond_former_limit(self):
        start = self.start(limit=5)
        cases = [self.ingest('budget-' + str(i), stamp=start + i + 1) for i in range(7)]
        self.tick()
        self.tick()
        self.assertEqual(self.facade.generate_calls, 7)
        self.assertEqual(self.service._budget(), {'limit': None, 'used': 0, 'remaining': None, 'ai_used': 7, 'ai_remaining': None, 'unlimited': True})
        self.service.set_reception(False)
        self.service.set_reception(True, False)
        self.service.set_reception(True, False)
        self.assertEqual(self.service._budget()['ai_used'], 7)
        self.service._next_poll = 0
        with patch.object(self.facade, 'read_recent_conversations', wraps=self.facade.read_recent_conversations) as reader:
            self.tick()
            self.service._next_probe = 0
            self.service._monitor_once()
            self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.manual_generate(cases[-1])['status'], 'succeeded')
        self.assertEqual(self.facade.generate_calls, 8)
        self.assertEqual(self.service._budget()['ai_used'], 8)

    def test_automatic_send_and_ai_continue_beyond_former_limits(self):
        start = self.start(limit=5, auto=True)
        cases = [self.ingest('send-budget-' + str(i), stamp=start + i + 1) for i in range(7)]
        self.tick()
        self.assertEqual(self.facade.generate_calls, 7)
        self.assertEqual(self.facade.send_calls, 7)
        self.assertEqual(self.service._budget()['used'], 7)
        self.assertEqual(self.service._budget()['ai_used'], 7)
        self.assertEqual(self.manual_generate(cases[-1])['status'], 'succeeded')
        self.idle()
        self.tick()
        self.assertEqual(self.facade.send_calls, 7)
        self.assertEqual(self.service._budget()['ai_used'], 8)
        self.assertIsNone(self.service._budget()['remaining'])

    def test_queued_ai_proceeds_after_former_budget_is_exceeded(self):
        start = self.start(limit=1)
        self.facade.block_read = True
        read = self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(1))
        self.ingest(stamp=start + 1)
        self.schedule(self.service)
        queued = self.generate_jobs()[0]
        self.service._ai_budget_used = 1
        self.facade.release.set()
        self.wait_job(read)
        job = self.wait_job(queued)
        self.assertEqual(job['status'], 'succeeded')
        self.assertTrue(job['external_started'])
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertEqual(self.service._budget()['ai_used'], 2)

    def test_final_ai_rechecks_queued_source_binding_before_facade_call(self):
        start = self.start()
        self.facade.block_read = True
        read = self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(1))
        self.ingest(stamp=start + 1)
        self.schedule(self.service)
        queued = self.generate_jobs()[0]
        queued['source_message_id'] = 'foreign-message'
        self.service.repo.put('job', queued['job_id'], queued)
        self.facade.release.set()
        self.wait_job(read)
        job = self.wait_job(queued)
        self.assertEqual(job['status'], 'deferred')
        self.assertEqual(job['last_error_code'], 'source_changed')
        self.assertEqual(job['retry_status'], 'replan')
        self.assertEqual(self.facade.send_calls, 0)
        self.assertFalse(job['external_started'])
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.service._budget()['ai_used'], 0)
        self.tick()
        current_jobs = [item for item in self.generate_jobs() if item['job_id'] != job['job_id']]
        self.assertEqual(len(current_jobs), 1)
        self.assertEqual(current_jobs[0]['source_message_id'], 'source-a')
        self.assertEqual(current_jobs[0]['status'], 'succeeded')
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertEqual(self.facade.send_calls, 0)

    def test_old_inquiry_remains_available_for_explicit_generate_and_confirm(self):
        case = self.ingest(stamp=now_ms() - 86_400_000)
        self.start(auto=True)
        job = self.manual_generate(case)
        self.assertEqual(job['status'], 'succeeded')
        self.idle()
        self.assertEqual(self.facade.send_calls, 0)
        draft = self.service.get_conversation('customer-a')['draft']
        confirmation = self.service.prepare_send(draft['draft_id'], draft['revision'])
        sent = self.wait_job(self.service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']}))
        self.assertEqual(sent['status'], 'succeeded', sent)
        self.assertEqual(self.facade.send_calls, 1)
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertEqual(self.service._budget()['used'], 0)

    def test_final_send_rechecks_local_source_timestamp_and_incoming_scope(self):
        start = self.start(auto=True)
        self.ingest(stamp=start + 1)
        self.facade.block_send = True
        captured = []
        original_send = self.facade.send
        def send(intent, authorization):
            captured.append(copy.deepcopy(intent))
            return original_send(intent, authorization)
        with patch.object(self.facade, 'send', side_effect=send):
            self.schedule(self.service)
            self.assertTrue(self.facade.started.wait(5), 'synthetic worker did not reach the controlled send boundary')
            intent = captured[0]
            self.assertEqual(intent['source_timestamp_ms'], start + 1)
            self.assertEqual(intent['frozen_scope']['started_at_ms'], start)
            key = encoded(['customer-a', 'source-a'])
            source = self.service.repo.get('message', key)
            source['timestamp_ms'] = start - 1
            self.service.repo.put('message', key, source)
            self.facade.release.set()
            self.idle()
        self.assertFalse(self.facade.observed_authorization)
        self.assertEqual(self.service.get_conversation('customer-a')['draft']['status'], 'stale')
        operation = self.service.repo.list('operation')[0]
        self.assertEqual(operation['status'], 'rejected')
        self.assertIs(operation['submission_started'], False)
        self.tick()
        self.assertEqual(len(self.service.repo.list('operation')), 1)

    def test_final_send_remains_authorized_beyond_former_limit(self):
        start = self.start(limit=1, auto=True)
        self.ingest(stamp=start + 1)
        self.facade.block_send = True
        self.schedule(self.service)
        self.assertTrue(self.facade.started.wait(1))
        # Prior usage does not revoke the current authorization.
        self.service._budget_used = 2
        self.facade.release.set()
        self.idle()
        self.assertTrue(self.facade.observed_authorization)
        self.assertEqual(self.service.repo.list('operation')[0]['status'], 'accepted')

    def test_pause_revokes_inflight_automatic_send_without_resetting_limits(self):
        start = self.start(auto=True)
        self.ingest(stamp=start + 1)
        self.facade.block_send = True
        self.schedule(self.service)
        self.assertTrue(self.facade.started.wait(1))
        self.service.set_reception(False)
        self.assertIsNone(self.service._frozen_scope)
        self.assertEqual(self.service._started_at_ms, start)
        self.facade.release.set()
        self.idle()
        self.assertFalse(self.facade.observed_authorization)
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.assertEqual(self.service._budget()['used'], 1)

    def test_unknown_automatic_send_does_not_retry_after_pause(self):
        start = self.start(auto=True)
        self.ingest(stamp=start + 1)
        self.facade.send_result = {'status': 'unknown', 'message_id': None, 'submission_started': True}
        self.tick()
        self.service.set_reception(False)
        self.service.set_reception(True, True)
        self.service._next_poll = time.monotonic() + 100
        self.tick()
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertEqual(self.facade.send_calls, 1)
        self.assertEqual(self.service.repo.list('operation')[0]['status'], 'unknown')
        self.assertEqual(self.service.get_conversation('customer-a')['draft']['status'], 'unknown')

    def test_scope_and_config_changes_rebuild_start_but_preserve_spent_budget(self):
        start = self.start()
        self.ingest(stamp=start + 1)
        self.tick()
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.service.set_scope('incoming', [], 5)
        self.assertIsNone(self.service._started_at_ms)
        self.assertFalse(self.service._running)
        self.service.set_reception(True, False)
        self.assertGreaterEqual(self.service._started_at_ms, start)
        self.assertEqual(self.service._budget()['ai_used'], 1)
        self.service.save_settings(poll_seconds=25)
        self.assertIsNone(self.service._started_at_ms)
        self.assertFalse(self.service._running)
        self.assertEqual(self.service._budget()['ai_used'], 1)

    def test_restart_requires_authorization_and_preserves_paid_source_metadata(self):
        start = self.start()
        self.ingest(stamp=start + 1)
        self.tick()
        paid = self.generate_jobs()[0]
        self.service.close()
        self.service = ReceptionService(self.root, 'live', seller_id='10001')
        self.service._activate_seller('10001')
        snapshot = self.service.get_snapshot()['reception']
        self.assertFalse(snapshot['running'])
        self.assertFalse(snapshot['auto_send'])
        self.assertIsNone(snapshot['started_at_ms'])
        self.assertEqual(snapshot['budget']['ai_used'], 1)
        self.assertEqual(snapshot['scope']['mode'], 'selected')
        self.assertEqual(snapshot['scope']['conversation_ids'], [])
        restored = self.service.repo.get('job', paid['job_id'])
        self.assertTrue(restored['external_started'])
        self.assertEqual(restored['source_message_id'], 'source-a')

    def assert_synthetic_check(self):
        before = self.service.repo.db.execute("SELECT kind,id,data FROM entities WHERE kind != 'job' ORDER BY kind,id").fetchall()
        contexts = []
        original = self.facade.generate
        def generate(context, request_id):
            prepare_context(context)
            case = context['case']
            target(case['seller_id'], case['buyer_id'], case['conversation_id'])
            self.assertEqual(case['seller_id'], '10001')
            self.assertIn(case['buyer_id'], SYNTHETIC_BUYERS)
            self.assertIn('#synthetic-ai-check', case['conversation_id'])
            self.assertTrue(context['recent_messages'][0]['message_id'].startswith('synthetic-'))
            self.assertEqual(context['knowledge'], [])
            contexts.append(copy.deepcopy(context))
            return original(context, request_id)
        original_list = self.service.repo.list
        def guarded_list(kind, **filters):
            if kind in ('message', 'case', 'conversation', 'policy'):
                raise AssertionError('AI check read customer data: ' + kind)
            return original_list(kind, **filters)
        with patch.object(self.facade, 'generate', side_effect=generate), \
                patch.object(self.service, 'get_context', side_effect=AssertionError('read real context')), \
                patch.object(self.service.repo, 'list', side_effect=guarded_list):
            job = self.wait_job(self.service.call('check_ai'))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertEqual(self.facade.send_calls, 0)
        self.assertEqual(self.facade.registration_calls, [])
        self.assertEqual(self.facade.read_calls, [])
        after = self.service.repo.db.execute("SELECT kind,id,data FROM entities WHERE kind != 'job' ORDER BY kind,id").fetchall()
        self.assertEqual(after, before)
        self.assertFalse(self.service._running)
        self.assertEqual(self.service.checks['ai']['status'], 'passed')
        self.assertEqual(self.service._budget()['ai_used'], 0)

    def test_ai_check_without_customers_uses_valid_synthetic_context_and_no_business_writes(self):
        self.assert_synthetic_check()

    def test_ai_check_with_existing_customers_does_not_read_customer_originals(self):
        self.ingest(stamp=now_ms() - 1000)
        self.assert_synthetic_check()

    def test_cancelled_ai_check_does_not_retry_or_apply_result(self):
        self.facade.block_generate = True
        ack = self.service.call('check_ai')
        self.assertTrue(self.facade.started.wait(1))
        self.service.set_reception(False)
        self.facade.release.set()
        job = self.wait_job(ack)
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertNotEqual(self.service.checks['ai']['status'], 'passed')
        self.assertEqual(self.service.repo.list('draft'), [])
        self.assertEqual(self.service.repo.list('outbox'), [])


if __name__ == '__main__':
    unittest.main()
