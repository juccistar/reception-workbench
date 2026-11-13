"""Conversation-local business notes and explicitly requested reply suggestions."""
import copy
import hashlib
import inspect

from .business_supplement import supplement_context
from .business_progress import apply_progress, project_progress
from .errors import ServiceError
from .reception_context import shared_context
from .model_payload import model_json
from .photos import with_direct_photo_reads
from .integrations.generation_metadata import checked_metadata
from .requirement_media import get_product_source_images
from .repository import encoded, new_id, now_ms
from .supplement_attachments import parse_attachment, attachment_rows, public_attachment, MAX_TOTAL, MAX_COUNT


def _strip_product_display(context):
    from .model_payload import _without_product_links_or_images
    case = context.get('case')
    if isinstance(case, dict):
        case.pop('product_main_image', None)
        if isinstance(case.get('requirements'), dict):
            case['requirements'].pop('product_url', None)
    for key in ('product_context', 'supplier_context'):
        if key in context:
            context[key] = _without_product_links_or_images(context[key])
    return context


class ConversationAssistanceMixin:
    def _supplement(self, cid):
        note = self.repo.get('conversation_supplement', cid) or {
            'conversation_id': cid, 'body': '', 'revision': None, 'updated_at': None}
        return {**note, 'attachments': [public_attachment(row) for row in attachment_rows(self.repo, cid)]}

    def upload_supplement_attachment(self, conversation_id, name, content_b64):
        item, original = parse_attachment(name, content_b64)
        with self._lock, self.repo.transaction():
            owner = self._chat_owner(conversation_id)
            rows = attachment_rows(self.repo, conversation_id)
            if len(rows) >= MAX_COUNT or sum(row['size'] for row in rows) + item['size'] > MAX_TOTAL:
                raise ServiceError('attachment_limit', '最多 5 个附件，合计不超过 20 MiB')
            previous = self._supplement(conversation_id)
            item.update(conversation_id=conversation_id, seller_id=owner['seller_id'],
                        buyer_id=owner['buyer_id'], original_b64=original)
            self.repo.put('supplement_attachment', item['id'], item)
            self.repo.put('conversation_supplement', conversation_id, {
                'conversation_id': conversation_id, 'body': previous['body'],
                'revision': (previous['revision'] or 0) + 1, 'updated_at': now_ms()})
            self._invalidate(conversation_id)
            result = self._supplement(conversation_id)
        self._changed(conversation_id)
        return result

    def remove_supplement_attachment(self, conversation_id, attachment_id):
        with self._lock, self.repo.transaction():
            owner = self._chat_owner(conversation_id)
            item = self.repo.get('supplement_attachment', attachment_id)
            if not item or item.get('conversation_id') != conversation_id or item.get('seller_id') != owner['seller_id']:
                raise ServiceError('invalid_attachment', '附件不存在或不属于当前客户')
            previous = self._supplement(conversation_id)
            self.repo.db.execute('DELETE FROM entities WHERE kind=? AND id=?', ('supplement_attachment', attachment_id))
            self.repo.put('conversation_supplement', conversation_id, {
                'conversation_id': conversation_id, 'body': previous['body'],
                'revision': (previous['revision'] or 0) + 1, 'updated_at': now_ms()})
            self._invalidate(conversation_id)
            result = self._supplement(conversation_id)
        self._changed(conversation_id)
        return result

    def save_conversation_supplement(self, conversation_id, body, expected_revision, client_mutation_id):
        self._chat_body(body)
        if (not isinstance(client_mutation_id, str) or not 1 <= len(client_mutation_id.strip()) <= 200
                or expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1)):
            raise ServiceError('invalid_params', '补充版本或保存编号无效')
        request = {'conversation_id': conversation_id, 'body': body, 'expected_revision': expected_revision}
        request_hash = hashlib.sha256(encoded(request).encode('utf-8')).hexdigest()
        with self._lock, self.repo.transaction():
            self._chat_owner(conversation_id)
            previous = self._supplement(conversation_id)
            prior = self.repo.get('supplement_mutation', client_mutation_id)
            if prior:
                if prior.get('request_hash') != request_hash:
                    raise ServiceError('mutation_conflict', '同一保存编号不能用于不同内容')
                if prior.get('revision') != previous['revision']:
                    raise ServiceError('revision_conflict', '业务补充已变化，请保留输入并重新读取')
                return previous
            if previous['revision'] != expected_revision:
                raise ServiceError('revision_conflict', '业务补充已变化，请保留输入并重新读取')
            if previous['body'] == body and previous['revision'] is not None:
                self.repo.put('supplement_mutation', client_mutation_id, {
                    'request_hash': request_hash, 'conversation_id': conversation_id,
                    'revision': previous['revision'], 'updated_at': previous['updated_at']})
                return previous
            owner = self._require('conversation', conversation_id)
            result = {'conversation_id': conversation_id, 'body': body,
                      'revision': (previous['revision'] or 0) + 1, 'updated_at': now_ms()}
            self.repo.put('conversation_supplement', conversation_id, result)
            if body != previous.get('body'):
                owner = self._require('conversation', conversation_id)
                case = self.repo.get('case', owner.get('case_id'))
                source = self._latest_buyer(conversation_id)
                messages = self._messages(conversation_id)
                note_id = supplement_context(self.repo, conversation_id).get('source_id')
                previous_work = self.repo.get('supplement_followup', conversation_id) or {}
                issue_ids = (list(previous_work.get('issue_ids') or []) if source
                             and previous_work.get('source_message_id') == source['message_id']
                             and previous_work.get('status') in ('pending', 'generating', 'generated') else [])
                if not issue_ids and source and note_id:
                    issue_ids = ['supplement_' + hashlib.sha256(encoded([conversation_id, source['message_id'], note_id]).encode('utf-8')).hexdigest()]
                if body.strip() and case and source and messages and messages[-1].get('role') == 'seller' and note_id and issue_ids:
                    self.repo.put('supplement_followup', conversation_id, {
                        'seller_id': owner['seller_id'], 'buyer_id': owner['buyer_id'], 'conversation_id': conversation_id,
                        'case_id': case['case_id'], 'source_message_id': source['message_id'],
                        'source_text_sha256': hashlib.sha256(source['text'].encode('utf-8')).hexdigest(),
                        'latest_message_id': messages[-1]['message_id'], 'business_note_source_id': note_id,
                        'issue_ids': issue_ids[:30], 'status': 'pending', 'due_at': now_ms() + 1500})
                else:
                    self.repo.put('supplement_followup', conversation_id, {'conversation_id': conversation_id, 'status': 'cancelled'})
            self.repo.put('supplement_mutation', client_mutation_id, {
                'request_hash': request_hash, 'conversation_id': conversation_id,
                'revision': result['revision'], 'updated_at': result['updated_at']})
            self._invalidate(conversation_id)
        self._changed(conversation_id)
        return self._supplement(conversation_id)

    def _manual_context(self, cid, case_id, input_body=None, input_revision=None):
        conversation = self._chat_owner(cid)
        case = self._require('case', case_id) if case_id else None
        if case and any(case.get(k) != conversation.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id')):
            raise ServiceError('case_conversation_mismatch', '采购事项不属于当前客户会话')
        context = shared_context(self.repo, conversation, case,
                                 task_mode='polish' if input_body is not None else 'manual_suggestion', settings=self.settings)
        context.update(seller_id=self.seller_id, buyer_id=conversation['buyer_id'], conversation_id=cid,
                       case_id=case_id, case=case, facts=copy.deepcopy(case.get('facts', {})) if case else {},
                       policies=copy.deepcopy(context['knowledge']))
        if case and input_body is None:
            context.update(self._product_context_for_case(case))
        if input_body is not None:
            context['unsent_reply'] = {'body': input_body, 'revision': input_revision}
        self._with_case_catalog_context(context)
        model_json(context)
        semantic = _strip_product_display(copy.deepcopy(context))
        # Follow-up projection is derived from these same sources and may be
        # updated by this suggestion; it is not an independent source edit.
        semantic.pop('followup_context', None)
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

    def generate_manual_suggestion(self, conversation_id, client_request_id, case_id=None,
                                   input_body=None, input_revision=None, input_hash=None):
        if (not isinstance(client_request_id, str) or not 1 <= len(client_request_id.strip()) <= 200
                or case_id is not None and (not isinstance(case_id, str) or not case_id)):
            raise ServiceError('invalid_params', '请求编号或事项无效')
        if input_body is None:
            if input_revision is not None or input_hash is not None:
                raise ServiceError('invalid_params', '润色正文版本无效')
        elif (not isinstance(input_body, str) or not input_body.strip() or len(input_body) > 50000
              or type(input_revision) is not int or input_revision < 1
              or not isinstance(input_hash, str)
              or hashlib.sha256(input_body.encode('utf-8')).hexdigest() != input_hash):
            raise ServiceError('invalid_params', '润色正文或版本无效')
        request = {'conversation_id': conversation_id, 'case_id': case_id,
                   'input_body': input_body, 'input_revision': input_revision, 'input_hash': input_hash}
        with self._lock, self.repo.transaction():
            self._chat_owner(conversation_id)
            prior = self.repo.get('manual_request', client_request_id)
            if prior:
                if prior['request'] != request:
                    raise ServiceError('request_conflict', '同一请求编号不能用于不同参数')
                return {'job_id': prior['job_id']}
            if input_body is not None:
                draft = self.repo.get('chat_draft', conversation_id)
                if (not draft or draft.get('conversation_id') != conversation_id or draft.get('body') != input_body
                        or draft.get('revision') != input_revision):
                    raise ServiceError('source_changed', '当前回复正文已变化，请保存后重新润色')
            context, fingerprint = self._manual_context(conversation_id, case_id, input_body, input_revision)
            if self.mode == 'live' and self.checks['ai']['status'] != 'passed':
                raise ServiceError('ai_capability', '请先检查 AI 接待能力')
            job = {'job_id': new_id('job'), 'kind': 'manual_suggestion', 'conversation_id': conversation_id,
                   'case_id': case_id, 'status': 'queued', 'stage': 'queued', 'message': '等待生成回复建议',
                   'created_at': now_ms(), 'started_at': None, 'completed_at': None, 'automatic': False}
            self.repo.put('job', job['job_id'], job, history=True)
            self.repo.put('manual_request', client_request_id, {'request': request, 'job_id': job['job_id']})
            self.repo.put('manual_latest', conversation_id, {'job_id': job['job_id']})
            self._queue.put((job['job_id'], 'manual_suggestion', {'context': context, 'fingerprint': fingerprint,
                              'request_id': client_request_id, 'input_hash': input_hash}, self._epoch, False))
        self._emit('job.updated', job)
        return {'job_id': job['job_id']}

    def _manual_suggestion(self, cid):
        suggestion = self.repo.get('manual_suggestion', cid)
        if suggestion:
            suggestion.pop('quality_notes', None)
            suggestion.pop('assistance_suggestions', None)
            suggestion.pop('followup_progress', None)
            try:
                revision = suggestion.get('input_revision')
                draft = self.repo.get('chat_draft', cid) if revision is not None else None
                input_current = (revision is None or bool(draft and draft.get('revision') == revision
                    and hashlib.sha256(draft.get('body', '').encode('utf-8')).hexdigest() == suggestion.get('input_hash')))
                _, current = self._manual_context(cid, suggestion.get('case_id'),
                    draft['body'] if input_current and draft else None, revision if input_current else None)
                suggestion['status'] = 'ready' if input_current and current == suggestion['context_fingerprint'] else 'stale'
            except ServiceError:
                suggestion['status'] = 'stale'
        return suggestion

    def _task_manual_suggestion(self, params, job_id, epoch, facade):
        context, fingerprint = params['context'], params['fingerprint']
        cid = context['conversation_id']
        with self._lock:
            case = self.repo.get('case', context.get('case_id')) if context.get('case_id') else None
            has_photos = bool(case and get_product_source_images(self.repo, case, self.repo.list('message', conversation_id=cid)))
        if context.get('unsent_reply') or not has_photos:
            with self._lock:
                token = self._read_token(facade)
                original_draft = copy.deepcopy(self.repo.get('chat_draft', cid))
                original_conversation = copy.deepcopy(self._require('conversation', cid))
                original_messages = copy.deepcopy(self.repo.list('message', conversation_id=cid))
                original_note = copy.deepcopy(self._supplement(cid))
            def guard():
                self._require_current_read(token)
                conversation = self._require('conversation', cid)
                if (epoch != self._epoch or self._closing
                        or any(conversation.get(key) != original_conversation.get(key) for key in
                               ('seller_id', 'buyer_id', 'case_id', 'revision', 'human_owned'))
                        or self.repo.get('chat_draft', cid) != original_draft
                        or self.repo.list('message', conversation_id=cid) != original_messages
                        or self._supplement(cid) != original_note):
                    raise ServiceError('cancelled', '润色来源或权限已变化')
        else:
            with self._job_phase(job_id, 'photo_prepare_ms'):
                guard = self._prepare_direct_photos(cid, job_id, epoch, facade)
        with self._lock:
            guard()
            unsent = context.get('unsent_reply')
            refreshed, refreshed_fingerprint = self._manual_context(
                cid, context['case_id'], unsent['body'] if unsent else None,
                unsent['revision'] if unsent else None)
            before, after = _strip_product_display(copy.deepcopy(context)), _strip_product_display(copy.deepcopy(refreshed))
            for key in ('photo_context', 'image_manifest', 'image_transport', 'message_view'):
                before.pop(key, None)
                after.pop(key, None)
            if before != after:
                return {'_job_status': 'stale', 'message': '建议依据已变化，请重新生成'}
            context, fingerprint = refreshed, refreshed_fingerprint
            photo_inputs = ([] if unsent and not context.get('supplement_image_refs')
                            else self._direct_photo_inputs(context))
        def started():
            with self._lock, self.repo.transaction():
                guard()
                self._charge_usage(ai=True)
        with self._job_phase(job_id, 'identity_ms'):
            self._ensure_identity(facade)
        found_product, catalog_product = None, None
        options = {'on_call_start': started, 'cancel_event': self._generation_cancel_event(job_id)}
        parameters = inspect.signature(facade.generate_manual).parameters
        if 'photo_inputs' in parameters:
            options['photo_inputs'] = photo_inputs
        if 'photo_deadline' in parameters and getattr(guard, 'photo_deadline', None) is not None:
            options['photo_deadline'] = guard.photo_deadline
        with self._job_phase(job_id, 'ai_elapsed_ms'):
            output = facade.generate_manual(context, job_id, **options)
        self._job_update(job_id, diagnostics={'image_reads': len(output.get('verified_image_reads', [])) if isinstance(output, dict) else 0})
        body = output.get('reply') if isinstance(output, dict) else None
        if not isinstance(body, str) or not body.strip() or len(body) > 50000:
            raise ServiceError('invalid_ai_output', '回复建议格式无效', stage='manual_suggestion')
        cid = context['conversation_id']
        with self._lock, self.repo.transaction():
            guard()
            unsent = context.get('unsent_reply')
            draft = self.repo.get('chat_draft', cid) if unsent else None
            input_current = not unsent or bool(draft and draft.get('body') == unsent['body']
                and draft.get('revision') == unsent['revision'])
            _, current = self._manual_context(cid, context['case_id'],
                unsent['body'] if unsent else None, unsent['revision'] if unsent else None)
            if (self._closing or not input_current or current != fingerprint
                    or self.repo.get('manual_latest', cid, {}).get('job_id') != job_id):
                return {'_job_status': 'stale', 'message': '依据或最新请求已变化，建议未发布'}
            suggestion_case_id = context['case_id']
            if not unsent:
                current_case = self._require('case', context['case_id']) if context.get('case_id') else None
                checked_context = with_direct_photo_reads(context, output.get('verified_image_reads', []))
                conversation = self._require('conversation', cid)
                applied = self._apply_requirement_output(conversation, current_case, output, checked_context, job_id)
                self._save_ai_conversation_state(conversation, None, output, checked_context)
                if applied['changed_case_ids'] or applied['pointer_changed']:
                    self._invalidate(cid)
                if len(applied['reply_case_ids']) == 1:
                    suggestion_case_id = applied['reply_case_ids'][0]
                _, fingerprint = self._manual_context(cid, suggestion_case_id)
            suggestion = {'suggestion_id': new_id('suggestion'), 'conversation_id': cid,
                          'case_id': suggestion_case_id, 'body': body, 'request_id': params['request_id'],
                          'mode': 'polish' if unsent else 'draft',
                          'input_revision': unsent['revision'] if unsent else None,
                          'input_hash': params['input_hash'],
                          'context_fingerprint': fingerprint, 'status': 'ready', 'updated_at': now_ms()}
            metadata = checked_metadata(output, with_direct_photo_reads(context, output.get('verified_image_reads', [])), manual=True)
            metadata.pop('assistance_suggestions', None)
            metadata.pop('quality_notes', None)
            suggestion.update(metadata)
            self.repo.put('manual_suggestion', cid, suggestion, history=True)
        self._changed(cid)
        return {'suggestion_id': suggestion['suggestion_id'], 'message': '回复建议已生成'}
