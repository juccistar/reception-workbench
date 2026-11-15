"""Conservative, evidence-preserving procurement fact operations."""
from __future__ import annotations

import copy
import hashlib
import re

from .repository import new_id, now_ms
from .errors import ServiceError


def buyer_fact_text_allowed(field, text, value=None):
    from .product_catalog import buyer_note
    if value is not None and value.casefold() not in buyer_note(text).casefold():
        return False
    # Buyer messages can quote the seller's commercial terms. Treat those as
    # ambiguous rather than silently converting them into buyer requirements.
    if re.search(r'\b(?:your (?:product|item|goods) (?:is|are|has|have)|you (?:can|offer|provide|said))\b|'
                 r'(?:你方|你们|卖家|供应商).{0,20}(?:产品是|商品是|可以提供|能够提供|说过)', text, re.I):
        return False
    if field not in ('budget', 'delivery'):
        return True
    return not re.search(r'\b(?:your|you|seller|supplier)\b.{0,45}\b(?:quot(?:e|ed|ation)|pric(?:e|ed)|offer(?:ed)?|deliver(?:y|ed)?)\b|'
                         r'(?:你方|你们|卖家|供应商).{0,20}(?:报价|价格|交期|交货)', text, re.I)


def evidence_snapshot(fact, messages):
    ids = fact.get('evidence_ids', [])
    return {
        'candidate_id': new_id('candidate'),
        'value': fact.get('value', ''),
        **({'display_value': fact['display_value']} if fact.get('display_value') else {}),
        'evidence_ids': list(ids),
        'evidence_versions': {mid: hashlib.sha256(messages[mid]['text'].encode('utf-8')).hexdigest()
                              for mid in ids if mid in messages},
        'source_excerpts': {mid: messages[mid]['text'] for mid in ids if mid in messages},
        'status': 'active',
    }


def merge_fact(previous, incoming, messages):
    """Preserve conflicts and explicit reviews until an evidenced correction or review."""
    if not previous:
        return copy.deepcopy(incoming)
    old, new = previous.get('value', ''), incoming.get('value', '')
    if old.casefold() == new.casefold():
        if previous.get('status') in ('conflict', 'source_invalid', 'rejected', 'pending') or previous.get('review'):
            return copy.deepcopy(previous)
        result = copy.deepcopy(incoming)
        result['evidence_ids'] = list(dict.fromkeys(previous.get('evidence_ids', []) + incoming.get('evidence_ids', [])))
        return result
    overlap = set(previous.get('evidence_ids', [])) & set(incoming.get('evidence_ids', []))
    if overlap and (old.casefold() in new.casefold() or new.casefold() in old.casefold()) and not previous.get('review'):
        return copy.deepcopy(previous if len(old) > len(new) else incoming)
    # An explicit correction must cite the new buyer wording, including both
    # the old and new values, so a repeated extraction cannot settle conflict.
    correction = any(re.search(r'\b(?:instead|correct(?:ion)?|change(?:d)?\s+(?:it\s+)?to|rather\s+than)\b|改为|改成|更正|不是.{0,30}而是',
                               messages[mid]['text'], re.I)
                     and old.casefold() in messages[mid]['text'].casefold()
                     and new.casefold() in messages[mid]['text'].casefold()
                     for mid in incoming.get('evidence_ids', []) if mid in messages)
    candidates = copy.deepcopy(previous.get('candidates') or [evidence_snapshot(previous, messages)])
    if not any(c.get('value', '').casefold() == new.casefold() and c.get('evidence_ids') == incoming.get('evidence_ids') for c in candidates):
        candidates.append(evidence_snapshot(incoming, messages))
    if correction and not previous.get('review'):
        result = copy.deepcopy(incoming)
        for candidate in candidates[:-1]:
            candidate['status'] = 'superseded'
    else:
        result = copy.deepcopy(previous if previous.get('review') or previous.get('status') == 'conflict' else incoming)
        result['status'] = 'conflict'
        result.pop('display_value', None)
    result['candidates'] = candidates
    return result


def fact_change(field, previous, current, *, kind=None):
    if previous == current:
        return None
    kind = kind or ('added' if previous is None else 'conflict' if current.get('status') == 'conflict' else 'updated')
    change = {'field': field, 'kind': kind, 'at': now_ms()}
    if current.get('value') is not None:
        change['value'] = current['value']
    if previous and previous.get('value') is not None:
        change['previous_value'] = previous['value']
    if current.get('evidence_ids'):
        change['evidence_ids'] = list(current['evidence_ids'])
    return change


