"""Account-owned WhatsApp model generation without a merchant service."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import threading
import time
import uuid

from .errors import ServiceError
from .integrations.reception_prompts import WHATSAPP, IMAGE_NATIVE, PROMPT_VERSION
from .model_inputs import resolve_whatsapp_images
from .model_payload import model_json
from .reply_messages import normalize_reply_output


class AccountGenerationMixin:
    def _account_prompt_current(self, version):
        return version == PROMPT_VERSION

    def _account_source_fingerprint(self, account, chat, segment):
        rows = [row for row in self.repo.list_owned('message', account, chat, segment) if not row.get('from_me')]
        rows.sort(key=lambda row: (row.get('timestamp_ms', 0), row.get('id', '')))
        supplement = self.repo.get('wa_supplement', self._account_key(account, chat, segment), {'body': '', 'revision': 0})
        return self._account_key(rows, supplement)

    def _init_account_generation(self):
        self._generation_cancel = threading.Event()
        self._generation_active = None
        self._generation_requests = {}
        self._generation_closed = False
        self._generation_timer = None
        self._generation_run_id = uuid.uuid4().hex
        self._retire_obsolete_account_batches()

    def _retire_obsolete_account_batches(self):
        # AccountRepository holds the workspace's exclusive process lock. A
        # persisted False is the exact pre-submit boundary: the host only gets
        # a send body after _wa_prepare_send persists submission_started=True.
        with self.repo.transaction():
            operations_by_batch, operations_by_source = {}, {}
            for op in self.repo.list('operation'):
                batch_id = op.get('batch_id')
                if isinstance(batch_id, str) and batch_id:
                    operations_by_batch.setdefault(batch_id, []).append(op)
                source = self._account_key(op.get('account_id'), op.get('chat_id'), op.get('pending_message_id'))
                operations_by_source.setdefault(source, []).append(op)
            batches = self.repo.list('reply_batch')
            batches_by_source = {}
            for batch in batches:
                source = self._account_key(batch.get('account_id'), batch.get('chat_id'), batch.get('pending_message_id'))
                batches_by_source.setdefault(source, []).append(batch)

            def wholly_unsent(batch):
                parts = batch.get('parts')
                return (batch.get('status') in ('prepared', 'stopped') and batch.get('next_index') == 0
                        and isinstance(parts, list) and bool(parts)
                        and all(isinstance(part, dict) and part.get('status') in ('planned', 'prepared', 'cancelled')
                                and part.get('message_id') is None and part.get('ack') is None
                                and part.get('submission_started', False) is False for part in parts))

            for batch in batches:
                batch_id = batch.get('batch_id')
                if (not isinstance(batch_id, str) or not batch_id or batch.get('status') == 'accepted'
                        or self._account_prompt_current(batch.get('prompt_version'))):
                    continue
                fields = ('account_id', 'connection_epoch', 'chat_id', 'merchant_id', 'pending_message_id',
                          'segment_id', 'binding_revision', 'source_fingerprint')
                if any(batch.get(key) is None for key in fields) or not batch.get('source_fingerprint'):
                    continue
                batch_ops = operations_by_batch.get(batch_id, [])
                parts = batch.get('parts')
                if not batch_ops or not isinstance(parts, list) or not parts or any(not isinstance(part, dict) for part in parts):
                    continue
                changed = False
                for op in batch_ops:
                    index = op.get('part_index')
                    if (op.get('status') != 'prepared' or op.get('submission_started') is not False
                            or op.get('message_id') is not None or op.get('ack') is not None or op.get('legacy_receipt')
                            or any(op.get(key) != batch.get(key) for key in fields)
                            or type(index) is not int or not 0 <= index < len(parts)
                            or parts[index].get('operation_id') != op.get('operation_id')
                            or parts[index].get('status') != 'prepared' or parts[index].get('message_id') is not None
                            or parts[index].get('ack') is not None or parts[index].get('submission_started', False) is not False
                            or parts[index].get('body') != op.get('body')):
                        continue
                    op.update(status='cancelled', cancellation_reason='prompt_revision_changed')
                    parts[index]['status'] = 'cancelled'
                    self.repo.put('operation', op['operation_id'], op)
                    changed = True
                if changed:
                    batch.update(status='stopped', updated_at=time.time_ns() // 1_000_000)
                    self.repo.put('reply_batch', batch['batch_id'], batch)
                if batch.get('status') != 'stopped':
                    continue
                # A new prompt may regenerate only a wholly unsent source. A
                # partially accepted batch or any unresolved submission keeps
                # its original attempt and receipts; counts are never reset.
                source_key = self._account_key(batch['account_id'], batch['chat_id'], batch['pending_message_id'])
                related = operations_by_source.get(source_key, [])
                if (not related or any(op.get('status') not in ('prepared', 'cancelled')
                        or op.get('submission_started') is not False or op.get('message_id') is not None
                        or op.get('ack') is not None or op.get('legacy_receipt') for op in related)
                        or not all(wholly_unsent(other) for other in batches_by_source[source_key])):
                    continue
                attempt = self.repo.get('generation_attempt', source_key, {})
                if (attempt.get('status') == 'completed' and type(attempt.get('count')) is int
                        and 0 < attempt['count'] < 3 and attempt.get('owner_run_id')
                        and all(op.get('owner_run_id') == attempt['owner_run_id'] for op in batch_ops)
                        and attempt.get('source_fingerprint') == batch['source_fingerprint']):
                    attempt.update(status='failed', runtime_settled=True)
                    self.repo.put('generation_attempt', source_key, attempt)

    def _arm_account_schedule(self, delay=1):
        if self._generation_closed or self._generation_timer:
            return
        def wake():
            with self.repo.lock:
                self._generation_timer = None
                if not self._generation_closed:
                    self._schedule_account_replies()
        self._generation_timer = threading.Timer(max(0.02, delay), wake)
        self._generation_timer.daemon = True
        self._generation_timer.start()

    def _cancel_account_generation(self, chat_id=None):
        if self._generation_active and (chat_id is None or self._generation_active['chat_id'] == chat_id):
            self._generation_active['cancel'].set()
        for request_id, (_, job) in list(self._generation_requests.items()):
            if chat_id is None or job['chat_id'] == chat_id:
                job['cancel'].set()
                del self._generation_requests[request_id]
        for operation in self.repo.list('operation'):
            if chat_id is None or operation.get('chat_id') == chat_id:
                self._cancel_account_prepared(operation)
        for batch in self.repo.list('reply_batch'):
            if batch.get('status') not in ('accepted', 'stopped') and (chat_id is None or batch['chat_id'] == chat_id):
                batch['status'] = 'stopped'
                self.repo.put('reply_batch', batch['batch_id'], batch)
        if self._generation_closed and self._generation_timer:
            self._generation_timer.cancel()
            self._generation_timer = None

    def _generation_current(self, job):
        if (not self._account_prompt_current(job.get('prompt_version'))
                or self._generation_closed or job['cancel'].is_set() or not self.enabled
                or not self.connected or (self.account, self.epoch) != (job['account_id'], job['connection_epoch'])
                or job['merchant_id'] not in self.allowed):
            return False
        binding = self.repo.get('binding', self._account_key(job['account_id'], job['chat_id']))
        row = self.repo.get('conversation', self._account_key(job['account_id'], job['chat_id']))
        return bool(binding and row and not row.get('human_owned') and not row.get('reception_paused') and row.get('pending')
                    and row.get('pending_message_id') == job['pending_message_id']
                    and binding.get('merchant_id') == job['merchant_id']
                    and binding.get('revision') == job['binding_revision']
                    and binding.get('segment_id') == job['segment_id']
                    and binding.get('requested_binding') is None
                    and self._account_source_fingerprint(job['account_id'], job['chat_id'], job['segment_id']) == job['source_fingerprint'])

    def _schedule_account_replies(self):
        if not self.enabled or self._generation_closed:
            return
        now = time.time_ns() // 1_000_000
        for request_id, (_, job) in list(self._generation_requests.items()):
            if job['context_deadline_ms'] <= now or not self._generation_current(job):
                del self._generation_requests[request_id]
                self._account_generation_error(job, 'merchant_unavailable')
        if self._generation_active:
            return
        if self._generation_requests:
            self._arm_account_schedule()
            return
        for row in sorted(self.repo.list('conversation'), key=lambda row: row.get('last_attempt_at_ms', 0)):
            if row.get('account_id') != self.account or not row.get('pending') or row.get('human_owned') or row.get('reception_paused'):
                continue
            chat = row['chat_id']
            binding = self.repo.get('binding', self._account_key(self.account, chat))
            if not binding or binding.get('merchant_id') not in self.allowed or binding.get('requested_binding') is not None:
                continue
            if any(op.get('account_id') == self.account and op.get('chat_id') == chat and op.get('status') in ('prepared', 'sending', 'unknown') for op in self.repo.list('operation')):
                continue
            source_key = self._account_key(self.account, chat, row.get('pending_message_id'))
            attempts = self.repo.get('generation_attempt', source_key, {})
            if (attempts.get('status') == 'unknown' and attempts.get('runtime_settled') is True
                    and attempts.get('source_fingerprint') != self._account_source_fingerprint(self.account, chat, binding['segment_id'])):
                attempts['status'] = 'failed'
                self.repo.put('generation_attempt', source_key, attempts)
            if attempts.get('count', 0) >= 3 or attempts.get('status') in ('in_flight', 'completed', 'unknown'):
                continue
            if max(row.get('retry_at_ms', 0), row.get('pending_ready_at_ms', 0)) > now:
                self._arm_account_schedule()
                continue
            request = {'request_id': uuid.uuid4().hex, 'account_id': self.account, 'chat_id': chat,
                       'merchant_id': binding['merchant_id'], 'conversation_id': binding.get('conversation_id'),
                       'binding_revision': binding['revision']}
            job = dict(request, connection_epoch=self.epoch, segment_id=binding['segment_id'],
                       pending_message_id=row['pending_message_id'], source_key=source_key,
                       cancel=threading.Event(), context_deadline_ms=now + 10000,
                       prompt_version=PROMPT_VERSION,
                       source_fingerprint=self._account_source_fingerprint(self.account, chat, binding['segment_id']))
            row['last_attempt_at_ms'] = now
            self.repo.put('conversation', self._account_key(self.account, chat), row)
            self._generation_requests[request['request_id']] = (request, job)
            self.emit({'event': 'whatsapp.context.requested', 'data': request.copy()})
            self._arm_account_schedule()
            break

    def _account_generation_error(self, job, code):
        key = self._account_key(job['account_id'], job['chat_id'])
        row = self.repo.get('conversation', key)
        if row and row.get('pending_message_id') == job['pending_message_id']:
            row.update(error_code=code, retry_at_ms=time.time_ns() // 1_000_000 + 5000)
            self.repo.put('conversation', key, row)

    def _generation_context_result(self, request, context, error):
        pair = self._generation_requests.pop(request.get('request_id'), None)
        if not pair or pair[0] != request:
            raise ServiceError('source_changed', '商家上下文请求已变化', stage='whatsapp_account')
        job = pair[1]
        if error is not None or context is None:
            self._account_generation_error(job, 'merchant_unavailable')
            self._schedule_account_replies()
            self._changed()
            return {'accepted': False}
        self._validate_account_context(request, context)
        if self._generation_active or not self._generation_current(job):
            raise ServiceError('source_changed', '商家绑定已变化', stage='whatsapp_account')
        settings = context['context']
        count = settings.get('context_message_count', 50)
        images_enabled = settings.get('reception_images_enabled', True)
        if type(count) is not int or not 1 <= count <= 200 or type(images_enabled) is not bool:
            raise ServiceError('invalid_context', '商家上下文设置无效', stage='whatsapp_account')
        refs, statuses = self._account_generation_images(job, count) if images_enabled else ([], [])
        if any(item['media']['status'] != 'ready' for item in statuses):
            self._account_generation_error(job, 'media_unavailable')
            self._schedule_account_replies()
            self._changed()
            return {'accepted': False}
        job.update(refs=refs, statuses=statuses, message_count=count)
        job['merchant_context'] = deepcopy(context)
        self._generation_active = job
        thread = threading.Thread(target=self._run_account_generation, args=(job,), daemon=True)
        job['thread'] = thread
        thread.start()
        return {'accepted': True}

    def _account_generation_images(self, job, count):
        rows = self.repo.list_owned('message', job['account_id'], job['chat_id'], job['segment_id'])
        rows.sort(key=lambda row: (row['timestamp_ms'], row['id']))
        refs, statuses = [], []
        for row in rows[-count:]:
            if row.get('has_image') is not True or row.get('from_me') is not False:
                continue
            version = self._account_key(job['account_id'], job['connection_epoch'], job['chat_id'], row['id'], str(row['timestamp_ms']))
            media_id = self._account_key(job['account_id'], job['connection_epoch'], job['chat_id'], row['id'], version)
            asset = self.repo.db.execute('SELECT status,sha256 FROM account_media WHERE id=? AND segment_id=?', (media_id, job['segment_id'])).fetchone()
            source_id = 'wa_' + self._account_key(job['account_id'], row['id'])[:20]
            media = {'status': asset[0] if asset else 'pending', 'source_version': version}
            if asset and asset[0] == 'ready':
                media.update(media_id=media_id, sha256=asset[1])
                refs.append({'source_id': source_id, 'source_version': version, 'image_id': media_id, 'sha256': asset[1], 'role': 'buyer'})
            statuses.append({'source_id': source_id, 'media': media})
        return refs, statuses

    def _account_runtime_finished(self, job):
        # Called by the runtime only after the task, input cleanup and budget
        # release have finished. The account lock serializes the timeout race.
        with self.repo.lock:
            job['runtime_finished'] = True
            if self._generation_closed or not job.get('cleanup_timed_out'):
                return
            attempt = self.repo.get('generation_attempt', job['source_key'], {})
            if (attempt.get('status') == 'unknown' and attempt.get('owner_run_id') == self._generation_run_id
                    and attempt.get('attempt_id') == job.get('attempt_id')):
                attempt['runtime_settled'] = True
                if not self._generation_current(job):
                    attempt['status'] = 'failed'
                self.repo.put('generation_attempt', job['source_key'], attempt)
            if self._generation_active is job:
                self._generation_active = None
            self._schedule_account_replies()
            self._changed()

    def _run_account_generation(self, job):
        from .model_media import OwnedInput, estimate_input_reservation, start_photo_deadline
        from .model_router import ModelRouter
        entered = False
        owner = None
        try:
            with self.repo.lock:
                if not self._generation_current(job):
                    return
                context = self.get_whatsapp_context(job['chat_id'], message_count=job['message_count'])
                context.update(deepcopy(job['merchant_context']['context']))
                context['seller_id'] = job['merchant_context']['seller_id']
                context['model_image_refs'] = job['refs']
                media = {item['source_id']: item['media'] for item in job['statuses']}
                for message in context['whatsapp_messages']:
                    if message['source_id'] in media:
                        message['media'] = media[message['source_id']]
                router = self.router
            if router is None:
                raise ValueError('model unavailable')
            settings = getattr(self, 'settings', {})
            settings = settings if isinstance(settings, dict) else {}
            batch_mib = settings.get('image_batch_mib', 64)
            if type(batch_mib) is not int or not 8 <= batch_mib <= 256:
                batch_mib = 64
            photo_deadline = start_photo_deadline(settings)
            owner = OwnedInput(estimate_input_reservation(context, None))
            images, manifest = resolve_whatsapp_images(context, lambda ref: self._account_resolve_image(ref, job['account_id'], job['connection_epoch'], job['chat_id']), cancel=job['cancel'], deadline=photo_deadline, owner=owner, batch_mib=batch_mib)
            context.pop('model_image_refs', None)
            context['image_manifest'] = manifest
            context['image_transport'] = 'inline' if images else 'none'
            required = ('text', 'structure') + (('single_image', 'combined') if len(images) == 1 else ('multi_image', 'combined') if images else ())
            snapshot = router.resolve('reception', required, allow_unchecked=True)
            def begin_attempt():
                nonlocal entered
                with self.repo.lock:
                    if not self._generation_current(job):
                        raise ServiceError('cancelled', 'WhatsApp 来源已变化')
                    attempt = self.repo.get('generation_attempt', job['source_key'], {'count': 0})
                    if attempt.get('count', 0) >= 3:
                        raise ServiceError('retry_budget_exhausted', '本轮模型尝试次数已用完')
                    job['attempt_id'] = uuid.uuid4().hex
                    attempt.update(count=attempt.get('count', 0) + 1, status='in_flight',
                                   owner_run_id=self._generation_run_id, attempt_id=job['attempt_id'],
                                   source_fingerprint=job['source_fingerprint'], runtime_settled=False)
                    self.repo.put('generation_attempt', job['source_key'], attempt)
                    entered = True
            result = router.generate('reception', WHATSAPP + ('\n' + IMAGE_NATIVE if images else ''), model_json(context),
                                     images=images, required_capabilities=required, cancel=job['cancel'], on_call_start=begin_attempt,
                                     _allow_unchecked=True, _on_runtime_finished=lambda: self._account_runtime_finished(job),
                                     **({'_owned_input': owner, 'deadline': float(photo_deadline) + 120} if isinstance(router, ModelRouter) else {}))
            router.assert_current(snapshot)
            if result.get('model_snapshot') != asdict(snapshot):
                raise ServiceError('source_changed', '模型配置已变化')
            if images:
                expected = [{key: image[key] for key in ('alias', 'image_id', 'source_id', 'source_version', 'role', 'sha256')} for image in images]
                evidence = result.get('image_input_evidence')
                if not isinstance(evidence, dict) or evidence.get('status') != 'completed' or evidence.get('images') != expected:
                    raise ValueError('missing image evidence')
                from .image_assets import MAX_BYTES
                for ref in job['refs']:
                    transient = OwnedInput(MAX_BYTES * 3)
                    try:
                        self._account_resolve_image(ref, job['account_id'], job['connection_epoch'], job['chat_id'])
                    finally:
                        transient.close()
            answer = normalize_reply_output(result['output'])
            from .integrations.analysis import invalid_reply_body
            if any(invalid_reply_body(part['body']) for part in answer['reply_messages']):
                raise ValueError('invalid reply')
            if not isinstance(answer.get('wa_summary'), str) or len(answer['wa_summary']) > 4000:
                raise ValueError('invalid summary')
            allowed = {m['source_id'] for m in context['whatsapp_messages']}
            if context.get('wa_supplement', {}).get('body'):
                allowed.add(context['wa_supplement']['source_id'])
            background = context.get('international_background') or {}
            allowed.update(m.get('source_id') for m in background.get('messages', []))
            allowed.update(m.get('source_id') for m in context.get('knowledge', []) if isinstance(m, dict))
            supplement = background.get('supplement') or {}
            allowed.add(supplement.get('source_id'))
            allowed.update(a.get('id') for a in supplement.get('attachments', []) if isinstance(a, dict) and a.get('text'))
            if any(source not in allowed for source in answer['reply_sources']):
                raise ValueError('invalid sources')
            with self.repo.lock:
                if not self._generation_current(job):
                    attempt = self.repo.get('generation_attempt', job['source_key'], {'count': 0})
                    attempt['status'] = 'failed'
                    self.repo.put('generation_attempt', job['source_key'], attempt)
                    return
                attempt = self.repo.get('generation_attempt', job['source_key'], {'count': 0})
                attempt['status'] = 'completed'
                batch_id = uuid.uuid4().hex
                batch = {'batch_id': batch_id, 'parts': [dict(part, part_index=index, status='planned', message_id=None) for index, part in enumerate(answer['reply_messages'])], 'next_index': 0,
                         'account_id': job['account_id'], 'connection_epoch': job['connection_epoch'],
                         'chat_id': job['chat_id'], 'merchant_id': job['merchant_id'], 'segment_id': job['segment_id'],
                         'binding_revision': job['binding_revision'], 'pending_message_id': job['pending_message_id'],
                         'context_revision': job['merchant_context']['revision'], 'context_instance_id': job['merchant_context']['instance_id'],
                         'model_snapshot': result['model_snapshot'], 'source_fingerprint': job['source_fingerprint'],
                         'prompt_version': job['prompt_version'], 'status': 'prepared',
                         'created_at': time.time_ns() // 1_000_000, 'updated_at': time.time_ns() // 1_000_000}
                self.repo.db.execute('BEGIN IMMEDIATE')
                try:
                    self.repo.put('generation_attempt', job['source_key'], attempt)
                    self.repo.put('reply_batch', batch_id, batch)
                    self.repo.put('wa_summary', self._account_key(job['account_id'], job['chat_id'], job['segment_id']), {'body': answer['wa_summary']})
                    operation_id = self._account_create_part(batch)
                    self.repo.db.execute('COMMIT')
                except BaseException:
                    self.repo.db.execute('ROLLBACK')
                    raise
            self.emit({'event': 'whatsapp.send.requested', 'data': {'operation_id': operation_id}})
        except Exception as exc:
            with self.repo.lock:
                if not self._generation_closed:
                    timed_out = isinstance(exc, ServiceError) and exc.code == 'model_cleanup_timeout'
                    job['cleanup_timed_out'] = timed_out and not job.get('runtime_finished')
                    attempt = self.repo.get('generation_attempt', job['source_key'], {'count': 0})
                    obsolete_finished = not self._generation_current(job) and not job['cleanup_timed_out']
                    attempt['status'] = 'unknown' if entered and not obsolete_finished else 'failed'
                    if entered and job.get('runtime_finished'):
                        attempt['runtime_settled'] = True
                    self.repo.put('generation_attempt', job['source_key'], attempt)
                    self._account_generation_error(job, exc.code if isinstance(exc, ServiceError) else 'model_invalid_response')
        finally:
            if owner is not None:
                owner.close_if_owned()
            with self.repo.lock:
                if not self._generation_closed:
                    if self._generation_active is job and not job.get('cleanup_timed_out'):
                        self._generation_active = None
                    self._schedule_account_replies()
                    self._changed()

    def _account_create_part(self, batch):
        operation_id = uuid.uuid4().hex
        op = {key: batch[key] for key in ('account_id', 'connection_epoch', 'chat_id', 'merchant_id', 'segment_id',
              'binding_revision', 'pending_message_id', 'context_revision', 'context_instance_id', 'model_snapshot', 'batch_id', 'source_fingerprint')}
        op.update(prompt_version=batch.get('prompt_version'),
                  operation_id=operation_id, part_index=batch['next_index'], body=batch['parts'][batch['next_index']]['body'],
                  status='prepared', submission_started=False, message_id=None, ack=None, owner_run_id=self._generation_run_id)
        batch['parts'][batch['next_index']].update(status='prepared', operation_id=operation_id)
        batch.update(status='prepared', updated_at=time.time_ns() // 1_000_000)
        self.repo.put('reply_batch', batch['batch_id'], batch)
        self.repo.put('operation', operation_id, op)
        return operation_id

    def _account_batch_projection(self, batch):
        parts = batch['parts']
        return {'batch_id': batch['batch_id'], 'status': batch['status'], 'total': len(parts),
                'accepted': sum(part.get('status') == 'accepted' for part in parts),
                'current_index': batch['next_index'] if batch['next_index'] < len(parts) else None,
                'pending_operation_ids': [part['operation_id'] for part in parts if part.get('operation_id') and part.get('status') in ('prepared', 'sending', 'unknown')],
                'channel': 'whatsapp', 'chat_id': batch['chat_id'], 'created_at': batch.get('created_at'), 'updated_at': batch.get('updated_at'),
                'parts': [{key: part.get(key) for key in ('part_index', 'status', 'message_id')} for part in parts]}
