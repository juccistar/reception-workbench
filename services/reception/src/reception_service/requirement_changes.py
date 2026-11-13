"""Explicit, atomic customer-local requirement updates; unchanged turns write nothing."""
from __future__ import annotations

import copy
import hashlib
import re

from .errors import ServiceError
from .repository import encoded, now_ms

MAX_CHANGES = 32


def requirement_revision(case):
    value = case.get('requirements_revision')
    if type(value) is int and value >= 0:
        return value
    return int(case.get('requirements_format') == 'ai-v1' and any(
        isinstance(row, dict) and bool(row.get('value')) for row in (case.get('requirements') or {}).values()))


def case_catalog(repo, owner):
    rows = [row for row in repo.list('case', conversation_id=owner['conversation_id'])
            if all(row.get(key) == owner.get(key) for key in ('seller_id', 'buyer_id'))]
    return [{'case_ref': f'c{index + 1}', 'case_id': row['case_id'], 'revision': row['revision'],
             'requirements_revision': requirement_revision(row), 'state': row['state'],
             'requirements': copy.deepcopy(row.get('requirements') or {}),
             'procurement_key': row.get('procurement_key', '')}
            for index, row in enumerate(rows)]


def _fail(message='需求变更格式或依据无效，已有内容未修改'):
    raise ServiceError('invalid_requirement_change', message, stage='requirements')


def _messages(context):
    owner = context.get('case') or context
    return {row['message_id']: row for row in context.get('historical_evidence', []) + context.get('recent_messages', [])
            if isinstance(row, dict) and isinstance(row.get('message_id'), str)
            and all(row.get(key) == owner.get(key) for key in ('seller_id', 'buyer_id', 'conversation_id'))}


def _sources(ids, messages):
    if (not isinstance(ids, list) or not ids or len(ids) > 200
            or any(not isinstance(mid, str) or mid not in messages for mid in ids)
            or not any(messages[mid]['role'] == 'buyer' for mid in ids)):
        _fail('需求变更缺少当前客户的买家原文依据')
    return list(dict.fromkeys(ids))