def invalidate_case_sources(case, edited_ids):
    changed = False
    changes = []
    ref = case.get('product_ref')
    if isinstance(ref, dict) and edited_ids.intersection(ref.get('source_message_ids') or []):
        ref['source_invalid'] = True
        changed = True
    for field, fact in case.get('facts', {}).items():
        if edited_ids.intersection(fact.get('evidence_ids', [])):
            previous = copy.deepcopy(fact)
            fact['status'] = 'source_invalid'
            change = fact_change(field, previous, fact, kind='source_invalid')
            if change:
                changes.append(change)
            changed = True
        for candidate in fact.get('candidates', []):
            if edited_ids.intersection(candidate.get('evidence_ids', [])):
                if candidate.get('status') != 'source_invalid':
                    changes.append({'field': field, 'kind': 'source_invalid', 'value': candidate.get('value', ''),
                                    'evidence_ids': list(candidate.get('evidence_ids', [])), 'at': now_ms()})
                candidate['status'] = 'source_invalid'
                changed = True
    for item in case.get('requirement_items', []):
        if edited_ids.intersection(item.get('evidence_ids', []) + item.get('source_message_ids', [])):
            if item.get('status') != 'source_invalid':
                changes.append({'field': item.get('field', ''), 'kind': 'source_invalid', 'value': item.get('value', ''),
                                'evidence_ids': list(item.get('evidence_ids', [])), 'at': now_ms()})
            item['status'] = 'source_invalid'
            changed = True
    if changes:
        case['last_changes'] = changes
    if changed and case.get('state') == 'waiting_quote':
        case['quote_review_required'] = True
    return changed


