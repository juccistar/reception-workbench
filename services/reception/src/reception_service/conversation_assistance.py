"""Conversation-local business notes and explicitly requested reply suggestions."""
import copy
import hashlib

from .context import applicable_policies
from .errors import ServiceError
from .repository import encoded, new_id, now_ms


class ConversationAssistanceMixin:
    def _supplement(self, cid):
        return self.repo.get('conversation_supplement', cid) or {
            'conversation_id': cid, 'body': '', 'revision': None, 'updated_at': None}

    def save_conversation_supplement(self, conversation_id, body, expected_revision, client_mutation_id):
        self._chat_body(body)
        if (not isinstance(client_mutation_id, str) or not 1 <= len(client_mutation_id.strip()) <= 200
                or expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1)):
            raise ServiceError('invalid_params', '补充版本或保存编号无效')
        request = {'conversation_id': conversation_id, 'body': body, 'expected_revision': expected_revision}
        with self._lock, self.repo.transaction():
            self._chat_owner(conversation_id)
            prior = self.repo.get('supplement_mutation', client_mutation_id)
            if prior:
                if prior['request'] != request:
                    raise ServiceError('mutation_conflict', '同一保存编号不能用于不同内容')
                return prior['result']
            previous = self._supplement(conversation_id)
            if previous['revision'] != expected_revision:
                raise ServiceError('revision_conflict', '业务补充已变化，请保留输入并重新读取')
            result = {'conversation_id': conversation_id, 'body': body,
                      'revision': (previous['revision'] or 0) + 1, 'updated_at': now_ms()}
            self.repo.put('conversation_supplement', conversation_id, result, history=True)
            self.repo.put('supplement_mutation', client_mutation_id, {'request': request, 'result': result})
        self._changed(conversation_id)
        return result

    def _manual_context(self, cid, case_id):
        conversation = self._chat_owner(cid)
        case = self._require('case', case_id) if case_id else None
        if case and any(case.get(k) != conversation.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id')):
            raise ServiceError('case_conversation_mismatch', '采购事项不属于当前客户会话')
        policies = applicable_policies(self.repo.list('policy'), case or {'facts': {}, 'procurement_key': ''})
        messages = sorted((m for m in self.repo.list('message', conversation_id=cid)
                           if all(m.get(k) == conversation.get(k)
                                  for k in ('seller_id', 'buyer_id', 'conversation_id'))),
                          key=lambda m: (m['timestamp_ms'] if m['timestamp_ms'] is not None else -1, m['message_id']))[-20:]
        context = {'seller_id': self.seller_id, 'buyer_id': conversation['buyer_id'], 'conversation_id': cid,
                   'case_id': case_id, 'case': case, 'facts': copy.deepcopy(case.get('facts', {})) if case else {},
                   'recent_messages': messages, 'policies': policies, 'knowledge': policies,
                   'supplement': self._supplement(cid)}
        if len(encoded(context)) > 120000:
            raise ServiceError('context_too_large', '当前资料过长，请精简业务补充', stage='context')
        semantic = copy.deepcopy(context)
        if semantic['case']:
            for field in ('revision', 'updated_at', 'state'):
                semantic['case'].pop(field, None)
            for fact in semantic['case'].get('facts', {}).values():
                if isinstance(fact, dict):
                    fact.pop('display_value', None)
        for fact in semantic['facts'].values():
            if isinstance(fact, dict):
                fact.pop('display_value', None)
        fingerprint = hashlib.sha256(encoded(semantic).encode('utf-8')).hexdigest()
        return context, fingerprint

    def generate_manual_suggestion(self, conversation_id, client_request_id, case_id=None):
        if (not isinstance(client_request_id, str) or not 1 <= len(client_request_id.strip()) <= 200
                or case_id is not None and (not isinstance(case_id, str) or not case_id)):
            raise ServiceError('invalid_params', '请求编号或事项无效')
        request = {'conversation_id': conversation_id, 'case_id': case_id}
        with self._lock, self.repo.transaction():
            self._chat_owner(conversation_id)
            prior = self.repo.get('manual_request', client_request_id)
            if prior:
                if prior['request'] != request:
                    raise ServiceError('request_conflict', '同一请求编号不能用于不同参数')
                return {'job_id': prior['job_id']}
            context, fingerprint = self._manual_context(conversation_id, case_id)
            if self.mode == 'live' and self.checks['ai']['status'] != 'passed':
                raise ServiceError('ai_capability', '请先检查 AI 接待能力')
            job = {'job_id': new_id('job'), 'kind': 'manual_suggestion', 'conversation_id': conversation_id,
                   'case_id': case_id, 'status': 'queued', 'stage': 'queued', 'message': '等待生成回复建议',
                   'created_at': now_ms(), 'started_at': None, 'completed_at': None, 'automatic': False}
            self.repo.put('job', job['job_id'], job, history=True)
            self.repo.put('manual_request', client_request_id, {'request': request, 'job_id': job['job_id']})
            self.repo.put('manual_latest', conversation_id, {'job_id': job['job_id']})
            self._queue.put((job['job_id'], 'manual_suggestion', {'context': context, 'fingerprint': fingerprint,
                              'request_id': client_request_id}, self._epoch, False))
        self._emit('job.updated', job)
        return {'job_id': job['job_id']}

    def _manual_suggestion(self, cid):
        suggestion = self.repo.get('manual_suggestion', cid)
        if suggestion:
            try:
                _, current = self._manual_context(cid, suggestion.get('case_id'))
                suggestion['status'] = 'ready' if current == suggestion['context_fingerprint'] else 'stale'
            except ServiceError:
                suggestion['status'] = 'stale'
        return suggestion

    def _task_manual_suggestion(self, params, job_id, epoch, facade):
        context, fingerprint = params['context'], params['fingerprint']
        def started():
            with self._lock, self.repo.transaction():
                self._charge_usage(ai=True)
        self._ensure_identity(facade)
        output = facade.generate_manual(context, job_id, on_call_start=started, cancel_event=self._stop)
        body = output.get('reply') if isinstance(output, dict) else None
        if not isinstance(body, str) or not body.strip() or len(body) > 50000:
            raise ServiceError('invalid_ai_output', '回复建议格式无效', stage='manual_suggestion')
        cid = context['conversation_id']
        with self._lock, self.repo.transaction():
            _, current = self._manual_context(cid, context['case_id'])
            if self._closing or current != fingerprint or self.repo.get('manual_latest', cid, {}).get('job_id') != job_id:
                return {'_job_status': 'stale', 'message': '依据或最新请求已变化，建议未发布'}
            suggestion = {'suggestion_id': new_id('suggestion'), 'conversation_id': cid,
                          'case_id': context['case_id'], 'body': body, 'request_id': params['request_id'],
                          'context_fingerprint': fingerprint, 'status': 'ready', 'updated_at': now_ms()}
            self.repo.put('manual_suggestion', cid, suggestion, history=True)
        self._changed(cid)
        return {'suggestion_id': suggestion['suggestion_id'], 'message': '回复建议已生成'}
