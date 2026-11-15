"""Bounded business dispatch and one cancellable AI execution slot per merchant."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import queue
import threading
import time

from .errors import ServiceError
from .repository import now_ms

AI_JOBS = frozenset({'generate_draft', 'manual_suggestion', 'check_ai', 'product_lookup', 'source_product', 'whatsapp_reply'})


class BusinessQueue(queue.Queue):
    """Keep the standard unfinished-task contract while selecting ready work."""
    def __init__(self, capacity=256):
        super().__init__(maxsize=capacity)
        self._claims = {}
        self._deferred_completions = set()

    def defer_completion(self, item):
        with self.mutex:
            self._deferred_completions.add(id(item))

    def release_completion(self, item):
        with self.mutex:
            self._deferred_completions.discard(id(item))
        return self.complete(item)

    def _claim(self, item):
        # A distinct tuple identifies this dequeue even if the same job tuple
        # was accidentally enqueued twice. Keep it alive until completion.
        claimed = tuple(value for value in item)
        self._claims[id(claimed)] = (claimed, threading.get_ident())
        return claimed

    def _get(self):
        return self._claim(super()._get())

    def complete(self, item):
        """Settle only this dequeue, including exceptional outer cleanup."""
        with self.all_tasks_done:
            if id(item) in self._deferred_completions:
                return False
            if self._claims.pop(id(item), None) is None:
                return False
            self._settle()
            return True

    def _settle(self):
        unfinished = self.unfinished_tasks - 1
        if unfinished < 0:
            raise ValueError('task_done() called too many times')
        self.unfinished_tasks = unfinished
        if unfinished == 0:
            self.all_tasks_done.notify_all()

    def task_done(self):
        # Retain Queue's public API for callers that explicitly dequeue/finish.
        with self.all_tasks_done:
            owned = next((key for key, (_, owner) in self._claims.items() if owner == threading.get_ident()), None)
            if owned is None:
                owned = next(iter(self._claims), None)
            if owned is not None:
                self._claims.pop(owned)
            self._settle()

    def put(self, item, block=False, timeout=None):
        with self.not_full:
            # Guard and insertion share the queue lock with shutdown draining.
            # A producer that passed before revocation is drained; a later one
            # is rejected instead of leaving an orphan entry after worker exit.
            guard = getattr(self, 'executor_guard', None)
            if guard:
                guard()
            if self.maxsize > 0 and self._qsize() >= self.maxsize:
                raise ServiceError('task_queue_full', '后台待执行任务较多，请稍后重试；当前输入已保留', stage='queue')
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def take_ready(self, eligible, priority, *, fair=False):
        with self.not_empty:
            candidates = [(index, item, priority(item)) for index, item in enumerate(self.queue) if eligible(item)]
            if not candidates:
                return None
            urgent = [entry for entry in candidates if entry[2] <= 1]
            if urgent:
                index, item, _ = min(urgent, key=lambda entry: (entry[2], entry[0]))
            elif fair:
                index, item, _ = candidates[0]
            else:
                index, item, _ = min(candidates, key=lambda entry: (entry[2], entry[0]))
            del self.queue[index]
            self.not_full.notify()
            return self._claim(item)


class ExecutionMixin:
    def _init_execution(self):
        self._generation_events = {}
        self._generation_active = {}
        self._generation_kinds = {}
        self._generation_read_bindings = {}
        self._execution_local = threading.local()
        self._dispatch_turn = 0
        self._close_lock = threading.Lock()

    def _finish_product_claim(self, job_id, reason):
        with self.repo.transaction():
            job = self.repo.get('job', job_id) or {}
            key = job.get('catalog_product_id')
            row = self.repo.get('product_catalog', key) if key else None
            if row and row.get('request_id') == job_id and row['lookup']['status'] == 'querying':
                row['lookup'] = {'status': 'unknown', 'reason': reason, 'queried_at': None}
                self.repo.put('product_catalog', key, row, history=True)

    def _generation_cancel_event(self, job_id):
        with self._lock:
            event = self._generation_events.setdefault(job_id, threading.Event())
            if self._stop.is_set():
                event.set()
            return event

    def _cancel_generation_tasks(self, conversation_id=None, reason='source_changed', automatic_only=False,
                                 exclude_job_id=None):
        with self._lock:
            own = getattr(self._execution_local, 'job_id', None)
            for job in self.repo.list('job'):
                if (job.get('kind') == 'whatsapp_reply' and reason != 'shutdown'
                        or job.get('kind') not in AI_JOBS or job.get('status') not in ('queued', 'running')
                        or job.get('kind') == 'source_product' and reason != 'shutdown'
                        or job['job_id'] in (own, exclude_job_id)
                        or conversation_id and job.get('conversation_id') != conversation_id
                        or automatic_only and not job.get('automatic')):
                    continue
                self._generation_cancel_event(job['job_id']).set()
                if job['status'] == 'queued':
                    if job.get('kind') == 'product_lookup':
                        row = self.repo.get('product_catalog', job.get('catalog_product_id'))
                        if row and row.get('request_id') == job['job_id'] and row['lookup']['status'] == 'querying':
                            row['lookup'] = {'status': 'unknown', 'reason': 'cancelled', 'queried_at': None}
                            self.repo.put('product_catalog', row['catalog_product_id'], row, history=True)
                    self._job_update(job['job_id'], status='cancelled', stage='cancelled', completed_at=now_ms(),
                                     message='来源或任务已更新，旧生成未执行',
                                     diagnostics={'cancel_reason': reason})

    def _work_priority(self, item):
        _, kind, params, _, automatic = item
        if kind == 'send_chat':
            return 0
        if kind in ('automatic_send', 'continuity_send', 'reconcile_operation', 'confirm_send'):
            return 1
        if kind in ('check_connection', 'check_ai', 'manual_suggestion'):
            return 2
        if kind == 'sync_messages':
            return 3 if params.get('conversation_id') else 5
        if kind == 'generate_draft':
            return 4
        return 6

    def _take_ready_work(self):
        with self._lock:
            self._dispatch_turn += 1
            return self._queue.take_ready(
                lambda item: (item[1] != 'source_product' or self._source_product_ready(item[2]))
                    and (item[1] not in AI_JOBS or not any(
                        (kind == 'source_product') == (item[1] == 'source_product')
                        for kind in self._generation_kinds.values())),
                self._work_priority, fair=self._dispatch_turn % 8 == 0)

    def _dispatch_owned(self, item):
        try:
            if self._executor_health.failed.is_set() or self._stop.is_set():
                self._cancel_queued(item)
                return
            self._dispatch_work(item)
        except BaseException:
            self._executor_health.fail('failed', 'executor_fault')
            try:
                self._preserve_dispatch_failure(item)
            except Exception as error:
                self._executor_health.log('dispatch', item[0], error)
            raise
        finally:
            # Running AI owns its dequeue until its own finalizer. For every
            # other path this is an idempotent completion of the exact lease.
            if item[0] not in self._generation_active:
                self._queue.complete(item)
                self._executor_health.finish_job(item[0])

    def _dispatch_work(self, item):
        self._executor_health.start_job(item)
        if item[1] not in AI_JOBS:
            self._run_queued(item)
            return
        job_id, kind = item[:2]
        with self._lock:
            current = self.repo.get('job', job_id, {})
            if current.get('status') != 'queued' or self._stop.is_set():
                self._run_queued(item)
                return
            facade = self.integrations
            try:
                event = self._generation_cancel_event(job_id)
                # Only actual LiveIntegration instances are cloned. Test facades and
                # Demo substitutes retain their configured behavior.
                from .integrations.live import LiveIntegration
                if type(facade) is LiveIntegration and kind != 'check_ai':
                    original = facade
                    facade = LiveIntegration(self.data_root / self.mode, copy.deepcopy(self.settings))
                    facade.configure_model_router(self._model_router)
                    facade.configure_whatsapp_image_resolver(self._wa_resolve_current_image)
                    facade._visual_workspace_identity = copy.deepcopy(original._visual_workspace_identity)
                    self._generation_read_bindings[id(facade)] = (
                        facade, original, self._monitor_generation, self.repo, job_id)
                thread = threading.Thread(target=self._executor_health.run,
                                          args=(self._execute_generation, 'generation', item, facade),
                                          name='reception-ai-' + job_id[-8:], daemon=True)
                self._generation_active[job_id] = (thread, event)
                self._generation_kinds[job_id] = kind
                self._queue.defer_completion(item)
                thread.start()
            except Exception:
                if kind == 'product_lookup':
                    self._finish_product_claim(job_id, 'task_start_failed')
                self._generation_active.pop(job_id, None)
                self._generation_kinds.pop(job_id, None)
                self._generation_events.pop(job_id, None)
                self._generation_read_bindings.pop(id(facade), None)
                try:
                    self._job_update(job_id, status='failed', stage='queue', completed_at=now_ms(),
                                     message='后台生成任务未能启动，请重试',
                                     error={'code': 'task_failed', 'message': '后台生成任务未能启动'})
                finally:
                    self._queue.release_completion(item)
                    self._executor_health.finish_job(job_id)
                    self._changed()

    def _execute_generation(self, item, facade):
        job_id = item[0]
        self._execution_local.job_id = job_id
        try:
            self._run_queued(item, facade=facade)
        except BaseException as error:
            self._executor_health.log('generation', job_id, error)
            self._executor_health.fail('failed', 'generation_cleanup_failed')
        finally:
            self._queue.release_completion(item)
            self._executor_health.finish_job(job_id)
            self._execution_local.job_id = None
            with self._lock:
                self._generation_active.pop(job_id, None)
                self._generation_kinds.pop(job_id, None)
                self._generation_events.pop(job_id, None)
                self._generation_read_bindings.pop(id(facade), None)
            # The business consumer owns scheduling and its retry policy.

    def _execution_read_current(self, token):
        binding = self._generation_read_bindings.get(id(token[0]))
        if not binding:
            return False
        facade, owner, generation, repository, job_id = binding
        active = self._generation_active.get(job_id)
        return (facade is token[0] and owner is self.integrations
                and generation == token[1] == self._monitor_generation
                and repository is token[2] is self.repo and active is not None and not active[1].is_set())

    @contextmanager
    def _job_phase(self, job_id, phase):
        started = time.monotonic()
        previous = self._executor_health.enter_phase(job_id, phase)
        try:
            yield
        finally:
            self._executor_health.leave_phase(job_id, previous)
            self._job_update(job_id, diagnostics={phase: int((time.monotonic() - started) * 1000)})

    def _join_generation_tasks(self):
        with self._lock:
            active = list(self._generation_active.values())
            for _, event in active:
                event.set()
        deadline = time.monotonic() + 10
        for thread, _ in active:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread, _ in active):
            self._executor_health.fail('stalled', 'generation_shutdown_timeout')
            raise ServiceError('executor_unavailable', '生成任务尚未退出，无法安全关闭。', stage='shutdown')
