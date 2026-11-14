"""Durable reception continuations; no external I/O inside a state transaction.

A plan is a business obligation, an operation is one immutable platform attempt.
Unknown operations are reconciled, never replaced by another submission.
"""
from __future__ import annotations

import copy
import hashlib

from .errors import ServiceError
from .repository import encoded, new_id, now_ms
from .business_supplement import supplement_context
from .send_retry_policy import (decision as send_retry_decision, retry_message as send_retry_message,
                                settle_waiting_jobs)
from .integrations.live import LiveIntegration


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class ContinuityMixin:
    def _hydrate_demo_messages(self):
        # The demo transport's authoritative message list is local too. Restore
        # it from its own persisted demo history, never from a live workspace.
        from .integrations.demo import DemoIntegration
        if self.mode == 'demo' and isinstance(self.integrations, DemoIntegration) and not self.integrations.messages:
            self.integrations.messages = copy.deepcopy(self.repo.list('message'))

    def _draft_basis_current(self, draft):
        """An unsubmitted attempt may wait only while its exact evidence still exists."""
        case = self.repo.get('case', draft['case_id'])
        conversation = self.repo.get('conversation', draft['conversation_id'])
        if (not case or not conversation or case['revision'] != draft['source_case_revision']
                or conversation['revision'] != draft['source_conversation_revision']
                or conversation['case_id'] != case['case_id'] or case['state'] == 'closed'):
            return False
        try:
            self._intent_source(draft, case)
            return self.get_context(case['case_id'])['fingerprint'] == draft['context_fingerprint']
        except ServiceError:
            return False

    def _charge_usage(self, *, ai=False):
        if ai:
            self._ai_budget_used += 1
        else:
            self._budget_used += 1
        self.repo.put('meta', 'reception_usage', {'used': self._budget_used, 'ai_used': self._ai_budget_used})

    def _restore_continuity(self):
        usage = self.repo.get('meta', 'reception_usage', {})
        self._budget_used = max(0, usage.get('used', 0))
        self._ai_budget_used = max(0, usage.get('ai_used', 0))
        for plan in self.repo.list('continuity_plan'):
            operation = self.repo.get('operation', plan.get('operation_id'))
            if operation:
                self._continuity_receipt(operation, {})
            if plan.get('task_id') and plan.get('status') not in ('accepted', 'cancelled'):
                self._followup_state(plan, 'waiting_authorization')

    def _save_assistance_result(self, task_id, expected_revision, result, result_public, schedule_followup, client_mutation_id):
        if (not isinstance(task_id, str) or type(expected_revision) is not int or expected_revision < 1
                or not isinstance(result, str) or not result.strip() or len(result) > 1200
                or type(result_public) is not bool or type(schedule_followup) is not bool
                or not isinstance(client_mutation_id, str) or not 1 <= len(client_mutation_id) <= 200):
            raise ServiceError('invalid_params', '请填写不超过1200字符的有效核实结果')
        self._chat_body(result, sending=True)
        if schedule_followup and not result_public:
            raise ServiceError('invalid_params', '内部结果不能安排对客跟进')
        payload = dict(task_id=task_id, expected_revision=expected_revision, result=result,
                       result_public=result_public, schedule_followup=schedule_followup)
        with self._lock, self.repo.transaction():
            task = self._require('assistance_task', task_id)
            conversation = self._chat_owner(task['conversation_id'])
            case = self._require('case', task['case_id'])
            if (task['seller_id'] != self.seller_id or task['buyer_id'] != conversation['buyer_id']
                    or case['conversation_id'] != task['conversation_id'] or case['buyer_id'] != task['buyer_id']
                    or case['seller_id'] != self.seller_id):
                raise ServiceError('operation_owner_mismatch', '协助任务不属于当前已核验客户')
            saved = self.repo.get('assistance_mutation', client_mutation_id)
            if saved:
                if saved['payload'] != payload:
                    raise ServiceError('mutation_conflict', '操作编号已用于不同结果')
                return saved['response']
            if task['revision'] != expected_revision:
                raise ServiceError('revision_conflict', '协助任务已变化')
            if task.get('resolution') == result and task.get('result_public') is result_public:
                previous = next((p for p in self.repo.list('continuity_plan', conversation_id=task['conversation_id'])
                                 if p.get('task_id') == task_id and p.get('result_version') == task.get('result_version')
                                 and p['status'] != 'cancelled'), None)
                if previous:
                    response = {'task': copy.deepcopy(task), 'followup_id': previous['plan_id'],
                                'followup_status': task['followup_status']}
                    self.repo.put('assistance_mutation', client_mutation_id, {'payload': payload, 'response': response})
                    return response
            was_public = task.get('result_public') is True
            task.update(status='resolved', resolution=result, result_public=result_public,
                        result_version=task.get('result_version', 0) + 1,
                        revision=task['revision'] + 1, updated_at=now_ms(), followup_status='not_scheduled',
                        followup_job_id=None)
            for previous in self.repo.list('continuity_plan', conversation_id=task['conversation_id']):
                if previous.get('task_id') == task_id and previous['status'] == 'pending':
                    previous.update(status='cancelled', updated_at=now_ms())
                    self.repo.put('continuity_plan', previous['plan_id'], previous, history=True)
            self.repo.put('assistance_task', task_id, task, history=True)
            plan = None
            if schedule_followup:
                plan = self._new_continuity_plan(case, None, 'assistance', result, task=task)
                state = self._continuity_wait_reason(plan)
                self._followup_state(plan, state or 'queued')
                task = self._require('assistance_task', task_id)
            # Public verification changes the context. Fence old in-flight results
            # without taking ownership or touching the independently saved chat.
            if result_public or was_public:
                conversation['revision'] += 1
                self.repo.put('conversation', conversation['conversation_id'], conversation)
                self._invalidate(conversation['conversation_id'])
            response = {'task': copy.deepcopy(task), 'followup_id': plan['plan_id'] if plan else None,
                        'followup_status': task['followup_status']}
            self.repo.put('assistance_mutation', client_mutation_id, {'payload': payload, 'response': response})
        self._changed(task['conversation_id'], task['case_id'])
        return response

    def _new_continuity_plan(self, case, source, phase, body, *, task=None, draft=None):
        identity = [self.seller_id, case['conversation_id'], phase,
                    [task['task_id'], task['result_version']] if task else
                    [source['message_id'], text_hash(source['text'])]]
        if phase == 'supplement' and draft:
            identity.append(sorted(draft.get('supplement_issue_ids', [])))
        request_id = 'continue_' + text_hash(encoded(identity))
        # A fresh draft after changed evidence is a new immutable plan. Its
        # transport request remains tied to the same source/phase, so the native
        # ledger can release only a definite unsubmitted rejection.
        if draft:
            identity.append([draft['draft_id'], draft['revision']])
        plan_id = 'continue_' + text_hash(encoded(identity))
        previous = self.repo.get('continuity_plan', plan_id)
        if previous:
            return previous
        plan = dict(plan_id=plan_id, client_request_id=request_id, seller_id=self.seller_id, buyer_id=case['buyer_id'],
                    conversation_id=case['conversation_id'], case_id=case['case_id'], phase=phase,
                    body=body, source_message_id=source['message_id'] if source else None,
                    source_text_sha256=text_hash(source['text']) if source else None,
                    source_timestamp_ms=source['timestamp_ms'] if source else None,
                    status='pending', operation_id=None, attempt=0, next_retry_at=0,
                    created_at=now_ms(), updated_at=now_ms())
        if task:
            plan.update(task_id=task['task_id'], result_version=task['result_version'])
        if draft:
            plan.update(draft_id=draft['draft_id'], draft_revision=draft['revision'])
            if phase == 'supplement':
                plan['supplement_issue_ids'] = sorted(draft.get('supplement_issue_ids', []))
                plan['result_version'] = plan['supplement_issue_ids']
        self.repo.put('continuity_plan', plan_id, plan, history=True)
        return plan

    def _followup_state(self, plan, state, job_id=None):
        if not plan.get('task_id'):
            return
        task = self.repo.get('assistance_task', plan['task_id'])
        if not task or task.get('result_version') != plan.get('result_version'):
            return
        if task.get('followup_status') == state and (job_id is None or task.get('followup_job_id') == job_id):
            return
        task.update(followup_status=state, updated_at=now_ms(), revision=task['revision'] + 1)
        if job_id:
            task['followup_job_id'] = job_id
        self.repo.put('assistance_task', task['task_id'], task, history=True)

    def _supplement_continuation(self, case):
        cid = case['conversation_id']
        work = self.repo.get('supplement_followup', cid) or {}
        conversation = self.repo.get('conversation', cid)
        messages = self._messages(cid)
        source = self._latest_buyer(cid)
        if (work.get('status') not in ('pending', 'generating', 'generated') or not conversation or not source or not messages
                or work.get('case_id') != case.get('case_id') or conversation.get('case_id') != case.get('case_id')
                or any(work.get(k) != conversation.get(k) or case.get(k) != conversation.get(k)
                       for k in ('seller_id', 'buyer_id', 'conversation_id'))
                or work.get('source_message_id') != source['message_id']
                or work.get('source_text_sha256') != text_hash(source['text'])
                or messages[-1].get('role') != 'seller' or messages[-1].get('message_id') != work.get('latest_message_id')
                or supplement_context(self.repo, cid).get('source_id') != work.get('business_note_source_id')
                or not work.get('issue_ids')):
            return None
        return {k: copy.deepcopy(work[k]) for k in ('source_message_id', 'latest_message_id', 'business_note_source_id', 'issue_ids')} | {'phase': 'supplement'}

    def _supplement_source(self, case, *, excluding=None):
        """A selected customer may have an owed issue predating this grant."""
        cid = case['conversation_id']
        scope = self._frozen_scope or {}
        if scope.get('mode') == 'incoming':
            return self._automatic_source(cid, excluding=excluding)
        conversation = self.repo.get('conversation', cid)
        source = self._latest_buyer(cid)
        if (not self._supplement_continuation(case) or not self.account_verified or not conversation
                or scope.get('seller_id') != self.seller_id or cid not in scope.get('conversation_ids', [])
                or self._message_sync_pending(cid) or self._conversation_unresolved(cid, excluding=excluding)
                or not source or type(source.get('timestamp_ms')) is not int
                or any(m.get('role') == 'buyer' and m.get('timestamp_ms') is None for m in self._messages(cid))):
            return None
        return source

    def _generation_continuation(self, case):
        supplement = self._supplement_continuation(case)
        if supplement:
            return supplement
        messages = self._messages(case['conversation_id'])
        source = self._latest_buyer(case['conversation_id'])
        if not messages or not source or messages[-1]['role'] != 'seller':
            return None
        latest = messages[-1]
        for operation in self.repo.list('operation', conversation_id=case['conversation_id']):
            intent = operation.get('intent', {})
            if (operation.get('purpose') == 'continuity' and operation.get('status') == 'accepted'
                    and intent.get('continuity_phase') == 'ack' and operation.get('message_id') == latest['message_id']
                    and operation.get('source_message_id') == source['message_id']
                    and intent.get('source_text_sha256') == text_hash(source['text'])
                    and operation.get('seller_id') == self.seller_id and operation.get('buyer_id') == case['buyer_id']
                    and intent.get('reply') == latest['text']):
                return dict(phase='final', source_message_id=source['message_id'],
                            acknowledgement_message_id=latest['message_id'])
        return None

    def _continuity_scope(self, conversation_id, source, phase):
        scope = self._frozen_scope
        if not scope or scope.get('seller_id') != self.seller_id:
            return False
        if scope['mode'] != 'incoming':
            if conversation_id not in scope.get('conversation_ids', []):
                return False
            # A specifically scheduled, public verification is an outgoing
            # follow-up within the frozen customer scope. It is not a new inbound.
            if phase in ('assistance', 'supplement'):
                return True
        if self.mode == 'live' or scope['mode'] == 'incoming':
            baseline = self._source_baseline.get(conversation_id)
            if (self._started_at_ms is None or source['timestamp_ms'] <= self._started_at_ms
                    or baseline and (source['message_id'] == baseline['message_id']
                    or baseline.get('timestamp_ms') is not None and source['timestamp_ms'] <= baseline['timestamp_ms'])):
                return False
        return True

    def _continuity_wait_reason(self, plan, *, excluding=None):
        conversation = self.repo.get('conversation', plan['conversation_id'])
        case = self.repo.get('case', plan['case_id'])
        if (not conversation or not case or not self.account_verified or plan['seller_id'] != self.seller_id
                or conversation['buyer_id'] != plan['buyer_id'] or case['buyer_id'] != plan['buyer_id']
                or case['seller_id'] != self.seller_id or case['conversation_id'] != plan['conversation_id']):
            return 'waiting_connection'
        source = self._latest_buyer(plan['conversation_id'])
        if (not source or type(source.get('timestamp_ms')) is not int
                or source.get('seller_id') != self.seller_id or source.get('buyer_id') != plan['buyer_id']
                or source.get('conversation_id') != plan['conversation_id']):
            return 'waiting_connection'
        latest = self._messages(plan['conversation_id'])[-1]
        if plan['phase'] == 'ack' and latest['message_id'] != source['message_id']:
            return 'superseded'
        if plan.get('draft_id'):
            draft = self.repo.get('draft', plan['draft_id'])
            if (not draft or draft['revision'] != plan['draft_revision'] or draft['body'] != plan['body']
                    or draft['status'] not in ('pending', 'sending', 'unknown')
                    or draft['source_case_revision'] != case['revision']
                    or draft['source_conversation_revision'] != conversation['revision']):
                return 'superseded'
        if conversation.get('human_owned') or case['state'] == 'human_owned':
            return 'waiting_manual'
        active_case = self.repo.get('case', conversation.get('case_id'))
        if case['state'] == 'closed' or active_case and active_case['state'] == 'closed' or self._stop_requested(source['text']):
            return 'contact_stopped'
        if self._closing or not self._running or not self._auto_send or self._manual_only:
            return 'waiting_authorization'
        if not self._continuity_scope(plan['conversation_id'], source, plan['phase']):
            return 'waiting_authorization'
        if self.mode == 'live' and self.checks['messages']['status'] != 'passed':
            return 'waiting_connection'
        if self._conversation_unresolved(plan['conversation_id'], excluding=excluding):
            return 'waiting_reconciliation'
        if self._message_sync_pending(plan['conversation_id']):
            return 'waiting_connection'
        if plan.get('task_id'):
            task = self.repo.get('assistance_task', plan['task_id'])
            if (not task or not task.get('result_public') or task.get('result_version') != plan['result_version']
                    or task.get('resolution') != plan['body']):
                return 'superseded'
            if source['message_id'] != task.get('source_message_id'):
                answered = any(o['status'] == 'accepted' and o.get('source_message_id') == source['message_id']
                               and o.get('intent', {}).get('continuity_phase') != 'ack'
                               for o in self.repo.list('operation', conversation_id=plan['conversation_id']))
                active_case = self.repo.get('case', conversation.get('case_id'))
                if (not answered and active_case and self._automatic_source(plan['conversation_id'])
                        and self._post_quote_analysis_allowed(active_case, source)):
                    return 'waiting_message_processing'
        elif (plan.get('source_message_id') != source['message_id']
              or plan.get('source_text_sha256') != text_hash(source['text'])):
            return 'superseded'
        return None

    def _queue_continuity_plan(self, params):
        with self._lock, self.repo.transaction():
            if set(params) != {'plan_id'}:
                raise ServiceError('invalid_params', '接待跟进参数无效')
            plan = self._require('continuity_plan', params['plan_id'])
            active = next((j for j in self.repo.list('job', conversation_id=plan['conversation_id'])
                           if j.get('plan_id') == plan['plan_id'] and j['status'] in ('queued', 'running')), None)
            if active:
                return {'job_id': active['job_id']}
            if plan['status'] != 'pending' or plan.get('next_retry_at', 0) > now_ms() or self._continuity_wait_reason(plan):
                raise ServiceError('cancelled', '接待跟进正在等待当前授权或来源')
            source = self._latest_buyer(plan['conversation_id'])
            job = dict(job_id=new_id('job'), kind='continuity_send', plan_id=plan['plan_id'],
                       source_message_id=source['message_id'] if source else None,
                       case_id=plan['case_id'], conversation_id=plan['conversation_id'], automatic=True,
                       status='queued', stage='queued', message='等待发送接待跟进',
                       created_at=now_ms(), started_at=None, completed_at=None)
            self.repo.put('job', job['job_id'], job, history=True)
            self._followup_state(plan, 'queued', job['job_id'])
            self._queue.put((job['job_id'], 'continuity_send', copy.deepcopy(params), self._epoch, True))
        self._emit('job.updated', job)
        return {'job_id': job['job_id']}

    def _system_intent(self, plan, operation_id):
        source = self._latest_buyer(plan['conversation_id'])
        latest = self._messages(plan['conversation_id'])[-1]
        if plan['phase'] == 'ack' and latest['message_id'] != source['message_id']:
            raise ServiceError('source_changed', '当前消息已被回复，无需再确认')
        return dict(purpose='continuity', send_mode='automatic', continuity_phase=plan['phase'],
                    seller_id=self.seller_id, buyer_id=plan['buyer_id'], conversation_id=plan['conversation_id'],
                    case_id=plan['case_id'], draft_id=plan.get('draft_id', plan['plan_id']),
                    revision=plan.get('draft_revision', plan.get('result_version', 1)),
                    operation_id=operation_id, client_request_id=plan.get('client_request_id', plan['plan_id']) + (':' + text_hash(source['message_id'] + source['text'])[:12]
                    if plan['phase'] == 'assistance' else ''),
                    reply=plan['body'], source_message_id=source['message_id'],
                    source_timestamp_ms=source['timestamp_ms'], source_text_sha256=text_hash(source['text']),
                    latest_message_id=latest['message_id'], latest_message_role=latest['role'],
                    latest_text_sha256=text_hash(latest['text']), frozen_scope=copy.deepcopy(self._frozen_scope),
                    budget_reserved=True)

    def _system_authorized(self, operation, plan, epoch, facade):
        current = self.repo.get('operation', operation['operation_id'])
        saved_plan = self.repo.get('continuity_plan', plan['plan_id'])
        if (epoch != self._epoch or facade is not self.integrations or not current or current['status'] != 'sending'
                or current.get('intent') != operation['intent'] or not saved_plan or saved_plan['status'] != 'sending'
                or saved_plan.get('operation_id') != operation['operation_id']
                or self._continuity_wait_reason(saved_plan, excluding=operation['operation_id'])):
            return False
        intent = operation['intent']
        source = self._latest_buyer(plan['conversation_id'])
        latest = self._messages(plan['conversation_id'])[-1]
        if (self._frozen_scope != intent['frozen_scope'] or source['message_id'] != intent['source_message_id']
                or source['timestamp_ms'] != intent['source_timestamp_ms']
                or text_hash(source['text']) != intent['source_text_sha256']
                or latest['message_id'] != intent['latest_message_id'] or latest['role'] != intent['latest_message_role']
                or text_hash(latest['text']) != intent['latest_text_sha256']):
            return False
        if plan.get('draft_id'):
            draft = self.repo.get('draft', plan['draft_id'])
            case = self.repo.get('case', plan['case_id'])
            if (not draft or draft['status'] != 'sending' or draft['revision'] != plan['draft_revision']
                    or draft['body'] != plan['body'] or not case or case['revision'] != draft['source_case_revision']
                    or self._require('conversation', plan['conversation_id'])['revision'] != draft['source_conversation_revision']
                    or self.get_context(plan['case_id'])['fingerprint'] != draft['context_fingerprint']):
                return False
        return True

    def _task_continuity_send(self, params, job_id, epoch, facade):
        try:
            self._ensure_identity(facade)
        except ServiceError:
            with self._lock, self.repo.transaction():
                waiting = self._require('continuity_plan', params['plan_id'])
                waiting['next_retry_at'] = now_ms() + 5000
                self.repo.put('continuity_plan', waiting['plan_id'], waiting)
                self._followup_state(waiting, 'waiting_connection')
            raise
        with self._lock, self.repo.transaction():
            plan = self._require('continuity_plan', params['plan_id'])
            if plan['status'] != 'pending':
                return {'message': '此接待阶段已有操作，未重复提交'}
            if epoch != self._epoch or plan.get('next_retry_at', 0) > now_ms() or self._continuity_wait_reason(plan):
                raise ServiceError('cancelled', '接待跟进权限、来源或重试时间已变化')
            if plan.get('draft_id'):
                batch_draft = self._require('draft', plan['draft_id'])
                if len(batch_draft.get('reply_messages', [])) > 1:
                    return self._begin_reply_batch(batch_draft, self._require('case', plan['case_id']), epoch, job_id, plan=plan)
            source = self._latest_buyer(plan['conversation_id'])
            retry_state, attempts, retry_at = send_retry_decision(
                self.repo, self.seller_id, plan['conversation_id'], source['message_id'],
                plan['phase'], plan.get('result_version'))
            if retry_state in ('blocked', 'exhausted') or retry_state == 'waiting' and retry_at > now_ms():
                plan.update(status=retry_state if retry_state != 'waiting' else 'pending',
                            next_retry_at=retry_at or 0, updated_at=now_ms())
                self.repo.put('continuity_plan', plan['plan_id'], plan, history=True)
                return {'_job_status': 'failed' if retry_state != 'waiting' else 'deferred',
                        'message': send_retry_message(retry_state, attempts, retry_at, now_ms())}
            if plan.get('draft_id'):
                draft, _, _ = self._validate_send(plan['draft_id'], plan['draft_revision'])
                if draft['body'] != plan['body']:
                    raise ServiceError('stale_draft', '跟进正文已变化')
            self._chat_body(plan['body'], sending=True)
            plan['attempt'] += 1
            operation_id = new_id('send')
            intent = self._system_intent(plan, operation_id)
            operation = dict(operation_id=operation_id, job_id=job_id, kind='send', purpose='continuity',
                             plan_id=plan['plan_id'], seller_id=self.seller_id, buyer_id=plan['buyer_id'],
                             case_id=plan['case_id'], case_revision=self._require('case', plan['case_id'])['revision'],
                             conversation_id=plan['conversation_id'], source_message_id=intent['source_message_id'],
                             draft_id=intent['draft_id'], draft_revision=intent['revision'], intent=intent,
                             status='sending', message='等待平台核验接待跟进', message_id=None,
                             submission_started=False, updated_at=now_ms())
            plan.update(status='sending', operation_id=operation_id, updated_at=now_ms())
            self.repo.put('continuity_plan', plan['plan_id'], plan, history=True)
            self.repo.put('operation', operation_id, operation, history=True)
            self._job_update(job_id, operation_id=operation_id, plan_id=plan['plan_id'],
                             source_message_id=intent['source_message_id'])
            if plan.get('draft_id'):
                draft.update(status='sending', operation_id=operation_id, updated_at=now_ms())
                self.repo.put('draft', draft['draft_id'], draft, history=True)
            self._followup_state(plan, 'sending', job_id)
        def authorization():
            with self._lock:
                return self._system_authorized(operation, plan, epoch, facade)
        result = dict(status='rejected', submission_started=False)
        if authorization():
            with self._lock, self.repo.transaction():
                self._charge_usage()
            try:
                result = facade.send_chat(copy.deepcopy(intent), authorization=authorization)
            except Exception:
                result = {}  # The native boundary might already have been crossed.
        status, mid, submitted = self._chat_receipt(result)
        with self._lock, self.repo.transaction():
            failure = LiveIntegration._send_diagnostics(result) if status == 'rejected' else {}
            if status == 'rejected' and not failure:
                failure = {'error_code': 'adapter_error', 'stage': 'adapter',
                           'diagnostics': {'code': 'adapter_error', 'stage': 'adapter'}}
            operation.update(status=status, message_id=mid, submission_started=submitted, updated_at=now_ms(),
                             message={'accepted': '平台已受理接待跟进', 'rejected': '接待跟进在提交前被拒绝',
                                      'unknown': '接待跟进结果待核查，未重新发送'}[status], **failure)
            self.repo.put('operation', operation_id, operation, history=True)
            self._continuity_receipt(operation, result)
        self._changed(plan['conversation_id'], plan['case_id'])
        saved_plan = self._require('continuity_plan', plan['plan_id'])
        job_message = operation['message']
        if status == 'rejected' and saved_plan['status'] != 'cancelled':
            retry_state, attempts, retry_at = send_retry_decision(
                self.repo, self.seller_id, plan['conversation_id'], operation['source_message_id'],
                plan['phase'], plan.get('result_version'))
            job_message = send_retry_message(retry_state, attempts, retry_at, now_ms())
        return dict(_job_status='succeeded' if status == 'accepted' else 'unknown' if status == 'unknown' else
                    'stale' if saved_plan['status'] == 'cancelled' else 'deferred' if saved_plan['status'] == 'pending' else 'failed',
                    operation_id=operation_id, status=status, message=job_message,
                    **({'error': {'code': failure['error_code'], 'message': operation['message']},
                        'stage': failure['stage'], 'diagnostics': failure['diagnostics']} if failure else {}))

    def _continuity_receipt(self, operation, result):
        if operation.get('batch_id'):
            return  # The ordered batch settles the parent only after all parts.
        if operation.get('purpose') != 'continuity':
            return
        plan = self.repo.get('continuity_plan', operation.get('plan_id'))
        if not plan or plan.get('operation_id') != operation['operation_id']:
            return
        status = operation['status']
        was_accepted = plan['status'] == 'accepted'
        if status == 'accepted':
            plan.update(status='accepted', updated_at=now_ms())
            if plan.get('phase') == 'supplement':
                work = self.repo.get('supplement_followup', plan['conversation_id']) or {}
                if work.get('draft_id') == plan.get('draft_id'):
                    work.update(status='complete', updated_at=now_ms())
                    self.repo.put('supplement_followup', plan['conversation_id'], work)
            self._followup_state(plan, 'followed_up')
            draft = self.repo.get('draft', plan.get('draft_id'))
            if draft and draft['revision'] == plan['draft_revision']:
                draft.update(status='accepted', updated_at=now_ms())
                self.repo.put('draft', draft['draft_id'], draft, history=True)
                if not was_accepted:
                    case = self._require('case', plan['case_id'])
                    if not draft.get('social_reply') and case['state'] == 'clarifying':
                        case['state'] = 'waiting_customer'
                    for key in draft.get('question_keys', []):
                        case['questions'][key] = case['questions'].get(key, 0) + 1
                    case.update(revision=case['revision'] + 1, updated_at=now_ms())
                    self.repo.put('case', case['case_id'], case, history=True)
            self._chat_history(operation, result)
            for previous in self.repo.list('job', conversation_id=plan['conversation_id']):
                if (previous['job_id'] != operation.get('job_id') and previous.get('plan_id') == plan['plan_id']
                        and previous['status'] == 'deferred'):
                    self._job_update(previous['job_id'], status='cancelled', retry_at=None, retry_status='completed',
                                     message='后续尝试已受理，原未提交记录保留')
        elif status == 'unknown':
            plan.update(status='unknown', updated_at=now_ms())
            self._followup_state(plan, 'waiting_reconciliation')
            draft = self.repo.get('draft', plan.get('draft_id'))
            if draft and draft['revision'] == plan['draft_revision']:
                draft.update(status='unknown', updated_at=now_ms())
                self.repo.put('draft', draft['draft_id'], draft, history=True)
        elif status == 'rejected' and operation.get('submission_started') is False:
            retry_state, attempts, retry_at = send_retry_decision(
                self.repo, self.seller_id, plan['conversation_id'], operation['source_message_id'],
                plan['phase'], plan.get('result_version'))
            plan.update(status='pending' if retry_state == 'waiting' else retry_state,
                        updated_at=now_ms(), next_retry_at=retry_at or 0)
            self._followup_state(plan, 'queued' if retry_state == 'waiting' else retry_state)
            if operation.get('job_id') and self.repo.get('job', operation['job_id']):
                self._job_update(operation['job_id'], retry_at=retry_at,
                                 retry_attempt=attempts, retry_status=retry_state)
            settle_waiting_jobs(self.repo, self._job_update, self.seller_id,
                                plan['conversation_id'], operation['source_message_id'],
                                plan['phase'], plan.get('result_version'))
            draft = self.repo.get('draft', plan.get('draft_id'))
            if draft and draft['revision'] == plan['draft_revision'] and draft['status'] in ('sending', 'unknown'):
                current = self._draft_basis_current(draft)
                draft.update(status=('pending' if retry_state == 'waiting' else retry_state) if current else 'stale', updated_at=now_ms())
                self.repo.put('draft', draft['draft_id'], draft, history=True)
                if not current:
                    plan.update(status='cancelled', updated_at=now_ms())
                    if operation.get('job_id') and self.repo.get('job', operation['job_id']):
                        self._job_update(operation['job_id'], retry_at=None, retry_status='superseded')
        self.repo.put('continuity_plan', plan['plan_id'], plan, history=True)
        if status in ('accepted', 'unknown'):
            settle_waiting_jobs(self.repo, self._job_update, self.seller_id,
                                plan['conversation_id'], operation['source_message_id'],
                                plan['phase'], plan.get('result_version'))

    def _schedule_continuity(self):
        """Called by the existing idle worker scheduler; never sleeps or spawns writers."""
        jobs = self.repo.list('job')
        active_plans = {j.get('plan_id') for j in jobs if j['status'] in ('queued', 'running')}
        active_receipts = {j.get('operation_id') for j in jobs if j['kind'] == 'reconcile_operation'
                           and j['status'] in ('queued', 'running')}
        for operation in self.repo.list('operation'):
            if (operation['status'] == 'unknown' and operation['operation_id'] not in active_receipts
                    and operation.get('next_reconcile_at', 0) <= now_ms() and self.account_verified):
                try:
                    with self.repo.transaction():
                        operation['reconcile_attempt'] = operation.get('reconcile_attempt', 0) + 1
                        operation['next_reconcile_at'] = now_ms() + min(60000, 2000 * 2 ** min(operation['reconcile_attempt'], 5))
                        self.repo.put('operation', operation['operation_id'], operation)
                        self._enqueue('reconcile_operation', {'operation_id': operation['operation_id']})
                except ServiceError as error:
                    if error.code != 'task_queue_full':
                        raise
        for plan in self.repo.list('continuity_plan'):
            if plan['status'] != 'pending' or plan['plan_id'] in active_plans:
                continue
            reason = self._continuity_wait_reason(plan)
            if reason in ('superseded', 'contact_stopped'):
                plan.update(status='cancelled', updated_at=now_ms())
                self.repo.put('continuity_plan', plan['plan_id'], plan, history=True)
                draft = self.repo.get('draft', plan.get('draft_id'))
                if (draft and draft['revision'] == plan['draft_revision'] and draft['status'] == 'pending'
                        and draft.get('origin') != 'manual' and not self._draft_basis_current(draft)):
                    draft.update(status='stale', updated_at=now_ms())
                    self.repo.put('draft', draft['draft_id'], draft, history=True)
                self._followup_state(plan, 'failed')
                continue
            self._followup_state(plan, reason or 'queued')
            if not reason and plan.get('next_retry_at', 0) <= now_ms():
                try:
                    self._queue_continuity_plan({'plan_id': plan['plan_id']})
                except ServiceError:
                    pass

    def _maybe_acknowledge(self, job, code):
        if (not job.get('automatic') or not self._auto_send or not self._running
                or job.get('retry_attempt', 0) < 2 and code not in
                ('timeout', 'native_timeout', 'connection_error', 'channel_unavailable', 'native_failed')):
            return
        source = self._latest_buyer(job['conversation_id'])
        case = self.repo.get('case', job['case_id'])
        if (not source or not case or source['message_id'] != job['source_message_id']
                or self._messages(job['conversation_id'])[-1]['message_id'] != source['message_id']):
            return
        body = '收到你的消息了。' if any('一' <= c <= '鿿' for c in source['text']) else 'Got your message.'
        self._new_continuity_plan(case, source, 'ack', body)

    def _can_replan_generation(self, job_id):
        job = self.repo.get('job', job_id)
        if not job or job.get('epoch') != self._epoch or self._closing:
            return False
        case = self.repo.get('case', job['case_id'])
        conversation = self.repo.get('conversation', job['conversation_id'])
        source = self._latest_buyer(job['conversation_id'])
        return bool(case and conversation and source and not conversation.get('human_owned')
                    and case['state'] not in ('human_owned', 'closed') and not self._stop_requested(source['text'])
                    and not any(d.get('origin') == 'manual' and d['status'] in ('pending', 'stale')
                                for d in self.repo.list('draft', case_id=case['case_id']))
                    and source['seller_id'] == case['seller_id'] == self.seller_id
                    and source['buyer_id'] == case['buyer_id'] == conversation['buyer_id']
                    and (not job.get('automatic') or self._running and self._in_scope(job['conversation_id'])))

    def _schedule_generation_retries(self):
        jobs = self.repo.list('job')
        active = {j.get('conversation_id') for j in jobs if j['status'] in ('queued', 'running')
                  and j['kind'] in ('generate_draft', 'continuity_send', 'automatic_send')}
        for job in sorted(jobs, key=lambda j: j['created_at'], reverse=True):
            if job['kind'] != 'generate_draft' or job['status'] != 'deferred' or job.get('retry_at') is None:
                continue
            if job.get('retry_status') not in ('waiting', 'replan') or job['conversation_id'] in active:
                continue
            case = self.repo.get('case', job['case_id'])
            conversation = self.repo.get('conversation', job['conversation_id'])
            source = self._latest_buyer(job['conversation_id'])
            superseded = bool(not case or not conversation or not source or conversation.get('human_owned')
                              or case['state'] in ('closed', 'human_owned') or self._stop_requested(source['text'])
                              or job.get('epoch') != self._epoch
                              or job.get('retry_status') != 'replan' and source['message_id'] != job.get('source_message_id'))
            if superseded:
                self._job_update(job['job_id'], status='stale', retry_status='superseded', message='旧来源或权限已变化，等待当前消息规划')
                continue
            if job['retry_at'] > now_ms() or self._conversation_unresolved(job['conversation_id']):
                continue
            if job.get('automatic') and (not self._running or not self._in_scope(job['conversation_id'])):
                continue
            # A queued acknowledgement is sent before attempting the final reply.
            if any(p['conversation_id'] == job['conversation_id'] and p['phase'] == 'ack'
                   and p['status'] in ('pending', 'sending', 'unknown') for p in self.repo.list('continuity_plan')):
                continue
            try:
                with self.repo.transaction():
                    job.update(status='stale', retry_status='requeued', message='已按最新上下文安排下一次生成')
                    self.repo.put('job', job['job_id'], job, history=True)
                    params = {'case_id': case['case_id'], 'expected_case_revision': case['revision']}
                    if job.get('supplement_followup'):
                        work = self.repo.get('supplement_followup', job['conversation_id']) or {}
                        if work.get('job_id') != job['job_id'] or not self._supplement_continuation(case):
                            continue
                        work.update(status='pending', due_at=0)
                        self.repo.put('supplement_followup', job['conversation_id'], work)
                        params['supplement_followup'] = True
                    self._enqueue('generate_draft', params, automatic=job.get('automatic') is True)
                self._emit('job.updated', job)
                active.add(job['conversation_id'])
            except ServiceError:
                # The source can change between inspection and claim; normal
                # scheduling will see it rather than reviving an old request.
                pass
