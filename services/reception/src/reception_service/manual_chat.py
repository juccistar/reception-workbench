"""Conversation-owned handwritten chat, independent of procurement and AI permission."""
from __future__ import annotations

import copy
import unicodedata

from .errors import ServiceError
from .repository import encoded, new_id, now_ms
from .reply_batches import freeze_reply_batch


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
        operations = self.repo.list('operation', conversation_id=conversation_id)
        if any(o['operation_id'] != excluding and o.get('status') in ('prepared', 'sending', 'unknown') for o in operations):
            return True
        excluded = next((o for o in operations if o.get('operation_id') == excluding), None)
        excluded_batch = excluded.get('batch_id') if excluded else excluding
        return any(batch.get('status') in ('prepared', 'sending', 'waiting_retry', 'waiting_receipt')
                   and (not excluded_batch or batch.get('batch_id') != excluded_batch)
                   for batch in self.repo.list('reply_batch', conversation_id=conversation_id))

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

    def save_chat_draft(self, conversation_id, body, expected_revision, client_mutation_id, message_parts=None, remaining_batch_id=None):
        self._chat_body(body)
        if message_parts is not None:
            if (not isinstance(message_parts, list) or not 1 <= len(message_parts) <= 3
                    or any(not isinstance(part, str) for part in message_parts)
                    or body != '\n\n'.join(message_parts)):
                raise ServiceError('invalid_params', '分条正文与组合正文不一致')
            for part in message_parts:
                self._chat_body(part)
        if (not isinstance(client_mutation_id, str) or not client_mutation_id.strip()
                or expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1)):
            raise ServiceError('invalid_params', '聊天版本或幂等编号无效')
        request = {'conversation_id': conversation_id, 'body': body, 'expected_revision': expected_revision}
        if message_parts is not None:
            request['message_parts'] = list(message_parts)
        if remaining_batch_id is not None:
            if not isinstance(remaining_batch_id, str) or not remaining_batch_id or message_parts is None:
                raise ServiceError('invalid_params', '剩余回复批次无效')
            request['remaining_batch_id'] = remaining_batch_id
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
            linked_batch_id = (previous.get('blocked_batch_id') or previous.get('batch_id')) if previous else None
            linked_batch = self.repo.get('reply_batch', linked_batch_id) if linked_batch_id else None
            if linked_batch and (linked_batch.get('origin') != 'manual' or linked_batch.get('conversation_id') != conversation_id
                                 or linked_batch.get('seller_id') != self.seller_id or linked_batch.get('buyer_id') != conversation['buyer_id']):
                raise ServiceError('operation_owner_mismatch', '回复批次不属于当前客户')
            if remaining_batch_id is not None:
                accepted = [part['body'] for part in (linked_batch or {}).get('parts', []) if part.get('status') == 'accepted']
                old_parts = previous.get('message_parts') if previous else None
                if (not linked_batch or remaining_batch_id != linked_batch_id or linked_batch.get('status') != 'stopped'
                        or not accepted or len(accepted) >= len(linked_batch['parts'])
                        or any(part.get('status') != 'accepted' for part in linked_batch['parts'][:len(accepted)])
                        or not isinstance(old_parts, list) or len(old_parts) != len(linked_batch['parts']) or old_parts[:len(accepted)] != accepted
                        or message_parts != old_parts[len(accepted):]
                        or any(part.get('status') == 'unknown' for part in linked_batch['parts'])):
                    raise ServiceError('invalid_params', '请保留已发送前缀并先核查原回执，再确认剩余内容')
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
            if linked_batch_id and remaining_batch_id is None:
                draft['blocked_batch_id'] = linked_batch_id
                if linked_batch and linked_batch.get('status') == 'stopped':
                    draft['blocked_accepted_parts'] = [p['body'] for p in linked_batch['parts'] if p.get('status') == 'accepted']
            if message_parts is not None:
                draft['message_parts'] = list(message_parts)
                draft['validation']['ready'] = all(part.strip() for part in message_parts)
                draft['validation']['issues'] = [] if draft['validation']['ready'] else ['正文为空']
            conversation['chat_revision'] = revision
            self.repo.put('conversation', conversation_id, conversation)
            self.repo.put('chat_draft', conversation_id, draft, history=True)
            self.repo.put('chat_mutation', client_mutation_id, {'request': request, 'result': draft})
        self._changed(conversation_id)
        return draft

    def _enqueue_manual_batch_locked(self, conversation, draft, request):
        self._begin_manual_edit_locked(conversation)
        cid, rid = conversation['conversation_id'], request['client_request_id']
        batch = freeze_reply_batch([{'body': body, 'reply_sources': []} for body in draft['message_parts']],
            origin='manual', seller_id=self.seller_id, buyer_id=conversation['buyer_id'], conversation_id=cid,
            draft_id=draft['draft_id'], draft_revision=draft['revision'], draft_body=draft['body'],
            case_id=draft.get('case_id'), source_message_id=draft.get('source_message_id'),
            source_case_revision=draft.get('source_case_revision'), root_request_id=rid, epoch=self._epoch,
            baseline_messages=copy.deepcopy(self._messages(cid)))
        self.repo.put('reply_batch', batch['batch_id'], batch, history=True)
        job, operation = self._manual_batch_enqueue_part(batch, draft)
        ack = {'job_id': job['job_id'], 'batch_id': batch['batch_id']}
        self.repo.put('chat_request', rid, {'request': request, 'ack': ack, 'operation_id': operation['operation_id']})
        self._emit('job.updated', job)
        self._changed(cid)
        return ack

    def _manual_batch_enqueue_part(self, batch, draft):
        index = batch['next_index']
        part = batch['parts'][index]
        cid, rid = batch['conversation_id'], batch['root_request_id']
        request_id = rid if index == 0 else f'{rid}:part:{index}'
        operation_id, job_id, stamp = new_id('send'), new_id('job'), now_ms()
        intent = {'seller_id': batch['seller_id'], 'buyer_id': batch['buyer_id'], 'conversation_id': cid,
                  'reply': part['body'], 'draft_id': batch['draft_id'], 'revision': batch['draft_revision'],
                  'client_request_id': request_id, 'operation_id': operation_id, 'send_mode': 'manual', 'purpose': 'chat',
                  'case_id': batch.get('case_id'), 'source_message_id': batch.get('source_message_id'),
                  'batch_id': batch['batch_id'], 'part_id': part['part_id'], 'part_index': index,
                  'batch_message_hashes': batch['message_hashes'], 'batch_root_request_id': rid}
        operation = {'operation_id': operation_id, 'job_id': job_id, 'kind': 'send', 'purpose': 'chat',
                     'seller_id': batch['seller_id'], 'buyer_id': batch['buyer_id'], 'conversation_id': cid,
                     'draft_id': batch['draft_id'], 'draft_revision': batch['draft_revision'],
                     'case_id': batch.get('case_id'), 'case_revision': batch.get('source_case_revision'),
                     'source_message_id': batch.get('source_message_id'), 'client_request_id': request_id,
                     'batch_id': batch['batch_id'], 'part_index': index, 'intent': intent, 'status': 'prepared',
                     'message': '人工消息已认领，等待发送', 'message_id': None,
                     'submission_started': False, 'updated_at': stamp}
        job = {'job_id': job_id, 'kind': 'send_chat', 'conversation_id': cid, 'case_id': batch.get('case_id'),
               'operation_id': operation_id, 'status': 'queued', 'stage': 'queued', 'message': '等待发送人工消息',
               'created_at': stamp, 'started_at': None, 'completed_at': None, 'automatic': False}
        part['status'] = 'sending'
        part['operation_ids'].append(operation_id)
        batch.update(status='sending', updated_at=stamp)
        if draft and draft.get('draft_id') == batch['draft_id'] and draft.get('revision') == batch['draft_revision']:
            draft.update(status='sending', operation_id=operation_id, batch_id=batch['batch_id'], updated_at=stamp)
            self.repo.put('chat_draft', cid, draft, history=True)
        self.repo.put('reply_batch', batch['batch_id'], batch, history=True)
        self.repo.put('operation', operation_id, operation, history=True)
        self.repo.put('job', job_id, job, history=True)
        self._queue.put((job_id, 'send_chat', {'operation_id': operation_id}, self._epoch, False))
        self._chat_facades[operation_id] = self.integrations
        return job, operation

    def _manual_batch_current(self, batch):
        if self._closing or batch.get('epoch') != self._epoch or self._executor_health.failed.is_set():
            return False
        try:
            conversation = self._chat_owner(batch['conversation_id'])
        except ServiceError:
            return False
        if conversation.get('buyer_id') != batch.get('buyer_id') or not conversation.get('human_owned'):
            return False
        baseline = batch.get('baseline_messages', [])
        baseline_ids = {m.get('message_id') for m in baseline}
        own_ids = {p.get('message_id') for p in batch['parts'] if p.get('status') == 'accepted' and p.get('message_id') not in baseline_ids}
        current = [m for m in self._messages(batch['conversation_id']) if m.get('message_id') not in own_ids]
        return current == baseline

    def _manual_batch_stop(self, batch):
        for part in batch['parts']:
            if part['status'] == 'planned':
                part['status'] = 'cancelled'
        batch.update(status='stopped', updated_at=now_ms())
        self.repo.put('reply_batch', batch['batch_id'], batch, history=True)
        draft = self.repo.get('chat_draft', batch['conversation_id'])
        if draft and (draft.get('batch_id') == batch['batch_id'] or draft.get('blocked_batch_id') == batch['batch_id']):
            draft['blocked_batch_id'] = batch['batch_id']
            draft['blocked_accepted_parts'] = [p['body'] for p in batch['parts'] if p['status'] == 'accepted']
            if draft.get('batch_id') == batch['batch_id'] and draft.get('revision') == batch['draft_revision']:
                draft.update(status='unknown' if any(p['status'] == 'unknown' for p in batch['parts']) else 'rejected', updated_at=now_ms())
            self.repo.put('chat_draft', batch['conversation_id'], draft, history=True)

    def _manual_batch_receipt(self, operation, result=None):
        batch = self.repo.get('reply_batch', operation.get('batch_id')) if operation.get('batch_id') else None
        if not batch or batch.get('origin') != 'manual':
            return
        index = operation.get('part_index')
        if type(index) is not int or not 0 <= index < len(batch['parts']):
            return
        part = batch['parts'][index]
        if operation['operation_id'] not in part.get('operation_ids', []):
            return
        status = operation.get('status')
        if status not in ('accepted', 'unknown', 'rejected') or part.get('status') == 'accepted':
            return
        part.update(status=status, message_id=operation.get('message_id'))
        if status != 'accepted':
            self._manual_batch_stop(batch)
            return
        batch['next_index'] = index + 1
        if batch['next_index'] == len(batch['parts']):
            batch.update(status='accepted', updated_at=now_ms())
            self.repo.put('reply_batch', batch['batch_id'], batch, history=True)
            draft = self.repo.get('chat_draft', batch['conversation_id'])
            if draft and draft.get('batch_id') == batch['batch_id'] and draft.get('revision') == batch['draft_revision']:
                draft.update(status='accepted', updated_at=now_ms())
                self.repo.put('chat_draft', batch['conversation_id'], draft, history=True)
        elif batch.get('status') == 'stopped' or not self._manual_batch_current(batch):
            self._manual_batch_stop(batch)
        else:
            batch.update(status='prepared', updated_at=now_ms())
            self.repo.put('reply_batch', batch['batch_id'], batch, history=True)

    def _schedule_manual_reply_batches(self):
        for batch in self.repo.list('reply_batch'):
            if batch.get('origin') != 'manual' or batch.get('status') != 'prepared':
                continue
            try:
                with self._lock, self.repo.transaction():
                    batch = self.repo.get('reply_batch', batch['batch_id'])
                    if not batch or batch.get('status') != 'prepared':
                        continue
                    if not self._manual_batch_current(batch) or self._conversation_unresolved(batch['conversation_id'], excluding=batch['batch_id']):
                        self._manual_batch_stop(batch)
                        continue
                    job, _ = self._manual_batch_enqueue_part(batch, self.repo.get('chat_draft', batch['conversation_id']))
            except ServiceError as error:
                if error.code == 'task_queue_full':
                    continue
                raise
            self._emit('job.updated', job)
            self._changed(batch['conversation_id'])

    def _recover_manual_reply_batches(self):
        for batch in self.repo.list('reply_batch'):
            if batch.get('origin') == 'manual' and batch.get('status') in ('prepared', 'sending', 'waiting_retry', 'waiting_receipt'):
                for part in batch['parts']:
                    if part.get('status') == 'sending' and part.get('operation_ids'):
                        operation = self.repo.get('operation', part['operation_ids'][-1])
                        if operation and operation.get('status') in ('accepted', 'unknown', 'rejected'):
                            part.update(status=operation['status'], message_id=operation.get('message_id'))
                        else:
                            part['status'] = 'unknown'
                self._manual_batch_stop(batch)

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
            blocked_id = draft.get('blocked_batch_id') or draft.get('batch_id')
            blocked = self.repo.get('reply_batch', blocked_id) if blocked_id else None
            if blocked and blocked.get('status') == 'stopped' and any(part.get('status') == 'accepted' for part in blocked.get('parts', [])):
                raise ServiceError('partial_batch', '此轮已有消息发送，请先确认只保留未发送内容')
            self._chat_body(draft['body'], sending=True)
            parts = draft.get('message_parts')
            if parts is not None:
                for part in parts:
                    self._chat_body(part, sending=True)
                if len(parts) > 1:
                    if len(rid) + len(':part:2') > 200:
                        raise ServiceError('invalid_params', '分条发送请求编号过长')
                    return self._enqueue_manual_batch_locked(conversation, draft, request)
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
        batch = self.repo.get('reply_batch', operation.get('batch_id')) if operation.get('batch_id') else None
        if batch:
            index = operation.get('part_index')
            valid_part = (batch.get('origin') == 'manual' and batch.get('status') == 'sending'
                          and type(index) is int and 0 <= index < len(batch['parts'])
                          and batch['next_index'] == index and batch['parts'][index].get('body') == intent.get('reply')
                          and batch['parts'][index].get('part_id') == intent.get('part_id')
                          and intent.get('seller_id') == batch.get('seller_id') == self.seller_id
                          and intent.get('buyer_id') == batch.get('buyer_id') == conversation.get('buyer_id')
                          and intent.get('conversation_id') == batch.get('conversation_id')
                          and intent.get('batch_message_hashes') == batch.get('message_hashes')
                          and intent.get('batch_root_request_id') == batch.get('root_request_id')
                          and operation['operation_id'] in batch['parts'][index].get('operation_ids', [])
                          and all(p.get('status') == 'accepted' for p in batch['parts'][:index]))
            return bool(valid_part and self._manual_batch_current(batch)
                        and self._chat_facades.get(operation['operation_id']) is facade
                        and current and current.get('status') == 'sending' and current.get('intent') == intent
                        and not self._conversation_unresolved(operation['conversation_id'], excluding=operation['operation_id']))
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
            self._manual_batch_receipt(operation, result)
            self._chat_facades.pop(operation_id, None)
        self._changed(operation['conversation_id'])
        return self._chat_job_result(operation)

    @staticmethod
    def _chat_job_result(operation):
        status = operation['status']
        return {'_job_status': 'succeeded' if status == 'accepted' else 'failed' if status == 'rejected' else 'unknown',
                'operation_id': operation['operation_id'], 'status': status, 'message_id': operation.get('message_id'),
                'submission_started': operation.get('submission_started'), 'message': operation['message']}
