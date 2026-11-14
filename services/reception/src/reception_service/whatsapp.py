"""WhatsApp-local reception, with optional read-only international context."""
from __future__ import annotations

import copy
import hashlib

from .business_supplement import supplement_context
from .collection_rules import collection_rules
from .errors import ServiceError
from .model_payload import model_json
from .policy_library import select_policy_inputs
from .repository import encoded, new_id, now_ms
from .reply_messages import normalize_reply_output
from .reply_batches import freeze_reply_batch, reply_batch_progress


def _key(*parts):
    return hashlib.sha256(encoded(parts).encode('utf-8')).hexdigest()


def _text(value, limit=300):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ServiceError('invalid_params', 'WhatsApp 标识无效')
    return value


class WhatsAppMixin:
    def _init_whatsapp(self):
        self._wa_account = None
        self._wa_epoch = None
        self._wa_status = 'disconnected'
        self._wa_enabled = False
        with self.repo.transaction():
            for batch in self.repo.list('wa_reply_batch'):
                if batch.get('status') not in ('accepted', 'stopped'):
                    self._wa_stop_batch(batch)
            for op in self.repo.list('wa_operation'):
                if op.get('status') == 'prepared':
                    op.update(status='cancelled', updated_at=now_ms())
                    self.repo.put('wa_operation', op['operation_id'], op, history=True)
                elif op.get('status') == 'sending':
                    op.update(status='unknown', updated_at=now_ms())
                    self.repo.put('wa_operation', op['operation_id'], op, history=True)

    def _wa_contact_key(self, chat_id, account=None):
        return _key(self.seller_id, account or self._wa_account, chat_id)

    def _wa_contact(self, chat_id):
        _text(chat_id)
        if not self.account_verified or not self._wa_account:
            raise ServiceError('identity_required', 'WhatsApp 账号未连接')
        row = self.repo.get('wa_conversation', self._wa_contact_key(chat_id))
        if not row or row.get('seller_id') != self.seller_id:
            raise ServiceError('not_found', 'WhatsApp 联系人不存在')
        return row

    def _wa_stop_batch(self, batch, *, preserve_pending=False):
        if batch.get('status') in ('accepted', 'stopped'):
            return
        can_replan = preserve_pending and not any(part['status'] in ('accepted', 'sending', 'unknown') for part in batch['parts'])
        batch['status'] = 'stopped'
        for part in batch['parts']:
            if part['status'] in ('planned', 'prepared'):
                part['status'] = 'cancelled'
        batch['updated_at'] = now_ms()
        self.repo.put('wa_reply_batch', batch['batch_id'], batch, history=True)
        key = self._wa_contact_key(batch['chat_id'], batch['account_id'])
        contact = self.repo.get('wa_conversation', key)
        if contact and contact.get('pending_message_id') == batch.get('pending_message_id') and not can_replan:
            contact['pending'] = False
            self.repo.put('wa_conversation', key, contact)

    def _wa_cancel(self, chat_id=None, *, preserve_pending=False):
        for batch in self.repo.list('wa_reply_batch'):
            if batch.get('seller_id') == self.seller_id and batch.get('account_id') == self._wa_account and (chat_id is None or batch.get('chat_id') == chat_id):
                self._wa_stop_batch(batch, preserve_pending=preserve_pending)
        for job in self.repo.list('job'):
            if job.get('kind') != 'whatsapp_reply' or job.get('status') not in ('queued', 'running'):
                continue
            if chat_id is not None and job.get('wa_chat_id') != chat_id:
                continue
            self._generation_cancel_event(job['job_id']).set()
            if job['status'] == 'queued':
                self._job_update(job['job_id'], status='cancelled', stage='cancelled', completed_at=now_ms(), message='WhatsApp 来源或权限已变化')
        for op in self.repo.list('wa_operation'):
            if op.get('seller_id') == self.seller_id and op.get('account_id') == self._wa_account and op.get('status') == 'prepared' and (chat_id is None or op.get('chat_id') == chat_id):
                op.update(status='cancelled', updated_at=now_ms())
                self.repo.put('wa_operation', op['operation_id'], op, history=True)

    def _wa_set_account(self, account_id, connection_epoch, status):
        if status not in ('connected', 'disconnected', 'connecting'):
            raise ServiceError('invalid_params', '连接状态无效')
        if status == 'connected':
            _text(account_id)
            _text(connection_epoch)
            if not self.account_verified:
                raise ServiceError('identity_required', '请先核实商家身份')
        with self._lock, self.repo.transaction():
            if (account_id, connection_epoch, status) != (self._wa_account, self._wa_epoch, self._wa_status):
                self._wa_enabled = False
                self._wa_cancel()
                self._wa_account, self._wa_epoch, self._wa_status = account_id, connection_epoch, status
        self._changed()
        return self.get_whatsapp_state()

    def get_whatsapp_state(self):
        with self._lock:
            account = self._wa_account
            rows = [r for r in self.repo.list('wa_conversation') if r.get('seller_id') == self.seller_id and r.get('account_id') == account]
            bindings = [self.repo.get('wa_binding', self._wa_contact_key(r['chat_id'])) or {'chat_id': r['chat_id'], 'conversation_id': None, 'revision': 0} for r in rows]
            all_pending = [r for r in self.repo.list('wa_operation') if r.get('seller_id') == self.seller_id
                           and r.get('status') in ('prepared', 'sending', 'unknown')]
            ops = [r for r in all_pending if r.get('account_id') == account]
            other_pending = [r for r in all_pending if r.get('account_id') != account]
            batches = [r for r in self.repo.list('wa_reply_batch') if r.get('seller_id') == self.seller_id and r.get('account_id') == account]
            return {'reply_batches': [{**reply_batch_progress(b), 'chat_id': b['chat_id']} for b in batches],
                    'connected': self._wa_status == 'connected', 'account_id': account, 'connection_epoch': self._wa_epoch,
                    'enabled': self._wa_enabled, 'conversations': [{k: r.get(k) for k in ('chat_id', 'name', 'human_owned', 'pending')} for r in rows],
                    'bindings': [{k: b.get(k) for k in ('chat_id', 'conversation_id', 'revision')} for b in bindings],
                    'pending_operations': [{k: o.get(k) for k in ('operation_id', 'chat_id', 'status', 'message_id', 'ack')} for o in ops],
                    'other_account_pending_count': len(other_pending),
                    'other_account_pending_operations': [{k: o.get(k) for k in ('operation_id', 'status')}
                                                         for o in other_pending]}

    def _wa_expected_identity(self, expected_account_id, expected_connection_epoch):
        if expected_account_id is not None or expected_connection_epoch is not None:
            self._wa_check_connection(expected_account_id, expected_connection_epoch)

    def save_whatsapp_binding(self, chat_id, conversation_id, expected_revision, expected_account_id=None, expected_connection_epoch=None):
        with self._lock, self.repo.transaction():
            self._wa_expected_identity(expected_account_id, expected_connection_epoch)
            self._wa_contact(chat_id)
            key = self._wa_contact_key(chat_id)
            old = self.repo.get('wa_binding', key) or {'revision': 0, 'conversation_id': None}
            if type(expected_revision) is not int or expected_revision != old['revision']:
                raise ServiceError('revision_conflict', '绑定已变化，请刷新')
            if conversation_id is not None:
                _text(conversation_id)
                self._chat_owner(conversation_id)
            row = {'seller_id': self.seller_id, 'account_id': self._wa_account, 'chat_id': chat_id,
                   'conversation_id': conversation_id, 'revision': old['revision'] + 1, 'updated_at': now_ms()}
            self.repo.put('wa_binding', key, row, history=True)
            self._wa_cancel(chat_id, preserve_pending=True)
            contact = self._wa_contact(chat_id)
            contact['attempt_fingerprint'] = None
            self.repo.put('wa_conversation', key, contact)
            self._schedule_whatsapp()
        self._changed()
        return {k: row[k] for k in ('chat_id', 'conversation_id', 'revision')}

    def set_whatsapp_reception(self, enabled, expected_account_id=None, expected_connection_epoch=None):
        if type(enabled) is not bool:
            raise ServiceError('invalid_params', '接待开关无效')
        if enabled:
            self._executor_health.require()
        with self._lock, self.repo.transaction():
            if enabled:
                self._executor_health.require()
            self._wa_expected_identity(expected_account_id, expected_connection_epoch)
            if enabled and (not self.account_verified or self._wa_status != 'connected' or self.mode == 'live' and self.checks['ai']['status'] != 'passed'):
                raise ServiceError('checks_required', '请先连接账号并完成 AI 检查')
            self._wa_enabled = enabled
            if not enabled:
                self._wa_cancel()
            else:
                self._schedule_whatsapp()
        self._changed()
        return self.get_whatsapp_state()

    def takeover_whatsapp(self, chat_id, enabled, expected_account_id=None, expected_connection_epoch=None):
        if type(enabled) is not bool:
            raise ServiceError('invalid_params', '接管状态无效')
        with self._lock, self.repo.transaction():
            self._wa_expected_identity(expected_account_id, expected_connection_epoch)
            contact = self._wa_contact(chat_id)
            contact.update(human_owned=enabled, attempt_fingerprint=None)
            self.repo.put('wa_conversation', self._wa_contact_key(chat_id), contact)
            if enabled:
                self._wa_cancel(chat_id)
            else:
                self._schedule_whatsapp()
        self._changed()
        return self.get_whatsapp_state()

    def _wa_check_connection(self, account_id, connection_epoch):
        if self._wa_status != 'connected' or account_id != self._wa_account or connection_epoch != self._wa_epoch:
            raise ServiceError('source_changed', 'WhatsApp 连接已变化')

    def _wa_ingest(self, account_id, connection_epoch, messages):
        if not isinstance(messages, list) or len(messages) > 500:
            raise ServiceError('invalid_params', 'WhatsApp 消息无效')
        with self._lock, self.repo.transaction():
            self._wa_check_connection(account_id, connection_epoch)
            for msg in messages:
                if not isinstance(msg, dict) or type(msg.get('is_history')) is not bool or type(msg.get('is_group')) is not bool or type(msg.get('from_me')) is not bool:
                    raise ServiceError('invalid_params', 'WhatsApp 消息属性无效')
                chat_id = _text(msg.get('chat_id'))
                mid = _text(msg.get('id'))
                body = msg.get('body')
                if not isinstance(body, str) or len(body) > 50000 or type(msg.get('timestamp_ms')) is not int:
                    raise ServiceError('invalid_params', 'WhatsApp 消息内容无效')
                if msg['is_group'] or chat_id.endswith(('@g.us', '@broadcast', '@newsletter')):
                    continue
                key = self._wa_contact_key(chat_id)
                contact = self.repo.get('wa_conversation', key) or {'seller_id': self.seller_id, 'account_id': account_id,
                    'chat_id': chat_id, 'name': '', 'human_owned': False, 'pending': False, 'pending_message_id': None}
                name = msg.get('name')
                if isinstance(name, str) and name.strip():
                    contact['name'] = name[:300]
                if msg.get('metadata_only') is True:
                    self.repo.put('wa_conversation', key, contact)
                    continue
                mkey = _key(self.seller_id, account_id, mid)
                prior = self.repo.get('wa_message', mkey)
                if prior:
                    if prior.get('receipt_projection') and msg['from_me'] and prior.get('chat_id') == chat_id:
                        prior.update(timestamp_ms=msg['timestamp_ms'], ack=msg.get('ack'), receipt_projection=False)
                        self.repo.put('wa_message', mkey, prior)
                    continue
                row = {'seller_id': self.seller_id, 'account_id': account_id, 'chat_id': chat_id, 'message_id': mid,
                       'body': body, 'timestamp_ms': msg['timestamp_ms'], 'from_me': msg['from_me'],
                       'is_history': msg['is_history'], 'ack': msg.get('ack')}
                self.repo.put('wa_message', mkey, row)
                if not msg['is_history']:
                    if not msg['from_me'] and body.strip():
                        contact.update(pending=True, pending_message_id=mid, attempt_fingerprint=None)
                        self._wa_cancel(chat_id)
                    elif msg['from_me']:
                        matched = next((o for o in self.repo.list('wa_operation') if o.get('account_id') == account_id and o.get('message_id') == mid), None)
                        uncertain = any(o.get('account_id') == account_id and o.get('chat_id') == chat_id and o.get('status') in ('sending', 'unknown') and not o.get('message_id') for o in self.repo.list('wa_operation'))
                        if not matched and uncertain:
                            row['deferred_from_me'] = True
                            self.repo.put('wa_message', mkey, row)
                        elif not matched:
                            contact.update(human_owned=True, pending=False)
                            self._wa_cancel(chat_id)
                        if type(msg.get('ack')) is int:
                            if matched and matched.get('submission_started'):
                                self._wa_transport_result(matched['operation_id'], account_id, matched['connection_epoch'],
                                                          'submitted', mid, msg['ack'])
                                contact = self.repo.get('wa_conversation', key) or contact
                            else:
                                ack_key = _key(self.seller_id, account_id, mid)
                                previous_ack = self.repo.get('wa_ack', ack_key) or {}
                                self.repo.put('wa_ack', ack_key, {'ack': max(msg['ack'], previous_ack.get('ack', -1))})
                self.repo.put('wa_conversation', key, contact)
            self._schedule_whatsapp()
        self._changed()
        return self.get_whatsapp_state()

    def get_whatsapp_context(self, chat_id, *, exclude_message_ids=()):
        with self._lock:
            self._wa_contact(chat_id)
            account = self._wa_account
            messages = [r for r in self.repo.list('wa_message') if r.get('seller_id') == self.seller_id and r.get('account_id') == account and r.get('chat_id') == chat_id and r.get('body', '').strip() and r.get('message_id') not in exclude_message_ids]
            messages.sort(key=lambda r: (r['timestamp_ms'], r['message_id']))
            recent = [{'source_id': 'wa_' + _key(account, r['message_id'])[:20], 'role': 'seller' if r['from_me'] else 'buyer',
                       'timestamp_ms': r['timestamp_ms'], 'text': r['body'][:3000]} for r in messages[-30:]]
            policies = select_policy_inputs(self.repo.list('policy'), {'facts': {}, 'procurement_key': ''})
            context = {'task_mode': 'whatsapp_reply', 'seller_id': self.seller_id, 'whatsapp_messages': recent,
                       'wa_summary': (self.repo.get('wa_summary', self._wa_contact_key(chat_id)) or {}).get('body', ''),
                       'knowledge': policies['knowledge'], 'guidance': policies['guidance'], 'style': policies['style'],
                       'collection_rules': collection_rules(self.settings, None),
                       'binding_revision': (self.repo.get('wa_binding', self._wa_contact_key(chat_id)) or {}).get('revision', 0)}
            binding = self.repo.get('wa_binding', self._wa_contact_key(chat_id)) or {}
            cid = binding.get('conversation_id')
            if cid:
                owner = self._chat_owner(cid)
                case = self.repo.get('case', owner.get('case_id')) or {}
                original = [m for m in self.repo.list('message', conversation_id=cid) if m.get('seller_id') == self.seller_id and m.get('buyer_id') == owner.get('buyer_id')]
                original.sort(key=lambda r: (r.get('timestamp_ms') or 0, r.get('message_id') or ''))
                requirements = {k: copy.deepcopy(v) for k, v in (case.get('requirements') or {}).items() if k != 'product_url'}
                evidence = {mid for value in requirements.values() if isinstance(value, dict)
                            for mid in value.get('evidence_ids', []) if isinstance(mid, str)}
                recent_ids = {m.get('message_id') for m in original[-20:]}
                needed = [m for m in original[:-20] if m.get('message_id') in evidence][-10:]
                background = [{'source_id': 'icbu_' + _key(m['message_id'])[:20], 'message_id': m['message_id'], 'role': m.get('role'),
                               'timestamp_ms': m.get('timestamp_ms'), 'text': str(m.get('text') or '')[:3000]}
                              for m in needed + [m for m in original[-20:] if m.get('message_id') in recent_ids]]
                supplement = supplement_context(self.repo, cid)
                supplement['attachments'] = [{k: a[k] for k in ('id', 'name', 'media_type', 'text', 'sha256') if k in a}
                                             for a in supplement.get('attachments', []) if isinstance(a.get('text'), str)]
                context['international_background'] = {'messages': background, 'summary': self.repo.get('conversation_summary', cid),
                    'requirements': requirements, 'supplement': supplement,
                    'case_revision': case.get('revision'), 'conversation_revision': owner.get('revision')}
            while True:
                try:
                    model_json(context)
                    break
                except ServiceError as exc:
                    if exc.code != 'context_too_long':
                        raise
                    background_rows = context.get('international_background', {}).get('messages', [])
                    if background_rows:
                        removable = next((i for i, row in enumerate(background_rows) if row.get('message_id') not in evidence), 0)
                        background_rows.pop(removable)
                    elif len(context['whatsapp_messages']) > 1:
                        context['whatsapp_messages'].pop(0)
                    else:
                        raise
            return context

    def _wa_fingerprint(self, chat_id, batch=None):
        own_ids = [p['message_id'] for p in batch['parts'] if p.get('status') == 'accepted' and p.get('message_id')] if batch else []
        return hashlib.sha256(encoded(self.get_whatsapp_context(chat_id, exclude_message_ids=own_ids)).encode('utf-8')).hexdigest()

    def _schedule_whatsapp(self):
        if not self._wa_enabled or self._wa_status != 'connected' or self._closing or self._stop.is_set():
            return
        for contact in self.repo.list('wa_conversation'):
            if contact.get('seller_id') != self.seller_id or contact.get('account_id') != self._wa_account or not contact.get('pending') or contact.get('human_owned'):
                continue
            chat = contact['chat_id']
            if any(b.get('seller_id') == self.seller_id and b.get('account_id') == self._wa_account and b.get('chat_id') == chat and b.get('status') not in ('accepted', 'stopped') for b in self.repo.list('wa_reply_batch')):
                continue
            if any(o.get('account_id') == self._wa_account and o.get('chat_id') == chat and o.get('status') in ('prepared', 'sending', 'unknown') for o in self.repo.list('wa_operation')):
                continue
            if any(j.get('kind') == 'whatsapp_reply' and j.get('wa_chat_id') == chat and j.get('status') in ('queued', 'running') for j in self.repo.list('job')):
                continue
            try:
                context = self.get_whatsapp_context(chat)
            except ServiceError as exc:
                contact['error_code'] = exc.code
                self.repo.put('wa_conversation', self._wa_contact_key(chat), contact)
                continue
            contact.pop('error_code', None)
            fingerprint = hashlib.sha256(encoded(context).encode('utf-8')).hexdigest()
            if contact.get('attempt_fingerprint') == fingerprint:
                continue
            job_id = new_id('wa_job')
            job = {'job_id': job_id, 'kind': 'whatsapp_reply', 'wa_chat_id': chat, 'conversation_id': None, 'case_id': None,
                   'status': 'queued', 'stage': 'queued', 'message': '等待生成 WhatsApp 回复', 'automatic': False,
                   'created_at': now_ms(), 'started_at': None, 'completed_at': None}
            try:
                self._queue.put((job_id, 'whatsapp_reply', {'chat_id': chat, 'account_id': self._wa_account, 'connection_epoch': self._wa_epoch,
                                'context': context, 'fingerprint': fingerprint, 'pending_message_id': contact['pending_message_id']}, self._epoch, False))
            except ServiceError as exc:
                if exc.code != 'task_queue_full':
                    raise
                contact['error_code'] = exc.code
                self.repo.put('wa_conversation', self._wa_contact_key(chat), contact)
                continue
            contact['attempt_fingerprint'] = fingerprint
            self.repo.put('wa_conversation', self._wa_contact_key(chat), contact)
            self.repo.put('job', job_id, job, history=True)
            self._emit('job.updated', job)

    def _wa_current(self, params, batch=None):
        self._wa_check_connection(params['account_id'], params['connection_epoch'])
        contact = self._wa_contact(params['chat_id'])
        return (self._wa_enabled and not contact.get('human_owned') and contact.get('pending')
                and contact.get('pending_message_id') == params['pending_message_id']
                and self._wa_fingerprint(params['chat_id'], batch) == params['fingerprint'])

    def _task_whatsapp_reply(self, params, job_id, epoch, facade):
        with self._lock:
            if not self._wa_current(params):
                raise ServiceError('cancelled', 'WhatsApp 来源已变化')
        self._ensure_identity(facade)
        with self._job_phase(job_id, 'ai_elapsed_ms'):
            answer = facade.generate_whatsapp(params['context'], job_id, cancel_event=self._generation_cancel_event(job_id))
        with self._lock, self.repo.transaction():
            if self._generation_cancel_event(job_id).is_set() or not self._wa_current(params):
                raise ServiceError('cancelled', 'WhatsApp 来源已变化')
            try:
                answer = normalize_reply_output(answer, allow_legacy=True)
            except (ValueError, TypeError) as exc:
                raise ServiceError('invalid_ai_output', 'WhatsApp 回复无效') from exc
            if not isinstance(answer.get('wa_summary'), str) or len(answer['wa_summary']) > 4000:
                raise ServiceError('invalid_ai_output', 'WhatsApp 回复无效')
            allowed = {r['source_id'] for r in params['context']['whatsapp_messages']}
            allowed.update(r['source_id'] for r in params['context'].get('international_background', {}).get('messages', []))
            for group in ('knowledge',):
                allowed.update(r.get('source_id') for r in params['context'].get(group, []) if isinstance(r, dict))
            supplement = params['context'].get('international_background', {}).get('supplement') or {}
            allowed.add(supplement.get('source_id'))
            allowed.update(a.get('id') for a in supplement.get('attachments', []) if isinstance(a, dict) and a.get('text'))
            if any(not isinstance(source, str) or source not in allowed for source in answer['reply_sources']):
                raise ServiceError('invalid_ai_output', 'WhatsApp 回复来源无效')
            stamp = now_ms()
            self.repo.put('wa_summary', self._wa_contact_key(params['chat_id']), {'body': answer['wa_summary'], 'updated_at': stamp})
            batch = freeze_reply_batch(answer['reply_messages'], seller_id=self.seller_id, account_id=params['account_id'],
                                       connection_epoch=params['connection_epoch'], chat_id=params['chat_id'],
                                       pending_message_id=params['pending_message_id'], fingerprint=self._wa_fingerprint(params['chat_id']))
            self.repo.put('wa_reply_batch', batch['batch_id'], batch, history=True)
            self.repo.put('wa_draft', batch['batch_id'], {'batch_id': batch['batch_id'], 'reply': answer['reply'],
                          'reply_messages': answer['reply_messages'], 'reply_sources': answer['reply_sources']}, history=True)
            op_id = self._wa_create_part_operation(batch)
        self._emit('whatsapp.send.requested', {'operation_id': op_id})
        return {'operation_id': op_id, 'batch_id': batch['batch_id']}

    def _wa_create_part_operation(self, batch):
        part = batch['parts'][batch['next_index']]
        op_id = new_id('wa_operation')
        stamp = now_ms()
        op = {k: batch[k] for k in ('seller_id', 'account_id', 'connection_epoch', 'chat_id', 'fingerprint', 'pending_message_id')}
        op.update(operation_id=op_id, batch_id=batch['batch_id'], part_index=part['part_index'], part_id=part['part_id'],
                  body=part['body'], reply_sources=part['reply_sources'], status='prepared', submission_started=False,
                  message_id=None, ack=None, created_at=stamp, updated_at=stamp)
        part['status'] = 'prepared'
        part['operation_ids'].append(op_id)
        batch['status'] = 'prepared'
        self.repo.put('wa_reply_batch', batch['batch_id'], batch, history=True)
        self.repo.put('wa_operation', op_id, op, history=True)
        return op_id

    def _wa_prepare_send(self, operation_id):
        self._executor_health.require()
        with self._lock, self.repo.transaction():
            self._executor_health.require()
            op = self.repo.get('wa_operation', _text(operation_id))
            if not op or op.get('seller_id') != self.seller_id or op.get('status') != 'prepared':
                raise ServiceError('operation_unavailable', '发送操作不可重复提交')
            params = {k: op[k] for k in ('account_id', 'connection_epoch', 'chat_id', 'pending_message_id', 'fingerprint')}
            batch = self.repo.get('wa_reply_batch', op.get('batch_id')) if op.get('batch_id') else None
            try:
                current = (not op.get('batch_id') or batch and batch.get('status') == 'prepared' and batch.get('next_index') == op.get('part_index')) and self._wa_current(params, batch)
            except ServiceError:
                current = False
            if not current:
                if batch:
                    self._wa_stop_batch(batch)
                op.update(status='cancelled', updated_at=now_ms())
                self.repo.put('wa_operation', operation_id, op, history=True)
                self._schedule_whatsapp()
                result = None
            else:
                op.update(status='unknown', submission_started=True, updated_at=now_ms())
                self.repo.put('wa_operation', operation_id, op, history=True)
                if batch:
                    batch['status'] = 'waiting_receipt'
                    batch['parts'][op['part_index']]['status'] = 'unknown'
                    self.repo.put('wa_reply_batch', batch['batch_id'], batch, history=True)
                result = {k: op[k] for k in ('operation_id', 'account_id', 'connection_epoch', 'chat_id', 'body')}
        if result is None:
            raise ServiceError('source_changed', '发送依据已变化')
        return result

    def _wa_transport_result(self, operation_id, account_id, connection_epoch, status, message_id=None, ack=None):
        if status not in ('accepted', 'submitted', 'unknown', 'failed') or ack is not None and type(ack) is not int:
            raise ServiceError('invalid_params', 'WhatsApp 发送结果无效')
        next_operation = None
        with self._lock, self.repo.transaction():
            op = self.repo.get('wa_operation', _text(operation_id))
            if not op or op.get('seller_id') != self.seller_id or (op.get('account_id'), op.get('connection_epoch')) != (account_id, connection_epoch) or not op.get('submission_started'):
                raise ServiceError('operation_unavailable', '发送记录不匹配')
            if message_id is not None:
                _text(message_id)
                if op.get('message_id') and op['message_id'] != message_id:
                    raise ServiceError('operation_conflict', '发送消息编号不匹配')
                op['message_id'] = message_id
                early_ack = self.repo.get('wa_ack', _key(self.seller_id, account_id, message_id)) or {}
                if type(early_ack.get('ack')) is int:
                    ack = max(ack if type(ack) is int else -1, early_ack['ack'])
            if ack is not None:
                op['ack'] = max(op.get('ack') if type(op.get('ack')) is int else -1, ack)
            op['status'] = 'accepted' if op.get('message_id') and type(op.get('ack')) is int and op['ack'] >= 1 else 'unknown'
            op['updated_at'] = now_ms()
            self.repo.put('wa_operation', operation_id, op, history=True)
            if op['status'] == 'accepted':
                message_key = _key(self.seller_id, account_id, op['message_id'])
                if not self.repo.get('wa_message', message_key):
                    self.repo.put('wa_message', message_key, {'seller_id': self.seller_id, 'account_id': account_id,
                        'chat_id': op['chat_id'], 'message_id': op['message_id'], 'body': op['body'],
                        'timestamp_ms': now_ms(), 'from_me': True, 'is_history': False, 'ack': op['ack'],
                        'receipt_projection': True})
            if op['status'] == 'accepted' and not op.get('batch_id'):
                contact = self.repo.get('wa_conversation', self._wa_contact_key(op['chat_id'], account_id))
                if contact and contact.get('pending_message_id') == op.get('pending_message_id'):
                    contact['pending'] = False
                    self.repo.put('wa_conversation', self._wa_contact_key(op['chat_id'], account_id), contact)
            for row in self.repo.list('wa_message'):
                if row.get('account_id') != account_id or row.get('chat_id') != op['chat_id'] or not row.get('deferred_from_me'):
                    continue
                if not op.get('message_id'):
                    continue
                row.pop('deferred_from_me', None)
                self.repo.put('wa_message', _key(self.seller_id, account_id, row['message_id']), row)
                if row['message_id'] != op['message_id']:
                    contact = self.repo.get('wa_conversation', self._wa_contact_key(op['chat_id'], account_id))
                    if contact:
                        contact.update(human_owned=True, pending=False)
                        self.repo.put('wa_conversation', self._wa_contact_key(op['chat_id'], account_id), contact)
                        self._wa_cancel(op['chat_id'])
            batch = self.repo.get('wa_reply_batch', op.get('batch_id')) if op.get('batch_id') else None
            if batch:
                part = batch['parts'][op['part_index']]
                was_accepted = part.get('status') == 'accepted'
                if op['status'] == 'accepted':
                    part.update(status='accepted', message_id=op['message_id'], ack=op['ack'])
                    if not was_accepted and batch.get('status') != 'stopped':
                        batch['next_index'] = op['part_index'] + 1
                        if batch['next_index'] == len(batch['parts']):
                            batch['status'] = 'accepted'
                            contact = self.repo.get('wa_conversation', self._wa_contact_key(op['chat_id'], account_id))
                            if contact and contact.get('pending_message_id') == op.get('pending_message_id'):
                                contact['pending'] = False
                                self.repo.put('wa_conversation', self._wa_contact_key(op['chat_id'], account_id), contact)
                        else:
                            try:
                                current = self._wa_current(batch, batch)
                            except ServiceError:
                                current = False
                            if current:
                                next_operation = self._wa_create_part_operation(batch)
                            else:
                                self._wa_stop_batch(batch)
                self.repo.put('wa_reply_batch', batch['batch_id'], batch, history=True)
        if next_operation:
            self._emit('whatsapp.send.requested', {'operation_id': next_operation})
        self._changed()
        return {'operation_id': operation_id, 'status': op['status'], 'message_id': op.get('message_id'), 'ack': op.get('ack')}

    def _wa_ack(self, account_id, connection_epoch, message_id, ack):
        _text(message_id)
        if type(ack) is not int:
            raise ServiceError('invalid_params', '回执无效')
        with self._lock:
            self._wa_check_connection(account_id, connection_epoch)
            op = next((o for o in self.repo.list('wa_operation') if o.get('seller_id') == self.seller_id and o.get('account_id') == account_id and o.get('message_id') == message_id and o.get('submission_started')), None)
            if not op:
                key = _key(self.seller_id, account_id, message_id)
                prior = self.repo.get('wa_ack', key) or {}
                self.repo.put('wa_ack', key, {'ack': max(ack, prior.get('ack', -1))})
                return {'matched': False}
        result = self._wa_transport_result(op['operation_id'], account_id, op['connection_epoch'], 'submitted', message_id, ack)
        return {'matched': True, **result}

    def reconcile_whatsapp_operation(self, operation_id):
        with self._lock:
            op = self.repo.get('wa_operation', _text(operation_id))
            if not op or op.get('seller_id') != self.seller_id or op.get('status') != 'unknown':
                raise ServiceError('operation_unavailable', '没有可核查的发送操作')
            self._wa_check_connection(op['account_id'], self._wa_epoch)
        self._emit('whatsapp.reconcile.requested', {'operation_id': operation_id})
        return {'operation_id': operation_id, 'status': 'unknown'}
