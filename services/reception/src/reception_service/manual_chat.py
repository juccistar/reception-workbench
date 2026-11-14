"""Conversation-owned handwritten chat, independent of procurement and AI permission."""
from __future__ import annotations

import copy
import unicodedata

from .errors import ServiceError
from .repository import encoded, new_id, now_ms


class ManualChatMixin:
    def _chat_owner(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ServiceError('invalid_params', '会话编号无效')
        conversation = self._require('conversation', conversation_id)
        seller = self.repo.get('meta', 'seller', {}).get('seller_id')
        if (not self.account_verified or not self.seller_id or seller != self.seller_id
                or conversation.get('seller_id', seller) != self.seller_id
                or conversation.get('conversation_id') != conversation_id
                or not isinstance(conversation.get('buyer_id'), str) or not conversation['buyer_id'].strip()):
            raise ServiceError('operation_owner_mismatch', '操作不属于当前已核验卖家及客户')
        return conversation

    def _conversation_unresolved(self, conversation_id, *, excluding=None):
        return any(o['operation_id'] != excluding and o.get('status') in ('prepared', 'sending', 'unknown')
                   for o in self.repo.list('operation', conversation_id=conversation_id))

    def _assert_conversation_resolved(self, conversation_id):
        if self._conversation_unresolved(conversation_id):
            raise ServiceError('send_unresolved', '本会话有发送中或结果未知的操作，请先核对原操作')

    @staticmethod
    def _chat_body(body, *, sending=False):
        if (not isinstance(body, str) or len(body) > 50_000
                or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\r\n' for c in body)
                or sending and not body.strip()):
            raise ServiceError('invalid_params', '聊天正文格式无效、为空或超出 50000 字符')

    def _begin_manual_edit_locked(self, conversation):
        # Only the first ownership change fences in-flight AI and its confirmations.
        # The separate chat revision is never advanced by messages, cases or policies.
        if not conversation.get('human_owned'):
            conversation.update(human_owned=True, revision=conversation['revision'] + 1)
            self._invalidate(conversation['conversation_id'])
        conversation['manual_chat'] = True
        self.repo.put('conversation', conversation['conversation_id'], conversation)

    def begin_manual_edit(self, conversation_id):
        with self._lock, self.repo.transaction():
            conversation = self._chat_owner(conversation_id)
            self._begin_manual_edit_locked(conversation)
        self._changed(conversation_id)
        return self.get_snapshot()

    def save_chat_draft(self, conversation_id, body, expected_revision, client_mutation_id):
        self._chat_body(body)
        if (not isinstance(client_mutation_id, str) or not client_mutation_id.strip()
                or expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1)):
            raise ServiceError('invalid_params', '聊天版本或幂等编号无效')
        request = {'conversation_id': conversation_id, 'body': body, 'expected_revision': expected_revision}
        with self._lock, self.repo.transaction():
            conversation = self._chat_owner(conversation_id)
            mutation = self.repo.get('chat_mutation', client_mutation_id)
            if mutation:
                if mutation['request'] != request:
                    raise ServiceError('mutation_conflict', '同一幂等编号不能保存不同内容')
                return mutation['result']
            previous = self.repo.get('chat_draft', conversation_id)
            if expected_revision != (previous['revision'] if previous else None):
                raise ServiceError('revision_conflict', '聊天草稿版本已变化，请保留输入并重新读取')
            self._begin_manual_edit_locked(conversation)
            stamp = now_ms()
            source = self._latest_buyer(conversation_id)
            case = self.repo.get('case', conversation.get('case_id'))
            revision = max(conversation.get('chat_revision', 0), previous['revision'] if previous else 0) + 1
            draft = {'draft_id': previous['draft_id'] if previous and previous['status'] == 'pending' else new_id('chat'),
                     'purpose': 'chat', 'conversation_id': conversation_id, 'seller_id': self.seller_id,
                     'buyer_id': conversation['buyer_id'], 'revision': revision, 'body': body, 'status': 'pending',
                     'case_id': case['case_id'] if case else None,
                     'source_message_id': source['message_id'] if source else None,
                     'source_case_revision': case['revision'] if case else None,
                     'source_conversation_revision': conversation['revision'], 'origin': 'manual',
                     'created_at': previous['created_at'] if previous else stamp, 'updated_at': stamp,
                     'validation': {'ready': bool(body.strip()), 'issues': [] if body.strip() else ['正文为空'],
                                    'sources': []},
                     'context_fingerprint': '', 'policy_versions': {}}
            conversation['chat_revision'] = revision
            self.repo.put('conversation', conversation_id, conversation)
            self.repo.put('chat_draft', conversation_id, draft, history=True)
            self.repo.put('chat_mutation', client_mutation_id, {'request': request, 'result': draft})
        self._changed(conversation_id)
        return draft

    def _enqueue_chat(self, params):
        required = {'conversation_id', 'draft_id', 'expected_revision', 'client_request_id'}
        if (set(params) != required
                or any(not isinstance(params.get(k), str) or not params[k].strip()
                       for k in ('conversation_id', 'draft_id', 'client_request_id'))
                or type(params.get('expected_revision')) is not int or params['expected_revision'] < 1):
            raise ServiceError('invalid_params', '聊天发送参数无效')
        request = copy.deepcopy(params)
        cid, rid = params['conversation_id'], params['client_request_id']
        with self._lock, self.repo.transaction():
            conversation = self._chat_owner(cid)
            claimed = self.repo.get('chat_request', rid)
            if claimed:
                if claimed['request'] != request:
                    raise ServiceError('request_conflict', '同一请求编号不能发送不同内容或版本')
                return claimed['ack']
            self._executor_health.require()
            if self._closing:
                raise ServiceError('closing', '服务正在关闭')
            self._assert_conversation_resolved(cid)
            draft = self._require('chat_draft', cid)
            if (draft['draft_id'] != params['draft_id'] or draft['revision'] != params['expected_revision']
                    or draft['seller_id'] != self.seller_id or draft['buyer_id'] != conversation['buyer_id']):
                raise ServiceError('revision_conflict', '聊天正文版本或归属已变化')
            if draft['status'] != 'pending':
                raise ServiceError('draft_not_sendable', '请保存新草稿后再发送')
            self._chat_body(draft['body'], sending=True)
            self._begin_manual_edit_locked(conversation)
            operation_id, job_id, stamp = new_id('send'), new_id('job'), now_ms()
            intent = {'seller_id': self.seller_id, 'buyer_id': conversation['buyer_id'], 'conversation_id': cid,
                      'reply': draft['body'], 'draft_id': draft['draft_id'], 'revision': draft['revision'],
                      'client_request_id': rid, 'operation_id': operation_id, 'send_mode': 'manual', 'purpose': 'chat',
                      'case_id': draft.get('case_id'), 'source_message_id': draft.get('source_message_id')}
            operation = {'operation_id': operation_id, 'job_id': job_id, 'kind': 'send', 'purpose': 'chat',
                         'seller_id': self.seller_id, 'buyer_id': conversation['buyer_id'], 'conversation_id': cid,
                         'draft_id': draft['draft_id'], 'draft_revision': draft['revision'],
                         'case_id': draft.get('case_id'), 'case_revision': draft.get('source_case_revision'),
                         'source_message_id': draft.get('source_message_id'), 'client_request_id': rid,
                         'intent': intent, 'status': 'prepared', 'message': '人工消息已认领，等待发送',
                         'message_id': None, 'submission_started': False, 'updated_at': stamp}
            job = {'job_id': job_id, 'kind': 'send_chat', 'conversation_id': cid, 'case_id': draft.get('case_id'),
                   'operation_id': operation_id, 'status': 'queued', 'stage': 'queued', 'message': '等待发送人工消息',
                   'created_at': stamp, 'started_at': None, 'completed_at': None, 'automatic': False}
            ack = {'job_id': job_id}
            draft.update(status='sending', operation_id=operation_id, updated_at=stamp)
            self.repo.put('chat_draft', cid, draft, history=True)
            self.repo.put('operation', operation_id, operation, history=True)
            self.repo.put('job', job_id, job, history=True)
            self.repo.put('chat_request', rid, {'request': request, 'ack': ack, 'operation_id': operation_id})
            # The worker takes the same lock before claiming the job, so a full
            # queue rolls the durable send claim back before it can be observed.
            self._queue.put((job_id, 'send_chat', {'operation_id': operation_id}, self._epoch, False))
            self._chat_facades[operation_id] = self.integrations
        self._emit('job.updated', job)
        self._changed(cid)
        return ack

    def _chat_authorized(self, operation, facade):
        if self._closing or self._executor_health.failed.is_set() or facade is not self.integrations:
            return False
        try:
            conversation = self._chat_owner(operation['conversation_id'])
        except ServiceError:
            return False
        current = self.repo.get('operation', operation['operation_id'])
        draft = self.repo.get('chat_draft', operation['conversation_id'])
        intent = operation['intent']
        original_draft_intact = (draft is not None and draft.get('draft_id') == intent['draft_id']
                                 and draft.get('revision') == intent['revision']
                                 and draft.get('body') == intent['reply']
                                 and draft.get('status') == 'sending'
                                 and draft.get('operation_id') == operation['operation_id'])
        newer_draft = (draft is not None and draft.get('draft_id') != intent['draft_id']
                       and type(draft.get('revision')) is int and draft['revision'] > intent['revision'])
        return (not self._closing and facade is self.integrations
                and self._chat_facades.get(operation['operation_id']) is facade
                and current is not None and current.get('status') == 'sending'
                and current.get('intent') == intent
                and current.get('seller_id') == intent['seller_id'] == self.seller_id
                and current.get('buyer_id') == intent['buyer_id'] == conversation['buyer_id']
                and current.get('draft_id') == intent['draft_id']
                and current.get('draft_revision') == intent['revision']
                and (original_draft_intact or newer_draft)
                and draft.get('seller_id') == self.seller_id and draft.get('buyer_id') == conversation['buyer_id']
                and not self._conversation_unresolved(operation['conversation_id'], excluding=operation['operation_id']))

    @staticmethod
    def _chat_receipt(result):
        result = result if isinstance(result, dict) else {}
        mid = result.get('message_id')
        if (result.get('status') == 'accepted' and isinstance(mid, str) and mid.strip()
                and result.get('submission_started') is True):
            return 'accepted', mid, True
        if result.get('status') == 'rejected' and result.get('submission_started') is False:
            return 'rejected', None, False
        return 'unknown', None, result.get('submission_started') is not False

    def _sync_chat_draft_status(self, operation):
        draft = self.repo.get('chat_draft', operation['conversation_id'])
        if (draft and draft.get('operation_id') == operation['operation_id']
                and draft['draft_id'] == operation['draft_id'] and draft['revision'] == operation['draft_revision']
                and draft['body'] == operation.get('intent', {}).get('reply')):
            draft.update(status=operation['status'], updated_at=now_ms())
            self.repo.put('chat_draft', operation['conversation_id'], draft, history=True)

    def _chat_history(self, operation, result):
        # A real receipt without a platform timestamp is not a dated platform
        # message. A later normal sync can import it with authoritative metadata.
        stamp = result.get('timestamp_ms')
        if type(stamp) is not int or stamp < 0:
            if self.mode != 'demo':
                return
            stamp = now_ms()
        cid = operation['conversation_id']
        try:
            conversation = self._chat_owner(cid)
        except ServiceError:
            return  # Keep the authoritative receipt even after ownership changed.
        if conversation['buyer_id'] != operation['buyer_id'] or self.seller_id != operation['seller_id']:
            return
        message = {'message_id': operation['message_id'], 'seller_id': operation['seller_id'],
                   'buyer_id': operation['buyer_id'], 'conversation_id': cid, 'role': 'seller',
                   'text': operation['intent']['reply'], 'timestamp_ms': stamp, 'attachments': [], 'own': True}
        # Insert only; do not replace a richer message already imported from the platform.
        key = encoded([cid, message['message_id']])
        if self.repo.get('message', key) is None:
            self.repo.put('message', key, message, history=True)
            ordered = self._messages(cid)
            conversation.update(last_message=ordered[-1]['text'], last_timestamp=ordered[-1]['timestamp_ms'],
                                revision=conversation['revision'] + 1)
            self.repo.put('conversation', cid, conversation)
            self._invalidate(cid)

    def _task_send_chat(self, params, job_id, epoch, facade):
        operation_id = params['operation_id']
        with self._lock, self.repo.transaction():
            operation = self._require('operation', operation_id)
            bound_facade = self._chat_facades.get(operation_id)
            if operation['status'] != 'prepared':
                return self._chat_job_result(operation)
            operation.update(status='sending', updated_at=now_ms())
            self.repo.put('operation', operation_id, operation, history=True)

        def authorized():
            if self._executor_health.failed.is_set():
                return False
            with self._lock:
                return self._chat_authorized(operation, bound_facade)

        # Checks before invoking the facade prove that no submission was attempted
        # when a queued operation has lost its identity or facade authorization.
        result = {'status': 'rejected', 'message_id': None, 'submission_started': False}
        if authorized():
            sender = getattr(bound_facade, 'send_chat', None)
            if callable(sender):
                try:
                    result = sender(copy.deepcopy(operation['intent']), authorization=authorized)
                except Exception:
                    result = {}  # The native boundary may already have been crossed.
        status, mid, submitted = self._chat_receipt(result)
        with self._lock, self.repo.transaction():
            operation.update(status=status, message_id=mid, submission_started=submitted, updated_at=now_ms(),
                             message={'accepted': '平台已受理发送', 'rejected': '发送已明确拒绝',
                                      'unknown': '发送结果未知，请核对原操作；禁止自动重试'}[status])
            self.repo.put('operation', operation_id, operation, history=True)
            self._sync_chat_draft_status(operation)
            if status == 'accepted':
                self._chat_history(operation, result)
            self._chat_facades.pop(operation_id, None)
        self._changed(operation['conversation_id'])
        return self._chat_job_result(operation)

    @staticmethod
    def _chat_job_result(operation):
        status = operation['status']
        return {'_job_status': 'succeeded' if status == 'accepted' else 'failed' if status == 'rejected' else 'unknown',
                'operation_id': operation['operation_id'], 'status': status, 'message_id': operation.get('message_id'),
                'submission_started': operation.get('submission_started'), 'message': operation['message']}