def validate_changes(output, context):
    """Validate every operation before applying any; omission is a real no-op."""
    from .context import REQUIREMENT_FIELDS, validated_requirements
    if 'requirements' in output or output.get('new_case'):
        _fail('AI 返回了旧版整份需求结构，请使用本版需求变更契约')
    raw = output.get('requirement_changes', [])
    refs = output.get('reply_case_refs', [])
    if (not isinstance(raw, list) or len(raw) > MAX_CHANGES or not isinstance(refs, list)
            or len(refs) > MAX_CHANGES or any(not isinstance(ref, str) for ref in refs)):
        _fail()
    catalogue = {row['case_ref']: row for row in context.get('case_catalog', [])}
    messages = _messages(context)
    accepted, seen = [], set()
    allowed = set(REQUIREMENT_FIELDS) - {'product_url'}
    for change in raw:
        if (not isinstance(change, dict) or set(change) - {'action', 'case_ref', 'trigger_evidence_ids', 'set_fields', 'clear_fields'}
                or change.get('action') not in ('create', 'update')):
            _fail()
        ref, action = change.get('case_ref'), change['action']
        if (not isinstance(ref, str) or ref in seen
                or action == 'update' and ref not in catalogue
                or action == 'create' and not re.fullmatch(r'new:[1-9][0-9]{0,2}', ref)):
            _fail('需求事项引用无效或重复')
        seen.add(ref)
        trigger = _sources(change.get('trigger_evidence_ids'), messages)
        sets, clears = change.get('set_fields', {}), change.get('clear_fields', {})
        if (not isinstance(sets, dict) or not isinstance(clears, dict) or set(sets) & set(clears)
                or (set(sets) | set(clears)) - allowed or action == 'create' and clears):
            _fail()
        target = catalogue.get(ref) or {}
        owner = context.get('case') or context
        target_context = dict(context, case={**owner, 'requirements': target.get('requirements', {})})
        # Empty values never mean deletion. An explicit, sourced clear is required.
        nonempty = {key: row for key, row in sets.items()
                    if not isinstance(row, dict) or row.get('value') not in (None, '', [])}
        values = validated_requirements(nonempty, target_context)
        if set(values) != set(nonempty):
            _fail('需求字段缺少有效内容或确切原文依据')
        for row in values.values():
            _sources(row['evidence_ids'], messages)
        cleared = {}
        for key, row in clears.items():
            if not isinstance(row, dict) or set(row) - {'evidence_ids', 'source_excerpts'}:
                _fail()
            ids = _sources(row.get('evidence_ids'), messages)
            excerpts = row.get('source_excerpts')
            if (not isinstance(excerpts, dict) or not excerpts or not set(ids) & set(trigger)
                    or any(mid not in ids or not isinstance(text, str) or not text
                           or text not in messages[mid]['text'] for mid, text in excerpts.items())
                    or not any(mid in excerpts and messages[mid]['role'] == 'buyer' for mid in ids)):
                _fail('清空需求必须引用客户撤回或更正的真实原文')
            cleared[key] = {'value': [] if key == 'product_images' else None, 'evidence_ids': ids,
                            'source_excerpts': dict(excerpts),
                            'evidence_versions': {mid: hashlib.sha256(messages[mid]['text'].encode()).hexdigest() for mid in ids}}
        if action == 'create':
            product = values.get('product') or {}
            excerpts = product.get('source_excerpts') or {}
            if (not product.get('value') or not any(mid in trigger and messages[mid]['role'] == 'buyer' for mid in excerpts)):
                _fail('新采购须有产品描述及对应买家原文片段')
        accepted.append({'action': action, 'case_ref': ref, 'trigger_evidence_ids': trigger,
                         'set_fields': values, 'clear_fields': cleared})
    known = set(catalogue) | {row['case_ref'] for row in accepted if row['action'] == 'create'}
    if any(ref not in known for ref in refs):
        _fail('回复引用了本轮未提供的采购事项')
    return accepted, list(dict.fromkeys(refs))


def reply_scopes(output, context):
    """Keep each offer and product-scoped policy tied to the reply's own cases."""
    catalogue = context.get('case_catalog')
    if not catalogue or context.get('unsent_reply'):
        return [context]
    changes = output.get('requirement_changes')
    changes = changes if isinstance(changes, list) else []
    refs = output.get('reply_case_refs')
    refs = refs if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs) else []
    refs = refs or [row.get('case_ref') for row in changes if isinstance(row, dict)]
    refs = refs or [context.get('selected_case_ref') or context.get('current_case_ref')]
    scopes = [row for row in catalogue if row['case_ref'] in refs]
    global_scope = {key: [row for row in context.get(key, []) if not row.get('product_scope')]
                    for key in ('knowledge', 'guidance', 'style')}
    return [global_scope, *scopes]


def _same_requirements(previous, current):
    def effective(rows):
        return {key: {'value': row.get('value'), 'evidence_ids': sorted(set(row.get('evidence_ids') or [])),
                      'evidence_versions': row.get('evidence_versions') or {}}
                for key, row in rows.items() if isinstance(row, dict) and key != 'product_url'
                and (row.get('value') or row.get('evidence_ids'))}
    return effective(previous) == effective(current)


def _creation_spans(change, messages):
    spans = []
    for mid, text in change['set_fields']['product'].get('source_excerpts', {}).items():
        if messages[mid]['role'] != 'buyer' or mid not in change['trigger_evidence_ids']:
            continue
        body = messages[mid]['text']
        start = body.index(text)
        if body.find(text, start + 1) >= 0:
            _fail('新采购原文片段重复，无法唯一确定建立来源')
        spans.append({'message_id': mid, 'start': start, 'end': start + len(text),
                      'text_sha256': hashlib.sha256(body.encode()).hexdigest()})
    return spans


