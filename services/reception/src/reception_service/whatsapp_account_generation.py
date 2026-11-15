"""Account-owned WhatsApp model generation without a merchant service."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import threading
import time
import uuid

from .errors import ServiceError
from .integrations.reception_prompts import WHATSAPP, IMAGE_NATIVE
from .model_inputs import resolve_whatsapp_images
from .model_payload import model_json
from .reply_messages import normalize_reply_output


class AccountGenerationMixin:
    def _account_source_fingerprint(self, account, chat, segment):
        rows = [row for row in self.repo.list('message') if row.get('account_id') == account
                and row.get('chat_id') == chat and row.get('segment_id') == segment and not row.get('from_me')]
        rows.sort(key=lambda row: (row.get('timestamp_ms', 0), row.get('id', '')))
        return self._account_key(rows)

    def _init_account_generation(self):
        self._generation_cancel = threading.Event()
        self._generation_active = None
        self._generation_requests = {}
        self._generation_closed = False
        self._generation_timer = None
        self._generation_run_id = uuid.uuid4().hex

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
        if (self._generation_closed or job['cancel'].is_set() or not self.enabled
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
            if attempts.get('count', 0) >= 3 or attempts.get('status') in ('in_flight', 'completed', 'unknown'):
                continue
            if max(row.get('retry_at_ms', 0), row.get('pending_ready_at_ms', 0)) > now:
                self._arm_account_schedule()
                continue
            refs, statuses = self._account_image_context(chat, binding['segment_id'])
            if any(item['media']['status'] == 'pending' for item in statuses):
                continue
            if any(item['media']['status'] != 'ready' for item in statuses):
                continue
            request = {'request_id': uuid.uuid4().hex, 'account_id': self.account, 'chat_id': chat,
                       'merchant_id': binding['merchant_id'], 'conversation_id': binding.get('conversation_id'),
                       'binding_revision': binding['revision']}
            job = dict(request, connection_epoch=self.epoch, segment_id=binding['segment_id'],
                       pending_message_id=row['pending_message_id'], source_key=source_key,
                       refs=refs, statuses=statuses, cancel=threading.Event(), context_deadline_ms=now + 10000,
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
        job['merchant_context'] = deepcopy(context)
        self._generation_active = job
        thread = threading.Thread(target=self._run_account_generation, args=(job,), daemon=True)
        job['thread'] = thread
        thread.start()
        return {'accepted': True}

    def _run_account_generation(self, job):
        entered = False
        try:
            with self.repo.lock:
                if not self._generation_current(job):
                    return
                context = self.get_whatsapp_context(job['chat_id'])
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
            images, manifest = resolve_whatsapp_images(context, lambda ref: self._account_resolve_image(ref, job['account_id'], job['connection_epoch'], job['chat_id']), cancel=job['cancel'])
            context.pop('model_image_refs', None)
            context['image_manifest'] = manifest
            context['image_transport'] = 'inline' if images else 'none'
            required = ('text', 'structure') + (('single_image', 'combined') if len(images) == 1 else ('multi_image', 'combined') if images else ())
            snapshot = router.resolve('reception', required)
            def begin_attempt():
                nonlocal entered
                with self.repo.lock:
                    if not self._generation_current(job):
                        raise ServiceError('cancelled', 'WhatsApp 来源已变化')
                    attempt = self.repo.get('generation_attempt', job['source_key'], {'count': 0})
                    if attempt.get('count', 0) >= 3:
                        raise ServiceError('retry_budget_exhausted', '本轮模型尝试次数已用完')
                    attempt.update(count=attempt.get('count', 0) + 1, status='in_flight')
                    self.repo.put('generation_attempt', job['source_key'], attempt)
                    entered = True
            result = router.generate('reception', WHATSAPP + ('\n' + IMAGE_NATIVE if images else ''), model_json(context),
                                     images=images, required_capabilities=required, cancel=job['cancel'], on_call_start=begin_attempt)
            router.assert_current(snapshot)
            if result.get('model_snapshot') != asdict(snapshot):
                raise ServiceError('source_changed', '模型配置已变化')
            if images:
                expected = [{key: image[key] for key in ('alias', 'image_id', 'source_id', 'source_version', 'role', 'sha256')} for image in images]
                evidence = result.get('image_input_evidence')
                if not isinstance(evidence, dict) or evidence.get('status') != 'completed' or evidence.get('images') != expected:
                    raise ValueError('missing image evidence')
                for ref in job['refs']:
                    self._account_resolve_image(ref, job['account_id'], job['connection_epoch'], job['chat_id'])
            answer = normalize_reply_output(result['output'])
            from .integrations.analysis import invalid_reply_body
            if any(invalid_reply_body(part['body']) for part in answer['reply_messages']):
                raise ValueError('invalid reply')
            if not isinstance(answer.get('wa_summary'), str) or len(answer['wa_summary']) > 4000:
                raise ValueError('invalid summary')
            allowed = {m['source_id'] for m in context['whatsapp_messages']}
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
                    return
                attempt = self.repo.get('generation_attempt', job['source_key'], {'count': 0})
                attempt['status'] = 'completed'
                batch_id = uuid.uuid4().hex
                batch = {'batch_id': batch_id, 'parts': [dict(part, part_index=index, status='planned', message_id=None) for index, part in enumerate(answer['reply_messages'])], 'next_index': 0,
                         'account_id': job['account_id'], 'connection_epoch': job['connection_epoch'],
                         'chat_id': job['chat_id'], 'merchant_id': job['merchant_id'], 'segment_id': job['segment_id'],
                         'binding_revision': job['binding_revision'], 'pending_message_id': job['pending_message_id'],
                         'context_revision': job['merchant_context']['revision'], 'context_instance_id': job['merchant_context']['instance_id'],
                         'model_snapshot': result['model_snapshot'], 'source_fingerprint': job['source_fingerprint'], 'status': 'prepared',
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
                attempt = self.repo.get('generation_attempt', job['source_key'], {'count': 0})
                attempt['status'] = 'unknown' if entered else 'failed'
                self.repo.put('generation_attempt', job['source_key'], attempt)
                self._account_generation_error(job, exc.code if isinstance(exc, ServiceError) else 'model_invalid_response')
        finally:
            with self.repo.lock:
                if self._generation_active is job:
                    self._generation_active = None
                self._schedule_account_replies()
                self._changed()

    def _account_create_part(self, batch):
        operation_id = uuid.uuid4().hex
        op = {key: batch[key] for key in ('account_id', 'connection_epoch', 'chat_id', 'merchant_id', 'segment_id',
              'binding_revision', 'pending_message_id', 'context_revision', 'context_instance_id', 'model_snapshot', 'batch_id', 'source_fingerprint')}
        op.update(operation_id=operation_id, part_index=batch['next_index'], body=batch['parts'][batch['next_index']]['body'],
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
