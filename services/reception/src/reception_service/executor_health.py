"""Independent, deliberately restricted diagnostics for the business executor."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

from . import __version__
from .errors import ServiceError
from .repository import now_ms


# Native calls have their own 150s identity / 35s read / 190s AI limits.
# Health budgets include those sequential calls and a local cleanup allowance.
PHASE_SECONDS = {'identity_ms': 180, 'message_read_ms': 220, 'context_ms': 90,
                 'ai_elapsed_ms': 420, 'send_ms': 900, 'outbox_ms': 300}
SEND_JOBS = frozenset({'send_chat', 'automatic_send', 'continuity_send', 'confirm_send',
                       'preview_send', 'reconcile_operation'})


class ExecutorHealth:
    def __init__(self, service):
        self.service = service
        self.lock = threading.Lock()
        self.state = 'idle'
        self.stage = 'idle'
        self.job_id = None
        self.progress = now_ms()
        self.code = None
        self.last_log = {}
        self.log_lock = threading.Lock()
        self.failed = threading.Event()
        self.activities = {}
        self.phase_deadline = None

    def _budget(self, kind):
        if kind in SEND_JOBS:
            return 900
        if kind in ('generate_draft', 'manual_suggestion', 'organize_requirements',
                    'check_ai', 'product_lookup', 'source_product', 'whatsapp_reply'):
            return 900 + self._photo_budget()
        return 300

    def _photo_budget(self):
        seconds = getattr(self.service, 'settings', {}).get('image_prepare_seconds', 60)
        return seconds if type(seconds) is int and 1 <= seconds <= 600 else 60

    def start_job(self, item):
        job_id, kind = item[:2]
        with self.lock:
            self.activities[job_id] = {'kind': kind, 'phase': kind,
                                       'deadline': time.monotonic() + self._budget(kind)}

    def finish_job(self, job_id):
        with self.lock:
            self.activities.pop(job_id, None)

    def enter_phase(self, job_id, phase):
        seconds = self._photo_budget() + 30 if phase == 'photo_prepare_ms' else PHASE_SECONDS.get(phase, 300)
        with self.lock:
            active = self.activities.get(job_id)
            if active is None:
                return None
            previous = dict(active)
            active.update(phase=phase, deadline=time.monotonic() + seconds)
            if self.job_id == job_id:
                self.stage, self.progress = phase, now_ms()
            return previous

    def leave_phase(self, job_id, previous):
        if previous is None:
            return
        with self.lock:
            if job_id in self.activities:
                previous['deadline'] = time.monotonic() + self._budget(previous['kind'])
                self.activities[job_id] = previous
                if self.job_id == job_id:
                    self.stage, self.progress = previous['phase'], now_ms()

    @contextmanager
    def phase(self, job_id, phase):
        previous = self.enter_phase(job_id, phase)
        try:
            yield
        finally:
            self.leave_phase(job_id, previous)

    def update(self, state, stage, job_id=None, code=None):
        with self.lock:
            if self.failed.is_set() and state not in ('failed', 'stalled', 'stopped'):
                return
            self.state, self.stage, self.job_id, self.code = state, stage, job_id, code
            self.progress = now_ms()
            self.phase_deadline = time.monotonic() + 60 if state == 'running' else None

    def snapshot(self):
        with self.lock:
            state, stage, job, progress, code = self.state, self.stage, self.job_id, self.progress, self.code
            observed = now_ms()  # Timestamp belongs to this state, not later serialization.
        worker = getattr(self.service, '_worker', None)
        alive = bool(worker and worker.is_alive())
        return {'state': state, 'worker_alive': alive, 'stage': stage, 'job_id': job,
                'last_progress_at_ms': progress, 'observed_at_ms': observed,
                'queue_depth': len(self.service._queue.queue), 'error_code': code}

    def require(self):
        if self.failed.is_set() or self.state == 'recovering':
            raise ServiceError('executor_unavailable', '后台执行异常，自动发送已暂停', stage='executor')

    def fail(self, state, code):
        with self.lock:
            if self.failed.is_set():
                return
            self.failed.set()
            self.state, self.code = state, code
            self.progress = now_ms()
        self.revoke()

    def revoke(self):
        s = self.service
        # Revoke local gates and publish before calling any integration cleanup.
        # A stalled worker may hold the business lock; no new grant is restored.
        s._running = False
        s._auto_send = False
        s._frozen_scope = None
        s._wa_enabled = False
        s._epoch += 1
        for event in list(s._generation_events.copy().values()):
            event.set()
        self.publish()
        try:
            s.integrations.revoke()
        except Exception:
            pass
        s._changed()

    def run(self, target, stage, *args):
        """Per-thread final boundary; no process-wide hook or raw traceback."""
        try:
            target(*args)
        except BaseException as error:
            self.log(stage, None, error)
            self.fail('failed', 'executor_fault')

    def publish(self):
        self.service._emit('executor.health', self.snapshot())

    def watch(self):
        while not self.service._stop.wait(1):
            snap = self.snapshot()
            if not snap['worker_alive']:
                self.fail('failed', 'worker_exited')
                return
            if self.check_stalled():
                return
            self.service._emit('executor.health', snap)

    def check_stalled(self, at=None):
        with self.lock:
            now = time.monotonic() if at is None else at
            overdue = next((job for job, active in self.activities.items() if now > active['deadline']), None)
            # Queue/scheduler locks are bounded separately from real external work.
            own_deadline = self.phase_deadline if self.job_id not in self.activities else None
            stalled = overdue is not None or own_deadline is not None and now > own_deadline
            if overdue is not None:
                self.job_id = overdue
                self.stage = self.activities[overdue]['phase']
        if stalled:
            self.fail('stalled', 'worker_stalled')
        return stalled

    def log(self, stage, job_id, error):
        try:
            name = type(error).__name__
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}', name):
                name = 'Exception'
            stage = stage if re.fullmatch(r'[a-z_]{1,40}', stage or '') else 'executor'
            job_id = job_id if isinstance(job_id, str) and re.fullmatch(r'(?:job|wa_job)_[A-Za-z0-9_-]{1,100}', job_id) else None
            key = (stage, name)
            with self.log_lock:
                stamp = time.monotonic()
                if stamp - self.last_log.get(key, -1e9) < 10:
                    return
                self.last_log[key] = stamp
                root = Path(__file__).resolve().parent
                frames = []
                tb = error.__traceback__
                while tb:
                    path = Path(tb.tb_frame.f_code.co_filename).resolve()
                    try:
                        path = path.relative_to(root).as_posix()
                    except ValueError:
                        path = '<external>'
                    function = tb.tb_frame.f_code.co_name
                    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,100}', function):
                        function = '<unknown>'
                    frames.append({'file': path, 'function': function, 'line': tb.tb_lineno})
                    tb = tb.tb_next
                seller = hashlib.sha256(self.service.seller_id.encode('utf-8')).hexdigest()
                directory = self.service.data_root / self.service.mode / 'diagnostics' / seller
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / 'executor.jsonl'
                row = json.dumps({'time_ms': now_ms(), 'version': __version__, 'stage': stage,
                                  'job_id': job_id, 'error_code': 'executor_fault',
                                  'exception_type': name, 'frames': frames[-30:]}, ensure_ascii=True) + '\n'
                payload = row.encode('utf-8')
                if path.exists() and path.stat().st_size + len(payload) > 2 * 1024 * 1024:
                    older = path.with_name(path.name + '.2')
                    older.unlink(missing_ok=True)
                    previous = path.with_name(path.name + '.1')
                    if previous.exists():
                        os.replace(previous, older)
                    os.replace(path, previous)
                with path.open('ab') as output:
                    output.write(payload)
        except Exception:
            pass