class RequirementChangesMixin:
    def _with_case_catalog_context(self, context):
        from .policy_library import select_policy_inputs
        if context.get('task_mode') == 'polish':
            return context
        for row in context.get('case_catalog', []):
            case = self._require('case', row['case_id'])
            row.update(self._product_context_for_case(case))
            policies = select_policy_inputs(self.repo.list('policy'), case)
            row['knowledge'], row['guidance'], row['style'] = policies['knowledge'], policies['guidance'], policies['style']
            context.setdefault('policy_versions', {}).update(policies['policy_versions'])
        return context

    def _apply_requirement_output(self, conversation, case, output, context, request_id):
        """Caller holds the service lock and transaction. All mutations are local."""
        from .context import REQUIREMENT_FIELDS, missing_fields_for_case
        from .product_identity import derive_product_link
        payload_hash = hashlib.sha256(encoded({key: output[key] for key in ('requirement_changes', 'reply_case_refs') if key in output}).encode()).hexdigest()
        prior = self.repo.get('requirement_application', request_id)
        if prior:
            if prior['payload_hash'] != payload_hash or prior['conversation_id'] != conversation['conversation_id']:
                _fail('同一需求应用编号不能用于不同结果')
            return copy.deepcopy(prior['result'])
        changes, reply_refs = validate_changes(output, context)
        catalogue = {row['case_ref']: row for row in context.get('case_catalog', [])}
        current_catalogue = case_catalog(self.repo, conversation)
        if [(r['case_id'], r['revision']) for r in current_catalogue] != [(r['case_id'], r['revision']) for r in context.get('case_catalog', [])]:
            raise ServiceError('source_changed', '同会话采购事项已变化，旧需求结果未应用')
        messages = _messages(context)
        mapping = {ref: row['case_id'] for ref, row in catalogue.items()}
        prepared, reserved = [], set()
        for change in changes:
            target, spans = None, []
            if change['action'] == 'update':
                target = self._require('case', mapping[change['case_ref']])
            else:
                spans = _creation_spans(change, messages)
                pid = (change['set_fields'].get('product_id') or {}).get('value')
                for existing in current_catalogue:
                    origin = self.repo.get('case_origin', existing['case_id']) or {}
                    original_pid = origin.get('creation_product_id')
                    overlaps = any(a['message_id'] == b.get('message_id') and a['text_sha256'] == b.get('text_sha256')
                                   and a['start'] < b.get('end', -1) and b.get('start', -1) < a['end']
                                   for a in spans for b in origin.get('creation_spans', []))
                    if overlaps and (not pid or not original_pid or pid == original_pid):
                        target = self._require('case', existing['case_id'])
                        change['_existing_creation'] = True
                        break
                if target is None:
                    for existing in current_catalogue:
                        candidate = self._require('case', existing['case_id'])
                        if (candidate['case_id'] not in reserved and candidate['state'] != 'closed'
                                and not candidate.get('facts') and not any(
                                    isinstance(row, dict) and row.get('value') for row in (candidate.get('requirements') or {}).values())
                                and not self.repo.list('draft', case_id=candidate['case_id'])
                                and not self.repo.list('outbox', case_id=candidate['case_id'])):
                            target = candidate
                            break
                if target is not None:
                    if target['case_id'] in reserved:
                        _fail('本轮多个新采购无法唯一归属，请先澄清')
                    reserved.add(target['case_id'])
            if target is not None:
                mapping[change['case_ref']] = target['case_id']
                if target['case_id'] in {item[1]['case_id'] for item in prepared if item[1] is not None}:
                    _fail('本轮不能通过不同引用重复修改同一事项')
            if spans:
                for previous, _, previous_spans in prepared:
                    same_source = any(a['message_id'] == b['message_id'] and a['text_sha256'] == b['text_sha256']
                                      and a['start'] < b['end'] and b['start'] < a['end']
                                      for a in spans for b in previous_spans)
                    previous_pid = (previous['set_fields'].get('product_id') or {}).get('value')
                    if same_source and (not pid or not previous_pid or pid == previous_pid):
                        _fail('同一原文范围不能重复建立两笔采购，请先明确区别')
            prepared.append((change, target, spans))
        changed, created, touched = [], [], []
        for change, target, spans in prepared:
            if target is None:
                trigger_buyer = next(mid for mid in change['trigger_evidence_ids'] if messages[mid]['role'] == 'buyer')
                target = self._new_case(conversation, source_message_id=trigger_buyer)
                mapping[change['case_ref']] = target['case_id']
                created.append(target['case_id'])
            identifier = target['case_id']
            touched.append(identifier)
            defaults = {key: {'value': [] if key == 'product_images' else None, 'evidence_ids': []} for key in REQUIREMENT_FIELDS}
            requirements = {**defaults, **copy.deepcopy(target.get('requirements') or {})}
            if not change.get('_existing_creation'):
                requirements.update(change['set_fields'])
                requirements.update(change['clear_fields'])
            if not _same_requirements(target.get('requirements') or {}, requirements):
                target['requirements_revision'] = requirement_revision(target) + 1
                target.update(requirements=requirements, requirements_format='ai-v1', revision=target['revision'] + 1, updated_at=now_ms())
                derive_product_link(target)
                target['missing'] = missing_fields_for_case(target.get('facts', {}), self.settings, target)
                if target['state'] not in ('closed', 'human_owned'):
                    target['state'] = 'clarifying' if target['missing'] else 'waiting_quote'
                self.repo.put('case', identifier, target, history=True)
                self._sync_requirement_product(target)
                changed.append(identifier)
            if spans:
                origin = self.repo.get('case_origin', identifier) or {}
                if not origin.get('creation_spans'):
                    origin.update(seller_id=self.seller_id, buyer_id=conversation['buyer_id'], conversation_id=conversation['conversation_id'],
                                  message_id=next(mid for mid in change['trigger_evidence_ids'] if messages[mid]['role'] == 'buyer'),
                                  initial_history=False, creation_spans=spans,
                                  creation_product_id=(change['set_fields'].get('product_id') or {}).get('value'))
                    self.repo.put('case_origin', identifier, origin, history=True)
        reply_ids = list(dict.fromkeys(mapping[ref] for ref in reply_refs)) if reply_refs else list(dict.fromkeys(touched))
        if not reply_ids and case is not None:
            reply_ids = [case['case_id']]
        pointer_changed = len(reply_ids) == 1 and conversation.get('case_id') != reply_ids[0]
        if pointer_changed:
            conversation.update(case_id=reply_ids[0], revision=conversation['revision'] + 1)
            self.repo.put('conversation', conversation['conversation_id'], conversation)
        result = {'reply_case_ids': reply_ids, 'changed_case_ids': changed, 'created_case_ids': created,
                  'pointer_changed': pointer_changed, 'mapping': mapping}
        if changes or reply_refs:
            self.repo.put('requirement_application', request_id, {'conversation_id': conversation['conversation_id'],
                          'payload_hash': payload_hash, 'result': result, 'updated_at': now_ms()})
        return result

    def _related_case_versions(self, case_ids):
        result = {}
        for identifier in case_ids:
            case = self._require('case', identifier)
            supplier = self._product_context_for_case(case)
            result[identifier] = {'revision': case['revision'], 'requirements_revision': requirement_revision(case),
                                  'supply': supplier, 'state': case['state']}
        return result

    def _related_cases_current(self, draft):
        expected = draft.get('related_case_versions')
        if expected is None:
            return True  # Legacy drafts retain their original checks.
        try:
            return expected == self._related_case_versions(draft.get('reply_case_ids') or [])
        except ServiceError:
            return False
