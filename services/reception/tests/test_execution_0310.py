"""Execution-slot isolation, cancellation, priority and bounded local admission."""
import queue
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError, safe_diagnostics
from reception_service.execution import BusinessQueue
from reception_service.service import ReceptionService


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.service = ReceptionService(Path(self.temporary.name))
        self.addCleanup(self.service.close)
        self.cid = self.service.repo.list('conversation')[0]['conversation_id']
        self.case = self.service.get_conversation(self.cid)['case']

    def wait_job(self, job_id, timeout=3):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            job = self.service.repo.get('job', job_id)
            if job['status'] not in ('queued', 'running'):
                return job
            threading.Event().wait(.01)
        self.fail('Job did not finish')

    def test_slow_generation_does_not_block_body_sync(self):
        entered, release, synced = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(params, job_id, epoch, facade):
            entered.set()
            release.wait(3)
            return {'message': 'Synthetic completion'}
        def sync(params, job_id, epoch, facade):
            synced.set()
            return {'message': 'Synthetic body synchronized'}
        self.service._task_manual_suggestion = slow
        self.service._task_sync_messages = sync
        request = self.service.generate_manual_suggestion(self.cid, 'slow-query', self.case['case_id'])
        self.assertTrue(entered.wait(1))
        fast = self.service._enqueue('sync_messages', {'conversation_id': self.cid})
        self.assertTrue(synced.wait(1), 'Body sync waited for the slow generation')
        self.assertEqual(self.wait_job(fast['job_id'])['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('job', request['job_id'])['status'], 'running')
        release.set()
        self.assertEqual(self.wait_job(request['job_id'])['status'], 'succeeded')

    def test_pause_cancels_running_generation_without_waiting_for_its_full_timeout(self):
        entered = threading.Event()
        def cancellable(params, job_id, epoch, facade):
            entered.set()
            event = self.service._generation_cancel_event(job_id)
            event.wait(2)
            if event.is_set():
                raise ServiceError('cancelled', 'Synthetic query cancelled')
            return {'message': 'Unexpected delayed result'}
        self.service._task_manual_suggestion = cancellable
        result = self.service.generate_manual_suggestion(self.cid, 'cancel-query', self.case['case_id'])
        self.assertTrue(entered.wait(1))
        self.service._pause()
        self.assertEqual(self.wait_job(result['job_id'], timeout=1)['status'], 'cancelled')
        self.assertFalse(self.service.repo.get('conversation', self.cid)['human_owned'])

    def test_registered_clone_read_lease_expires_on_cancel_or_owner_replacement(self):
        service, clone = self.service, object()
        event = service._generation_cancel_event('synthetic-clone')
        service._generation_active['synthetic-clone'] = (threading.current_thread(), event)
        owner = service.integrations
        service._generation_read_bindings[id(clone)] = (clone, owner, service._monitor_generation, service.repo, 'synthetic-clone')
        try:
            token = service._read_token(clone)
            self.assertTrue(service._read_current(token))
            event.set()
            self.assertFalse(service._read_current(token))
            event.clear()
            service.integrations = object()
            self.assertFalse(service._read_current(token))
        finally:
            service.integrations = owner
            service._generation_active.clear()
            service._generation_read_bindings.clear()

    def test_ready_send_precedes_older_generation_and_admission_is_bounded(self):
        work = BusinessQueue(2)
        generate = ('g', 'generate_draft', {}, 0, True)
        send = ('s', 'automatic_send', {}, 0, True)
        work.put(generate)
        work.put(send)
        with self.assertRaises(ServiceError) as caught:
            work.put(('overflow', 'sync_messages', {}, 0, False))
        self.assertEqual(caught.exception.code, 'task_queue_full')
        item = work.take_ready(lambda value: True, self.service._work_priority)
        self.assertEqual(item, send)
        work.task_done()
        self.assertEqual(work.get_nowait(), generate)
        work.task_done()
        self.assertEqual(work.unfinished_tasks, 0)

    def test_send_queue_rejection_rolls_back_claim_and_preserves_draft(self):
        service = self.service
        draft = service.save_chat_draft(self.cid, 'Synthetic reply', None, 'save-before-full')
        params = {'conversation_id': self.cid, 'draft_id': draft['draft_id'],
                  'expected_revision': draft['revision'], 'client_request_id': 'send-when-full'}
        before_jobs = service.repo.list('job')
        before_operations = service.repo.list('operation')
        with patch.object(service._queue, 'put', side_effect=ServiceError('task_queue_full', 'full')):
            with self.assertRaises(ServiceError) as error:
                service._enqueue_chat(params)
        self.assertEqual(error.exception.code, 'task_queue_full')
        self.assertEqual(service.repo.list('job'), before_jobs)
        self.assertEqual(service.repo.list('operation'), before_operations)
        self.assertEqual(service.repo.get('chat_draft', self.cid), draft)
        self.assertIsNone(service.repo.get('chat_request', 'send-when-full'))
        self.assertFalse(service._chat_facades)

    def test_queue_rejection_rolls_back_other_job_claims(self):
        service = self.service
        actions = [lambda: service._enqueue('sync_messages', {'conversation_id': self.cid}),
                   lambda: service.generate_manual_suggestion(self.cid, 'full-suggestion', self.case['case_id']),
                   lambda: service.organize_requirements(self.cid, self.case['case_id'], 'full-requirements')]
        for action in actions:
            with self.subTest(action=action):
                before_jobs = service.repo.list('job')
                with patch.object(service._queue, 'put', side_effect=ServiceError('task_queue_full', 'full')):
                    with self.assertRaises(ServiceError) as error:
                        action()
                self.assertEqual(error.exception.code, 'task_queue_full')
                self.assertEqual(service.repo.list('job'), before_jobs)
        self.assertIsNone(service.repo.get('manual_request', 'full-suggestion'))
        self.assertIsNone(service.repo.get('requirement_request', 'full-requirements'))

    def test_full_queue_does_not_consume_unknown_reconciliation_retry(self):
        service = self.service
        operation = {'operation_id': 'unknown-test', 'status': 'unknown', 'reconcile_attempt': 1,
                     'next_reconcile_at': 0}
        service.repo.put('operation', operation['operation_id'], operation)
        service.account_verified = True
        with patch.object(service, '_enqueue', side_effect=ServiceError('task_queue_full', 'full')):
            service._schedule_continuity()
        self.assertEqual(service.repo.get('operation', operation['operation_id']), operation)

    def test_full_queue_preserves_deferred_generation_retry(self):
        service = self.service
        source = service._latest_buyer(self.cid)
        job = {'job_id': 'deferred-test', 'kind': 'generate_draft', 'status': 'deferred',
               'retry_status': 'waiting', 'retry_at': 0, 'created_at': 1, 'epoch': service._epoch,
               'conversation_id': self.cid, 'case_id': self.case['case_id'],
               'source_message_id': source['message_id'], 'automatic': False}
        service.repo.put('job', job['job_id'], job)
        with patch.object(service, '_enqueue', side_effect=ServiceError('task_queue_full', 'full')):
            service._schedule_generation_retries()
        self.assertEqual(service.repo.get('job', job['job_id']), job)

    def test_generation_start_failure_releases_queue_and_worker_continues(self):
        service = self.service
        original = threading.Thread.start
        def fail_ai(thread):
            if thread.name.startswith('reception-ai-'):
                raise RuntimeError('synthetic thread start failure')
            return original(thread)
        with patch.object(threading.Thread, 'start', fail_ai):
            job = service.generate_manual_suggestion(self.cid, 'cannot-start', self.case['case_id'])['job_id']
            self.assertEqual(self.wait_job(job)['status'], 'failed')
        self.assertTrue(service._worker.is_alive())
        self.assertFalse(service._generation_active)
        self.assertNotIn(job, service._generation_events)
        self.assertEqual(service._queue.unfinished_tasks, 0)
        service._task_sync_messages = lambda *args: {'message': 'done'}
        other = service._enqueue('sync_messages', {'conversation_id': self.cid})
        self.assertEqual(self.wait_job(other['job_id'])['status'], 'succeeded')

    def test_clone_failure_releases_queue_and_worker(self):
        from reception_service.integrations.live import LiveIntegration
        service = self.service
        original = service.integrations
        facade = object.__new__(LiveIntegration)
        service.integrations = facade
        try:
            with patch.object(LiveIntegration, '__init__', side_effect=RuntimeError('clone failed')):
                job = service.generate_manual_suggestion(self.cid, 'clone-failed', self.case['case_id'])['job_id']
                self.assertEqual(self.wait_job(job)['status'], 'failed')
            self.assertTrue(service._worker.is_alive())
            self.assertFalse(service._generation_active)
            self.assertFalse(service._generation_read_bindings)
            self.assertEqual(service._queue.unfinished_tasks, 0)
        finally:
            service.integrations = original

    def test_cancelled_queued_generation_clears_event_after_dequeue(self):
        service = self.service
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(params, job_id, epoch, facade):
            entered.set()
            release.wait(3)
            return {'message': 'done'}
        service._task_manual_suggestion = slow
        first = service.generate_manual_suggestion(self.cid, 'first-slot', self.case['case_id'])['job_id']
        self.assertTrue(entered.wait(1))
        second = service.generate_manual_suggestion(self.cid, 'second-slot', self.case['case_id'])['job_id']
        service._cancel_generation_tasks(self.cid, exclude_job_id=first)
        self.assertEqual(service.repo.get('job', second)['status'], 'cancelled')
        release.set()
        self.wait_job(first)
        deadline = time.monotonic() + 2
        while second in service._generation_events and time.monotonic() < deadline:
            threading.Event().wait(.01)
        self.assertNotIn(second, service._generation_events)
        self.assertEqual(service._queue.unfinished_tasks, 0)

    def test_only_one_generation_runs_at_a_time(self):
        service = self.service
        first_started, second_started, release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(params, job_id, epoch, facade):
            if not first_started.is_set():
                first_started.set()
                release.wait(3)
            else:
                second_started.set()
            return {'message': 'done'}
        service._task_manual_suggestion = slow
        first = service.generate_manual_suggestion(self.cid, 'slot-first', self.case['case_id'])['job_id']
        self.assertTrue(first_started.wait(1))
        second = service.generate_manual_suggestion(self.cid, 'slot-second', self.case['case_id'])['job_id']
        self.assertFalse(second_started.wait(.15))
        release.set()
        self.wait_job(first)
        self.assertTrue(second_started.wait(2))
        self.assertEqual(self.wait_job(second)['status'], 'succeeded')

    def test_close_is_serialized_between_callers(self):
        service = self.service
        entered, release = threading.Event(), threading.Event()
        original = service._pause
        calls = []
        def pause(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        with patch.object(service, '_pause', side_effect=pause):
            first = threading.Thread(target=service.close)
            second = threading.Thread(target=service.close)
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            second.join(.1)
            self.assertTrue(second.is_alive())
            release.set()
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(calls), 1)

    def test_duplicate_queue_entries_claim_job_once(self):
        service = self.service
        calls = []
        item = ('duplicate-job', 'sync_messages', {'conversation_id': self.cid}, service._epoch, False)
        service._task_sync_messages = lambda *args: calls.append(1) or {'message': 'done'}
        with service._lock:
            service.repo.put('job', item[0], {'job_id': item[0], 'kind': item[1],
                             'conversation_id': self.cid, 'case_id': None, 'status': 'queued',
                             'created_at': 1, 'automatic': False})
            service._queue.put(item)
            service._queue.put(item)
            service._run_queued(service._queue.get_nowait())
            service._run_queued(service._queue.get_nowait())
        self.assertEqual(calls, [1])
        self.assertEqual(service._queue.unfinished_tasks, 0)

    def test_scheduler_fault_recovers_on_same_consumer_and_consumes_job(self):
        service = self.service
        worker = service._worker
        service._auto_send = True
        service._wa_enabled = True
        calls = []
        def schedule():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('private scheduler text')
        with patch.object(service, '_schedule', side_effect=schedule):
            deadline = time.monotonic() + 3
            while len(calls) < 2 and time.monotonic() < deadline:
                threading.Event().wait(.02)
            self.assertGreaterEqual(len(calls), 2)
            result = service._enqueue('sync_messages', {'conversation_id': self.cid})
            self.assertEqual(self.wait_job(result['job_id'])['status'], 'succeeded')
            self.assertIs(service._worker, worker)
            self.assertEqual(service._queue.unfinished_tasks, 0)
            self.assertFalse(service._executor_health.failed.is_set())
            self.assertFalse(service._auto_send)
            self.assertFalse(service._wa_enabled)

    def test_queue_fault_revokes_and_rejects_new_work(self):
        service = self.service
        service._auto_send = True
        service._wa_enabled = True
        epoch = service._epoch
        event = service._generation_cancel_event('job_synthetic')
        with patch.object(service, '_take_ready_work', side_effect=RuntimeError('secret text')):
            self.assertTrue(service._executor_health.failed.wait(2))
        self.assertFalse(service._auto_send)
        self.assertFalse(service._wa_enabled)
        self.assertGreater(service._epoch, epoch)
        self.assertTrue(event.is_set())
        self.assertEqual(service._executor_health.snapshot()['state'], 'failed')
        with self.assertRaises(ServiceError) as error:
            service._enqueue('sync_messages', {'conversation_id': self.cid})
        self.assertEqual(error.exception.code, 'executor_unavailable')
        self.assertTrue(service.get_snapshot()['conversations'])

    def test_delayed_health_snapshot_timestamp_cannot_postdate_a_newer_failure(self):
        service = self.service
        entered, release = threading.Event(), threading.Event()
        snapshots, events = [], []
        original = service._worker.is_alive
        def delayed_alive():
            if threading.current_thread().name == 'snapshot-probe':
                entered.set()
                release.wait(2)
                return True
            return original()
        service._emit_callback = lambda event: events.append(event)
        probe = threading.Thread(target=lambda: snapshots.append(service.get_executor_health()), name='snapshot-probe')
        with patch.object(service._worker, 'is_alive', side_effect=delayed_alive):
            probe.start()
            try:
                self.assertTrue(entered.wait(1))
                service._executor_health.fail('failed', 'executor_fault')
                failure = next(e['data'] for e in events if e['event'] == 'executor.health' and e['data']['state'] == 'failed')
                threading.Event().wait(.03)
            finally:
                release.set()
                probe.join(2)
        self.assertNotEqual(snapshots[0]['state'], 'failed')
        self.assertLessEqual(snapshots[0]['observed_at_ms'], failure['observed_at_ms'])

    def test_health_event_and_fast_path_do_not_wait_for_business_lock(self):
        service = self.service
        received = threading.Event()
        events = []
        def emit(event):
            if event['event'] == 'executor.health':
                events.append(event['data'])
                received.set()
        service._emit_callback = emit
        locked, release = threading.Event(), threading.Event()
        def hold_lock():
            with service._lock:
                locked.set()
                release.wait(3)
        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            self.assertTrue(locked.wait(1))
            self.assertTrue(received.wait(2))
            result = service.call('get_executor_health')
            self.assertTrue(result['worker_alive'])
            self.assertIn('observed_at_ms', events[-1])
            self.assertEqual(set(result), set(events[-1]))
        finally:
            release.set()
            holder.join(2)
        service.close()
        self.assertFalse(service._health_watchdog.is_alive())

    def test_unexpected_send_failure_preserves_unknown_on_restart(self):
        service = self.service
        draft = service.save_chat_draft(self.cid, 'Synthetic reply', None, 'save-unknown')
        def fail_send(*args):
            raise RuntimeError('sensitive send exception')
        service._task_send_chat = fail_send
        request = service._enqueue_chat({'conversation_id': self.cid, 'draft_id': draft['draft_id'],
                                        'expected_revision': draft['revision'], 'client_request_id': 'unknown-test'})
        self.assertTrue(service._executor_health.failed.wait(2))
        job = self.wait_job(request['job_id'])
        self.assertEqual(job['status'], 'unknown')
        operation = next(o for o in service.repo.list('operation') if o.get('job_id') == request['job_id'])
        self.assertEqual(operation['status'], 'unknown')
        service.close()
        reopened = ReceptionService(Path(self.temporary.name))
        try:
            self.assertEqual(reopened.repo.get('operation', operation['operation_id'])['status'], 'unknown')
            self.assertFalse(reopened._auto_send)
            self.assertFalse(reopened._wa_enabled)
        finally:
            reopened.close()

    def test_diagnostics_redact_and_rotate_and_ignore_write_failure(self):
        health = self.service._executor_health
        try:
            raise RuntimeError('SECRET message and customer body')
        except RuntimeError as error:
            health.log('schedule', 'job_test', error)
            files = list(Path(self.temporary.name).rglob('executor.jsonl'))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding='utf-8')
            self.assertNotIn('SECRET', content)
            self.assertIn('RuntimeError', content)
            files[0].write_bytes(b'x' * (2 * 1024 * 1024))
            health.last_log.clear()
            health.log('schedule', 'job_test', error)
            self.assertTrue(files[0].with_name('executor.jsonl.1').exists())
            health.last_log.clear()
            with patch('pathlib.Path.mkdir', side_effect=OSError('private')):
                health.log('schedule', 'job_test', error)

    def test_dequeue_leases_settle_exactly_once_even_for_duplicate_tuple(self):
        work = BusinessQueue()
        original = ('job_same', 'sync_messages', {}, 0, False)
        work.put(original)
        work.put(original)
        first, second = work.get_nowait(), work.get_nowait()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertFalse(work.complete(original))
        self.assertTrue(work.complete(first))
        self.assertFalse(work.complete(first))
        self.assertEqual(work.unfinished_tasks, 1)
        self.assertTrue(work.complete(second))
        self.assertEqual(work.unfinished_tasks, 0)
        self.assertFalse(work._claims)

    def test_dispatch_fault_before_task_releases_claim_and_preserves_failed_job(self):
        service = self.service
        with patch.object(service, '_dispatch_work', side_effect=RuntimeError('private dispatch')):
            result = service._enqueue('sync_messages', {'conversation_id': self.cid})
            self.assertTrue(service._executor_health.failed.wait(2))
            service._worker.join(2)
        self.assertFalse(service._worker.is_alive())
        self.assertEqual(service.repo.get('job', result['job_id'])['status'], 'failed')
        self.assertEqual(service._queue.unfinished_tasks, 0)
        self.assertFalse(service._queue._claims)

    def test_dispatch_fault_after_completion_does_not_double_settle_or_rewrite_success(self):
        service = self.service
        original = service._dispatch_work
        def after_dispatch(item):
            original(item)
            raise RuntimeError('private final callback')
        with patch.object(service, '_dispatch_work', side_effect=after_dispatch):
            result = service._enqueue('sync_messages', {'conversation_id': self.cid})
            self.assertTrue(service._executor_health.failed.wait(2))
            service._worker.join(2)
        self.assertEqual(service.repo.get('job', result['job_id'])['status'], 'succeeded')
        self.assertEqual(service._queue.unfinished_tasks, 0)
        self.assertFalse(service._queue._claims)

    def test_job_finalizer_fault_keeps_queue_accounting_and_safe_pause(self):
        service = self.service
        original = service._job_update
        def update(job_id, **changes):
            if 'task_elapsed_ms' in (changes.get('diagnostics') or {}):
                raise RuntimeError('private finalizer')
            return original(job_id, **changes)
        with patch.object(service, '_job_update', side_effect=update):
            result = service._enqueue('sync_messages', {'conversation_id': self.cid})
            self.assertTrue(service._executor_health.failed.wait(2))
            service._worker.join(2)
        self.assertEqual(service.repo.get('job', result['job_id'])['status'], 'succeeded')
        self.assertEqual(service._queue.unfinished_tasks, 0)
        self.assertFalse(service._auto_send)
        self.assertFalse(service._wa_enabled)

    def test_failed_worker_drains_unsubmitted_queue_even_when_a_cancellation_fails(self):
        service = self.service
        original = service._cancel_queued
        cancelled = []
        def cancel(item):
            cancelled.append(item[0])
            if len(cancelled) == 1:
                raise RuntimeError('private cancellation')
            original(item)
        def broken_take():
            with service._lock:
                raise RuntimeError('private queue')
        with patch.object(service, '_take_ready_work', side_effect=broken_take), patch.object(service, '_cancel_queued', side_effect=cancel):
            with service._lock:
                first = service._enqueue('sync_messages', {'conversation_id': self.cid})
                second = service._enqueue('check_feishu', {})
            self.assertTrue(service._executor_health.failed.wait(2))
            service._worker.join(2)
        self.assertEqual(set(cancelled), {first['job_id'], second['job_id']})
        self.assertEqual(service.repo.get('job', second['job_id'])['status'], 'cancelled')
        self.assertEqual(service._queue.unfinished_tasks, 0)
        self.assertFalse(service._queue._claims)
        # An unsuccessful durable cleanup is left for startup recovery, never run again here.
        service.close()
        reopened = ReceptionService(Path(self.temporary.name))
        try:
            self.assertEqual(reopened.repo.get('job', first['job_id'])['status'], 'cancelled')
        finally:
            reopened.close()

    def test_phase_budgets_allow_real_ai_wait_but_detect_a_stuck_external_read(self):
        service = self.service
        health = service._executor_health
        item = ('job_phase', 'generate_draft', {}, service._epoch, False)
        health.start_job(item)
        with health.phase(item[0], 'ai_elapsed_ms'):
            with health.lock:
                deadline = health.activities[item[0]]['deadline']
            self.assertFalse(health.check_stalled(at=deadline - 1))
        with health.phase(item[0], 'message_read_ms'):
            with health.lock:
                deadline = health.activities[item[0]]['deadline']
            self.assertTrue(health.check_stalled(at=deadline + 1))
        self.assertEqual(health.snapshot()['state'], 'stalled')
        self.assertFalse(service._running)
        self.assertFalse(service._auto_send)
        self.assertFalse(service._wa_enabled)
        health.finish_job(item[0])

    def test_health_revocation_is_published_before_integration_cleanup(self):
        service = self.service
        events = []
        service._running = service._auto_send = service._wa_enabled = True
        service._emit_callback = lambda event: events.append(event)
        def revoke():
            self.assertFalse(service._running)
            self.assertFalse(service._auto_send)
            self.assertFalse(service._wa_enabled)
            self.assertTrue(any(e['event'] == 'executor.health' and e['data']['state'] == 'failed' for e in events))
        with patch.object(service.integrations, 'revoke', side_effect=revoke):
            service._executor_health.fail('failed', 'executor_fault')

    def test_scheduler_third_failure_stops_without_restarting_or_reauthorizing(self):
        service = self.service
        original_worker = service._worker
        service._running = service._auto_send = service._wa_enabled = True
        with patch.object(service, '_schedule', side_effect=RuntimeError('private repeated schedule')) as schedule:
            self.assertTrue(service._executor_health.failed.wait(9))
            service._worker.join(2)
            self.assertEqual(schedule.call_count, 3)
        self.assertIs(service._worker, original_worker)
        self.assertFalse(service._worker.is_alive())
        self.assertFalse(service._running)
        self.assertFalse(service._auto_send)
        self.assertFalse(service._wa_enabled)
        self.assertEqual(service.get_executor_health()['error_code'], 'schedule_failed')

    def test_existing_exact_chat_ack_is_readable_when_new_work_is_unavailable(self):
        service = self.service
        draft = service.save_chat_draft(self.cid, 'Synthetic frozen body', None, 'save-prior-ack')
        params = {'conversation_id': self.cid, 'draft_id': draft['draft_id'],
                  'expected_revision': draft['revision'], 'client_request_id': 'exact-existing-request'}
        with service._lock:
            ack = service._enqueue_chat(params)
            service._executor_health.fail('failed', 'executor_fault')
        service._worker.join(2)
        self.assertEqual(service.call('send_chat', params), ack)
        self.assertEqual(len([j for j in service.repo.list('job') if j['kind'] == 'send_chat']), 1)
        current = service.get_conversation(self.cid)['chat_draft']
        other = service.save_chat_draft(self.cid, 'New body saved after pause', current['revision'], 'save-after-fault')
        with self.assertRaises(ServiceError) as caught:
            service.call('send_chat', {'conversation_id': self.cid, 'draft_id': other['draft_id'],
                                      'expected_revision': other['revision'], 'client_request_id': 'new-request'})
        self.assertEqual(caught.exception.code, 'executor_unavailable')
        self.assertIsNone(service.repo.get('chat_request', 'new-request'))

    def test_new_defaults_and_advanced_integer_boundaries(self):
        self.assertEqual(self.service.settings['probe_seconds'], 2)
        self.assertEqual(self.service.settings['merge_seconds'], 1)
        with self.assertRaises(ServiceError):
            self.service.save_settings(image_parallelism=True)
        with self.assertRaises(ServiceError):
            self.service.save_settings(image_batch_mib=10000)
        self.assertEqual(safe_diagnostics({'queue_wait_ms': 10, 'photo_ready': 2, 'photo_bytes': -1,
                                          'prompt': 'private', 'cancel_reason': 'source_changed'}),
                         {'queue_wait_ms': 10, 'photo_ready': 2, 'cancel_reason': 'source_changed'})


if __name__ == '__main__':
    unittest.main()
