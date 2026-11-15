"""Independent, fail-closed WhatsApp account process.

This module deliberately does not import or construct a merchant service.  The
unfinished reception path is disabled until account-wide ownership and legacy
source verification are available.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from .errors import ServiceError
from .whatsapp_account_media import AccountMediaMixin
from .whatsapp_account_generation import AccountGenerationMixin
from .workspace_lock import WorkspaceLock


def _error(code, message):
    raise ServiceError(code, message, stage='whatsapp_account')


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 300 or any(ord(c) < 32 for c in value):
        _error('invalid_params', 'WhatsApp 标识无效')
    return value


def _nullable(value):
    return None if value is None else _identifier(value)


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


class AccountRepository:
    """Account-owned store; serializes changes and commits with FULL durability."""
    def __init__(self, root, workspace_id):
        self.root = Path(root)
        self._workspace_lock = WorkspaceLock(self.root).acquire()
        self.db = None
        try:
            directory = self.root / 'whatsapp-accounts' / workspace_id
            directory.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(directory / 'account.sqlite3'), isolation_level=None, check_same_thread=False, timeout=5)
            self.lock = threading.RLock()
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kind,id))')
            self.db.execute('CREATE TABLE IF NOT EXISTS metadata (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
            existing = self.get('meta', 'workspace')
            if existing and existing['workspace_id'] != workspace_id:
                _error('workspace_mismatch', '账号工作区不匹配')
            self.put('meta', 'workspace', {'workspace_id': workspace_id})
        except BaseException:
            if self.db is not None:
                self.db.close()
            self._workspace_lock.release()
            raise

    def get(self, kind, key, default=None):
        row = self.db.execute('SELECT data FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, kind, key, value):
        self.db.execute('INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data',
                        (kind, key, json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)))

    def list(self, kind):
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM records WHERE kind=? ORDER BY rowid', (kind,))]

    @contextmanager
    def transaction(self):
        with self.lock:
            own = not self.db.in_transaction
            if own:
                self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                if own:
                    self.db.execute('COMMIT')
            except BaseException:
                if own:
                    self.db.execute('ROLLBACK')
                raise

    def close(self):
        try:
            result = self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if result and result[0]:
                raise RuntimeError('account database checkpoint incomplete')
            self.db.close()
            self.db = None
            self._workspace_lock.mark_clean_shutdown()
        finally:
            try:
                if self.db is not None:
                    self.db.close()
                    self.db = None
            finally:
                self._workspace_lock.release()


class WhatsAppAccountService(AccountMediaMixin, AccountGenerationMixin):
    _account_key = staticmethod(_key)
    def __init__(self, root, workspace_id, emit):
        self.workspace_id = workspace_id
        self.root = Path(root)
        self.repo = AccountRepository(self.root, workspace_id)
        self._init_account_media()
        self._init_account_generation()
        self.emit = emit
        self.account = None
        self.epoch = None
        self.connected = False
        self.enabled = False
        self.allowed = []
        self.model = {'ready': False, 'status': 'unavailable', 'message': '账号接待服务尚未就绪'}
        self.router = None
        self._model_packet_digest = None
        self.context_requests = {}
        self.context_leases = {}
        self._legacy_worker = None
        self._legacy_check_status = None

    def close(self):
        self.enabled = False
        self._generation_closed = True
        self._cancel_account_generation()
        self._cancel_account_media()
        if self.router is not None:
            self.router.close()
        if self._legacy_worker and self._legacy_worker is not threading.current_thread():
            self._legacy_worker.join()
        active = self._generation_active
        if active and active.get('thread') is not threading.current_thread():
            active['thread'].join()
        self.repo.close()

    def _changed(self):
        self.emit({'event': 'state.changed', 'data': {}})

    def call(self, method, params):
        methods = {'get_whatsapp_state', 'save_whatsapp_binding', 'save_whatsapp_default_merchant',
                   'set_whatsapp_reception', 'takeover_whatsapp', 'get_whatsapp_context',
                   'reconcile_whatsapp_operation', '_wa_get_reconcile_record', '_wa_set_account', '_wa_ingest', '_wa_ack',
                   '_wa_prepare_send', '_wa_transport_result', '_wa_get_context_request',
                   '_wa_set_merchant_context', '_wa_set_legacy_status', '_wa_verify_legacy_sources', '_configure_models',
                   '_wa_media_begin', '_wa_media_chunk', '_wa_media_finish', '_wa_media_fail',
                   'get_whatsapp_writeback_capabilities', '_wa_writeback_preview', '_wa_writeback_submit', 'shutdown'}
        if method not in methods or not isinstance(params, dict):
            _error('invalid_request', '请求方法或参数无效')
        try:
            if method == '_wa_verify_legacy_sources':
                return self._wa_verify_legacy_sources(**params)
            with self.repo.lock:
                return getattr(self, method)(**params)
        except TypeError:
            _error('invalid_params', '请求参数无效')

    def shutdown(self):
        self.enabled = False
        return {'ok': True}

    def _identity(self, account, epoch):
        if not self.connected or not account or not epoch or (account, epoch) != (self.account, self.epoch):
            _error('source_changed', 'WhatsApp 连接已变化')

    def _wa_set_account(self, account_id, connection_epoch, status):
        if status not in ('connected', 'connecting', 'disconnected'):
            _error('invalid_params', '连接状态无效')
        if status == 'connected':
            _identifier(account_id)
            _identifier(connection_epoch)
            owner = self.repo.get('meta', 'account')
            if owner and owner['account_id'] != account_id:
                self.enabled = self.connected = False
                self.account = self.epoch = None
                self._changed()
                _error('account_changed', '当前登录账号与工作区不匹配')
            if not owner:
                self.repo.put('meta', 'account', {'account_id': account_id})
        if (self.account, self.epoch, self.connected) != (account_id, connection_epoch, status == 'connected'):
            self._cancel_account_generation()
            self._cancel_account_media()
            self._cancel_account_generation()
            self.enabled = False
            self.allowed = []
        self.account = account_id if status == 'connected' else None
        self.epoch = connection_epoch if status == 'connected' else None
        self.connected = status == 'connected'
        self._changed()
        return self.get_whatsapp_state()

    def get_whatsapp_state(self):
        account = self.account
        rows = [r for r in self.repo.list('conversation') if r['account_id'] == account]
        bindings = [self.repo.get('binding', _key(account, r['chat_id'])) for r in rows]
        ops = [r for r in self.repo.list('operation') if r['account_id'] == account and r['status'] in ('prepared', 'sending', 'unknown')]
        default = self.repo.get('meta', 'default', {'merchant_id': None, 'revision': 0})
        return {'workspace_id': self.workspace_id, 'account_id': account, 'connection_epoch': self.epoch,
                'connected': self.connected, 'enabled': self.enabled, 'default_merchant_id': default['merchant_id'],
                'default_revision': default['revision'], 'allowed_merchant_ids': self.allowed,
                'conversations': [{**{k: r[k] for k in ('chat_id', 'name', 'human_owned', 'pending')}, 'reception_paused': r.get('reception_paused', False), 'error_code': r.get('error_code')} for r in rows],
                'bindings': [{k: b[k] for k in ('chat_id', 'merchant_id', 'conversation_id', 'revision', 'segment_id', 'requested_binding', 'requested_binding_revision') if k in b} for b in bindings], 'pending_operations': [{k: o.get(k) for k in ('operation_id','chat_id','merchant_id','status','message_id','ack')} for o in ops],
                'reply_batches': [self._account_batch_projection(batch) for batch in self.repo.list('reply_batch') if batch.get('account_id') == account][-50:], 'legacy': self._legacy_projection(account),
                'model': self.model.copy()}

    def _legacy_projection(self, account):
        legacy = self.repo.get('legacy', account) if account else None
        pending = sum(1 for op in self.repo.list('operation') if op.get('account_id') == account and op.get('status') in ('prepared', 'sending', 'unknown'))
        if self._legacy_check_status or not legacy:
            reason = '正在核查已登记的旧来源，完成前自动接待保持暂停' if self._legacy_check_status == 'running' else '旧来源核查未完成，请保留原目录后重试' if self._legacy_check_status == 'failed' else '旧来源尚未核验'
            return {'status': 'unverified', 'source_count': len(legacy['sources']) if legacy else 0, 'pending_count': pending, 'reason': reason}
        blocked = any(source['status'] == 'blocked' for source in legacy['sources'])
        return {'status': 'blocked' if blocked else 'ready', 'source_count': len(legacy['sources']),
                'pending_count': pending, 'reason': '旧来源核验未完成' if blocked else ''}

    def _conversation(self, chat_id):
        row = self.repo.get('conversation', _key(self.account, _identifier(chat_id)))
        if not row:
            _error('not_found', 'WhatsApp 联系人不存在')
        return row

    def _activate_account_binding(self, key, binding, target, revision):
        with self.repo.transaction():
            binding.update(target)
            binding.update(segment_id=uuid.uuid4().hex, revision=revision, requested_binding=None, route_selected=True)
            binding.pop('requested_binding_revision', None)
            self.repo.put('binding', key, binding)
            row = self.repo.get('conversation', key)
            if row:
                row.update(pending=False, reception_paused=True)
                self.repo.put('conversation', key, row)

    def save_whatsapp_default_merchant(self, merchant_id, expected_revision, expected_account_id, expected_connection_epoch):
        self._identity(expected_account_id, expected_connection_epoch)
        merchant_id = _nullable(merchant_id)
        old = self.repo.get('meta', 'default', {'merchant_id': None, 'revision': 0})
        if type(expected_revision) is not int or old['revision'] != expected_revision:
            _error('revision_conflict', '默认商家已变化，请刷新')
        self.repo.put('meta', 'default', {'merchant_id': merchant_id, 'revision': expected_revision + 1})
        self._cancel_account_generation()
        self.enabled = False
        self._changed()
        return self.get_whatsapp_state()

    def save_whatsapp_binding(self, chat_id, merchant_id, conversation_id, expected_revision, expected_account_id, expected_connection_epoch):
        self._identity(expected_account_id, expected_connection_epoch)
        self._conversation(chat_id)
        merchant_id, conversation_id = _nullable(merchant_id), _nullable(conversation_id)
        key = _key(self.account, chat_id)
        old = self.repo.get('binding', key)
        edit_revision = old.get('requested_binding_revision', old['revision'])
        if type(expected_revision) is not int or edit_revision != expected_revision:
            _error('revision_conflict', '绑定已变化，请刷新')
        target = {'merchant_id': merchant_id, 'conversation_id': conversation_id}
        if any(o['account_id'] == self.account and o['chat_id'] == chat_id and o['status'] in ('prepared','sending','unknown') for o in self.repo.list('operation')):
            old.update(requested_binding=target, requested_binding_revision=edit_revision + 1, route_selected=True)
            self.repo.put('binding', key, old)
        else:
            self._activate_account_binding(key, old, target, edit_revision + 1)
        self._cancel_account_generation(chat_id)
        self._schedule_account_replies()
        self._changed()
        return old

    def set_whatsapp_reception(self, enabled, allowed_merchant_ids=None, expected_account_id=None, expected_connection_epoch=None):
        if type(enabled) is not bool:
            _error('invalid_params', '接待设置无效')
        if enabled:
            self._identity(expected_account_id, expected_connection_epoch)
            if not isinstance(allowed_merchant_ids, list) or any(not isinstance(x, str) or not x for x in allowed_merchant_ids):
                _error('invalid_params', '接待设置无效')
        self.enabled = False
        self.allowed = []
        self._cancel_account_generation()
        if enabled:
            if not allowed_merchant_ids or self._legacy_projection(self.account)['status'] != 'ready' or self.router is None:
                _error('checks_required', '账号接待模型或旧来源核验尚未就绪')
            self.router.resolve('reception', ('text', 'structure'))
            self.enabled = True
            self.allowed = list(dict.fromkeys(allowed_merchant_ids))
            self._schedule_account_replies()
        self._changed()
        return self.get_whatsapp_state()

    def takeover_whatsapp(self, chat_id, enabled, expected_account_id, expected_connection_epoch):
        self._identity(expected_account_id, expected_connection_epoch)
        if type(enabled) is not bool:
            _error('invalid_params', '接管状态无效')
        row = self._conversation(chat_id)
        row['human_owned'] = enabled
        if not enabled:
            row['reception_paused'] = False
        if enabled:
            row['pending'] = False
        self.repo.put('conversation', _key(self.account, chat_id), row)
        self._cancel_account_generation(chat_id)
        self._schedule_account_replies()
        self._changed()
        return self.get_whatsapp_state()

    def _wa_ingest(self, account_id, connection_epoch, messages):
        self._identity(account_id, connection_epoch)
        if not isinstance(messages, list) or len(messages) > 500:
            _error('invalid_params', 'WhatsApp 消息无效')
        # Validate the complete batch before writing any observation.
        for m in messages:
            if not isinstance(m, dict) or any(type(m.get(k)) is not bool for k in ('is_history','is_group','from_me')):
                _error('invalid_params', 'WhatsApp 消息无效')
            _identifier(m.get('id')); _identifier(m.get('chat_id'))
            if not isinstance(m.get('body'), str) or len(m['body']) > 50000 or type(m.get('timestamp_ms')) is not int:
                _error('invalid_params', 'WhatsApp 消息无效')
        self.repo.db.execute('BEGIN IMMEDIATE')
        try:
            for m in messages:
                chat = m['chat_id']
                if m['is_group'] or chat.endswith(('@g.us', '@broadcast', '@newsletter')):
                    continue
                key = _key(account_id, chat)
                row = self.repo.get('conversation', key)
                if row is None:
                    row = {'account_id': account_id, 'chat_id': chat, 'name': '', 'human_owned': False, 'pending': False, 'reception_paused': False}
                    self.repo.put('binding', key, {'chat_id': chat, 'merchant_id': None, 'conversation_id': None,
                                                   'revision': 0, 'segment_id': uuid.uuid4().hex, 'requested_binding': None, 'route_selected': False})
                if isinstance(m.get('name'), str) and m['name'].strip():
                    row['name'] = m['name'][:300]
                msgkey = _key(account_id, m['id'])
                if not m.get('metadata_only') and self.repo.get('message', msgkey) is None:
                    binding = self.repo.get('binding', key)
                    if (binding.get('route_selected') is False and not m['is_history'] and not m['from_me']
                            and (m['body'].strip() or m.get('has_image'))):
                        default = self.repo.get('meta', 'default', {'merchant_id': None})
                        binding.update(merchant_id=default['merchant_id'], route_selected=True, segment_id=uuid.uuid4().hex)
                        self.repo.put('binding', key, binding)
                    stored = dict(m, account_id=account_id, segment_id=binding['segment_id'])
                    if not m['is_history'] and m['from_me']:
                        uncertain = [o for o in self.repo.list('operation') if o.get('account_id') == account_id
                                     and o.get('chat_id') == chat and o.get('submission_started')
                                     and o.get('status') in ('sending', 'unknown')]
                        own = any(o.get('message_id') == m['id'] for o in self.repo.list('operation')
                                  if o.get('account_id') == account_id and o.get('submission_started'))
                        if uncertain and any(not o.get('message_id') for o in uncertain):
                            stored['deferred_outgoing'] = True
                        elif not own:
                            row.update(human_owned=True, pending=False)
                    self.repo.put('message', msgkey, stored)
                    if not m['is_history'] and m['from_me']:
                        pass
                    elif not m['is_history'] and (m['body'].strip() or m.get('has_image')):
                        row['pending'] = True
                        row['pending_message_id'] = m['id']
                self.repo.put('conversation', key, row)
            self.repo.db.execute('COMMIT')
        except BaseException:
            self.repo.db.execute('ROLLBACK')
            raise
        if self._generation_active and not self._generation_current(self._generation_active):
            self._cancel_account_generation()
        self._schedule_account_replies()
        self._changed()
        return self.get_whatsapp_state()

    def get_whatsapp_context(self, chat_id):
        self._conversation(chat_id)
        binding = self.repo.get('binding', _key(self.account, chat_id))
        rows = [r for r in self.repo.list('message') if r['account_id'] == self.account and r['chat_id'] == chat_id and r['segment_id'] == binding['segment_id']]
        rows.sort(key=lambda r: (r['timestamp_ms'], r['id']))
        return {'task_mode': 'whatsapp_reply', 'whatsapp_messages': [{'source_id': 'wa_' + _key(self.account, r['id'])[:20],
                'role': 'seller' if r['from_me'] else 'buyer', 'timestamp_ms': r['timestamp_ms'], 'text': r['body'][:3000]} for r in rows[-30:]],
                'wa_summary': self.repo.get('wa_summary', _key(self.account, chat_id, binding['segment_id']), {}).get('body', ''),
                'knowledge': [], 'guidance': [], 'style': [], 'binding_revision': binding['revision']}

    def get_whatsapp_writeback_capabilities(self):
        return {'format': 'wa-writeback-capabilities-v1', 'available': False, 'enabled': False, 'reason': '反写尚未开放',
                'destinations': ['merchant_workbench', 'international_platform'],
                'content_kinds': ['summary', 'requirement_changes', 'note', 'message_reference']}

    def _wa_writeback_preview(self, **_request):
        _error('capability_unavailable', 'WhatsApp 反写能力尚未开放')

    def _wa_writeback_submit(self, **_request):
        _error('capability_unavailable', 'WhatsApp 反写能力尚未开放')

    def reconcile_whatsapp_operation(self, operation_id):
        op = self.repo.get('operation', _identifier(operation_id))
        if not op or op['status'] not in ('prepared', 'sending', 'unknown') or not self.connected or op['account_id'] != self.account:
            _error('operation_unavailable', '没有可核查的发送操作')
        self.emit({'event': 'whatsapp.reconcile.requested', 'data': {'operation_id': operation_id}})
        return {'operation_id': operation_id, 'status': op['status']}

    def _wa_get_reconcile_record(self, operation_id):
        op = self.repo.get('operation', _identifier(operation_id))
        if not op or not self.connected or op.get('account_id') != self.account:
            _error('operation_unavailable', '原账号的发送记录不可用')
        receipt = op.get('legacy_receipt') or {}
        message_id = op.get('message_id') or receipt.get('message_id')
        if not isinstance(message_id, str) or not message_id or not isinstance(op.get('body'), str):
            return None
        ack = op.get('ack') if type(op.get('ack')) is int else receipt.get('ack')
        return {'intent': {key: op[key] for key in ('operation_id', 'account_id', 'connection_epoch', 'chat_id', 'body')},
                'result': {'message_id': message_id, 'ack': ack if type(ack) is int else 0}}

    def _wa_transport_result(self, operation_id, account_id, connection_epoch, status, message_id=None, ack=None):
        op = self.repo.get('operation', _identifier(operation_id))
        if (not op or (op.get('account_id'), op.get('connection_epoch')) != (account_id, connection_epoch)
                or not op.get('submission_started') or status not in ('submitted', 'accepted', 'unknown', 'failed')
                or (ack is not None and type(ack) is not int)):
            _error('operation_unavailable', '发送记录不匹配')
        if message_id is not None:
            _identifier(message_id)
            if op.get('message_id') and op['message_id'] != message_id:
                _error('operation_conflict', '发送消息编号不匹配')
            op['message_id'] = message_id
            early = self.repo.get('ack', _key(account_id, message_id), {}).get('ack')
            if type(early) is int:
                ack = max(ack if ack is not None else -1, early)
        if ack is not None:
            op['ack'] = max(op.get('ack') if type(op.get('ack')) is int else -1, ack)
        was_accepted = op.get('status') == 'accepted'
        if not was_accepted:
            op['status'] = 'accepted' if op.get('message_id') and type(op.get('ack')) is int and op['ack'] >= 1 else 'unknown'
        next_operation = None
        self.repo.db.execute('BEGIN IMMEDIATE')
        try:
            self.repo.put('operation', operation_id, op)
            batch = self.repo.get('reply_batch', op.get('batch_id')) if op.get('batch_id') else None
            if batch:
                batch['parts'][op['part_index']].update(status=op['status'], message_id=op.get('message_id'))
                batch['updated_at'] = time.time_ns() // 1_000_000
                self.repo.put('reply_batch', batch['batch_id'], batch)
            if op.get('message_id'):
                conversation_key = _key(account_id, op['chat_id'])
                conversation = self.repo.get('conversation', conversation_key)
                for message in self.repo.list('message'):
                    if (message.get('account_id') == account_id and message.get('chat_id') == op['chat_id']
                            and message.get('deferred_outgoing')):
                        message.pop('deferred_outgoing', None)
                        if message['id'] != op['message_id'] and conversation:
                            conversation.update(human_owned=True, pending=False)
                        self.repo.put('message', _key(account_id, message['id']), message)
                if conversation:
                    self.repo.put('conversation', conversation_key, conversation)
            if op['status'] == 'accepted':
                message_key = _key(account_id, op['message_id'])
                if self.repo.get('message', message_key) is None and isinstance(op.get('body'), str):
                    self.repo.put('message', message_key, {'id': op['message_id'], 'account_id': account_id,
                                  'chat_id': op['chat_id'], 'segment_id': op.get('segment_id'),
                                  'body': op['body'], 'timestamp_ms': time.time_ns() // 1_000_000,
                                  'from_me': True, 'is_history': False, 'is_group': False})
                binding_key = _key(account_id, op['chat_id'])
                binding = self.repo.get('binding', binding_key)
                remaining = any(other['account_id'] == account_id and other['chat_id'] == op['chat_id'] and other['operation_id'] != operation_id and other['status'] in ('prepared','sending','unknown') for other in self.repo.list('operation'))
                if binding and binding.get('requested_binding') is not None and not remaining:
                    self._activate_account_binding(binding_key, binding, binding['requested_binding'],
                                                   binding.get('requested_binding_revision', binding['revision'] + 1))
                batch = self.repo.get('reply_batch', op.get('batch_id')) if op.get('batch_id') else None
                if batch and batch.get('status') != 'stopped' and not was_accepted and batch.get('next_index') == op.get('part_index'):
                    batch['next_index'] += 1
                    contact = self.repo.get('conversation', binding_key)
                    if batch['next_index'] == len(batch['parts']):
                        batch['status'] = 'accepted'
                        if contact and contact.get('pending_message_id') == op.get('pending_message_id'):
                            contact['pending'] = False
                            self.repo.put('conversation', binding_key, contact)
                    elif (self.enabled and self.connected and self.account == account_id and self.epoch == connection_epoch
                          and contact and contact.get('pending') and not contact.get('human_owned')
                          and contact.get('pending_message_id') == op.get('pending_message_id')
                          and binding and binding.get('requested_binding') is None
                          and binding.get('revision') == op.get('binding_revision')
                          and binding.get('segment_id') == op.get('segment_id')):
                        next_operation = self._account_create_part(batch)
                    else:
                        batch['status'] = 'stopped'
                    self.repo.put('reply_batch', batch['batch_id'], batch)
            self.repo.db.execute('COMMIT')
        except BaseException:
            self.repo.db.execute('ROLLBACK')
            raise
        if next_operation:
            self.emit({'event': 'whatsapp.send.requested', 'data': {'operation_id': next_operation}})
        self._changed()
        return {k: op.get(k) for k in ('operation_id','status','message_id','ack')}

    def _wa_ack(self, account_id, connection_epoch, message_id, ack):
        self._identity(account_id, connection_epoch)
        _identifier(message_id)
        if type(ack) is not int:
            _error('invalid_params', '回执无效')
        op = next((o for o in self.repo.list('operation') if o.get('account_id') == account_id and o.get('message_id') == message_id and o.get('submission_started')), None)
        if op:
            return {'matched': True, **self._wa_transport_result(op['operation_id'], account_id, op['connection_epoch'], 'submitted', message_id, ack)}
        key = _key(account_id, message_id)
        previous = self.repo.get('ack', key, {}).get('ack', -1)
        self.repo.put('ack', key, {'ack': max(ack, previous)})
        return {'matched': False}

    def _cancel_account_prepared(self, op):
        with self.repo.transaction():
            return self._cancel_account_prepared_locked(op)

    def _cancel_account_prepared_locked(self, op):
        if (op.get('status') == 'prepared' and op.get('submission_started') is False
                and op.get('owner_run_id') == self._generation_run_id
                and op.get('batch_id') and self.repo.get('reply_batch', op['batch_id'])):
            op['status'] = 'cancelled'
            self.repo.put('operation', op['operation_id'], op)
            batch = self.repo.get('reply_batch', op['batch_id'])
            batch['status'] = 'stopped'
            index = op.get('part_index')
            if type(index) is int and 0 <= index < len(batch.get('parts', [])):
                batch['parts'][index]['status'] = 'cancelled'
            self.repo.put('reply_batch', batch['batch_id'], batch)
            binding_key = _key(op['account_id'], op['chat_id'])
            binding = self.repo.get('binding', binding_key)
            remaining = any(other.get('account_id') == op['account_id'] and other.get('chat_id') == op['chat_id']
                            and other.get('status') in ('prepared', 'sending', 'unknown') for other in self.repo.list('operation'))
            if binding and binding.get('requested_binding') is not None and not remaining:
                self._activate_account_binding(binding_key, binding, binding['requested_binding'],
                                               binding.get('requested_binding_revision', binding['revision'] + 1))

    def _wa_get_context_request(self, operation_id):
        op = self.repo.get('operation', _identifier(operation_id))
        if not op or op.get('status') != 'prepared' or not op.get('merchant_id'):
            _error('operation_unavailable', '发送记录不可用')
        self._identity(op.get('account_id'), op.get('connection_epoch'))
        binding = self.repo.get('binding', _key(op['account_id'], op['chat_id']))
        if (not binding or binding.get('revision') != op.get('binding_revision')
                or binding.get('segment_id') != op.get('segment_id') or binding.get('requested_binding') is not None):
            self._cancel_account_prepared(op)
            _error('source_changed', '商家绑定已变化')
        request = {'request_id': uuid.uuid4().hex, 'account_id': op['account_id'], 'chat_id': op['chat_id'],
                   'merchant_id': op['merchant_id'], 'conversation_id': binding['conversation_id'],
                   'binding_revision': binding['revision']}
        self.context_requests[request['request_id']] = (request.copy(), operation_id)
        self.context_leases.pop(operation_id, None)
        return request

    def _wa_set_merchant_context(self, request, context, error=None):
        if not isinstance(request, dict) or not isinstance(request.get('request_id'), str):
            _error('invalid_params', '商家上下文请求无效')
        if request['request_id'] in self._generation_requests:
            return self._generation_context_result(request, context, error)
        pending = self.context_requests.pop(request['request_id'], None)
        if not pending or pending[0] != request:
            _error('source_changed', '商家上下文请求已变化')
        operation_id = pending[1]
        if error is not None or context is None:
            self.context_leases.pop(operation_id, None)
            operation = self.repo.get('operation', operation_id)
            if operation:
                self._cancel_account_prepared(operation)
            return {'accepted': False}
        self._validate_account_context(request, context)
        operation = self.repo.get('operation', operation_id)
        binding = self.repo.get('binding', _key(request['account_id'], request['chat_id']))
        if (not operation or operation.get('status') != 'prepared' or not binding
                or binding.get('requested_binding') is not None
                or binding['revision'] != request['binding_revision'] or binding['segment_id'] != operation.get('segment_id')):
            _error('source_changed', '商家绑定已变化')
        self.context_leases[operation_id] = context.copy()
        return {'accepted': True}

    def _validate_account_context(self, request, context):
        now = time.time_ns() // 1_000_000
        if (not isinstance(context, dict) or context.get('format') != 'wa-merchant-context-v1'
                or context.get('merchant_id') != request['merchant_id']
                or context.get('conversation_id') != request['conversation_id']
                or any(not isinstance(context.get(k), str) or not context[k] for k in ('seller_id', 'data_root', 'instance_id', 'revision'))
                or type(context.get('observed_at_ms')) is not int or type(context.get('expires_at_ms')) is not int
                or context['observed_at_ms'] > now or context['expires_at_ms'] <= now
                or not 0 < context['expires_at_ms'] - context['observed_at_ms'] <= 10000 or not isinstance(context.get('context'), dict)):
            _error('invalid_context', '商家上下文无效或已过期')

    def _wa_verify_legacy_sources(self, account_id, sources):
        from .whatsapp_account_maintenance import verify_legacy_sources
        with self.repo.lock:
            if not self.connected or account_id != self.account or self._generation_closed:
                _error('source_changed', 'WhatsApp 连接已变化')
            if self._legacy_worker and self._legacy_worker.is_alive():
                _error('maintenance_busy', '旧来源正在核查，请等待完成')
            if not isinstance(sources, list) or len(sources) > 512:
                _error('invalid_params', '旧来源清单无效')
            epoch = self.epoch
            self._cancel_account_generation()
            self.enabled = False
            self.allowed = []
            self._legacy_check_status = 'running'
            job_id = uuid.uuid4().hex
            def verify():
                try:
                    packet = verify_legacy_sources(account_id, sources, self.root / 'legacy-archives')
                    with self.repo.lock:
                        self._identity(account_id, epoch)
                        if self._generation_closed:
                            return
                        self._wa_set_legacy_status(**packet)
                        self._legacy_check_status = None
                except Exception:
                    with self.repo.lock:
                        self._legacy_check_status = 'failed'
                finally:
                    self._changed()
            self._legacy_worker = threading.Thread(target=verify, name='whatsapp-legacy-verification', daemon=True)
            self._legacy_worker.start()
            self._changed()
            return {'job_id': job_id}

    def _wa_set_legacy_status(self, **packet):
        self._cancel_account_generation()
        self.enabled = False
        self.allowed = []
        if (set(packet) != {'format', 'account_id', 'manifest_hash', 'verified_at_ms', 'sources'}
                or packet['format'] != 'wa-legacy-verification-v1' or not self.connected or packet['account_id'] != self.account
                or not isinstance(packet['manifest_hash'], str) or not re.fullmatch(r'[0-9a-f]{64}', packet['manifest_hash'])
                or type(packet['verified_at_ms']) is not int or not 0 < packet['verified_at_ms'] <= time.time_ns() // 1_000_000
                or not isinstance(packet['sources'], list) or len(packet['sources']) > 1000):
            _error('invalid_legacy_manifest', '旧来源核验结果无效')
        ids = set()
        incoming = {}
        for source in packet['sources']:
            if (not isinstance(source, dict) or set(source) != {'source_id', 'status', 'pending_operations'}
                    or source['status'] not in ('verified', 'blocked') or not isinstance(source['pending_operations'], list)):
                _error('invalid_legacy_manifest', '旧来源核验结果无效')
            source_id = _identifier(source['source_id'])
            if source_id in ids:
                _error('invalid_legacy_manifest', '旧来源核验结果重复')
            ids.add(source_id)
            for op in source['pending_operations']:
                if (not isinstance(op, dict) or op.get('account_id') != self.account
                        or op.get('status') not in ('prepared', 'sending', 'unknown')):
                    _error('invalid_legacy_manifest', '旧发送记录无效')
                op_id = _identifier(op.get('operation_id'))
                _identifier(op.get('chat_id'))
                _identifier(op.get('connection_epoch'))
                if op_id in incoming and incoming[op_id] != op:
                    _error('legacy_conflict', '旧发送记录冲突')
                old = self.repo.get('legacy_original', op_id)
                current = self.repo.get('operation', op_id)
                if (old is not None and old != op) or (old is None and current is not None and current != op):
                    _error('legacy_conflict', '旧发送记录冲突')
                incoming[op_id] = op
        previous = self.repo.get('legacy', self.account)
        if previous and not {source['source_id'] for source in previous['sources']}.issubset(ids):
            _error('legacy_sources_missing', '已登记的旧来源未包含在本次核验中')
        self.repo.db.execute('BEGIN IMMEDIATE')
        try:
            for op_id, op in incoming.items():
                if self.repo.get('operation', op_id) is None:
                    material = dict(op)
                    if material.get('legacy_receipt'):
                        material['submission_started'] = True
                    self.repo.put('operation', op_id, material)
                self.repo.put('legacy_original', op_id, op)
            self.repo.put('legacy', self.account, packet)
            self.repo.db.execute('COMMIT')
        except BaseException:
            self.repo.db.execute('ROLLBACK')
            raise
        self._changed()
        return self.get_whatsapp_state()['legacy']

    def _wa_prepare_send(self, operation_id):
        from .model_router import ModelSnapshot
        op = self.repo.get('operation', _identifier(operation_id))
        if not op or op.get('status') != 'prepared' or op.get('submission_started'):
            _error('operation_unavailable', '发送操作不可重复提交')
        self._identity(op.get('account_id'), op.get('connection_epoch'))
        binding = self.repo.get('binding', _key(op['account_id'], op['chat_id']))
        contact = self.repo.get('conversation', _key(op['account_id'], op['chat_id']))
        lease = self.context_leases.get(operation_id)
        now = time.time_ns() // 1_000_000
        other = any(row.get('operation_id') != operation_id and row.get('account_id') == op['account_id']
                    and row.get('chat_id') == op['chat_id'] and row.get('status') in ('prepared', 'sending', 'unknown')
                    for row in self.repo.list('operation'))
        if (not self.enabled or op.get('merchant_id') not in self.allowed or other or not binding or not contact
                or contact.get('human_owned') or contact.get('reception_paused') or not contact.get('pending')
                or contact.get('pending_message_id') != op.get('pending_message_id')
                or binding.get('merchant_id') != op.get('merchant_id') or binding.get('revision') != op.get('binding_revision')
                or binding.get('segment_id') != op.get('segment_id') or binding.get('requested_binding') is not None
                or (op.get('source_fingerprint') is not None and op['source_fingerprint'] != self._account_source_fingerprint(op['account_id'], op['chat_id'], op['segment_id']))
                or not lease or lease.get('revision') != op.get('context_revision')
                or (op.get('context_instance_id') is not None and lease.get('instance_id') != op['context_instance_id'])
                or lease.get('expires_at_ms', 0) <= now):
            self._cancel_account_prepared(op)
            _error('source_changed', '发送依据或授权已变化')
        try:
            snapshot = dict(op['model_snapshot'])
            snapshot['required_capabilities'] = tuple(snapshot['required_capabilities'])
            if self.router is None:
                raise ValueError()
            self.router.assert_current(ModelSnapshot(**snapshot))
            if not isinstance(op['body'], str) or not op['body'].strip() or len(op['body']) > 1200:
                raise ValueError()
        except (KeyError, TypeError, ValueError, ServiceError):
            self._cancel_account_prepared(op)
            _error('source_changed', '模型或发送正文已变化')
        op.update(status='unknown', submission_started=True)
        self.repo.put('operation', operation_id, op)
        self.context_leases.pop(operation_id, None)
        return {k: op[k] for k in ('operation_id', 'account_id', 'connection_epoch', 'chat_id', 'body')}

    def _configure_models(self, **packet):
        from .model_router import ModelRouter
        try:
            digest = hashlib.sha256(json.dumps(packet, sort_keys=True, ensure_ascii=False,
                                               separators=(',', ':'), allow_nan=False).encode()).digest()
            if self.router is None:
                self.router = ModelRouter()
            # Always validate the authority file, even for an identical packet.
            affected = self.router.configure(packet)
        except Exception:
            self.enabled = False
            self.allowed = []
            self._cancel_account_generation()
            self._model_packet_digest = None
            self._changed()
            raise
        reception_changed = 'reception' in affected if isinstance(affected, (set, frozenset)) else digest != self._model_packet_digest
        if reception_changed:
            self.enabled = False
            self.allowed = []
            self._cancel_account_generation()
        self._model_packet_digest = digest
        try:
            self.router.resolve('reception', ('text', 'structure'))
            ready = True
        except (ServiceError, AttributeError):
            ready = False
        self.model = {'ready': ready, 'status': 'ready' if ready else 'unavailable',
                      'message': '' if ready else '接待模型尚未就绪'}
        self._changed()
        return {'configured': True, 'reception_available': ready}

    def _unavailable(self, **kwargs):
        _error('account_service_unavailable', '账号接待功能尚未就绪')




def run_account_cli(args, emit):
    try:
        workspace = str(uuid.UUID(args.workspace_id or ''))
        if workspace != args.workspace_id or not args.data_dir.is_absolute() or '--data-dir' not in sys.argv:
            raise ValueError()
        if (args.seller_id or args.workctl or args.maintenance or args.inspect_launch_owner or args.source or args.destination
                or args.archive or args.backup_dir or args.allow_new):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        emit({'status': 'refused', 'code': 'invalid_arguments', 'message': '账号服务参数无效'})
        return 2
    service = WhatsAppAccountService(Path(args.data_dir), workspace, emit)
    try:
        for line in sys.stdin:
            request_id = None
            try:
                if len(line) > 2_000_000:
                    _error('request_too_large', '请求超过大小限制')
                request = json.loads(line)
                if not isinstance(request, dict) or set(request) - {'id', 'method', 'params'}:
                    _error('invalid_request', '请求结构无效')
                request_id = request.get('id')
                if type(request_id) not in (str, int) or not isinstance(request.get('method'), str):
                    _error('invalid_request', '请求编号或方法无效')
                result = service.call(request['method'], request.get('params', {}))
                emit({'id': request_id, 'result': result})
                if request['method'] == 'shutdown':
                    break
            except (json.JSONDecodeError, UnicodeError):
                emit({'id': request_id, 'error': ServiceError('invalid_json', '请求必须是 UTF-8 JSON', stage='protocol').to_dict()})
            except ServiceError as error:
                emit({'id': request_id, 'error': error.to_dict()})
            except Exception:
                emit({'id': request_id, 'error': ServiceError('internal_error', '账号服务操作未完成', stage='service').to_dict()})
    finally:
        service.close()
    return 0
