"""Read-only live activity monitor; message bodies stay on the business worker."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time

from .errors import ServiceError
from .repository import encoded, now_ms

AUDIT_SECONDS = 300
MAX_RETRY_SECONDS = 300
MAX_QUEUED_BODY_SYNCS = 4
BACKGROUND_READ_SECONDS = 60
RECENT_READ_ATTEMPTS = 4
RECENT_ACTIVITY_MS = 5 * 60 * 1000
OLD_ACTIVITY_MS = 24 * 60 * 60 * 1000


class MessageMonitorMixin:
    def _init_message_monitor(self):
        self._monitor_generation = 0
        self._monitor_wake = threading.Event()
        self._next_probe = 0.0
        self._probe_failures = 0
        self._message_sync = {
            'status': 'waiting_connection' if self.mode == 'live' else 'demo',
            'checked_at': None, 'poll_seconds': self.settings['probe_seconds'],
            'error_code': None, 'pending_count': 0, 'summary_requests': 0,
            'body_syncs': 0, 'failed_checks': 0, 'last_body_sync_at': None,
        }
        self._monitor_thread = None

    def _start_message_monitor(self):
        if self.mode == 'live':
            self._monitor_thread = threading.Thread(target=self._monitor_loop,
                                                    name='reception-message-monitor', daemon=True)
            self._monitor_thread.start()

    def _read_token(self, facade):
        # Reception pause/takeover does not revoke read-only monitoring. Settings,
        # facade replacement and shutdown do. The repository is seller-scoped.
        return facade, self._monitor_generation, self.repo

    def _read_current(self, token):
        return (not self._closing and not self._stop.is_set()
                and (token[0] is self.integrations or self._execution_read_current(token))
                and token[1] == self._monitor_generation and token[2] is self.repo)

    def _require_current_read(self, token):
        if not self._read_current(token):
            raise ServiceError('cancelled', '消息读取期间设置或连接已变化', stage='messages')

    def _monitor_ready(self):
        return (self.mode == 'live' and not self._closing and self.account_verified
                and self.checks['messages']['status'] == 'passed')

    def _message_sync_snapshot(self):
        result = copy.deepcopy(self._message_sync)
        result.update(poll_seconds=self.settings['probe_seconds'], pending_count=0,
                      waiting_activity_count=0, background_pending_count=0, active_read_count=0)
        if self.mode == 'live':
            stamp = now_ms()
            for conversation in self.repo.list('conversation'):
                cid = conversation['conversation_id']
                task = self.repo.get('message_sync_task', cid, {})
                activity = self.repo.get('message_activity', cid, {})
                if self._waiting_for_activity(cid, task, activity):
                    result['waiting_activity_count'] += 1
                    continue
                pending = bool(task.get('pending')) or self._message_sync_pending(cid)
                result['pending_count'] += int(pending)
                if pending and not self._urgent_message_activity(task, activity, stamp):
                    result['background_pending_count'] += 1
            result['active_read_count'] = len(self._active_message_reads())
            if not self._monitor_ready() and not self._closing:
                result['status'] = 'waiting_connection'
        return result

    def _waiting_for_activity(self, cid, task=None, activity=None):
        task = self.repo.get('message_sync_task', cid, {}) if task is None else task
        activity = self.repo.get('message_activity', cid, {}) if activity is None else activity
        return (task.get('wait_reason') == 'waiting_activity' and task.get('no_body_confirmed') is True
                and task.get('empty_version') == activity.get('version')
                and task.get('empty_target') == activity.get('latest_timestamp')
                and not self.repo.get('sync_gap', cid, {}))

    @staticmethod
    def _urgent_message_activity(task, activity, stamp):
        priority = task.get('read_priority')
        return priority in ('activity', 'selection') or (priority is None
                and activity.get('latest_timestamp', -1) >= stamp - RECENT_ACTIVITY_MS)

    def _message_sync_pending(self, cid):
        if self.mode != 'live':
            return False
        activity = self.repo.get('message_activity', cid, {})
        watermark = self.repo.get('message_watermark', cid, {})
        task = self.repo.get('message_sync_task', cid, {})
        if self._waiting_for_activity(cid, task, activity):
            return False
        return bool(task.get('pending') and task.get('activity_required', True) or
                    activity.get('version', 0) > watermark.get('activity_version', 0) or
                    activity.get('latest_timestamp', -1) > watermark.get('latest_timestamp', -1))

    def _observe_message_activity(self, info):
        cid, buyer, stamp = info.get('conversation_id'), info.get('buyer_id'), info.get('latest_timestamp')
        if (not isinstance(cid, str) or not cid or not isinstance(buyer, str) or not buyer
                or type(stamp) is not int or stamp < 0):
            raise ServiceError('invalid_page', '会话摘要格式无效', stage='messages')
        conversation = self.repo.get('conversation', cid)
        if not conversation:
            # No synthetic incoming message and no authorisation change.
            self._ingest(info, [])
        elif conversation['buyer_id'] != buyer:
            raise ServiceError('identity_mismatch', '会话摘要买家不匹配', stage='messages')
        old = self.repo.get('message_activity', cid, {})
        signature = info.get('activity_signature')
        clock = info.get('activity_clock', 'message')
        if clock not in ('message', 'conversation'):
            raise ServiceError('invalid_page', '会话活动时间口径无效', stage='messages')
        if signature is not None and (not isinstance(signature, str) or len(signature) > 128):
            raise ServiceError('invalid_page', '会话活动签名格式无效', stage='messages')
        if (stamp > old.get('latest_timestamp', -1) or
                stamp == old.get('latest_timestamp') and
                (signature and signature != old.get('signature') or clock != old.get('activity_clock', 'message'))):
            self.repo.put('message_activity', cid, {'conversation_id': cid, 'latest_timestamp': stamp,
                          'signature': signature, 'activity_clock': clock, 'version': old.get('version', 0) + 1,
                          'observed_at': now_ms(),
                          'latest_message_id': info.get('latest_message_id'),
                          'latest_text_sha256': info.get('latest_text_sha256')})
            task = self.repo.get('message_sync_task', cid, {})
            if task or old:
                # A changed existing activity is urgent even if its platform
                # clock is old (for example, a same-timestamp correction).
                task.update(conversation_id=cid, retry_at=0, failures=0, defer_count=0,
                            wait_reason=None, no_body_confirmed=False, empty_reads=0,
                            read_priority='activity' if old else None,
                            last_activity_observed_at=now_ms() if old else 0)
                task.pop('read_confirmation', None)
                self.repo.put('message_sync_task', cid, task)

    def _monitor_loop(self):
        while not self._stop.is_set():
            self._monitor_once()
            with self._lock:
                delay = max(0.05, self._next_probe - time.monotonic()) if self._monitor_ready() else 1.0
            self._monitor_wake.wait(min(delay, 60))
            self._monitor_wake.clear()

    def _monitor_once(self):
        with self._lock:
            if not self._monitor_ready() or time.monotonic() < self._next_probe:
                return
            facade = self.integrations
            token = self._read_token(facade)
            self._next_probe = time.monotonic() + self.settings['probe_seconds']
            self._message_sync['status'] = 'checking'
        try:
            # Always refresh the first screen. One persisted continuation page at
            # most follows it, at the slower configured sweep interval.
            self._read_summary_page(facade, token, None)
            with self._lock:
                self._require_current_read(token)
                # Dispatch the fresh head immediately; optional older pages
                # must not delay an already discovered customer message.
                self._dispatch_message_syncs(audit=False)
                sweep = self.repo.get('message_monitor', 'summary', {})
                cursor = sweep.get('next_cursor')
                scan_due = now_ms() >= sweep.get('next_scan_at', 0)
            if cursor is not None and scan_due:
                self._read_summary_page(facade, token, cursor)
            with self._lock:
                self._require_current_read(token)
                self._probe_failures = 0
                self._message_sync.update(status='watching', checked_at=now_ms(), error_code=None)
                self._dispatch_message_syncs()
        except Exception as error:
            with self._lock:
                if not self._read_current(token):
                    return
                self._probe_failures += 1
                self._next_probe = time.monotonic() + min(
                    MAX_RETRY_SECONDS, self.settings['probe_seconds'] * 2 ** min(self._probe_failures - 1, 6))
                # Keep a bounded, content-free status; exception messages may
                # contain customer data and never belong in this DTO.
                code = error.code if isinstance(error, ServiceError) else 'summary_failed'
                allowed = {'invalid_page', 'summary_cursor_stalled', 'identity_mismatch', 'seller_mismatch',
                           'identity_blocked', 'runtime_blocked', 'native_failed', 'native_timeout', 'cli_timeout'}
                self._message_sync.update(status='retrying', error_code=code if code in allowed else 'summary_failed')
                self._message_sync['failed_checks'] += 1
                # Good first-page results can still proceed if a continuation
                # fails. A failed customer cannot hold up other pending reads.
                self._dispatch_message_syncs(audit=False)
        self._changed()

    def _read_summary_page(self, facade, token, cursor):
        with self._lock:
            self._require_current_read(token)
            self._message_sync['summary_requests'] += 1
        page = facade.read_recent_conversations(self.seller_id, before_ms=cursor, count=20)
        with self._lock, self.repo.transaction():
            self._require_current_read(token)
            try:
                more, next_cursor = self._history_cursor(page, cursor)
            except ServiceError as error:
                raise ServiceError('summary_cursor_stalled', '会话摘要游标未向过去推进', stage='messages') from error
            items = page.get('items')
            if not isinstance(items, list):
                raise ServiceError('invalid_page', '会话摘要列表无效', stage='messages')
            for info in items:
                if not isinstance(info, dict):
                    raise ServiceError('invalid_page', '会话摘要格式无效', stage='messages')
                self._observe_message_activity(info)
            sweep = self.repo.get('message_monitor', 'summary', {})
            if cursor is not None:
                sweep = {'next_cursor': next_cursor if more else None,
                         'next_scan_at': now_ms() + int(self.settings['poll_seconds'] * 1000)}
                self.repo.put('message_monitor', 'summary', sweep)
            elif more and sweep.get('next_cursor') is None:
                # Never restart an in-progress sweep just because the head moves.
                self.repo.put('message_monitor', 'summary', {
                    'next_cursor': next_cursor, 'next_scan_at': sweep.get('next_scan_at', 0)})

    def _active_message_reads(self):
        with self.repo.lock:
            return [json.loads(row[0]) for row in self.repo.db.execute(
                "SELECT data FROM entities WHERE kind='job' AND json_extract(data,'$.kind')='sync_messages' "
                "AND json_extract(data,'$.status') IN ('queued','running')")]

    def _dispatch_message_syncs(self, *, audit=True):
        """New activity is urgent; account-wide historical reads share one budget."""
        jobs = self._active_message_reads()
        active = {job['conversation_id'] for job in jobs}
        if None in active:
            return
        stamp = now_ms()
        urgent, historical = [], []
        activities = {row['conversation_id']: row for row in self.repo.list('message_activity')}
        for conversation in self.repo.list('conversation'):
            cid = conversation['conversation_id']
            if cid in active:
                continue
            activity = activities.get(cid, {})
            task = self._defer_legacy_empty_history(cid, self.repo.get('message_sync_task', cid, {}), activity, stamp)
            if self._waiting_for_activity(cid, task, activity):
                continue
            if self._message_sync_pending(cid):
                if stamp < task.get('retry_at', 0):
                    continue
                if self._urgent_message_activity(task, activity, stamp):
                    urgent.append((activity.get('latest_timestamp', -1), cid))
                else:
                    historical.append((task.get('last_read_at', 0), -activity.get('latest_timestamp', -1), cid, 'history'))
                continue
            if audit:
                watermark = self.repo.get('message_watermark', cid, {})
                case = self.repo.get('case', conversation['case_id'], {})
                checked = watermark.get('checked_at', case.get('created_at', stamp))
                recheck = watermark.get('recheck_at')
                if stamp - checked >= AUDIT_SECONDS * 1000 or recheck is not None and stamp >= recheck:
                    historical.append((recheck if recheck is not None else checked, 0, cid, 'audit'))
        for _, cid in sorted(urgent, key=lambda item: item[0], reverse=True):
            if len(active) >= MAX_QUEUED_BODY_SYNCS:
                break
            self._enqueue('sync_messages', {'conversation_id': cid}, read_reason='activity')
            active.add(cid)
        budget = self.repo.get('message_monitor', 'body_budget', {})
        if (not historical or len(active) >= MAX_QUEUED_BODY_SYNCS
                or stamp < budget.get('next_history_at', 0)
                or any(job.get('sync_reason') in ('history', 'audit') for job in jobs)):
            return
        _, _, cid, reason = min(historical)
        self._enqueue('sync_messages', {'conversation_id': cid}, read_reason=reason)
        # Initial archive migration can take seconds. Start the budget after
        # enqueueing so a slow scan cannot shorten the actual read interval.
        self.repo.put('message_monitor', 'body_budget', {'next_history_at': now_ms() + BACKGROUND_READ_SECONDS * 1000})

    def _defer_legacy_empty_history(self, cid, task, activity, stamp):
        """Reuse two completed empty reads from 0.2.7, without rereading 589 archives."""
        if (task.get('wait_reason') != 'empty_history' or task.get('defer_count', 0) < 2
                or task.get('target_version') != activity.get('version')
                or task.get('target_timestamp') != activity.get('latest_timestamp')
                or activity.get('latest_timestamp', stamp) >= stamp - OLD_ACTIVITY_MS
                or task.get('last_activity_observed_at', 0) >= stamp - RECENT_ACTIVITY_MS):
            return task
        history = self.repo.get('coverage', cid, {})
        if (history.get('updated_at') is None or history.get('latest_ms') is not None
                or history.get('next_cursor') is not None or self.repo.get('sync_gap', cid, {})
                or self._messages(cid)):
            return task
        task.update(empty_reads=max(2, task.get('empty_reads', 0)),
                    empty_version=activity['version'], empty_target=activity['latest_timestamp'])
        self._park_empty_history(cid, task)
        return task

    def _park_empty_history(self, cid, task):
        from .unread import establish_empty_baseline
        establish_empty_baseline(self.repo, cid, task.get('empty_target'))
        task.update(pending=False, activity_required=False, wait_reason='waiting_activity',
                    no_body_confirmed=True, retry_at=0, failures=0, job_id=None)
        self.repo.put('message_sync_task', cid, task)
        history = self.repo.get('coverage', cid, {})
        if history:
            history.update(status='partial', message='暂无可读正文，有新活动或打开客户时再次读取')
            self.repo.put('coverage', cid, history)

    def _defer_message_sync(self, cid, target, version, reason):
        """Bound recent index retries, then stop polling confirmed empty archives."""
        task = self.repo.get('message_sync_task', cid, {})
        count = task.get('defer_count', 0) + 1
        empty = reason == 'empty_history' and not self._messages(cid) and not self.repo.get('sync_gap', cid, {})
        same = task.get('empty_version') == version and task.get('empty_target') == target
        empty_reads = task.get('empty_reads', 0) + 1 if empty and same else int(empty)
        task.update(conversation_id=cid, pending=True, target_timestamp=target, target_version=version,
                    activity_required=task.get('activity_required', True), wait_reason=reason,
                    defer_count=count, failures=0, empty_reads=empty_reads, no_body_confirmed=False,
                    empty_version=version, empty_target=target, last_read_at=now_ms())
        self._message_sync['last_body_sync_at'] = now_ms()
        if (empty and empty_reads >= 2 and target is not None and target < now_ms() - OLD_ACTIVITY_MS
                and task.get('last_activity_observed_at', 0) < now_ms() - RECENT_ACTIVITY_MS):
            self._park_empty_history(cid, task)
            return
        if count >= RECENT_READ_ATTEMPTS:
            task['read_priority'] = 'history'
        delay = min(MAX_RETRY_SECONDS, self.settings['probe_seconds'] * 2 ** min(count - 1, 6))
        task['retry_at'] = now_ms() + int(delay * 1000)
        self.repo.put('message_sync_task', cid, task)
        if reason == 'empty_history':
            history = self.repo.get('coverage', cid, {})
            if history:
                history.update(status='partial', message='暂未取得消息正文，已安排稍后补读')
                self.repo.put('coverage', cid, history)

    def _request_empty_history(self, cid):
        """Only an explicit customer-open event may bypass the historical budget."""
        if not self._monitor_ready() or self._messages(cid):
            return
        task = self.repo.get('message_sync_task', cid, {})
        stamp = now_ms()
        if stamp - task.get('last_selected_at', 0) < BACKGROUND_READ_SECONDS * 1000:
            return
        self._enqueue('sync_messages', {'conversation_id': cid}, read_reason='selection')
        task = self.repo.get('message_sync_task', cid, {})
        task['last_selected_at'] = stamp
        self.repo.put('message_sync_task', cid, task)

    def _bind_message_sync(self, job):
        cid = job['conversation_id']
        task = self.repo.get('message_sync_task', cid, {})
        activity = self.repo.get('message_activity', cid, {})
        watermark = self.repo.get('message_watermark', cid, {})
        # Freeze each attempt. A retry may adopt newer durable activity, so an
        # intervening correction cannot strand it on an obsolete text hash.
        target = activity.get('latest_timestamp', task.get('target_timestamp', watermark.get('latest_timestamp')))
        version = activity.get('version', task.get('target_version', 0))
        pending = (activity.get('version', 0) > watermark.get('activity_version', 0) or
                   activity.get('latest_timestamp', -1) > watermark.get('latest_timestamp', -1))
        job['sync_message_id'] = activity.get('latest_message_id', task.get('target_message_id')) if pending else None
        job['sync_text_sha256'] = activity.get('latest_text_sha256', task.get('target_text_sha256')) if pending else None
        job['sync_target'] = target
        job['sync_clock'] = activity.get('activity_clock', 'message')
        job['sync_version'] = version
        job['sync_generation'] = self._monitor_generation
        task.update(conversation_id=cid, pending=True, wait_reason=None, no_body_confirmed=False,
                    read_priority=job.get('sync_reason') or task.get('read_priority'),
                    activity_required=pending or bool(self.repo.get('sync_gap', cid, {})),
                    target_timestamp=target, target_version=version,
                    target_message_id=job['sync_message_id'], target_text_sha256=job['sync_text_sha256'],
                    job_id=job['job_id'], failures=task.get('failures', 0), retry_at=0)
        self.repo.put('message_sync_task', cid, task)

    def _complete_message_sync(self, cid, target, version, latest, job_id, token, message_id=None, text_hash=None,
                               *, activity_clock='message', changed=False, read_messages=None):
        """Acknowledge activity separately from the body's platform timestamp."""
        self._require_current_read(token)
        if target is not None and latest is None:
            return False
        exact_evidence = message_id is not None or text_hash is not None
        observed = self._messages(cid) if read_messages is None else read_messages
        if exact_evidence:
            candidates = [m for m in observed if
                          (m['message_id'] == message_id if message_id is not None else
                           m['timestamp_ms'] == (latest if activity_clock == 'conversation' else target))]
            if text_hash is not None:
                candidates = [m for m in candidates if hashlib.sha256(m['text'].encode('utf-8')).hexdigest() == text_hash]
            if not candidates:
                return False
        elif activity_clock == 'message' and target is not None and latest < target:
            return False  # A true message-clock target still requires its body frontier.
        watermark = self.repo.get('message_watermark', cid, {})
        task = self.repo.get('message_sync_task', cid, {})
        weak_summary = activity_clock == 'conversation' and message_id is None
        fresh_activity = version > watermark.get('activity_version', 0)
        if weak_summary and fresh_activity and not any(m['timestamp_ms'] == latest for m in observed):
            return False
        if weak_summary and fresh_activity and not changed:
            # The native summary exposes an activity time, not a message ID. If
            # no body changed, confirm a second completed read after retry backoff.
            # Never ignore a sub-second activity change or forge message times.
            fields = ('message_id', 'seller_id', 'buyer_id', 'conversation_id', 'role', 'text', 'timestamp_ms', 'attachments')
            newest = sorted(({key: m.get(key) for key in fields} for m in observed if m['timestamp_ms'] == latest),
                            key=lambda m: m['message_id'])
            confirmation = {'target': target, 'version': version,
                            'fingerprint': hashlib.sha256(encoded(newest).encode('utf-8')).hexdigest()}
            if task.get('read_confirmation') != confirmation:
                task['read_confirmation'] = confirmation
                self.repo.put('message_sync_task', cid, task)
                return False
        processed = target if target is not None else latest
        if processed is not None:
            watermark.update(conversation_id=cid,
                             latest_timestamp=max(watermark.get('latest_timestamp', -1), processed),
                             activity_version=max(watermark.get('activity_version', 0), version),
                             body_latest_timestamp=latest, checked_at=now_ms(),
                             # One near-term read catches delayed native indexing;
                             # it clears after this activity has been acknowledged.
                             recheck_at=now_ms() + 30_000 if weak_summary and fresh_activity else None)
            self.repo.put('message_watermark', cid, watermark)
        if task and task.get('job_id') in (None, job_id):
            task.update(pending=False, job_id=None, failures=0, retry_at=0, wait_reason=None, defer_count=0,
                        no_body_confirmed=False, empty_reads=0, last_read_at=now_ms())
            self.repo.put('message_sync_task', cid, task)
        self._message_sync['last_body_sync_at'] = now_ms()
        self._message_sync['body_syncs'] += 1
        return True

    def _message_sync_job_finished(self, job_id, *, status=None):
        with self._lock:
            job = self.repo.get('job', job_id, {})
            if 'sync_target' not in job:
                return
            cid = job['conversation_id']
            task = self.repo.get('message_sync_task', cid, {})
            if task.get('job_id') != job_id:
                return
            if task.get('wait_reason') == 'waiting_activity':
                task['job_id'] = None
                self.repo.put('message_sync_task', cid, task)
                return
            if not task.get('pending'):
                return
            if status == 'deferred':
                task['job_id'] = None
                self.repo.put('message_sync_task', cid, task)
                return
            failures = task.get('failures', 0) + 1
            task.update(job_id=None, wait_reason=None, failures=failures, retry_at=now_ms() + int(1000 * min(
                MAX_RETRY_SECONDS, self.settings['probe_seconds'] * 2 ** min(failures - 1, 6))))
            self.repo.put('message_sync_task', cid, task)
