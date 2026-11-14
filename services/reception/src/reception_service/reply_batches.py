"""Frozen ordered replies; channel owners retain their single-message receipts."""
from __future__ import annotations

import copy
import hashlib

from .errors import ServiceError
from .repository import encoded, new_id, now_ms

ACTIVE_REPLY_BATCH_STATES = frozenset({'prepared', 'sending', 'waiting_retry', 'waiting_receipt'})


def freeze_reply_batch(messages, **owner):
    """Freeze validated bodies before any submission; never split or rewrite text."""
    if not isinstance(messages, list) or not 1 <= len(messages) <= 3:
        raise ServiceError('invalid_reply_messages', '回复条目数量无效')
    rows = []
    for index, message in enumerate(messages):
        body, sources = message.get('body'), message.get('reply_sources', [])
        if (not isinstance(body, str) or not body.strip() or not isinstance(sources, list)
                or any(not isinstance(source, str) for source in sources)):
            raise ServiceError('invalid_reply_messages', '回复条目正文或来源无效')
        rows.append({'part_id': new_id('reply_part'), 'part_index': index,
                     'body': body, 'reply_sources': list(dict.fromkeys(sources)),
                     'body_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
                     'status': 'planned', 'operation_ids': [], 'message_id': None})
    stamp = now_ms()
    identity = [{'body': row['body'], 'reply_sources': row['reply_sources']} for row in rows]
    return {**copy.deepcopy(owner), 'batch_id': owner.get('batch_id') or new_id('reply_batch'),
            'status': 'prepared', 'parts': rows, 'next_index': 0,
            'messages_hash': hashlib.sha256(encoded(identity).encode('utf-8')).hexdigest(),
            'message_hashes': [row['body_sha256'] for row in rows],
            'created_at': stamp, 'updated_at': stamp}


def reply_batch_progress(batch):
    rows = batch.get('parts', [])
    current = next((row.get('part_index') for row in rows
                    if row.get('status') not in ('accepted', 'cancelled')), None)
    pending = [row['operation_ids'][-1] for row in rows
               if row.get('status') in ('sending', 'unknown') and row.get('operation_ids')]
    return {'batch_id': batch['batch_id'], 'status': batch.get('status', 'stopped'),
            'total': len(rows), 'accepted': sum(row.get('status') == 'accepted' for row in rows),
            'current_index': current, 'pending_operation_ids': pending}


