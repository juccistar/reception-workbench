"""Frozen ordered replies; channel owners retain their single-message receipts."""
from __future__ import annotations

import copy
import hashlib

from .errors import ServiceError
from .repository import encoded, new_id, now_ms
from .send_gate import SendGateDecision

ACTIVE_REPLY_BATCH_STATES = frozenset({'prepared', 'sending', 'waiting_retry', 'waiting_receipt', 'waiting_sync'})
SYNC_WAIT_MS = 60_000


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


def reply_batch_progress(batch, *, retry_at=None):
    rows = batch.get('parts', [])
    current = next((row.get('part_index') for row in rows
                    if row.get('status') not in ('accepted', 'cancelled')), None)
    pending = [row['operation_ids'][-1] for row in rows
               if row.get('status') in ('sending', 'unknown') and row.get('operation_ids')]
    whatsapp = 'chat_id' in batch
    return {'batch_id': batch['batch_id'], 'status': batch.get('status', 'stopped'),
            'origin': batch.get('origin', 'automatic'),
            'channel': batch.get('channel', 'whatsapp' if whatsapp else 'international'),
            'phase': batch.get('phase') or 'reply',
            'created_at': batch.get('created_at'), 'updated_at': batch.get('updated_at'),
            **({'chat_id': batch.get('chat_id')} if whatsapp else {'conversation_id': batch.get('conversation_id')}),
            'parts': [{key: row.get(key) for key in ('part_index', 'status', 'message_id')} for row in rows],
            'message': batch.get('message'),
            'total': len(rows), 'accepted': sum(row.get('status') == 'accepted' for row in rows),
            'current_index': current, 'pending_operation_ids': pending,
            **({'retry_at': retry_at} if type(retry_at) is int and batch.get('status') == 'waiting_retry' else {}),
            **{key: batch[key] for key in ('wait_reason', 'sync_wait_started_at', 'sync_wait_deadline_at', 'last_gate_reason')
               if key in batch}}