class ProcurementMixin:
    def _product_image_refs(self, case):
        from .requirement_media import get_product_source_images, _media_id
        messages = self.repo.list('message', conversation_id=case['conversation_id'])
        return [_media_id('customer_image', message['message_id'])
                for message in get_product_source_images(self.repo, case, messages)]

    def _product_sku(self, case, found):
        return None

    def _prepare_product_context(self, context, facade, job_id):
        return None, None

    def request_product_lookup(self, case_id, expected_product_id, client_request_id):
        from .product_catalog import claim
        if (not isinstance(case_id, str) or not case_id or not isinstance(expected_product_id, str)
                or not re.fullmatch(r'[1-9][0-9]{0,24}', expected_product_id)
                or not isinstance(client_request_id, str) or not 1 <= len(client_request_id.strip()) <= 200):
            raise ServiceError('invalid_params', '商品查询参数无效')
        request = {'case_id': case_id, 'expected_product_id': expected_product_id}
        with self._lock, self.repo.transaction():
            case = self._requirement_media_case(case_id)
            prior = self.repo.get('product_lookup_request', client_request_id)
            if prior:
                if prior['request'] != request:
                    raise ServiceError('mutation_conflict', '请求编号已用于其他商品')
                return {'job_id': prior['job_id']}
            ref = case.get('product_ref') or {}
            if ref.get('source_invalid') or expected_product_id not in (ref.get('product_id'), ref.get('observed_product_id')):
                raise ServiceError('source_changed', '商品来源或编号已变化')
            job_id = new_id('job')
            row, claimed = claim(self.repo, self.seller_id, expected_product_id, job_id, retry=True)
            if not claimed:
                job_id = row['request_id']
            else:
                job = {'job_id': job_id, 'kind': 'product_lookup', 'conversation_id': case['conversation_id'],
                       'case_id': case_id, 'catalog_product_id': row['catalog_product_id'], 'status': 'queued',
                       'stage': 'queued', 'message': '等待重新获取商品资料', 'created_at': now_ms(),
                       'started_at': None, 'completed_at': None, 'automatic': False}
                self.repo.put('job', job_id, job, history=True)
                self._queue.put((job_id, 'product_lookup', request, self._epoch, False))
            self.repo.put('product_lookup_request', client_request_id, {'request': request, 'job_id': job_id})
        if claimed:
            self._emit('job.updated', job)
        self._changed(case['conversation_id'], case_id)
        return {'job_id': job_id}

    def _task_product_lookup(self, params, job_id, epoch, facade):
        from .product_catalog import lookup
        with self._lock:
            case = self._requirement_media_case(params['case_id'])
            ref = case.get('product_ref') or {}
            if (epoch != self._epoch or self._closing or ref.get('source_invalid')
                    or params['expected_product_id'] not in (ref.get('product_id'), ref.get('observed_product_id'))):
                self._generation_cancel_event(job_id).set()
        try:
            if not self._generation_cancel_event(job_id).is_set():
                self._ensure_identity(facade)
        except Exception:
            from .product_catalog import key_for
            with self.repo.transaction():
                key = key_for(self.seller_id, 'alibaba', params['expected_product_id'])
                row = self.repo.get('product_catalog', key)
                if row and row.get('request_id') == job_id and row['lookup']['status'] == 'querying':
                    row['lookup'] = {'status': 'failed', 'reason': 'identity_unverified', 'queried_at': now_ms()}
                    self.repo.put('product_catalog', key, row, history=True)
            raise
        result = lookup(self.repo, self.seller_id, {'product_id': params['expected_product_id']}, facade,
                        job_id, self._generation_cancel_event(job_id), reserved=True)
        self._changed(case['conversation_id'], case['case_id'])
        return {'message': '商品资料查询已结束', 'lookup': result.get('lookup') if result else None}

    def set_case_category(self, case_id, category_label, expected_revision, client_mutation_id):
        from .context import missing_fields_for_case
        if (not isinstance(category_label, str) or not re.fullmatch(r'[一-鿿A-Za-z]{1,8}', category_label.strip())
                or not re.search(r'[一-鿿]', category_label)):
            raise ServiceError('invalid_params', '品类须为不超过八个字的简短中文名称')
        payload = {'case_id': case_id, 'category_label': category_label, 'expected_revision': expected_revision}
        if not isinstance(client_mutation_id, str) or not 1 <= len(client_mutation_id) <= 200 or type(expected_revision) is not int:
            raise ServiceError('invalid_params', '操作编号或版本无效')
        with self._lock, self.repo.transaction():
            saved = self.repo.get('procurement_mutation', client_mutation_id)
            if saved:
                if saved['payload'] != payload or saved['method'] != 'set_case_category':
                    raise ServiceError('mutation_conflict', '操作编号已用于其他修改')
                return saved['response']
            case = self._require('case', case_id)
            if case['revision'] != expected_revision:
                raise ServiceError('revision_conflict', '采购事项已变化')
            case.update(category_label=category_label.strip(), category_label_manual=True,
                        revision=case['revision'] + 1, updated_at=now_ms())
            case['missing'] = missing_fields_for_case(case['facts'], self.settings, case)
            if case.get('state') == 'waiting_quote' and case['missing']:
                case['quote_review_required'] = True
            self.repo.put('case', case_id, case, history=True)
            self._invalidate(case['conversation_id'])
            self.repo.put('procurement_mutation', client_mutation_id,
                          {'method': 'set_case_category', 'payload': payload, 'response': copy.deepcopy(case)})
        self._changed(case['conversation_id'], case_id)
        return case

    def create_procurement_case(self, conversation_id, expected_revision, client_mutation_id, category_label=None):
        from .context import missing_fields_for_case
        if (type(expected_revision) is not int or not isinstance(client_mutation_id, str)
                or not 1 <= len(client_mutation_id) <= 200 or (category_label is not None and
                (not isinstance(category_label, str) or not re.fullmatch(r'[一-鿿A-Za-z]{1,8}', category_label.strip())
                 or not re.search(r'[一-鿿]', category_label)))):
            raise ServiceError('invalid_params', '事项参数无效')
        payload = dict(conversation_id=conversation_id, expected_revision=expected_revision, category_label=category_label)
        with self._lock, self.repo.transaction():
            saved = self.repo.get('procurement_mutation', client_mutation_id)
            if saved:
                if saved['method'] != 'create_procurement_case' or saved['payload'] != payload:
                    raise ServiceError('mutation_conflict', '操作编号已用于其他修改')
                return saved['response']
            conversation = self._require('conversation', conversation_id)
            if conversation['revision'] != expected_revision:
                raise ServiceError('revision_conflict', '会话已变化')
            if self._conversation_unresolved(conversation_id):
                raise ServiceError('send_pending', '会话存在未决发送，请先核查')
            case = self._new_case(conversation)
            if category_label:
                case.update(category_label=category_label.strip(), category_label_manual=True)
                case['missing'] = missing_fields_for_case(case['facts'], self.settings, case)
                self.repo.put('case', case['case_id'], case, history=True)
            conversation['revision'] += 1
            self.repo.put('conversation', conversation_id, conversation)
            self._invalidate(conversation_id)
            self.repo.put('procurement_mutation', client_mutation_id,
                          {'method': 'create_procurement_case', 'payload': payload, 'response': copy.deepcopy(case)})
        self._changed(conversation_id, case['case_id'])
        return case

    def move_requirement(self, case_id, target_case_id, field, expected_revision, target_expected_revision, client_mutation_id, candidate_id=None):
        from .context import FIELDS, missing_fields_for_case
        if (field not in FIELDS or case_id == target_case_id or type(expected_revision) is not int
                or type(target_expected_revision) is not int or not isinstance(client_mutation_id, str)
                or not 1 <= len(client_mutation_id) <= 200):
            raise ServiceError('invalid_params', '需求归属参数无效')
        payload = dict(case_id=case_id, target_case_id=target_case_id, field=field, candidate_id=candidate_id,
                       expected_revision=expected_revision, target_expected_revision=target_expected_revision)
        with self._lock, self.repo.transaction():
            saved = self.repo.get('procurement_mutation', client_mutation_id)
            if saved:
                if saved['method'] != 'move_requirement' or saved['payload'] != payload:
                    raise ServiceError('mutation_conflict', '操作编号已用于其他修改')
                return saved['response']
            source, target = self._require('case', case_id), self._require('case', target_case_id)
            if source['conversation_id'] != target['conversation_id'] or source['buyer_id'] != target['buyer_id']:
                raise ServiceError('case_conversation_mismatch', '事项不属于同一客户')
            if source['revision'] != expected_revision or target['revision'] != target_expected_revision:
                raise ServiceError('revision_conflict', '采购事项已变化')
            if (self._conversation_unresolved(source['conversation_id'])
                    or source['state'] == 'waiting_quote' or target['state'] == 'waiting_quote'):
                raise ServiceError('operation_protected', '存在未决发送或待报价事项，不能移动需求')
            if field not in source.get('facts', {}):
                raise ServiceError('invalid_params', '来源需求不存在')
            if candidate_id is None:
                if field in target.get('facts', {}):
                    raise ServiceError('invalid_params', '目标已有同一字段')
                target.setdefault('facts', {})[field] = source['facts'].pop(field)
            else:
                origin = source['facts'][field]
                if origin.get('review', {}).get('status') == 'confirmed':
                    raise ServiceError('operation_protected', '人工已复核字段不能拆移候选')
                selected = next((c for c in origin.get('candidates', [])
                                 if c.get('candidate_id') == candidate_id and c.get('status') == 'active'), None)
                if not selected:
                    raise ServiceError('invalid_params', '需求候选不存在或来源已失效')
                origin['candidates'] = [c for c in origin['candidates'] if c['candidate_id'] != candidate_id]
                remaining = [c for c in origin['candidates'] if c.get('status') == 'active']
                if origin.get('value') == selected['value']:
                    if remaining:
                        origin.update(value=remaining[0]['value'], evidence_ids=remaining[0]['evidence_ids'])
                    else:
                        origin.update(value='', evidence_ids=[])
                    origin.pop('display_value', None)
                origin['status'] = 'conflict' if len(remaining) > 1 else 'pending'
                target_fact = target.setdefault('facts', {}).get(field)
                if target_fact is None:
                    target['facts'][field] = {'value': selected['value'], 'evidence_ids': selected['evidence_ids'],
                                              'status': 'pending', 'candidates': [selected]}
                else:
                    if target_fact.get('review', {}).get('status') == 'confirmed':
                        raise ServiceError('operation_protected', '目标字段已经人工复核')
                    if not target_fact.get('candidates'):
                        messages = {m['message_id']: m for m in self._messages(source['conversation_id'])}
                        target_fact['candidates'] = [evidence_snapshot(target_fact, messages)]
                    target_fact['candidates'].append(selected)
                    target_fact['status'] = 'conflict'
            for case in (source, target):
                case['missing'] = missing_fields_for_case(case['facts'], self.settings, case)
                case.update(revision=case['revision'] + 1, updated_at=now_ms())
                self.repo.put('case', case['case_id'], case, history=True)
            self._invalidate(source['conversation_id'])
            response = {'case': copy.deepcopy(source), 'target_case': copy.deepcopy(target)}
            self.repo.put('procurement_mutation', client_mutation_id,
                          {'method': 'move_requirement', 'payload': payload, 'response': response})
        self._changed(source['conversation_id'], case_id)
        return response

    def review_requirement(self, case_id, field, decision, expected_revision, client_mutation_id,
                           candidate_id=None, value=None, evidence_ids=None):
        from .context import FIELDS, missing_fields_for_case
        if (field not in FIELDS or decision not in ('confirm', 'candidate', 'reject', 'pending', 'edit')
                or type(expected_revision) is not int or not isinstance(client_mutation_id, str)
                or not 1 <= len(client_mutation_id) <= 200):
            raise ServiceError('invalid_params', '需求复核参数无效')
        payload = dict(case_id=case_id, field=field, decision=decision, expected_revision=expected_revision,
                       candidate_id=candidate_id, value=value, evidence_ids=evidence_ids)
        with self._lock, self.repo.transaction():
            saved = self.repo.get('procurement_mutation', client_mutation_id)
            if saved:
                if saved['method'] != 'review_requirement' or saved['payload'] != payload:
                    raise ServiceError('mutation_conflict', '操作编号已用于其他修改')
                return saved['response']
            case = self._require('case', case_id)
            if case['revision'] != expected_revision:
                raise ServiceError('revision_conflict', '采购事项已变化')
            fact = copy.deepcopy(case.get('facts', {}).get(field))
            previous_fact = copy.deepcopy(fact)
            if decision == 'edit':
                messages = {m['message_id']: m for m in self._messages(case['conversation_id'])}
                if (not isinstance(value, str) or not value.strip() or not isinstance(evidence_ids, list)
                        or not evidence_ids or any(not isinstance(mid, str) or mid not in messages
                        or messages[mid]['role'] != 'buyer' or not buyer_fact_text_allowed(field, messages[mid]['text'])
                        or value.casefold() not in messages[mid]['text'].casefold()
                        for mid in evidence_ids)):
                    raise ServiceError('invalid_evidence', '补录必须有本客户买家原文依据')
                fact = {'value': value.strip(), 'evidence_ids': list(dict.fromkeys(evidence_ids)), 'status': 'confirmed',
                        'candidates': (fact or {}).get('candidates', [])}
            elif not fact:
                raise ServiceError('invalid_params', '需求字段不存在')
            if decision == 'candidate':
                selected = next((c for c in fact.get('candidates', []) if c.get('candidate_id') == candidate_id
                                 and c.get('status') == 'active'), None)
                if not selected:
                    raise ServiceError('invalid_params', '需求候选不存在或来源已失效')
                fact.update(value=selected['value'], evidence_ids=selected['evidence_ids'], status='confirmed')
                if selected.get('display_value'):
                    fact['display_value'] = selected['display_value']
                else:
                    fact.pop('display_value', None)
            elif decision == 'confirm':
                if fact.get('status') != 'confirmed':
                    raise ServiceError('invalid_evidence', '请先选择有效候选')
            elif decision == 'reject' and candidate_id:
                candidate = next((c for c in fact.get('candidates', []) if c.get('candidate_id') == candidate_id
                                  and c.get('status') == 'active'), None)
                if not candidate:
                    raise ServiceError('invalid_params', '需求候选不存在或来源已失效')
                candidate['status'] = 'rejected'
                fact['status'] = 'conflict' if sum(c.get('status') == 'active' for c in fact['candidates']) > 1 else 'pending'
            elif decision in ('reject', 'pending'):
                fact['status'] = 'rejected' if decision == 'reject' else 'pending'
            fact['review'] = {'status': 'confirmed' if decision in ('edit', 'confirm', 'candidate') else
                              'rejected' if decision == 'reject' and not candidate_id else 'pending',
                              'reviewed_at': now_ms(), 'evidence_ids': list(fact.get('evidence_ids', []))}
            case.setdefault('facts', {})[field] = fact
            change = fact_change(field, previous_fact, fact, kind='reviewed')
            if change:
                case['last_changes'] = [change]
            case['missing'] = missing_fields_for_case(case['facts'], self.settings, case)
            if case.get('state') == 'waiting_quote':
                case['quote_review_required'] = True
            case.update(revision=case['revision'] + 1, updated_at=now_ms())
            self.repo.put('case', case_id, case, history=True)
            self._invalidate(case['conversation_id'])
            self.repo.put('procurement_mutation', client_mutation_id,
                          {'method': 'review_requirement', 'payload': payload, 'response': copy.deepcopy(case)})
        self._changed(case['conversation_id'], case_id)
        return case