class ReplyBatchMixin:
    """International automatic replies reuse the ordinary guarded send per part."""

    def _begin_reply_batch(self, draft, case, epoch, job_id, *, plan=None):
        with self._lock, self.repo.transaction():
            current, case, conversation = self._validate_send(draft['draft_id'], draft['revision'])
            phase = plan['phase'] if plan else 'reply'
            existing = next((row for row in self.repo.list('reply_batch', conversation_id=draft['conversation_id'])
                             if row.get('origin') == 'automatic' and row.get('source_message_id') == draft['source_message_id']
                             and row.get('phase') == phase
                             and (phase != 'supplement' or set(row.get('supplement_issue_ids', []))
                                  & set(draft.get('supplement_issue_ids', [])))), None)
            if existing:
                if existing.get('parent_draft_id') != draft['draft_id']:
                    raise ServiceError('duplicate_send', '原来源已有冻结回复，不能另起批次')
                return {'batch_id': existing['batch_id'], 'message': '原分条回复已保存，未重复提交'}
            from .send_retry_policy import decision
            retry_state, attempts, _ = decision(self.repo, self.seller_id, current['conversation_id'], current['source_message_id'],
                                               phase, current.get('supplement_issue_ids') if phase == 'supplement' else None)
            if retry_state in ('blocked', 'exhausted') or attempts:
                raise ServiceError('send_retry_blocked', '原来源已有发送尝试，不能用新分条重置原链')
            batch = freeze_reply_batch(current['reply_messages'], origin='automatic', channel='international',
                                       seller_id=self.seller_id, buyer_id=case['buyer_id'],
                                       conversation_id=current['conversation_id'], case_id=case['case_id'],
                                       parent_draft_id=current['draft_id'], parent_revision=current['revision'],
                                       source_message_id=current['source_message_id'], epoch=epoch,
                                       frozen_scope=copy.deepcopy(self._frozen_scope), phase=phase,
                                       plan_id=plan['plan_id'] if plan else None,
                                       root_request_id=plan.get('client_request_id', plan['plan_id']) if plan else current['draft_id'],
                                       supplement_issue_ids=current.get('supplement_issue_ids', []),
                                       expected_case_revision=case['revision'],
                                       expected_conversation_revision=conversation['revision'],
                                       expected_fingerprint=current['context_fingerprint'])
            for part in batch['parts']:
                child = copy.deepcopy(current)
                child.update(draft_id=new_id('draft'), body=part['body'], status='planned',
                             batch_id=batch['batch_id'], part_id=part['part_id'], part_index=part['part_index'],
                             parent_draft_id=current['draft_id'], reply_messages=[{'body': part['body'], 'reply_sources': part['reply_sources']}],
                             question_keys=current.get('question_keys', []) if part['part_index'] == len(batch['parts']) - 1 else [])
                child['validation'] = {**child['validation'], 'sources': part['reply_sources']}
                part['draft_id'] = child['draft_id']
                self.repo.put('draft', child['draft_id'], child, history=True)
            current.update(status='sending', reply_batch_id=batch['batch_id'], updated_at=now_ms())
            self.repo.put('draft', current['draft_id'], current, history=True)
            self.repo.put('reply_batch', batch['batch_id'], batch, history=True)
            if plan:
                plan.update(status='sending', batch_id=batch['batch_id'], updated_at=now_ms())
                self.repo.put('continuity_plan', plan['plan_id'], plan, history=True)
        self._changed(batch['conversation_id'], batch['case_id'])
        return {'batch_id': batch['batch_id'], 'message': '分条回复已冻结，等待逐条受理'}

    def _reply_batch_basis_current(self, batch, *, excluding=None):
        if (batch.get('origin') != 'automatic' or batch.get('stopped')
                or batch.get('epoch') != self._epoch or not self._running or not self._auto_send
                or self._closing or self._manual_only or not self.account_verified
                or batch.get('frozen_scope') != self._frozen_scope):
            return False
        case = self.repo.get('case', batch['case_id'])
        conversation = self.repo.get('conversation', batch['conversation_id'])
        parent = self.repo.get('draft', batch['parent_draft_id'])
        if (not case or not conversation or not parent or conversation.get('human_owned')
                or case['state'] in ('closed', 'human_owned') or case['seller_id'] != self.seller_id
                or conversation.get('case_id') != case['case_id']
                or parent['revision'] != batch['parent_revision']
                or case['revision'] != batch['expected_case_revision']
                or conversation['revision'] != batch['expected_conversation_revision']
                or self._conversation_unresolved(batch['conversation_id'], excluding=excluding or batch['batch_id'])):
            return False
        excluded = excluding or batch['batch_id']
        source = (self._supplement_source(case, excluding=excluded) if batch.get('phase') == 'supplement'
                  else self._automatic_source(batch['conversation_id'], excluding=excluded))
        if (not source or source['message_id'] != batch['source_message_id']
                or source.get('seller_id') != self.seller_id or source.get('buyer_id') != case['buyer_id']
                or source.get('conversation_id') != batch['conversation_id']
                or conversation.get('seller_id') != self.seller_id or conversation.get('buyer_id') != case['buyer_id']
                or case.get('conversation_id') != batch['conversation_id']
                or not self._in_scope(batch['conversation_id'], excluding=excluded)
                or self._stop_requested(source['text'])):
            return False
        try:
            return self.get_context(case['case_id'])['fingerprint'] == batch['expected_fingerprint']
        except ServiceError:
            return False

    def _reply_batch_send_snapshot(self, batch):
        cid = batch['conversation_id']
        return {'conversation': self.repo.get('conversation', cid),
                'coverage': self.repo.get('coverage', cid),
                'messages': self.repo.list('message', conversation_id=cid)}

    def _reply_batch_echo_basis_current(self, batch, operation, result, snapshot):
        """Compare the original basis after excluding only the exact accepted echo."""
        cid = batch['conversation_id']
        mid = result.get('message_id')
        if (result.get('status') != 'accepted' or result.get('submission_started') is not True
                or not isinstance(mid, str) or not mid.strip() or self._message_sync_pending(cid)):
            return False
        key = encoded([cid, mid])
        echo = self.repo.get('message', key)
        if (not echo or echo.get('role') != 'seller' or echo.get('text') != operation['intent']['reply']
                or echo.get('seller_id') != self.seller_id or echo.get('buyer_id') != batch['buyer_id']
                or echo.get('conversation_id') != cid or echo.get('message_id') != mid
                or any(row['message_id'] == mid for row in snapshot['messages'])):
            return False
        messages = self.repo.list('message', conversation_id=cid)
        if sorted((encoded(row) for row in messages if row['message_id'] != mid)) != sorted(map(encoded, snapshot['messages'])):
            return False
        conversation = self.repo.get('conversation', cid)
        original = snapshot['conversation']
        if not conversation or not original or conversation.get('revision') != original.get('revision', -1) + 1:
            return False
        ordered = sorted(messages, key=lambda row: (row['timestamp_ms'] if row['timestamp_ms'] is not None else -1, row['message_id']))
        expected = {**original, 'revision': original['revision'] + 1,
                    'last_message': ordered[-1]['text'], 'last_timestamp': ordered[-1]['timestamp_ms']}
        if conversation != expected:
            return False
        current_coverage = self.repo.get('coverage', cid)
        old_coverage = snapshot['coverage']
        if not current_coverage or not old_coverage:
            return False
        ignored = {'earliest_ms', 'latest_ms', 'updated_at', 'message'}
        if ({k: v for k, v in current_coverage.items() if k not in ignored}
                != {k: v for k, v in old_coverage.items() if k not in ignored}):
            return False
        stamps = [row['timestamp_ms'] for row in messages if row['timestamp_ms'] is not None]
        if (current_coverage.get('earliest_ms') != (min(stamps) if stamps else None)
                or current_coverage.get('latest_ms') != (max(stamps) if stamps else None)
                or current_coverage.get('message') != '已读取部分消息'):
            return False
        # The enclosing service lock and transaction isolate this temporary view;
        # the savepoint is always rolled back, including on validation errors.
        self.repo.db.execute('SAVEPOINT reply_echo_projection')
        try:
            self.repo.db.execute("DELETE FROM entities WHERE kind='message' AND id=?", (key,))
            self.repo.put('conversation', cid, original)
            self.repo.put('coverage', cid, old_coverage)
            return self._reply_batch_basis_current(batch, excluding=operation['operation_id'])
        finally:
            self.repo.db.execute('ROLLBACK TO reply_echo_projection')
            self.repo.db.execute('RELEASE reply_echo_projection')

    def _validate_reply_batch_child(self, draft, expected_revision, *, manual=False):
        batch = self._require('reply_batch', draft['batch_id'])
        index = batch['next_index']
        part = batch['parts'][index] if 0 <= index < len(batch['parts']) else {}
        if (manual or draft['revision'] != expected_revision or draft['status'] != 'pending'
                or part.get('draft_id') != draft['draft_id'] or part.get('part_id') != draft.get('part_id')
                or part.get('body') != draft['body'] or part.get('status') not in ('planned', 'rejected')
                or not self._reply_batch_basis_current(batch)):
            raise ServiceError('stale_draft', '分条回复的原来源、顺序或授权已变化')
        context = self.get_context(batch['case_id'])
        from .context import validate_reply
        if not validate_reply(draft['body'], part['reply_sources'], context)['ready']:
            raise ServiceError('validation_blocked', '分条回复正文或来源无效')
        return draft, self._require('case', batch['case_id']), self._require('conversation', batch['conversation_id'])

    def _reply_batch_intent(self, draft, intent):
        batch = self._require('reply_batch', draft['batch_id'])
        part = batch['parts'][draft['part_index']]
        intent.update(batch_id=batch['batch_id'], part_id=part['part_id'], part_index=part['part_index'],
                      batch_message_hashes=batch['message_hashes'], batch_root_request_id=batch['root_request_id'],
                      client_request_id=batch['root_request_id'] + (f":part:{part['part_index']}" if part['part_index'] else ''))
        if batch['phase'] != 'reply':
            from .continuity import text_hash
            source = self._latest_buyer(batch['conversation_id'])
            latest = self._messages(batch['conversation_id'])[-1]
            intent.update(purpose='continuity', continuity_phase=batch['phase'],
                          source_text_sha256=text_hash(source['text']),
                          latest_message_id=latest['message_id'], latest_message_role=latest['role'],
                          latest_text_sha256=text_hash(latest['text']))
        return intent

    def _reply_batch_attempt_started(self, draft, operation):
        if not draft.get('batch_id'):
            return
        batch = self._require('reply_batch', draft['batch_id'])
        part = batch['parts'][draft['part_index']]
        part['operation_ids'].append(operation['operation_id'])
        part['status'] = 'sending'
        operation.update(batch_id=batch['batch_id'], part_id=part['part_id'], part_index=part['part_index'])
        if batch.get('plan_id'):
            operation.update(purpose='continuity', plan_id=batch['plan_id'])
        batch.update(status='sending', updated_at=now_ms())
        self.repo.put('reply_batch', batch['batch_id'], batch, history=True)

    def _reply_batch_retry_options(self, draft):
        if not draft.get('batch_id'):
            return {}
        batch = self._require('reply_batch', draft['batch_id'])
        return {'part_id': draft['part_id'], 'phase': batch['phase'],
                'result_version': batch.get('supplement_issue_ids') if batch['phase'] == 'supplement' else None}

    def _stop_reply_batch(self, batch, reason):
        batch['stopped'] = True
        for part in batch['parts']:
            if part['status'] in ('planned', 'rejected'):
                part['status'] = 'cancelled'
                child = self.repo.get('draft', part.get('draft_id'))
                if child and child['status'] not in ('accepted', 'unknown'):
                    child.update(status='stale', updated_at=now_ms())
                    self.repo.put('draft', child['draft_id'], child, history=True)
        unknown = any(part['status'] in ('sending', 'unknown') for part in batch['parts'])
        batch.update(status='waiting_receipt' if unknown else 'stopped', message=reason, updated_at=now_ms())
        parent = self.repo.get('draft', batch['parent_draft_id'])
        if parent and parent['revision'] == batch['parent_revision']:
            parent.update(status='unknown' if unknown else 'stale', updated_at=now_ms())
            self.repo.put('draft', parent['draft_id'], parent, history=True)
        if batch.get('plan_id'):
            plan = self.repo.get('continuity_plan', batch['plan_id'])
            if plan:
                plan.update(status='unknown' if unknown else 'cancelled', updated_at=now_ms())
                self.repo.put('continuity_plan', plan['plan_id'], plan, history=True)
        self.repo.put('reply_batch', batch['batch_id'], batch, history=True)

    def _reply_batch_receipt(self, operation, result=None, *, basis_current=False):
        if not operation.get('batch_id') or operation.get('purpose') == 'chat':
            return
        batch = self.repo.get('reply_batch', operation['batch_id'])
        if not batch or batch.get('origin') != 'automatic':
            return
        part = next((row for row in batch['parts'] if row['part_id'] == operation.get('part_id')), None)
        if not part or operation['operation_id'] not in part['operation_ids']:
            return
        # A superseded rejected attempt cannot overwrite a later attempt's receipt.
        if part['operation_ids'][-1] != operation['operation_id'] or part['status'] == 'accepted':
            return
        status = operation['status']
        part.update(status=status, message_id=operation.get('message_id'))
        parent = self.repo.get('draft', batch['parent_draft_id'])
        if status == 'accepted':
            batch['next_index'] = part['part_index'] + 1
            if batch['next_index'] == len(batch['parts']):
                batch.update(status='accepted', updated_at=now_ms())
                if parent and parent['revision'] == batch['parent_revision']:
                    parent.update(status='accepted', updated_at=now_ms())
                    self.repo.put('draft', parent['draft_id'], parent, history=True)
                if batch.get('plan_id'):
                    plan = self.repo.get('continuity_plan', batch['plan_id'])
                    if plan:
                        plan.update(status='accepted', updated_at=now_ms())
                        self.repo.put('continuity_plan', plan['plan_id'], plan, history=True)
                        self._followup_state(plan, 'followed_up')
                if batch['phase'] == 'supplement':
                    work = self.repo.get('supplement_followup', batch['conversation_id']) or {}
                    if work.get('draft_id') == batch['parent_draft_id']:
                        work.update(status='complete', updated_at=now_ms())
                        self.repo.put('supplement_followup', batch['conversation_id'], work)
            elif basis_current and not batch.get('stopped'):
                case = self._require('case', batch['case_id'])
                conversation = self._require('conversation', batch['conversation_id'])
                context = self.get_context(case['case_id'])
                batch.update(status='prepared', expected_case_revision=case['revision'],
                             expected_conversation_revision=conversation['revision'], expected_fingerprint=context['fingerprint'])
                for remaining in batch['parts'][batch['next_index']:]:
                    child = self._require('draft', remaining['draft_id'])
                    child.update(source_case_revision=case['revision'], source_conversation_revision=conversation['revision'],
                                 context_fingerprint=context['fingerprint'],
                                 related_case_versions=self._related_case_versions(child.get('reply_case_ids', [case['case_id']])))
                    self.repo.put('draft', child['draft_id'], child, history=True)
            else:
                self._stop_reply_batch(batch, '原来源或授权已变化，剩余条目已停止')
                return
        elif status == 'unknown':
            batch['status'] = 'waiting_receipt'
            if parent:
                parent.update(status='unknown', updated_at=now_ms())
                self.repo.put('draft', parent['draft_id'], parent, history=True)
        elif status == 'rejected':
            child = self.repo.get('draft', part['draft_id'])
            if child and child['status'] == 'pending' and basis_current and not batch.get('stopped'):
                batch['status'] = 'waiting_retry'
            else:
                self._stop_reply_batch(batch, '本条未提交，剩余自动回复已停止')
                return
        batch['updated_at'] = now_ms()
        self.repo.put('reply_batch', batch['batch_id'], batch, history=True)

    def _schedule_reply_batches(self):
        for batch in self.repo.list('reply_batch'):
            if batch.get('origin') != 'automatic' or batch.get('status') not in ACTIVE_REPLY_BATCH_STATES:
                continue
            unknown = next((row for row in batch['parts'] if row['status'] in ('sending', 'unknown')), None)
            if unknown:
                # The exact platform ID is unavailable until an in-flight send
                # returns; receipt handling rechecks every condition before tail.
                if unknown['status'] == 'sending':
                    continue
                if not self._reply_batch_basis_current(batch, excluding=unknown['operation_ids'][-1] if unknown['operation_ids'] else None):
                    self._stop_reply_batch(batch, '授权或对话已变化，未提交尾部已停止')
                continue
            if not self._reply_batch_basis_current(batch):
                self._stop_reply_batch(batch, '授权或对话已变化，剩余条目已停止')
                continue
            index = batch['next_index']
            if not 0 <= index < len(batch['parts']):
                continue
            part = batch['parts'][index]
            child = self._require('draft', part['draft_id'])
            if part['status'] not in ('planned', 'rejected') or child.get('retry_after', 0) > now_ms():
                continue
            if any(job.get('draft_id') == child['draft_id'] and job['status'] in ('queued', 'running')
                   for job in self.repo.list('job', conversation_id=batch['conversation_id'])):
                continue
            from .send_retry_policy import decision
            state, _, retry_at = decision(self.repo, self.seller_id, batch['conversation_id'], batch['source_message_id'],
                                          **self._reply_batch_retry_options(child))
            if state in ('blocked', 'exhausted'):
                self._stop_reply_batch(batch, '原条目的发送尝试已停止，不能换任务重试')
                continue
            if state == 'waiting' and retry_at > now_ms():
                continue
            child.update(status='pending', updated_at=now_ms())
            self.repo.put('draft', child['draft_id'], child)
            try:
                self._enqueue('automatic_send', {'draft_id': child['draft_id'], 'expected_revision': child['revision']}, automatic=True)
            except ServiceError as error:
                if error.code != 'task_queue_full':
                    self._stop_reply_batch(batch, '分条调度条件已变化，剩余条目已停止')

    def _recover_reply_batches(self):
        for batch in self.repo.list('reply_batch'):
            if batch.get('origin') != 'automatic' or batch.get('status') not in ACTIVE_REPLY_BATCH_STATES:
                continue
            for part in batch['parts']:
                if part.get('operation_ids'):
                    operation = self.repo.get('operation', part['operation_ids'][-1])
                    if operation:
                        part.update(status=operation['status'], message_id=operation.get('message_id'))
            self._stop_reply_batch(batch, '程序已重启，未提交条目不会自动续发')
