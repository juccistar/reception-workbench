"""Live message scheduling with temporary databases and synthetic facades only."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import patch

from test_core import MockFacade
from reception_service import __version__
from reception_service.errors import ServiceError
from reception_service.integrations.accio_native.gateway import digest
from reception_service.repository import encoded, now_ms
from reception_service.service import ReceptionService
from reception_test_support.model import configure_synthetic_model, synthetic_snapshot


class MonitorFacade(MockFacade):
    def __init__(self):
        super().__init__()
        self.seller = '10001'
        self.summaries = {None: self.summary_page([])}
        self.summary_calls = []
        self.bodies = {}
        self.summary_hook = None
        self.body_hook = None

    def generate(self, context, request_id):
        snapshot = synthetic_snapshot(self.model_router)
        result = super().generate(context, request_id)
        result['model_snapshot'] = snapshot
        return result

    @staticmethod
    def summary_page(items, more=False, cursor=None):
        return {'items': copy.deepcopy(items), 'has_more': more, 'next_cursor': cursor}

    def read_recent_conversations(self, seller_id, before_ms=None, count=20):
        self.summary_calls.append((before_ms, count))
        if self.summary_hook:
            return self.summary_hook(before_ms, count)
        return copy.deepcopy(self.summaries.get(before_ms, self.summary_page([])))

    def read_message_page(self, seller_id, buyer_id, conversation_id, before_ms=None, count=100):
        self.read_calls.append((conversation_id, before_ms, count))
        if self.body_hook:
            return self.body_hook(conversation_id, before_ms)
        value = self.bodies.get(conversation_id, [])
        if isinstance(value, Exception):
            raise value
        return {'messages': copy.deepcopy(value), 'has_more': False, 'next_cursor': None}


class MessageMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.facade = MonitorFacade()
        self.factory = patch('reception_service.service.build_integrations', return_value=self.facade)
        self.factory.start()
        self.start_monitor = ReceptionService._start_message_monitor
        self.monitor_patch = patch.object(ReceptionService, '_start_message_monitor', return_value=None)
        self.monitor_patch.start()
        self.schedule = ReceptionService._schedule
        self.scheduler_patch = patch.object(ReceptionService, '_schedule', return_value=None)
        self.scheduler_patch.start()
        self.service = ReceptionService(self.root, 'live', seller_id='10001')
        configure_synthetic_model(self.service, self.root / 'model-authority.json')
        self.facade.model_router = self.service._model_router
        self.baseline_jobs = set()

    def tearDown(self):
        self.facade.release.set()
        self.service.close()
        self.scheduler_patch.stop()
        self.monitor_patch.stop()
        self.factory.stop()
        self.temp.cleanup()

    def test_scope_persists_and_all_includes_later_customers_without_ai_check(self):
        self.connect([self.info()])
        self.assertNotEqual(self.service.checks['ai']['status'], 'passed')
        self.service.set_scope('all', [])
        self.service._model_config_valid = False
        self.service.set_reception(True, auto_send=True)
        self.service._ingest(self.info('later'), [])
        self.assertTrue(self.service._in_scope('later'))
        self.service._pause()
        self.service._scope = {'mode': 'selected', 'conversation_ids': [], 'max_auto_replies': None}
        self.service._load_scope()
        self.assertEqual(self.service._scope['mode'], 'all')
        self.assertFalse(self.service._running)

    def test_legacy_incoming_scope_migrates_on_restart_without_authorization(self):
        self.connect([self.info()])
        self.service.repo.put('reception_scope', 'international', {'mode': 'incoming', 'conversation_ids': [], 'max_auto_replies': 5})
        self.service.close()
        self.service = ReceptionService(self.root, 'live', seller_id='10001')
        self.service._activate_seller('10001')
        self.assertEqual(self.service._scope['mode'], 'all')
        self.assertEqual(self.service.repo.get('reception_scope', 'international')['mode'], 'all')
        self.assertFalse(self.service._running)
        self.assertIsNone(self.service._authorized_scope)

    def test_independent_ai_check_does_not_pause_running_reception(self):
        self.connect([self.info()])
        self.service.set_scope('all', [])
        self.service.set_reception(True, auto_send=True)
        self.wait_job(self.service.call('check_ai'))
        self.assertTrue(self.service._running)
        self.assertTrue(self.service._auto_send)

    def test_history_only_buyer_cannot_become_automatic_source(self):
        self.connect([self.info()])
        self.service.set_scope('all', [])
        self.service.set_reception(True, auto_send=True)
        cid = 'customer-a'
        old_last_buyer = self.service.repo.get('conversation', cid)['last_buyer_at']
        message = self.message(cid, now_ms() + 1000, 'backfilled')
        self.service._ingest(self.info(cid), [message], history=True)
        self.assertEqual(self.service.repo.get('conversation', cid)['last_buyer_at'], old_last_buyer)
        self.assertIsNone(self.service._automatic_source(cid))

    def test_fill_history_during_reception_does_not_create_reply_source(self):
        self.connect([self.info()])
        self.service.set_scope('all', [])
        self.service.set_reception(True, auto_send=True)
        cid = 'customer-a'
        self.facade.bodies[cid] = [self.message(cid, now_ms() + 1000, 'history-head')]
        result = self.wait_job(self.service.call('fill_history', {'conversation_id': cid}))
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertIsNone(self.service._automatic_source(cid))
        self.assertFalse(any(j.get('kind') == 'generate_draft' for j in self.service.repo.list('job')))

    def test_history_marker_survives_ordinary_sync_and_later_exact_activity_promotes(self):
        self.connect([self.info()])
        cid = 'customer-a'
        stamp = now_ms()
        message = self.message(cid, stamp, 'history-head', 'New buyer request')
        self.facade.bodies[cid] = [message]
        self.assertEqual(self.wait_job(self.service.call('fill_history', {'conversation_id': cid}))['status'], 'succeeded')
        key = encoded([cid, message['message_id']])
        self.assertIsNotNone(self.service.repo.get('history_only_message', key))
        self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'succeeded')
        self.assertIsNotNone(self.service.repo.get('history_only_message', key))
        info = dict(self.info(cid, stamp=stamp, signature='new'), latest_message_id=message['message_id'],
                    latest_text_sha256=hashlib.sha256(message['text'].encode()).hexdigest())
        self.facade.summaries[None] = self.facade.summary_page([info])
        conversation = self.service.repo.get('conversation', cid)
        conversation['last_buyer_at'] = None
        self.service.repo.put('conversation', cid, conversation)
        before_case = self.service.repo.get('case', conversation['case_id'])
        self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'succeeded')
        self.assertIsNone(self.service.repo.get('history_only_message', key))
        promoted = self.service.repo.get('conversation', cid)
        self.assertIsNotNone(promoted['last_buyer_at'])
        promoted_case = self.service.repo.get('case', promoted['case_id'])
        self.assertEqual(promoted_case['revision'], before_case['revision'] + 1)
        self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('conversation', cid)['last_buyer_at'], promoted['last_buyer_at'])
        self.assertEqual(self.service.repo.get('case', promoted['case_id'])['revision'], promoted_case['revision'])

    def test_weak_activity_after_history_promotes_and_schedules_buyer(self):
        self._assert_confirmed_history_head('none')

    def test_summary_id_without_body_promotes_the_identified_message(self):
        self._assert_confirmed_history_head('id')

    def test_summary_body_without_id_requires_two_matching_native_heads(self):
        self._assert_confirmed_history_head('body')

    def _assert_confirmed_history_head(self, evidence):
        self.connect([self.info()])
        self.idle()
        self.service.set_scope('all', [])
        self.service.set_reception(True, auto_send=True)
        cid = 'customer-a'
        message = self.message(cid, now_ms() + 1000, 'weak-live')
        baseline = self.message(cid)
        self.facade.bodies[cid] = [baseline, message]
        self.assertEqual(self.wait_job(self.service.call('fill_history', {'conversation_id': cid}))['status'], 'succeeded')
        key = encoded([cid, message['message_id']])
        self.assertIsNotNone(self.service.repo.get('history_only_message', key))
        info = dict(self.info(cid, stamp=2000, signature=digest({'activity_at': 2000, 'latest': {}})),
                    activity_clock='conversation')
        if evidence == 'id':
            info['latest_message_id'] = message['message_id']
        elif evidence == 'body':
            info['latest_text_sha256'] = hashlib.sha256(message['text'].encode()).hexdigest()
        self.facade.summaries[None] = self.facade.summary_page([info])
        with patch.object(self.service, '_dispatch_message_syncs', return_value=None):
            if evidence != 'id':
                self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'deferred')
                self.assertIsNotNone(self.service.repo.get('history_only_message', key))
            self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'succeeded')
        self.assertIsNone(self.service.repo.get('history_only_message', key))
        self.assertIsNotNone(self.service.repo.get('conversation', cid)['last_buyer_at'])
        self.assertEqual(self.service._automatic_source(cid)['message_id'], message['message_id'])
        with patch.object(self.service, '_enqueue') as enqueue:
            with patch('reception_service.service.now_ms', return_value=now_ms() + 10000):
                self.schedule(self.service)
            self.assertTrue(any(call.args[0] == 'generate_draft' for call in enqueue.call_args_list))

    def test_weak_confirmation_requires_two_native_heads_not_the_local_cache(self):
        self.connect([self.info()])
        self.idle()
        self.service.set_scope('all', [])
        self.service.set_reception(True, auto_send=True)
        cid, stamp = 'customer-a', now_ms() + 1000
        older = self.message(cid, stamp, 'weak-a', 'Earlier same-second request')
        head = self.message(cid, stamp, 'weak-z', 'Latest same-second request')
        self.facade.bodies[cid] = [older, head]
        self.wait_job(self.service.call('fill_history', {'conversation_id': cid}))
        self.facade.summaries[None] = self.facade.summary_page([
            dict(self.info(cid, stamp=2000, signature='weak-live-after-fill'), activity_clock='conversation')])
        key = encoded([cid, head['message_id']])
        with patch.object(self.service, '_dispatch_message_syncs', return_value=None):
            self.facade.bodies[cid] = [older]
            self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'deferred')
            self.facade.bodies[cid] = [older, head]
            self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'deferred')
            self.assertIsNotNone(self.service.repo.get('history_only_message', key))
            self.assertIsNone(self.service._automatic_source(cid))
            self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'succeeded')
        self.assertIsNone(self.service.repo.get('history_only_message', key))
        self.assertIsNotNone(self.service.repo.get('history_only_message', encoded([cid, older['message_id']])))
        self.assertEqual(self.service._automatic_source(cid)['message_id'], head['message_id'])

    def test_automatic_source_projection_is_authoritative_outside_the_message_window(self):
        self.connect([self.info()])
        self.idle()
        self.service.set_scope('all', [])
        self.service.set_reception(True, auto_send=True)
        cid = 'customer-a'
        stamp = now_ms() + 1000
        source = self.message(cid, stamp, 'source-outside-window', 'Original request')
        later = [{**self.message(cid, stamp + index + 1, f'seller-{index}', 'Seller follow-up'), 'role': 'seller'}
                 for index in range(105)]
        self.service._ingest(self.info(cid), [source, *later], has_more=False)
        detail = self.service.get_conversation(cid)
        self.assertNotIn(source['message_id'], {row['message_id'] for row in detail['messages']})
        self.assertEqual(detail['automatic_source'], {'message_id': source['message_id'],
                         'text_sha256': hashlib.sha256(source['text'].encode()).hexdigest()})
        changed = {**source, 'text': 'Corrected request'}
        self.service._ingest(self.info(cid), [changed], has_more=False)
        self.assertEqual(self.service.get_conversation(cid)['automatic_source']['text_sha256'],
                         hashlib.sha256(changed['text'].encode()).hexdigest())
        self.service.set_reception(False)
        self.assertIsNone(self.service.get_conversation(cid)['automatic_source'])

    def test_pending_live_activity_before_history_promotes_exact_body(self):
        self.connect([self.info()])
        self.idle()
        self.service.set_scope('all', [])
        self.service.set_reception(True, auto_send=True)
        cid = 'customer-a'
        message = self.message(cid, now_ms() + 1000, 'live-before-history', 'Live buyer request')
        info = dict(self.info(cid, stamp=message['timestamp_ms'], signature='live'),
                    latest_message_id=message['message_id'],
                    latest_text_sha256=hashlib.sha256(message['text'].encode()).hexdigest())
        with patch.object(self.service, '_dispatch_message_syncs', return_value=None):
            with self.service._lock, self.service.repo.transaction():
                self.service._observe_message_activity(info)
            self.facade.bodies[cid] = [message]
            result = self.wait_job(self.service.call('fill_history', {'conversation_id': cid}))
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertIsNone(self.service.repo.get('history_only_message', encoded([cid, message['message_id']])))
            self.assertEqual(self.service._automatic_source(cid)['message_id'], message['message_id'])

    def test_activity_during_history_survives_restart_and_promotes_exact_message(self):
        self.connect([self.info()])
        self.idle()
        cid = 'customer-a'
        message = self.message(cid, now_ms(), 'concurrent', 'Concurrent buyer request')
        started, release = threading.Event(), threading.Event()
        def body(_cid, _cursor):
            started.set()
            if not release.wait(3):
                raise AssertionError('history release missing')
            return {'messages': [message], 'has_more': False, 'next_cursor': None}
        self.facade.body_hook = body
        dispatch = patch.object(self.service, '_dispatch_message_syncs', return_value=None)
        dispatch.start()
        self.addCleanup(dispatch.stop)
        enqueue = self.service._enqueue
        def hold_followup(kind, *args, **kwargs):
            if kind == 'sync_messages':
                return {'job_id': 'held-until-restart'}
            return enqueue(kind, *args, **kwargs)
        followup = patch.object(self.service, '_enqueue', side_effect=hold_followup)
        followup.start()
        self.addCleanup(followup.stop)
        ack = self.service.call('fill_history', {'conversation_id': cid})
        info = dict(self.info(cid, stamp=message['timestamp_ms'], signature='concurrent'),
                    latest_message_id=message['message_id'],
                    latest_text_sha256=hashlib.sha256(message['text'].encode()).hexdigest())
        try:
            self.assertTrue(started.wait(2))
            with self.service._lock, self.service.repo.transaction():
                self.service._observe_message_activity(info)
        finally:
            release.set()
        result = self.wait_job(ack)
        self.assertIn(result['status'], ('succeeded', 'deferred'), result)
        key = encoded([cid, message['message_id']])
        self.assertIsNotNone(self.service.repo.get('history_only_message', key))
        self.service.close()
        self.service = ReceptionService(self.root, 'live', seller_id='10001')
        self.service._activate_seller('10001')
        self.service.checks['messages']['status'] = 'passed'
        self.assertIsNotNone(self.service.repo.get('history_only_message', key))
        self.facade.body_hook = None
        self.facade.bodies[cid] = [message]
        self.facade.summaries[None] = self.facade.summary_page([info])
        self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'succeeded')
        self.assertIsNone(self.service.repo.get('history_only_message', key))

    def test_cancel_running_manual_suggestion_discards_late_result(self):
        self.connect([self.info()])
        cid = 'customer-a'
        started, release = threading.Event(), threading.Event()
        case = self.service.get_conversation(cid)['case']
        def generate_manual(context, request_id, *, on_call_start, cancel_event):
            on_call_start()
            started.set()
            if not release.wait(3):
                raise AssertionError('synthetic AI release missing')
            return {'reply_messages': [{'body': 'Late reply', 'reply_sources': [context['recent_messages'][-1]['message_id']]}],
                    'model_snapshot': synthetic_snapshot(self.service._model_router)}
        self.facade.generate_manual = generate_manual
        ack = self.service.generate_manual_suggestion(cid, 'cancel-late', case['case_id'])
        try:
            self.assertTrue(started.wait(2))
            result = self.service.call('cancel_manual_suggestion', {'conversation_id': cid, 'job_id': ack['job_id']})
            self.assertEqual(result['status'], 'cancel_requested')
        finally:
            release.set()
        job = self.wait_job(ack)
        self.assertNotEqual(job['status'], 'succeeded', job)
        suggestion = self.service.get_conversation(cid).get('manual_suggestion')
        self.assertFalse(suggestion and suggestion.get('status') == 'ready', suggestion)

    def test_cancel_manual_job_rejects_other_job_kinds(self):
        self.connect([self.info()])
        cid = 'customer-a'
        job = {'job_id': 'manual-test', 'kind': 'manual_suggestion', 'conversation_id': cid,
               'status': 'queued', 'created_at': now_ms()}
        self.service.repo.put('job', job['job_id'], job)
        self.service.repo.put('manual_latest', cid, {'job_id': job['job_id']})
        result = self.service.call('cancel_manual_suggestion', {'conversation_id': cid, 'job_id': job['job_id']})
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.service.repo.get('manual_latest', cid)['job_id'], None)
        job.update(job_id='running-manual', status='running')
        self.service.repo.put('job', job['job_id'], job)
        self.service.repo.put('manual_latest', cid, {'job_id': job['job_id']})
        result = self.service.call('cancel_manual_suggestion', {'conversation_id': cid, 'job_id': job['job_id']})
        self.assertEqual(result['status'], 'cancel_requested')
        self.assertTrue(self.service._generation_cancel_event(job['job_id']).is_set())
        self.assertEqual(self.service.repo.get('job', job['job_id'])['status'], 'running')
        job.update(job_id='send-test', kind='send_chat')
        self.service.repo.put('job', job['job_id'], job)
        with self.assertRaises(ServiceError):
            self.service.call('cancel_manual_suggestion', {'conversation_id': cid, 'job_id': job['job_id']})

    def info(self, cid='customer-a', stamp=1000, signature=None):
        result = {'conversation_id': cid, 'buyer_id': 'buyer-' + cid,
                  'customer_name': 'Synthetic customer', 'latest_timestamp': stamp}
        if signature is not None:
            result['activity_signature'] = signature
        return result

    def message(self, cid='customer-a', stamp=1000, mid='old', text='Synthetic buyer request'):
        return {'seller_id': '10001', 'buyer_id': 'buyer-' + cid, 'conversation_id': cid,
                'message_id': mid, 'role': 'buyer', 'text': text, 'timestamp_ms': stamp, 'attachments': []}

    def connect(self, infos=None):
        self.facade.summaries[None] = self.facade.summary_page(infos or [])
        for info in infos or []:
            self.facade.bodies.setdefault(info['conversation_id'], [self.message(
                info['conversation_id'], info['latest_timestamp'])])
        job = self.wait_job(self.service.call('check_connection'))
        self.assertEqual(job['status'], 'succeeded', job)
        if len(infos or []) > 1:
            # Most tests start with an already caught-up workspace. Bootstrap
            # intentionally verifies only one customer; complete this fixture's
            # baseline explicitly before measuring subsequent incremental work.
            self.assertEqual(self.wait_job(self.service.call('sync_messages'))['status'], 'succeeded')
        self.baseline_jobs = {j['job_id'] for j in self.service.list_jobs(include_background=True)}
        self.facade.read_calls.clear()
        self.facade.summary_calls.clear()

    def wait_job(self, ack):
        job = self.service.repo.get('job', ack['job_id'], {})
        # Initial seller activation creates a durable SQLite database. A disk
        # commit has no four-second contract. Allow this setup up to 60s while
        # retaining its terminal status assertion and the timeout diagnostics.
        timeout = 60 if job.get('kind') == 'check_connection' else 4
        self.until(lambda: self.service.repo.get('job', ack['job_id'], {}).get('status')
                   not in (None, 'queued', 'running'), timeout=timeout)
        return self.service.repo.get('job', ack['job_id'])

    def until(self, predicate, *, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.005)
        frames = sys._current_frames()
        threads = '\n'.join(
            f'{thread.name}:\n{"".join(traceback.format_stack(frames[thread.ident]))}'
            for thread in threading.enumerate()
            if thread.name.startswith('reception-') and thread.ident in frames
        )
        self.fail(f'synthetic task did not finish; jobs={self.service.list_jobs(include_background=True)!r}; '
                  f'health={self.service.get_executor_health()!r}\n{threads}')

    def idle(self):
        self.until(lambda: not any(j['status'] in ('queued', 'running') for j in self.service.list_jobs(include_background=True)))

    def tick(self):
        self.service._next_probe = 0
        self.service._monitor_once()

    def jobs(self):
        return [j for j in self.service.list_jobs(include_background=True) if j['kind'] == 'sync_messages' and j['job_id'] not in self.baseline_jobs]

    def test_connection_verifies_one_body_and_defers_remaining_customers_and_pages(self):
        infos = [self.info('customer-' + str(i), stamp=1000 + i) for i in range(20)]
        self.facade.summaries[None] = self.facade.summary_page(infos, more=True, cursor=500)
        later = self.info('customer-20', stamp=400)
        self.facade.summaries[500] = self.facade.summary_page([later])
        for info in [*infos, later]:
            self.facade.bodies[info['conversation_id']] = [self.message(info['conversation_id'], info['latest_timestamp'])]
        job = self.wait_job(self.service.call('check_connection'))
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(self.facade.summary_calls, [(None, 20)])
        self.assertEqual([row[0] for row in self.facade.read_calls], ['customer-0'])
        self.assertEqual(len(self.service.repo.list('message_watermark')), 1)
        self.assertEqual(self.service.get_snapshot()['message_sync']['pending_count'], 19)
        self.assertEqual(self.service.repo.get('message_monitor', 'summary')['next_cursor'], 500)
        # Historical catch-up shares the account-wide one-per-minute budget.
        base = now_ms()
        for minute in range(24):
            with patch('reception_service.message_monitor.now_ms', return_value=base + minute * 60_000):
                self.tick()
                self.idle()
            if self.service.get_snapshot()['message_sync']['pending_count'] == 0:
                break
        self.assertEqual(self.service.get_snapshot()['message_sync']['pending_count'], 0)
        self.assertEqual(len(self.service.repo.list('message_watermark')), 21)
        self.assertEqual(self.facade.send_calls, 0)
        self.assertEqual(self.facade.generate_calls, 0)

    def test_connection_keeps_body_index_lag_pending_instead_of_marking_summary_as_processed(self):
        self.facade.summaries[None] = self.facade.summary_page([self.info()])
        job = self.wait_job(self.service.call('check_connection'))
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(self.service.checks['messages']['status'], 'passed')
        self.assertIsNone(self.service.repo.get('message_watermark', 'customer-a'))
        self.assertEqual(self.service.get_snapshot()['message_sync']['pending_count'], 1)
        self.assertIsNone(self.service._automatic_source('customer-a'))
        self.assertEqual(self.service.get_messages('customer-a')['items'], [])

    def test_large_historical_backlog_enqueues_one_customer_per_minute(self):
        self.connect()
        with self.service._lock, self.service.repo.transaction():
            for i in range(100):
                self.service._observe_message_activity(self.info('customer-' + str(i), stamp=1000 + i))
            queued = []
            with patch.object(self.service, '_enqueue', side_effect=lambda kind, params, **kwargs: queued.append(params['conversation_id'])):
                self.service._dispatch_message_syncs()
        self.assertEqual(queued, ['customer-99'])
        self.assertEqual(self.service.get_snapshot()['message_sync']['pending_count'], 100)
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.send_calls, 0)

    def test_initial_check_watermarks_and_unchanged_probes_have_zero_body_calls_or_jobs(self):
        self.connect([self.info()])
        watermark = self.service.repo.get('message_watermark', 'customer-a')
        self.assertEqual(watermark['latest_timestamp'], 1000)
        before = self.service.repo.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0]
        for _ in range(4):
            self.tick()
        self.idle()
        self.assertEqual(self.facade.summary_calls, [(None, 20)] * 4)
        self.assertEqual(self.facade.read_calls, [])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.service.repo.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], before)
        self.assertEqual(self.service.get_snapshot()['message_sync']['pending_count'], 0)

    def test_conversation_activity_clock_does_not_need_body_timestamp_to_equal_it(self):
        self.connect([self.info()])
        info = dict(self.info(stamp=1739), activity_clock='conversation')
        self.facade.summaries[None] = self.facade.summary_page([info])
        self.tick()
        self.idle()
        self.assertTrue(self.service._message_sync_pending('customer-a'))
        task = self.service.repo.get('message_sync_task', 'customer-a')
        task['retry_at'] = 0
        self.service.repo.put('message_sync_task', 'customer-a', task)
        self.tick()
        self.idle()
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        watermark = self.service.repo.get('message_watermark', 'customer-a')
        self.assertEqual(watermark['latest_timestamp'], 1739)
        self.assertEqual(watermark['body_latest_timestamp'], 1000)
        self.assertIsNotNone(watermark['recheck_at'])
        calls = len(self.facade.read_calls)
        self.tick()
        self.idle()
        self.assertEqual(len(self.facade.read_calls), calls)
        # A later activity within the same second is still detected and read.
        self.facade.summaries[None] = self.facade.summary_page([dict(info, latest_timestamp=1790)])
        self.facade.bodies['customer-a'] = [self.message(mid='second-id-in-same-ms')]
        self.tick()
        self.idle()
        self.assertGreater(len(self.facade.read_calls), calls)
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 1790)
        self.assertEqual(self.service.get_messages('customer-a')['items'][-1]['timestamp_ms'], 1000)

    def test_native_clock_new_body_can_ack_activity_without_changing_platform_message_time(self):
        self.connect()
        self.facade.summaries[None] = self.facade.summary_page([dict(self.info(stamp=2739), activity_clock='conversation')])
        self.facade.bodies['customer-a'] = [self.message(stamp=2000)]
        self.tick()
        self.idle()
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        watermark = self.service.repo.get('message_watermark', 'customer-a')
        self.assertEqual((watermark['latest_timestamp'], watermark['body_latest_timestamp']), (2739, 2000))
        self.assertEqual(self.service.get_messages('customer-a')['items'][0]['timestamp_ms'], 2000)

    def test_near_term_recheck_does_not_cancel_inflight_ai_without_new_activity(self):
        self.connect([self.info()])
        self.service.checks['ai']['status'] = 'passed'
        self.service.set_scope('selected', ['customer-a'])
        start = self.service.set_reception(True)['reception']['started_at_ms']
        self.facade.summaries[None] = self.facade.summary_page([dict(self.info(stamp=start + 700), activity_clock='conversation')])
        self.facade.bodies['customer-a'] = [self.message(stamp=start + 1, mid='fresh', text='I am interested in ceramic mugs.')]
        self.tick()
        self.idle()
        case = self.service.get_conversation('customer-a')['case']
        self.facade.block_generate = True
        ack = self.service._enqueue('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']}, automatic=True)
        try:
            self.assertTrue(self.facade.started.wait(2))
            watermark = self.service.repo.get('message_watermark', 'customer-a')
            watermark['recheck_at'] = 0
            self.service.repo.put('message_watermark', 'customer-a', watermark)
            before = self.service.get_context(case['case_id'])
            self.tick()
            self.assertFalse(self.service._message_sync_pending('customer-a'))
            self.until(lambda: self.service.repo.get('message_watermark', 'customer-a').get('recheck_at') is None)
            after = self.service.get_context(case['case_id'])
            self.assertEqual(before['fingerprint'], after['fingerprint'])
            self.assertEqual(self.service.repo.get('job', ack['job_id'])['status'], 'running')
        finally:
            self.facade.release.set()
        job = self.wait_job(ack)
        self.assertEqual(job['status'], 'succeeded', job)
        self.idle()
        self.assertIsNone(self.service.repo.get('message_watermark', 'customer-a')['recheck_at'])
        self.assertEqual(self.facade.generate_calls, 1)
        self.assertEqual(self.facade.send_calls, 0)

    def test_coverage_fingerprint_ignores_only_observation_time(self):
        from reception_service.reception_context import semantic_fingerprint
        context = {'history_coverage': {'status': 'partial', 'next_cursor': 20,
                                        'earliest_ms': 10, 'latest_ms': 30, 'updated_at': 1}}
        same = copy.deepcopy(context)
        same['history_coverage']['updated_at'] = 2
        self.assertEqual(semantic_fingerprint(context), semantic_fingerprint(same))
        same['history_coverage']['next_cursor'] = 10
        self.assertNotEqual(semantic_fingerprint(context), semantic_fingerprint(same))
        same['history_coverage']['next_cursor'] = 20
        same['history_coverage']['status'] = 'complete'
        self.assertNotEqual(semantic_fingerprint(context), semantic_fingerprint(same))

    def test_no_change_recheck_keeps_inflight_manual_suggestion_current(self):
        self.connect([self.info()])
        self.service.checks['ai']['status'] = 'passed'
        started, release = threading.Event(), threading.Event()
        case = self.service.get_conversation('customer-a')['case']
        def generate_manual(context, request_id, *, on_call_start, cancel_event):
            on_call_start()
            started.set()
            if not release.wait(3):
                raise AssertionError('synthetic AI release missing')
            return {'reply_messages': [{'body': 'Thank you for your message.', 'reply_sources': [context['recent_messages'][-1]['message_id']]}],
                    'model_snapshot': synthetic_snapshot(self.service._model_router)}
        self.facade.generate_manual = generate_manual
        ack = self.service.generate_manual_suggestion('customer-a', 'manual-recheck', case['case_id'])
        try:
            self.assertTrue(started.wait(2))
            watermark = self.service.repo.get('message_watermark', 'customer-a')
            watermark['recheck_at'] = 0
            self.service.repo.put('message_watermark', 'customer-a', watermark)
            _, before = self.service._manual_context('customer-a', case['case_id'])
            self.tick()
            self.until(lambda: self.service.repo.get('message_watermark', 'customer-a').get('recheck_at') is None)
            _, after = self.service._manual_context('customer-a', case['case_id'])
            self.assertEqual(before, after)
            self.assertEqual(self.service.repo.get('job', ack['job_id'])['status'], 'running')
        finally:
            release.set()
        job = self.wait_job(ack)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(self.service.get_conversation('customer-a')['manual_suggestion']['status'], 'ready')

    def test_only_changed_customer_reads_body_and_preserves_takeover(self):
        self.connect([self.info(), self.info('customer-b')])
        self.service.takeover('customer-a', True)
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=2000), self.info('customer-b')])
        self.facade.bodies['customer-a'] = [self.message(stamp=2000, mid='new')]
        self.tick()
        self.idle()
        self.assertEqual([call[0] for call in self.facade.read_calls], ['customer-a'])
        detail = self.service.get_conversation('customer-a')
        self.assertTrue(detail['conversation']['human_owned'])
        self.assertTrue(self.service.repo.get('conversation', 'customer-a')['manual_chat'])
        self.assertEqual(detail['case']['state'], 'clarifying')
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.send_calls, 0)

    def test_new_summary_creates_empty_customer_without_synthetic_incoming(self):
        self.connect()
        self.facade.block_generate = True
        info = self.info('new-customer', stamp=2000)
        self.facade.summaries[None] = self.facade.summary_page([info])
        started, release = threading.Event(), threading.Event()
        def body(cid, cursor):
            started.set()
            self.assertTrue(release.wait(3))
            return {'messages': [self.message(cid, 2000)], 'has_more': False, 'next_cursor': None}
        self.facade.body_hook = body
        try:
            self.tick()
            self.assertTrue(started.wait(1))
            detail = self.service.get_conversation('new-customer')
            self.assertEqual(detail['messages'], [])
            self.assertEqual(detail['conversation']['last_message'], '')
            self.assertEqual(detail['conversation']['unread_count'], 0)
            self.assertIsNone(self.service._automatic_source('new-customer'))
            self.assertFalse(self.service.get_snapshot()['reception']['running'])
        finally:
            release.set()
        self.idle()
        self.assertEqual(self.facade.generate_calls, 0)

    def test_body_failure_does_not_advance_watermark_or_block_other_customer(self):
        self.connect([self.info(), self.info('customer-b')])
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=2000), self.info('customer-b', 2000)])
        self.facade.bodies['customer-a'] = ServiceError('offline', 'private customer text')
        self.facade.bodies['customer-b'] = [self.message('customer-b', 2000)]
        self.tick()
        self.idle()
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 1000)
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-b')['latest_timestamp'], 2000)
        self.assertEqual(self.service.get_snapshot()['message_sync']['pending_count'], 1)
        self.tick()
        self.idle()
        self.assertEqual(len(self.jobs()), 2, 'failed customer must back off')
        self.facade.bodies['customer-a'] = [self.message(stamp=2000, mid='new')]
        task = self.service.repo.get('message_sync_task', 'customer-a')
        task['retry_at'] = 0
        self.service.repo.put('message_sync_task', 'customer-a', task)
        self.tick()
        self.idle()
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 2000)

    def test_summary_failure_is_safe_and_backs_off_without_jobs(self):
        self.connect([self.info()])
        def offline(cursor, count):
            raise ServiceError('offline', 'PRIVATE CUSTOMER BODY')
        self.facade.summary_hook = offline
        self.tick()
        self.service._monitor_once()
        self.assertEqual(len(self.facade.summary_calls), 1)
        snapshot = self.service.get_snapshot()['message_sync']
        self.assertEqual(snapshot['status'], 'retrying')
        self.assertEqual(snapshot['error_code'], 'summary_failed')
        self.assertNotIn('PRIVATE', str(snapshot))
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.facade.read_calls, [])
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 1000)

    def test_summary_continuation_reaches_customer_21_and_keeps_refreshing_head(self):
        infos = [self.info('customer-' + str(i), 3000 - i) for i in range(20)]
        self.connect(infos)
        self.facade.summaries[None] = self.facade.summary_page(infos, True, 2000)
        self.facade.summaries[2000] = self.facade.summary_page([self.info('customer-21', 1000)], True, 900)
        self.facade.summaries[900] = self.facade.summary_page([self.info('customer-22', 800)])
        self.facade.bodies['customer-21'] = [self.message('customer-21', 1000)]
        self.facade.bodies['customer-22'] = [self.message('customer-22', 800)]
        self.tick()
        self.idle()
        self.assertEqual(self.facade.summary_calls, [(None, 20), (2000, 20)])
        self.assertEqual([c[0] for c in self.facade.read_calls], ['customer-21'])
        self.assertEqual(self.service.repo.get('message_monitor', 'summary')['next_cursor'], 900)
        self.tick()
        self.assertEqual(self.facade.summary_calls[-1], (None, 20))
        sweep = self.service.repo.get('message_monitor', 'summary')
        sweep['next_scan_at'] = 0
        self.service.repo.put('message_monitor', 'summary', sweep)
        self.tick()
        self.idle()
        self.assertEqual(self.facade.summary_calls[-2:], [(None, 20), (900, 20)])
        self.assertIsNotNone(self.service.repo.get('conversation', 'customer-22'))

    def test_summary_cursor_rejects_non_integer_and_non_decreasing_continuations(self):
        self.connect()
        for bad in (True, '100', 0, -1, None):
            with self.subTest(cursor=bad):
                self.facade.summaries[None] = self.facade.summary_page([], True, bad)
                self.tick()
                self.assertEqual(self.service.get_snapshot()['message_sync']['error_code'], 'summary_cursor_stalled')
        self.facade.summaries[None] = self.facade.summary_page([], True, 100)
        self.facade.summaries[100] = self.facade.summary_page([], True, 100)
        self.tick()
        self.assertEqual(self.service.repo.get('message_monitor', 'summary')['next_cursor'], 100)
        self.assertEqual(self.service.get_snapshot()['message_sync']['error_code'], 'summary_cursor_stalled')

    def test_active_customer_merges_new_activity_without_duplicate_job_or_target_overwrite(self):
        self.connect([self.info()])
        started, release = threading.Event(), threading.Event()
        def body(cid, cursor):
            started.set()
            self.assertTrue(release.wait(3))
            return {'messages': [self.message(stamp=2000, mid='new')], 'has_more': False, 'next_cursor': None}
        self.facade.body_hook = body
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=2000)])
        try:
            self.tick()
            self.assertTrue(started.wait(1))
            self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=3000)])
            for _ in range(3):
                self.tick()
            self.assertEqual(len(self.jobs()), 1)
            self.assertEqual(self.jobs()[0]['sync_target'], 2000)
            self.assertIsNone(self.service._automatic_source('customer-a'))
        finally:
            release.set()
        self.idle()
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 2000)
        self.assertTrue(self.service._message_sync_pending('customer-a'))
        self.facade.body_hook = None
        self.facade.bodies['customer-a'] = [self.message(stamp=3000, mid='newer')]
        self.tick()
        self.idle()
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.jobs()[0]['sync_target'], 3000)
        self.assertFalse(self.service._message_sync_pending('customer-a'))

    def test_body_index_behind_summary_keeps_target_and_retries(self):
        self.connect([self.info()])
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=2000)])
        self.tick()
        self.idle()
        self.assertEqual(self.jobs()[0]['status'], 'deferred')
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 1000)
        self.assertTrue(self.service._message_sync_pending('customer-a'))
        self.assertGreater(self.service.repo.get('message_sync_task', 'customer-a')['retry_at'], now_ms())

    def test_page_budget_keeps_target_until_saved_gap_completes(self):
        self.connect([self.info()])
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=3000)])
        pages = [
            {'messages': [self.message(stamp=3000, mid='new')], 'has_more': True, 'next_cursor': 2500},
            {'messages': [self.message()], 'has_more': False, 'next_cursor': None},
        ]
        self.facade.body_hook = lambda cid, cursor: pages.pop(0)
        with patch('reception_service.service.SYNC_PAGE_LIMIT', 1):
            self.tick()
            self.idle()
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 1000)
        self.assertEqual(self.service.repo.get('coverage', 'customer-a')['next_cursor'], 2500)
        task = self.service.repo.get('message_sync_task', 'customer-a')
        task['retry_at'] = 0
        self.service.repo.put('message_sync_task', 'customer-a', task)
        self.tick()
        self.idle()
        self.assertEqual(self.facade.read_calls[-1][1], 2500)
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 3000)
        self.assertFalse(self.service._message_sync_pending('customer-a'))

    def test_audit_finds_same_millisecond_new_id_and_revision_one_customer_per_tick(self):
        self.connect([self.info(), self.info('customer-b')])
        for cid in ('customer-a', 'customer-b'):
            water = self.service.repo.get('message_watermark', cid)
            water['checked_at'] = now_ms() - 301_000
            self.service.repo.put('message_watermark', cid, water)
        self.facade.bodies['customer-a'] = [self.message(text='Revised source'), self.message(mid='same-ms-new')]
        self.tick()
        self.idle()
        self.assertEqual([c[0] for c in self.facade.read_calls], ['customer-a'])
        messages = self.service.get_messages('customer-a')['items']
        self.assertEqual({m['message_id'] for m in messages}, {'old', 'same-ms-new'})
        self.assertEqual(len(self.service.repo.history('message', encoded(['customer-a', 'old']))), 2)
        self.tick()
        self.idle()
        self.assertEqual(len(self.facade.read_calls), 1)
        with patch('reception_service.message_monitor.now_ms', return_value=now_ms() + 61_000):
            self.tick()
            self.idle()
        self.assertEqual([c[0] for c in self.facade.read_calls], ['customer-a', 'customer-b'])

    def test_audit_accepts_body_revision_when_summary_fingerprint_itself_stays_stale(self):
        info = self.info(signature='old-signature')
        info.update(latest_message_id='old', latest_text_sha256=hashlib.sha256(b'Synthetic buyer request').hexdigest())
        self.connect([info])
        water = self.service.repo.get('message_watermark', 'customer-a')
        water['checked_at'] = now_ms() - 301_000
        self.service.repo.put('message_watermark', 'customer-a', water)
        self.facade.bodies['customer-a'] = [self.message(text='Body correction missing from summary index')]
        self.tick()
        self.idle()
        self.assertEqual(self.jobs()[0]['status'], 'succeeded')
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        self.assertEqual(self.service.get_messages('customer-a')['items'][0]['text'],
                         'Body correction missing from summary index')

    def test_same_millisecond_signature_change_triggers_immediate_sync(self):
        self.connect([self.info(signature='first')])
        self.facade.summaries[None] = self.facade.summary_page([self.info(signature='changed')])
        self.facade.bodies['customer-a'] = [self.message(text='Revised source')]
        self.tick()
        self.idle()
        self.assertEqual(len(self.facade.read_calls), 1)
        self.assertEqual(self.service.get_messages('customer-a')['items'][0]['text'], 'Revised source')
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        self.tick()
        self.assertEqual(len(self.facade.read_calls), 1)

    def test_same_millisecond_latest_id_and_text_hash_require_body_index_to_catch_up(self):
        self.connect([self.info(signature='first')])
        for mid, body in [('same-ms-new', 'New buyer text'), ('old', 'Corrected buyer text')]:
            with self.subTest(message_id=mid):
                info = self.info(signature=mid)
                info.update(latest_message_id=mid, latest_text_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest())
                self.facade.summaries[None] = self.facade.summary_page([info])
                self.tick()
                self.idle()
                self.assertTrue(self.service._message_sync_pending('customer-a'))
                self.assertEqual(self.jobs()[0]['status'], 'deferred')
                task = self.service.repo.get('message_sync_task', 'customer-a')
                task['retry_at'] = 0
                self.service.repo.put('message_sync_task', 'customer-a', task)
                self.facade.bodies['customer-a'] = [self.message(mid=mid, text=body)]
                self.tick()
                self.idle()
                self.assertFalse(self.service._message_sync_pending('customer-a'))

    def test_retry_can_adopt_new_revision_without_waiting_for_obsolete_text_hash(self):
        self.connect([self.info(signature='first')])
        info = self.info(signature='intermediate')
        info.update(latest_message_id='old', latest_text_sha256=hashlib.sha256(b'Intermediate edit').hexdigest())
        self.facade.summaries[None] = self.facade.summary_page([info])
        self.tick()
        self.idle()
        self.assertTrue(self.service._message_sync_pending('customer-a'))
        info.update(activity_signature='final', latest_text_sha256=hashlib.sha256(b'Final edit').hexdigest())
        self.facade.summaries[None] = self.facade.summary_page([info])
        self.facade.bodies['customer-a'] = [self.message(text='Final edit')]
        task = self.service.repo.get('message_sync_task', 'customer-a')
        task['retry_at'] = 0
        self.service.repo.put('message_sync_task', 'customer-a', task)
        self.tick()
        self.idle()
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        self.assertEqual(self.jobs()[0]['status'], 'succeeded')

    def test_sweep_cursor_and_pending_activity_survive_restart(self):
        self.connect([self.info()])
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=2000)], True, 1500)
        self.facade.summaries[1500] = self.facade.summary_page([], True, 1200)
        self.facade.bodies['customer-a'] = ServiceError('offline', 'synthetic')
        self.tick()
        self.idle()
        self.service.close()
        self.service = ReceptionService(self.root, 'live', seller_id='10001')
        self.service._activate_seller('10001')
        self.service.checks['messages']['status'] = 'passed'
        sweep = self.service.repo.get('message_monitor', 'summary')
        self.assertEqual(sweep['next_cursor'], 1200)
        sweep['next_scan_at'] = 0
        self.service.repo.put('message_monitor', 'summary', sweep)
        self.facade.summaries[1200] = self.facade.summary_page([self.info('customer-older', 1000)])
        self.facade.bodies['customer-older'] = [self.message('customer-older')]
        self.facade.summary_calls.clear()
        self.tick()
        self.idle()
        self.assertEqual(self.facade.summary_calls, [(None, 20), (1200, 20)])
        self.assertTrue(self.service._message_sync_pending('customer-a'))
        self.assertIsNotNone(self.service.repo.get('conversation', 'customer-older'))

    def test_explicit_account_refresh_reads_all_summary_pages(self):
        self.connect([self.info()])
        self.facade.summaries[None] = self.facade.summary_page([self.info()], True, 900)
        self.facade.summaries[900] = self.facade.summary_page([self.info('older-customer', 800)])
        self.facade.bodies['older-customer'] = [self.message('older-customer', 800)]
        job = self.wait_job(self.service.call('sync_messages'))
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(self.facade.summary_calls, [(None, 100), (900, 100)])
        self.assertEqual({c[0] for c in self.facade.read_calls}, {'customer-a', 'older-customer'})
        self.assertEqual(self.service.repo.get('message_watermark', 'older-customer')['latest_timestamp'], 800)

    def test_newly_synced_reply_and_other_customer_body_both_progress(self):
        self.connect([self.info(), self.info('customer-b')])
        self.service.checks['ai']['status'] = 'passed'
        self.service.settings['merge_seconds'] = 0
        self.service.set_scope('selected', ['customer-a', 'customer-b'])
        start = self.service.set_reception(True)['reception']['started_at_ms']
        events = []
        ai_started, ai_release, other_read = threading.Event(), threading.Event(), threading.Event()
        generate = self.facade.generate
        def ai(context, request_id):
            cid = context['case']['conversation_id']
            events.append('generate-' + cid)
            if cid == 'customer-a':
                ai_started.set()
                if not ai_release.wait(3):
                    raise AssertionError('synthetic AI release missing')
            return generate(context, request_id)
        self.facade.generate = ai
        def body(cid, cursor):
            events.append('read-' + cid)
            if cid == 'customer-b':
                other_read.set()
            return {'messages': [self.message(cid, start + 1, 'fresh', 'I am interested in ceramic mugs.')],
                    'has_more': False, 'next_cursor': None}
        self.facade.body_hook = body
        self.facade.summaries[None] = self.facade.summary_page([
            self.info(stamp=start + 1), self.info('customer-b', start + 1)])
        self.scheduler_patch.stop()
        try:
            self.tick()
            self.assertTrue(ai_started.wait(2), events)
            self.assertTrue(other_read.wait(2), events)
        finally:
            ai_release.set()
            self.scheduler_patch.start()
        self.idle()
        self.assertIn('generate-customer-a', events)
        self.assertIn('read-customer-b', events)
        self.assertEqual(self.facade.send_calls, 0)

    def test_explicit_ai_check_does_not_wait_for_all_pending_customer_bodies(self):
        self.connect([self.info(), self.info('customer-b')])
        started, release = threading.Event(), threading.Event()
        ai_started, ai_release, other_read = threading.Event(), threading.Event(), threading.Event()
        events = []
        freeze = self.service._freeze_reception_model
        def ai():
            events.append('ai-check')
            ai_started.set()
            if not ai_release.wait(3):
                raise AssertionError('synthetic AI release missing')
            return freeze()
        self.service._freeze_reception_model = ai
        def body(cid, cursor):
            events.append('read-' + cid)
            if cid == 'customer-a':
                started.set()
                if not release.wait(3):
                    raise AssertionError('synthetic body release missing')
            if cid == 'customer-b':
                other_read.set()
            return {'messages': [self.message(cid, 2000, 'fresh')], 'has_more': False, 'next_cursor': None}
        self.facade.body_hook = body
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=2000), self.info('customer-b', 2000)])
        self.tick()
        try:
            self.assertTrue(started.wait(2))
            checked = self.service.call('check_ai')
            release.set()
            self.assertTrue(ai_started.wait(2), events)
            self.assertTrue(other_read.wait(2), events)
        finally:
            release.set()
            ai_release.set()
        self.assertEqual(self.wait_job(checked)['status'], 'succeeded')
        self.idle()
        self.assertIn('ai-check', events)
        self.assertIn('read-customer-b', events)
        self.assertEqual(self.facade.send_calls, 0)

    def test_summary_activity_during_automatic_ai_makes_result_stale(self):
        self.connect([self.info()])
        self.service.checks['ai']['status'] = 'passed'
        self.service.set_scope('selected', ['customer-a'])
        start = self.service.set_reception(True, True)['reception']['started_at_ms']
        self.service._ingest(self.info(), [self.message(stamp=start + 1, mid='fresh', text='I am interested in ceramic mugs.')])
        case = self.service.get_conversation('customer-a')['case']
        self.facade.block_generate = True
        ack = self.service._enqueue('generate_draft', {'case_id': case['case_id'],
                                                     'expected_case_revision': case['revision']}, automatic=True)
        self.assertTrue(self.facade.started.wait(1))
        with self.service._lock:
            self.service._observe_message_activity(self.info(stamp=start + 2))
        self.facade.release.set()
        job = self.wait_job(ack)
        self.assertEqual(job['status'], 'failed', job)
        self.assertEqual(job['retry_status'], 'exhausted')
        self.assertEqual(job['last_error_code'], 'source_changed')
        self.assertIsNone(self.service.get_conversation('customer-a')['draft'])
        self.assertEqual(self.facade.send_calls, 0)
        # The completed old request is not replayed. A genuinely new buyer
        # message must still pass the monitor and automatic scheduler.
        self.facade.block_generate = False
        self.facade.bodies['customer-a'] = [self.message(),
            self.message(stamp=start + 1, mid='fresh', text='I am interested in ceramic mugs.'),
            self.message(stamp=start + 3, mid='new-buyer-source', text='I need 200 ceramic mugs.')]
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=start + 3)])
        # Simulate a slow read: the merge window starts when the buyer message
        # is ingested, not when reception was enabled.
        with patch('reception_service.service.now_ms', return_value=start + 5000):
            self.tick()
            self.idle()
        ingested_at = self.service.repo.get('conversation', 'customer-a')['last_buyer_at']
        self.assertEqual(ingested_at, start + 5000)
        schedule_at = ingested_at + self.service.settings['merge_seconds'] * 1000 + 1
        with patch('reception_service.service.now_ms', return_value=schedule_at):
            self.schedule(self.service)
        self.idle()
        fresh_jobs = [row for row in self.service.repo.list('job')
                      if row.get('kind') == 'generate_draft' and row.get('source_message_id') == 'new-buyer-source']
        self.assertEqual(len(fresh_jobs), 1, {
            'jobs': self.service.list_jobs(include_background=True),
            'sync_task': self.service.repo.get('message_sync_task', 'customer-a'),
            'activity': self.service.repo.get('message_activity', 'customer-a'),
            'watermark': self.service.repo.get('message_watermark', 'customer-a'),
            'conversation': self.service.repo.get('conversation', 'customer-a'),
            'source': self.service._automatic_source('customer-a'),
            'settings': self.service.settings,
        })
        self.assertEqual(fresh_jobs[0]['status'], 'succeeded', fresh_jobs[0])
        self.assertEqual(self.facade.generate_calls, 2)

    def test_demo_retains_existing_worker_full_poll(self):
        self.service.close()
        self.facade.seller = 'demo-seller'
        self.service = ReceptionService(self.root, 'demo')
        captured = []
        self.service._next_poll = 0
        with patch.object(self.service, '_enqueue', side_effect=lambda kind, params, **kwargs: captured.append((kind, params))):
            self.schedule(self.service)
        self.assertEqual(captured, [('sync_messages', {})])
        self.assertIsNone(self.service._monitor_thread)

    def test_old_facade_summary_result_is_discarded_after_settings_save(self):
        self.connect()
        started, release = threading.Event(), threading.Event()
        def blocked(cursor, count):
            started.set()
            self.assertTrue(release.wait(3))
            return self.facade.summary_page([self.info('late-customer')])
        self.facade.summary_hook = blocked
        thread = threading.Thread(target=self.tick)
        thread.start()
        self.assertTrue(started.wait(1))
        replacement = MonitorFacade()
        try:
            with patch('reception_service.service.build_integrations', return_value=replacement):
                self.service.save_settings(probe_seconds=2)
            self.assertIsNone(self.service.repo.get('conversation', 'late-customer'))
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.service.repo.get('conversation', 'late-customer'))
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.service.get_snapshot()['message_sync']['status'], 'waiting_connection')

    def test_replaced_facade_late_body_result_is_discarded(self):
        self.connect([self.info()])
        started, release = threading.Event(), threading.Event()
        def body(cid, cursor):
            started.set()
            self.assertTrue(release.wait(3))
            return {'messages': [self.message(stamp=2000, mid='late')], 'has_more': False, 'next_cursor': None}
        self.facade.body_hook = body
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=2000)])
        try:
            self.tick()
            self.assertTrue(started.wait(1))
            self.service.integrations = MonitorFacade()
        finally:
            release.set()
        self.idle()
        self.assertEqual(self.jobs()[0]['status'], 'cancelled')
        self.assertEqual([m['message_id'] for m in self.service.get_messages('customer-a')['items']], ['old'])
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 1000)

    def test_monitor_progresses_on_separate_thread_while_ai_worker_is_blocked(self):
        self.connect([self.info()])
        self.facade.block_generate = True
        case = self.service.get_conversation('customer-a')['case']
        ack = self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']})
        self.assertTrue(self.facade.started.wait(1))
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=2000)])
        self.facade.bodies['customer-a'] = [self.message(stamp=2000, mid='new')]
        self.start_monitor(self.service)
        self.service._monitor_wake.set()
        try:
            self.until(lambda: self.service.repo.get('message_activity', 'customer-a', {}).get('latest_timestamp') == 2000)
            self.assertEqual(self.service.repo.get('job', ack['job_id'])['status'], 'running')
            self.until(lambda: len(self.jobs()) == 1 and self.jobs()[0]['status'] == 'succeeded')
            self.assertEqual(self.service.repo.get('job', ack['job_id'])['status'], 'running')
        finally:
            self.facade.release.set()
        self.idle()
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], 2000)

    def test_close_joins_monitor_before_closing_database_and_discards_late_summary(self):
        self.connect()
        started, release = threading.Event(), threading.Event()
        def blocked(cursor, count):
            started.set()
            self.assertTrue(release.wait(3))
            return self.facade.summary_page([self.info('after-close')])
        self.facade.summary_hook = blocked
        self.start_monitor(self.service)
        self.assertTrue(started.wait(1))
        closer = threading.Thread(target=self.service.close)
        closer.start()
        self.until(lambda: self.service._closing)
        self.assertTrue(closer.is_alive())
        self.assertIsNone(self.service.repo.get('conversation', 'after-close'))
        release.set()
        closer.join(3)
        self.assertFalse(closer.is_alive())
        self.assertFalse(self.service._monitor_thread.is_alive())
        self.assertTrue(self.service._closed)

    def test_settings_validate_probe_bounds_and_snapshot_is_fixed_content_free_dto(self):
        self.assertEqual(self.service.get_snapshot()['settings']['probe_seconds'], 2)
        for invalid in (True, 1, 61, '5', float('nan')):
            with self.subTest(value=invalid), self.assertRaises(ServiceError):
                self.service.save_settings(probe_seconds=invalid)
        for valid in (2, 60, 2.5):
            self.assertEqual(self.service.save_settings(probe_seconds=valid)['settings']['probe_seconds'], valid)
        sync = self.service.get_snapshot()['message_sync']
        self.assertEqual(set(sync), {'status', 'checked_at', 'poll_seconds', 'error_code', 'pending_count',
                                     'summary_requests', 'body_syncs', 'failed_checks', 'last_body_sync_at',
                                     'waiting_activity_count', 'background_pending_count', 'active_read_count'})
        self.assertEqual(sync['poll_seconds'], 2.5)
        self.assertEqual(self.service.get_snapshot()['app']['version'], __version__)

    def test_pending_activity_blocks_old_automatic_source_after_reception_pause(self):
        self.connect([self.info()])
        self.service.checks['ai']['status'] = 'passed'
        self.service.set_scope('selected', ['customer-a'])
        start = self.service.set_reception(True)['reception']['started_at_ms']
        self.service._ingest(self.info(), [self.message(stamp=start + 1, mid='fresh')])
        self.assertIsNotNone(self.service._automatic_source('customer-a'))
        with self.service._lock:
            self.service._observe_message_activity(self.info(stamp=start + 2))
        self.assertIsNone(self.service._automatic_source('customer-a'))
        self.service.set_reception(False)
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=start + 2)])
        self.facade.bodies['customer-a'] = [self.message(stamp=start + 2, mid='newest')]
        self.tick()
        self.idle()
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['latest_timestamp'], start + 2)
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertFalse(self.service.get_snapshot()['reception']['running'])

    def test_old_empty_history_waits_without_failure_or_false_watermark(self):
        self.connect()
        old = now_ms() - 2 * 24 * 60 * 60 * 1000
        self.facade.summaries[None] = self.facade.summary_page([dict(self.info(stamp=old), activity_clock='conversation')])
        self.tick()
        self.idle()
        task = self.service.repo.get('message_sync_task', 'customer-a')
        self.assertEqual(self.jobs()[0]['status'], 'deferred')
        self.assertEqual(task['wait_reason'], 'empty_history')
        self.assertEqual(task['failures'], 0)
        self.assertEqual(task['empty_reads'], 1)
        self.assertIsNone(self.service.repo.get('message_watermark', 'customer-a'))
        self.assertEqual(self.service.repo.get('coverage', 'customer-a')['status'], 'partial')
        self.assertTrue(self.service._message_sync_pending('customer-a'))
        with patch('reception_service.message_monitor.now_ms', return_value=now_ms() + 61_000):
            self.tick()
            self.idle()
        task = self.service.repo.get('message_sync_task', 'customer-a')
        self.assertEqual(task['wait_reason'], 'waiting_activity')
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        self.assertIsNone(self.service.repo.get('message_watermark', 'customer-a'))
        self.assertEqual(self.service.get_snapshot()['message_sync']['waiting_activity_count'], 1)
        for hour in range(1, 5):
            with patch('reception_service.message_monitor.now_ms', return_value=now_ms() + hour * 3_600_000):
                self.tick()
                self.idle()
        self.assertEqual(len(self.facade.read_calls), 2)
        self.assertEqual(len(self.jobs()), 2)
        self.assertFalse(any(job['kind'] == 'sync_messages' for job in self.service.list_jobs()))
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.send_calls, 0)

    def test_new_activity_immediately_wakes_an_empty_archived_conversation(self):
        self.connect()
        old = now_ms() - 2 * 24 * 60 * 60 * 1000
        self.facade.summaries[None] = self.facade.summary_page([dict(self.info(stamp=old), activity_clock='conversation')])
        self.tick()
        self.idle()
        new = now_ms()
        self.facade.summaries[None] = self.facade.summary_page([dict(self.info(stamp=new), activity_clock='conversation')])
        self.facade.bodies['customer-a'] = [self.message(stamp=new, mid='new-actual-message')]
        self.tick()
        self.idle()
        self.assertEqual(len(self.facade.read_calls), 2)
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['body_latest_timestamp'], new)
        self.assertIsNone(self.service.repo.get('message_sync_task', 'customer-a')['wait_reason'])

    def test_upgrade_parks_existing_empty_archive_retries_without_another_read(self):
        self.connect()
        info = dict(self.info(stamp=1000), activity_clock='conversation')
        with self.service._lock:
            self.service._observe_message_activity(info)
            self.service._ingest(info, [], full=True, has_more=False)
            self.service.repo.put('message_sync_task', 'customer-a', dict(conversation_id='customer-a',
                pending=True, activity_required=True, target_timestamp=1000, target_version=1,
                failures=0, wait_reason='empty_history', defer_count=3, retry_at=0, job_id=None))
        self.tick()
        self.idle()
        self.assertEqual(self.facade.read_calls, [])
        self.assertEqual(self.jobs(), [])
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        self.assertEqual(self.service.repo.get('message_sync_task', 'customer-a')['wait_reason'], 'waiting_activity')
        self.assertIsNone(self.service.repo.get('message_watermark', 'customer-a'))

    def test_recent_index_lag_uses_short_retry_and_transport_errors_stay_failed(self):
        self.connect()
        current = now_ms()
        self.facade.summaries[None] = self.facade.summary_page([dict(self.info(stamp=current), activity_clock='conversation')])
        self.tick()
        self.idle()
        task = self.service.repo.get('message_sync_task', 'customer-a')
        self.assertLess(task['retry_at'] - now_ms(), 10_000)
        self.assertEqual(self.jobs()[0]['status'], 'deferred')
        task['retry_at'] = 0
        self.service.repo.put('message_sync_task', 'customer-a', task)
        self.facade.bodies['customer-a'] = ServiceError('native_timeout', 'Synthetic timeout')
        self.tick()
        self.idle()
        failed = next(job for job in self.jobs() if job['status'] == 'failed')
        self.assertEqual(failed['error']['code'], 'native_timeout')
        self.assertEqual(self.service.repo.get('message_sync_task', 'customer-a')['failures'], 1)
        self.assertIsNone(self.service.repo.get('message_watermark', 'customer-a'))

    def test_empty_archive_defer_survives_restart(self):
        self.connect()
        self.facade.summaries[None] = self.facade.summary_page([dict(self.info(stamp=1000), activity_clock='conversation')])
        self.tick()
        self.idle()
        before = self.service.repo.get('message_sync_task', 'customer-a')['retry_at']
        self.service.close()
        self.service = ReceptionService(self.root, 'live', seller_id='10001')
        self.service._activate_seller('10001')
        self.service.checks['messages']['status'] = 'passed'
        self.facade.read_calls.clear()
        self.tick()
        self.idle()
        self.assertEqual(self.facade.read_calls, [])
        self.assertEqual(self.service.repo.get('message_sync_task', 'customer-a')['retry_at'], before)
        self.assertTrue(self.service._message_sync_pending('customer-a'))

    def park_empty(self):
        self.connect()
        info = dict(self.info(stamp=1000, signature='initial'), activity_clock='conversation')
        self.facade.summaries[None] = self.facade.summary_page([info])
        self.tick()
        self.idle()
        with patch('reception_service.message_monitor.now_ms', return_value=now_ms() + 61_000):
            self.tick()
            self.idle()
        self.assertEqual(self.service.repo.get('message_sync_task', 'customer-a')['wait_reason'], 'waiting_activity')
        return info

    def test_parked_empty_history_wakes_on_same_timestamp_new_signature(self):
        info = self.park_empty()
        self.facade.read_calls.clear()
        self.facade.summaries[None] = self.facade.summary_page([dict(info, activity_signature='changed')])
        self.facade.bodies['customer-a'] = [self.message(stamp=1000, mid='actual-new-message')]
        self.tick()
        self.idle()
        self.assertEqual(len(self.facade.read_calls), 1)
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        self.assertEqual(self.service.repo.get('message_watermark', 'customer-a')['activity_version'], 2)
        self.assertEqual(self.service.get_snapshot()['message_sync']['waiting_activity_count'], 0)

    def test_new_activity_on_old_clock_gets_recent_index_grace(self):
        info = self.park_empty()
        self.facade.summaries[None] = self.facade.summary_page([dict(info, activity_signature='new-event')])
        self.facade.read_calls.clear()
        for _ in range(2):
            task = self.service.repo.get('message_sync_task', 'customer-a')
            task['retry_at'] = 0
            self.service.repo.put('message_sync_task', 'customer-a', task)
            self.tick()
            self.idle()
        self.assertEqual(len(self.facade.read_calls), 2)
        self.assertTrue(self.service._message_sync_pending('customer-a'))
        self.assertFalse(self.service._waiting_for_activity('customer-a'))
        self.assertIsNone(self.service.repo.get('message_watermark', 'customer-a'))

    def test_parked_empty_history_stays_quiet_after_restart_and_many_ticks(self):
        self.park_empty()
        self.service.close()
        self.service = ReceptionService(self.root, 'live', seller_id='10001')
        self.service._activate_seller('10001')
        self.service.checks['messages']['status'] = 'passed'
        self.facade.read_calls.clear()
        before = len(self.service.repo.list('job'))
        for minute in range(0, 181, 10):
            with patch('reception_service.message_monitor.now_ms', return_value=now_ms() + minute * 60_000):
                self.tick()
                self.idle()
        self.assertEqual(self.facade.read_calls, [])
        self.assertEqual(len(self.service.repo.list('job')), before)
        self.assertEqual(self.service.get_snapshot()['message_sync']['waiting_activity_count'], 1)
        self.assertEqual(self.service.get_snapshot()['message_sync']['pending_count'], 0)
        self.assertIsNone(self.service._automatic_source('customer-a'))

    def test_only_explicit_customer_open_reads_empty_history_once(self):
        self.park_empty()
        self.facade.read_calls.clear()
        for _ in range(10):
            self.service.get_conversation('customer-a')
            self.service.get_snapshot()
        self.assertEqual(self.facade.read_calls, [])
        self.service.get_conversation('customer-a', refresh_empty=True)
        self.idle()
        self.assertEqual(len(self.facade.read_calls), 1)
        self.assertEqual(self.jobs()[0]['sync_reason'], 'selection')
        self.service.get_conversation('customer-a', refresh_empty=True)
        self.idle()
        self.assertEqual(len(self.facade.read_calls), 1)
        self.assertEqual(self.service.repo.get('message_sync_task', 'customer-a')['wait_reason'], 'waiting_activity')

    def test_explicit_history_completion_can_discover_body_without_new_summary(self):
        self.park_empty()
        self.facade.read_calls.clear()
        self.facade.bodies['customer-a'] = [self.message(stamp=1000, mid='delayed-index-body')]
        job = self.wait_job(self.service.call('fill_history', {'conversation_id': 'customer-a'}))
        self.assertEqual(job['status'], 'succeeded')
        self.idle()
        self.assertEqual(len(self.facade.read_calls), 1)
        self.assertFalse(self.service._waiting_for_activity('customer-a'))
        self.assertFalse(self.service._message_sync_pending('customer-a'))
        self.assertEqual(self.service.get_messages('customer-a')['items'][0]['message_id'], 'delayed-index-body')
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.send_calls, 0)

    def test_global_background_budget_does_not_hold_up_new_incoming_activity(self):
        self.connect()
        first = self.info('archived', stamp=1000)
        self.facade.summaries[None] = self.facade.summary_page([first])
        base = now_ms()
        with patch('reception_service.message_monitor.now_ms', return_value=base):
            self.tick()
            self.idle()
        self.facade.summaries[None] = self.facade.summary_page([self.info('other-archive', 2000)])
        with patch('reception_service.message_monitor.now_ms', return_value=base + 59_000):
            self.tick()
            self.idle()
        self.assertEqual(len(self.facade.read_calls), 1)
        current = base + 60_000
        self.facade.summaries[None] = self.facade.summary_page([self.info('new-incoming', current)])
        self.facade.bodies['new-incoming'] = [self.message('new-incoming', current, mid='new-incoming-body')]
        with patch('reception_service.message_monitor.now_ms', return_value=base + 59_500):
            self.tick()
            self.idle()
        self.assertEqual([row[0] for row in self.facade.read_calls], ['archived', 'new-incoming'])
        self.assertEqual(self.service.repo.get('message_watermark', 'new-incoming')['latest_timestamp'], current)

    def test_slow_archive_scan_does_not_shorten_actual_background_interval(self):
        self.connect()
        self.facade.summaries[None] = self.facade.summary_page([
            self.info('first-archive', 1000), self.info('second-archive', 2000)])
        clock = [now_ms()]
        scanned = [False]
        original = self.service._defer_legacy_empty_history

        def slow_scan(cid, task, activity, stamp):
            if not scanned[0]:
                scanned[0] = True
                clock[0] += 3500
            return original(cid, task, activity, stamp)

        with patch('reception_service.message_monitor.now_ms', side_effect=lambda: clock[0]), \
                patch.object(self.service, '_defer_legacy_empty_history', side_effect=slow_scan):
            self.tick()
            self.idle()
        queued_at = clock[0]
        budget = self.service.repo.get('message_monitor', 'body_budget')
        self.assertGreaterEqual(budget['next_history_at'], queued_at + 60_000)
        with patch('reception_service.message_monitor.now_ms', return_value=queued_at + 59_999):
            self.tick()
            self.idle()
        self.assertEqual(len(self.facade.read_calls), 1)
        with patch('reception_service.message_monitor.now_ms', return_value=queued_at + 60_000):
            self.tick()
            self.idle()
        self.assertEqual(len(self.facade.read_calls), 2)

    def test_global_background_budget_is_durable_across_restart(self):
        self.connect()
        self.facade.summaries[None] = self.facade.summary_page([self.info('first-archive', 1000)])
        self.tick()
        self.idle()
        budget = self.service.repo.get('message_monitor', 'body_budget')
        self.service.close()
        self.service = ReceptionService(self.root, 'live', seller_id='10001')
        self.service._activate_seller('10001')
        self.service.checks['messages']['status'] = 'passed'
        self.facade.summaries[None] = self.facade.summary_page([self.info('second-archive', 2000)])
        self.facade.read_calls.clear()
        self.tick()
        self.idle()
        self.assertEqual(self.facade.read_calls, [])
        self.assertEqual(self.service.repo.get('message_monitor', 'body_budget'), budget)

    def test_recent_empty_activity_has_finite_fast_retries_without_false_completion(self):
        self.connect()
        self.facade.summaries[None] = self.facade.summary_page([self.info(stamp=now_ms())])
        for _ in range(4):
            task = self.service.repo.get('message_sync_task', 'customer-a', {})
            if task:
                task['retry_at'] = 0
                self.service.repo.put('message_sync_task', 'customer-a', task)
            self.tick()
            self.idle()
        self.assertEqual(len(self.facade.read_calls), 4)
        task = self.service.repo.get('message_sync_task', 'customer-a')
        self.assertEqual(task['read_priority'], 'history')
        task['retry_at'] = 0
        self.service.repo.put('message_sync_task', 'customer-a', task)
        self.service.repo.put('message_monitor', 'body_budget', {'next_history_at': now_ms() + 60_000})
        self.tick()
        self.idle()
        self.assertEqual(len(self.facade.read_calls), 4)
        self.assertTrue(self.service._message_sync_pending('customer-a'))
        self.assertIsNone(self.service.repo.get('message_watermark', 'customer-a'))

    def test_background_jobs_do_not_emit_activity_rows_and_real_errors_are_aggregated(self):
        self.connect()
        events = []
        self.service._emit_callback = events.append
        for index in range(2):
            cid = 'timeout-customer-' + str(index)
            self.facade.summaries[None] = self.facade.summary_page([self.info(cid, now_ms() + index)])
            self.facade.bodies[cid] = ServiceError('native_timeout', 'PRIVATE_CUSTOMER_TEXT')
            self.tick()
            self.idle()
        self.assertFalse(any(event['event'] == 'job.updated' and event['data'].get('background_sync') for event in events))
        aggregate = [job for job in self.service.list_jobs() if job['job_id'].startswith('sync-error:')]
        self.assertEqual(len(aggregate), 1)
        self.assertEqual(aggregate[0]['diagnostics']['count'], 2)
        self.assertEqual(aggregate[0]['error']['code'], 'native_timeout')
        self.assertNotIn('PRIVATE', str(aggregate))
        raw = [job for job in self.service.repo.list('job') if job.get('background_sync') and job['status'] == 'failed']
        self.assertEqual(len(raw), 2)


if __name__ == '__main__':
    unittest.main()