class ReplyBatchMixin:
    """International automatic replies reuse the ordinary guarded send per part."""

    def _reply_batch_progress(self, batch):
        retry_at = None
        index = batch.get('next_index')
        parts = batch.get('parts', [])
        if batch.get('status') == 'waiting_retry' and type(index) is int and 0 <= index < len(parts):
            child = self.repo.get('draft', parts[index].get('draft_id')) if parts[index].get('draft_id') else None
            retry_at = child.get('retry_after') if child else None
        return reply_batch_progress(batch, retry_at=retry_at)

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
                                       expected_fingerprint=current['context_fingerprint'],
                                       model_snapshot=copy.deepcopy(current.get('model_snapshot')))
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

    def _reply_batch_awaits_echo(self, batch):
        return any(part['status'] == 'accepted' and (not part.get('message_id') or
            self.repo.get('message', encoded([batch['conversation_id'], part['message_id']])) is None)
            for part in batch['parts'])

    def _reply_batch_gate(self, batch, *, excluding=None, check_sync=True):
        # Every hard condition is evaluated before the sole recoverable condition.
        if self._closing:
            return SendGateDecision(False, 'closing')
        if (batch.get('origin') != 'automatic' or batch.get('stopped')
                or batch.get('epoch') != self._epoch or not self._running or not self._auto_send
                or self._manual_only or not self.account_verified
                or batch.get('frozen_scope') != self._frozen_scope):
            return SendGateDecision(False, 'authorization_revoked')
        if self.mode == 'live':
            try:
                self._assert_reception_model(batch.get('model_snapshot'))
            except ServiceError:
                return SendGateDecision(False, 'authorization_revoked')
        case = self.repo.get('case', batch['case_id'])
        conversation = self.repo.get('conversation', batch['conversation_id'])
        parent = self.repo.get('draft', batch['parent_draft_id'])
        if conversation and (conversation.get('human_owned') or case and case['state'] == 'human_owned'):
            return SendGateDecision(False, 'conversation_taken_over')
        if (not case or not conversation or not parent or case['state'] == 'closed'
                or case['seller_id'] != self.seller_id or batch.get('seller_id') != self.seller_id
                or conversation.get('case_id') != case['case_id']
                or parent['revision'] != batch['parent_revision']
                or case['revision'] != batch['expected_case_revision']
                or conversation['revision'] != batch['expected_conversation_revision']
                or self._conversation_unresolved(batch['conversation_id'], excluding=excluding or batch['batch_id'])):
            return SendGateDecision(False, 'basis_changed')
        excluded = excluding or batch['batch_id']
        source = (self._supplement_source(case, excluding=excluded, allow_pending_sync=True)
                  if batch.get('phase') == 'supplement' else
                  self._automatic_source(batch['conversation_id'], excluding=excluded, allow_pending_sync=True))
        if (not source or source['message_id'] != batch['source_message_id']
                or source.get('seller_id') != self.seller_id or source.get('buyer_id') != case['buyer_id']
                or source.get('conversation_id') != batch['conversation_id']
                or conversation.get('seller_id') != self.seller_id or conversation.get('buyer_id') != case['buyer_id']
                or case.get('conversation_id') != batch['conversation_id']
                or not self._in_scope(batch['conversation_id'], excluding=excluded, allow_pending_sync=True)
                or self._stop_requested(source['text'])):
            return SendGateDecision(False, 'source_changed')
        try:
            if self.get_context(case['case_id'])['fingerprint'] != batch['expected_fingerprint']:
                return SendGateDecision(False, 'basis_changed')
        except ServiceError:
            return SendGateDecision(False, 'guard_evaluation_failed')
        if check_sync and (self._message_sync_pending(batch['conversation_id']) or self._reply_batch_awaits_echo(batch)):
            activity = self.repo.get('message_activity', batch['conversation_id'], {})
            return SendGateDecision(False, 'source_sync_pending', {
                'sync_target_version': activity.get('version', 0),
                'sync_target_timestamp': activity.get('latest_timestamp', 0)})
        return SendGateDecision(True)

    def _reply_batch_basis_current(self, batch, *, excluding=None, check_sync=True):
        return self._reply_batch_gate(batch, excluding=excluding, check_sync=check_sync).allowed

    def _reply_batch_send_snapshot(self, batch):
        cid = batch['conversation_id']
        return {'conversation': self.repo.get('conversation', cid),
                'coverage': self.repo.get('coverage', cid),
                'messages': self.repo.list('message', conversation_id=cid)}

    def _reply_batch_refresh_basis(self, batch, *, accepted_self_sync=False):
        """Called only after proving the old frozen basis, never to adopt new input."""
        case = self._require('case', batch['case_id'])
        conversation = self._require('conversation', batch['conversation_id'])
        if batch.get('phase') == 'supplement' and batch['next_index']:
            work = self.repo.get('supplement_followup', batch['conversation_id']) or {}
            previous_part = batch['parts'][batch['next_index'] - 1]
            if (work.get('draft_id') == batch['parent_draft_id'] and work.get('source_message_id') == batch['source_message_id']
                    and previous_part.get('status') == 'accepted' and previous_part.get('message_id')):
                work.update(latest_message_id=previous_part['message_id'], updated_at=now_ms())
                self.repo.put('supplement_followup', batch['conversation_id'], work)
        context = self.get_context(case['case_id'])
        batch.update(expected_case_revision=case['revision'], expected_conversation_revision=conversation['revision'],
                     expected_fingerprint=context['fingerprint'])
        if accepted_self_sync:
            batch['accepted_self_sync'] = {'message_ids': [p['message_id'] for p in batch['parts']
                if p['status'] == 'accepted'], 'observed_at': now_ms()}
        for remaining in batch['parts'][batch['next_index']:]:
            child = self._require('draft', remaining['draft_id'])
            from .context import validate_reply
            validation = validate_reply(child['body'], remaining['reply_sources'], context)
            child.update(source_case_revision=case['revision'], source_conversation_revision=conversation['revision'],
                         context_fingerprint=context['fingerprint'], validation=validation,
                         related_case_versions=self._related_case_versions(child.get('reply_case_ids', [case['case_id']])))
            # Ingest invalidates pending drafts. Only the proven self echo may restore them.
            if child['status'] == 'stale' and validation['ready']:
                child['status'] = 'pending' if remaining['part_index'] == batch['next_index'] else 'planned'
            self.repo.put('draft', child['draft_id'], child, history=True)
        batch['basis_snapshot'] = self._reply_batch_send_snapshot(batch)

    def _reply_batch_rebase_self_sync(self, batch):
        """Project away only transport changes to exact accepted messages."""
        snapshot = batch.get('basis_snapshot')
        if not snapshot or not any(p['status'] == 'accepted' for p in batch['parts']):
            return False
        cid = batch['conversation_id']
        original_rows = {row['message_id']: row for row in snapshot['messages']}
        current_rows = {row['message_id']: row for row in self.repo.list('message', conversation_id=cid)}
        if set(current_rows) != set(original_rows):
            # An accepted receipt may lack a platform timestamp. The first real
            # echo must match that exact operation before the waiting tail moves.
            added = set(current_rows) - set(original_rows)
            if len(added) != 1 or not set(original_rows).issubset(current_rows):
                return False
            mid = next(iter(added))
            part = next((p for p in batch['parts'] if p['status'] == 'accepted' and p.get('message_id') == mid), None)
            operation = (self.repo.get('operation', part['operation_ids'][-1])
                         if part and part.get('operation_ids') else None)
            if (not operation or operation.get('status') != 'accepted' or operation.get('message_id') != mid
                    or operation.get('submission_started') is not True
                    or self._reply_batch_operation_snapshot(batch, operation) is None):
                return False
            result = {'status': 'accepted', 'message_id': mid, 'submission_started': True}
            if not self._reply_batch_echo_basis_current(batch, operation, result, snapshot, check_sync=False):
                return False
            self._reply_batch_refresh_basis(batch, accepted_self_sync=True)
            self.repo.put('reply_batch', batch['batch_id'], batch, history=True)
            return True
        accepted = {p['message_id']: p for p in batch['parts'] if p['status'] == 'accepted' and p.get('message_id')}
        changed = []
        for mid, original in original_rows.items():
            current = current_rows[mid]
            if current == original:
                continue
            part = accepted.get(mid)
            if (not part or any(current.get(k) != v for k, v in {
                    'message_id': mid, 'seller_id': self.seller_id, 'buyer_id': batch['buyer_id'],
                    'conversation_id': cid, 'role': 'seller', 'text': part['body']}.items())
                    or {k: v for k, v in current.items() if k != 'timestamp_ms'}
                    != {k: v for k, v in original.items() if k != 'timestamp_ms'}):
                return False
            changed.append(mid)
        if not changed:
            return False
        def order(rows):
            return [r['message_id'] for r in sorted(rows.values(), key=lambda r: (
                r['timestamp_ms'] if r['timestamp_ms'] is not None else -1, r['message_id']))]
        if order(original_rows) != order(current_rows):
            return False
        original = snapshot['conversation']
        conversation = self.repo.get('conversation', cid)
        last = current_rows[order(current_rows)[-1]]
        if (not conversation or conversation['revision'] <= original['revision']
                or conversation != {**original, 'revision': conversation['revision'],
                                    'last_message': last['text'], 'last_timestamp': last['timestamp_ms']}):
            return False
        old_coverage, current_coverage = snapshot['coverage'], self.repo.get('coverage', cid)
        ignored = {'earliest_ms', 'latest_ms', 'updated_at', 'message'}
        if (not old_coverage or not current_coverage or
                {k: v for k, v in old_coverage.items() if k not in ignored}
                != {k: v for k, v in current_coverage.items() if k not in ignored}):
            return False
        stamps = [r['timestamp_ms'] for r in current_rows.values() if r['timestamp_ms'] is not None]
        if (current_coverage.get('earliest_ms') != (min(stamps) if stamps else None)
                or current_coverage.get('latest_ms') != (max(stamps) if stamps else None)):
            return False
        # The service lock + savepoint make this a read-only projection. All
        # cases, policies, supplements, media and source checks remain current.
        self.repo.db.execute('SAVEPOINT reply_self_sync')
        try:
            for mid in changed:
                self.repo.put('message', encoded([cid, mid]), original_rows[mid])
            self.repo.put('conversation', cid, original)
            self.repo.put('coverage', cid, old_coverage)
            valid = self._reply_batch_basis_current(batch, check_sync=False)
        finally:
            self.repo.db.execute('ROLLBACK TO reply_self_sync')
            self.repo.db.execute('RELEASE reply_self_sync')
        if valid:
            self._reply_batch_refresh_basis(batch, accepted_self_sync=True)
            self.repo.put('reply_batch', batch['batch_id'], batch, history=True)
        return valid

    def _wait_reply_batch_sync(self, batch):
        part = batch['parts'][batch['next_index']]
        stamp = now_ms()
        started = part.setdefault('sync_wait_started_at', stamp)
        deadline = part.setdefault('sync_wait_deadline_at', started + SYNC_WAIT_MS)
        if stamp >= deadline:
            batch['last_gate_reason'] = 'source_sync_timeout'
            self._stop_reply_batch(batch, '会话同步等待已超过60秒，尚未提交的回复已停止')
            return False
        activity = self.repo.get('message_activity', batch['conversation_id'], {})
        changed = batch.get('status') != 'waiting_sync'
        part['status'] = 'waiting_sync'
        batch.update(status='waiting_sync', wait_reason='source_sync_pending', last_gate_reason='source_sync_pending',
                     sync_wait_started_at=started, sync_wait_deadline_at=deadline,
                     sync_target_version=activity.get('version', 0), sync_target_timestamp=activity.get('latest_timestamp'),
                     message=f"已发送{batch['next_index']}/{len(batch['parts'])}条，正在同步会话，第{batch['next_index'] + 1}条尚未提交")
        if changed:
            batch['updated_at'] = stamp
            self.repo.put('reply_batch', batch['batch_id'], batch, history=True)
            self._changed(batch['conversation_id'], batch['case_id'])
        if self._monitor_ready():
            self._dispatch_message_syncs(audit=False)
        return True

    def _reply_batch_preflight_wait(self, draft):
        if not draft.get('batch_id'):
            return None
        batch = self._require('reply_batch', draft['batch_id'])
        decision = self._reply_batch_gate(batch)
        if decision.reason == 'basis_changed' and self._reply_batch_rebase_self_sync(batch):
            decision = self._reply_batch_gate(batch)
        if decision.reason == 'source_sync_pending':
            self._wait_reply_batch_sync(batch)
            return {'_job_status': 'deferred', 'batch_id': batch['batch_id'],
                    'message': batch.get('message', '等待会话同步，尚未创建发送操作')}
        return None

    def _reply_batch_operation_snapshot(self, batch, operation):
        record = operation.get('batch_send_basis')
        intent = operation.get('intent')
        part = next((p for p in batch['parts'] if p['part_id'] == operation.get('part_id')), None)
        if (not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1
                or not isinstance(intent, dict) or not part
                or not part.get('operation_ids') or part['operation_ids'][-1] != operation.get('operation_id')
                or operation.get('batch_id') != batch['batch_id'] or operation.get('part_index') != part['part_index']
                or operation.get('seller_id') != self.seller_id or operation.get('buyer_id') != batch['buyer_id']
                or operation.get('conversation_id') != batch['conversation_id']
                or operation.get('case_id') != batch['case_id']
                or operation.get('source_message_id') != batch['source_message_id']
                or intent.get('reply') != part['body']
                or any(record.get(key) != operation.get(key) for key in ('operation_id', 'batch_id', 'part_id'))):
            return None
        saved = self.repo.get('reply_batch_send_basis', operation['operation_id'])
        if not isinstance(saved, dict) or {key: value for key, value in saved.items() if key != 'snapshot'} != record:
            return None
        snapshot = saved.get('snapshot')
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('conversation'), dict)
                or not isinstance(snapshot.get('coverage'), dict) or not isinstance(snapshot.get('messages'), list)
                or type(snapshot['conversation'].get('revision')) is not int
                or any(not isinstance(row, dict) or not isinstance(row.get('message_id'), str)
                       or not row['message_id'].strip() for row in snapshot['messages'])
                or any(snapshot['conversation'].get(key) != batch.get(key)
                       for key in ('seller_id', 'buyer_id', 'conversation_id'))):
            return None
        return snapshot

    def _reply_batch_receipt_basis_current(self, batch, operation, result, snapshot, *, check_sync=False):
        current = self._reply_batch_basis_current(batch, excluding=operation['operation_id'], check_sync=check_sync)
        mid = result.get('message_id')
        if (result.get('status') == 'accepted' and result.get('submission_started') is True
                and isinstance(mid, str) and mid.strip()
                and self.repo.get('message', encoded([batch['conversation_id'], mid])) is not None):
            return bool(snapshot and self._reply_batch_echo_basis_current(
                batch, operation, result, snapshot, check_sync=check_sync))
        return current

    def _reply_batch_unknown_can_wait(self, batch, operation):
        if not operation or operation.get('status') != 'unknown':
            return False
        gate = self._reply_batch_gate(batch, excluding=operation['operation_id'], check_sync=False)
        if gate.allowed:
            return True
        if gate.reason != 'basis_changed':
            return False
        snapshot = self._reply_batch_operation_snapshot(batch, operation)
        if snapshot is None:
            return False
        original_ids = {row['message_id'] for row in snapshot['messages']}
        candidates = [row['message_id'] for row in self.repo.list('message', conversation_id=batch['conversation_id'])
                      if row['message_id'] not in original_ids]
        # This only preserves a blocked unknown operation while its receipt is
        # checked. A matching body never authorizes acceptance or a tail send.
        return len(candidates) == 1 and self._reply_batch_echo_projection_current(
            batch, operation, snapshot, candidates[0], check_sync=False)

    def _reply_batch_echo_basis_current(self, batch, operation, result, snapshot, *, check_sync=True):
        """Compare the original basis after excluding only the exact accepted echo."""
        mid = result.get('message_id')
        if (result.get('status') != 'accepted' or result.get('submission_started') is not True
                or not isinstance(mid, str) or not mid.strip()):
            return False
        return self._reply_batch_echo_projection_current(batch, operation, snapshot, mid, check_sync=check_sync)

    def _reply_batch_echo_projection_current(self, batch, operation, snapshot, mid, *, check_sync):
        """Read-only basis comparison; this alone is not proof of acceptance."""
        cid = batch['conversation_id']
        if not snapshot or check_sync and self._message_sync_pending(cid):
            return False
        key = encoded([cid, mid])
        echo = self.repo.get('message', key)
        if (not echo or echo.get('role') != 'seller' or echo.get('text') != operation['intent']['reply']
                or echo.get('attachments') != []
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
                or current_coverage.get('latest_ms') != (max(stamps) if stamps else None)):
            return False
        # The enclosing service lock and transaction isolate this temporary view;
        # the savepoint is always rolled back, including on validation errors.
        self.repo.db.execute('SAVEPOINT reply_echo_projection')
        try:
            self.repo.db.execute("DELETE FROM entities WHERE kind='message' AND id=?", (key,))
            self.repo.put('conversation', cid, original)
            self.repo.put('coverage', cid, old_coverage)
            return self._reply_batch_basis_current(batch, excluding=operation['operation_id'], check_sync=check_sync)
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
        batch.update(stopped=True, wait_reason=None)
        for part in batch['parts']:
            if part['status'] in ('planned', 'rejected', 'waiting_sync'):
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
                batch.update(status='prepared', wait_reason=None)
                self._reply_batch_refresh_basis(batch)
                if self._message_sync_pending(batch['conversation_id']) or self._reply_batch_awaits_echo(batch):
                    self._wait_reply_batch_sync(batch)
            else:
                self._stop_reply_batch(batch, '原来源或授权已变化，剩余条目已停止')
                return
        elif status == 'unknown':
            batch['status'] = 'waiting_receipt'
            if parent:
                parent.update(status='unknown', updated_at=now_ms())
                self.repo.put('draft', parent['draft_id'], parent, history=True)
        elif status == 'rejected':
            batch['last_gate_reason'] = operation.get('error_code')
            child = self.repo.get('draft', part['draft_id'])
            if child and child['status'] == 'pending' and basis_current and not batch.get('stopped'):
                from .send_retry_policy import decision
                state, _, _ = decision(self.repo, self.seller_id, batch['conversation_id'], batch['source_message_id'],
                                       **self._reply_batch_retry_options(child))
                batch['last_gate_reason'] = operation.get('error_code')
                if state == 'waiting_sync':
                    self._wait_reply_batch_sync(batch)
                else:
                    batch['status'] = 'waiting_retry'
            else:
                from .integrations.live import SNAPSHOT_ERROR_MESSAGES
                if child and child.get('status') == 'exhausted':
                    batch['last_gate_reason'] = 'send_attempts_exhausted'
                reason = ('原条目已达到最多3次发送尝试' if batch.get('last_gate_reason') == 'send_attempts_exhausted'
                          else SNAPSHOT_ERROR_MESSAGES.get(operation.get('error_code'), '本条未提交'))
                self._stop_reply_batch(batch, reason + '；剩余自动回复已停止')
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
                operation = self.repo.get('operation', unknown['operation_ids'][-1]) if unknown['operation_ids'] else None
                if not self._reply_batch_unknown_can_wait(batch, operation):
                    self._stop_reply_batch(batch, '授权或对话已变化，未提交尾部已停止')
                continue
            gate = self._reply_batch_gate(batch)
            if gate.reason == 'basis_changed' and self._reply_batch_rebase_self_sync(batch):
                gate = self._reply_batch_gate(batch)
            index = batch['next_index']
            if not 0 <= index < len(batch['parts']):
                continue
            part = batch['parts'][index]
            if batch['status'] == 'waiting_sync':
                failed_sync = any(j.get('kind') == 'sync_messages' and j.get('status') == 'failed'
                    and (j.get('completed_at') or 0) >= part.get('sync_wait_started_at', 0)
                    for j in self.repo.list('job', conversation_id=batch['conversation_id']))
                if failed_sync or now_ms() >= part['sync_wait_deadline_at']:
                    batch['last_gate_reason'] = 'source_sync_failed' if failed_sync else 'source_sync_timeout'
                    self._stop_reply_batch(batch, '会话同步失败，未提交尾部已停止' if failed_sync else
                                           '会话同步等待已超过60秒，尚未提交的回复已停止')
                    continue
            if gate.reason == 'source_sync_pending':
                self._wait_reply_batch_sync(batch)
                continue
            if not gate.allowed:
                batch['last_gate_reason'] = gate.reason
                self._stop_reply_batch(batch, {'authorization_revoked': '发送授权已撤销，未提交尾部已停止',
                    'conversation_taken_over': '当前会话已由人工接管，未提交尾部已停止',
                    'source_changed': '买家来源已变化，未提交尾部已停止'}.get(gate.reason,
                        '业务依据或对话已变化，剩余条目已停止'))
                continue
            if batch['status'] == 'waiting_sync':
                part['status'] = 'rejected' if part.get('operation_ids') else 'planned'
                batch.update(status='prepared', wait_reason=None, updated_at=now_ms())
                self.repo.put('reply_batch', batch['batch_id'], batch, history=True)
                self._changed(batch['conversation_id'], batch['case_id'])
            child = self._require('draft', part['draft_id'])
            if part['status'] not in ('planned', 'rejected') or (child.get('retry_after') or 0) > now_ms():
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
