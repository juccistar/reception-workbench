"""Merchant-owned policies and the read-only built-in catalogue."""
from __future__ import annotations

import copy
import json
import re
from importlib.resources import files

from .errors import ServiceError
from .repository import new_id, now_ms

_CATEGORIES = ('product', 'business', 'reception', 'style')
_USAGES = ('human_only', 'internal_guidance', 'customer_reference')
_UNSET = object()
_PLACEHOLDER = re.compile(r'\[[^\]\n]*(?:待填写|可选)[^\]\n]*\]|【[^】\n]*(?:待填写|可选)[^】\n]*】')


def normalize_policy(policy):
    result = copy.deepcopy(policy)
    legacy_public = policy.get('public') is True
    result.setdefault('category', 'product' if policy.get('kind') == 'product' else 'business')
    result.setdefault('enabled', legacy_public)
    result.setdefault('usage', 'customer_reference' if legacy_public else 'human_only')
    result.setdefault('customer_wording', '')
    result.setdefault('internal_notes', '')
    result.setdefault('template_id', None)
    result.setdefault('template_version', None)
    if type(result.get('priority')) is not int or not 0 <= result['priority'] <= 100:
        result['priority'] = 0
    result['public'] = bool(result['enabled'] and result['usage'] == 'customer_reference' and _policy_content(result))
    return result


def _usable_text(text):
    return '\n'.join(line for line in str(text or '').splitlines() if not _PLACEHOLDER.search(line)).strip()


def _template_text(text):
    """Keep filled lines only; empty fields and placeholders are not facts."""
    lines = []
    for line in _usable_text(text).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        value = re.split(r'[:：]', stripped, maxsplit=1)[-1].strip()
        if not value or value.casefold() in ('待填写', '待确认', '未填写', '未知', 'tbd', 'n/a', 'none', 'null', '-', '—'):
            continue
        lines.append(line)
    return '\n'.join(lines).strip()


def _policy_content(policy):
    text = policy.get('content')
    if policy.get('template_id') in ('T01', 'T02', 'T03'):
        return _template_text(text)
    return _usable_text(text)


def _applies(policy, case, stamp):
    if not policy['enabled'] or policy.get('confirmed') is not True or policy['usage'] == 'human_only':
        return False
    if policy.get('valid_until') is not None and policy['valid_until'] <= stamp:
        return False
    scope = policy.get('product_scope') or []
    facts = case.get('facts') or {}
    product = str((facts.get('product') or {}).get('value') or '').casefold()
    procurement = str(case.get('procurement_key') or '').casefold()
    return not scope or any(str(s).casefold() in product or str(s).casefold() == procurement for s in scope if str(s).strip())


def select_policy_inputs(policies, case, *, at_ms=None):
    """Return distinct public and internal projections; choose one style deterministically.

    When styles conflict, higher priority wins, followed by product scope and
    the lexicographically smallest policy ID. The caller can detect a
    conflict from style_conflicts and present its IDs and revisions.
    """
    stamp = now_ms() if at_ms is None else at_ms
    knowledge, guidance, styles = [], [], []
    for raw in policies:
        p = normalize_policy(raw)
        if not _applies(p, case, stamp):
            continue
        base = {k: copy.deepcopy(p.get(k)) for k in ('kind', 'category', 'title', 'product_scope', 'confirmed', 'valid_until', 'revision', 'priority')}
        source = p['policy_id']
        content = _policy_content(p)
        if p.get('template_id') in ('T01', 'T02', 'T03') and not content:
            continue
        if p['usage'] == 'customer_reference' and content:
            knowledge.append({**base, 'policy_id': source, 'source_id': source, 'content': content, 'public': True})
        elif p['usage'] == 'internal_guidance':
            if content:
                row = {**base, 'policy_id': source + ':internal', 'source_id': source + ':internal', 'content': content, 'public': False}
                (styles if p['category'] == 'style' else guidance).append(row)
            wording = _usable_text(p.get('customer_wording'))
            if wording:
                knowledge.append({**base, 'policy_id': source + ':customer', 'source_id': source + ':customer', 'content': wording, 'public': True})
    for rows in (knowledge, guidance, styles):
        rows.sort(key=lambda p: p['source_id'])
    styles.sort(key=lambda p: (-p['priority'], not bool(p.get('product_scope')), p['source_id']))
    conflicts = [{'source_id': p['source_id'], 'revision': p['revision']} for p in styles] if len(styles) > 1 else []
    chosen = styles[:1]
    versions = {p['source_id']: p['revision'] for p in knowledge + guidance + chosen}
    return {'knowledge': knowledge, 'guidance': guidance, 'style': chosen, 'policy_versions': versions, 'style_conflicts': conflicts}


def _require_identity(service):
    if not service.account_verified:
        raise ServiceError('identity_required', '请先核验卖家身份再保存该卖家的资料')


def _invalidate_affected(service, before, after):
    # Compare the actual inputs for each case, not the global catalogue revision.
    affected = set()
    for case in service.repo.list('case'):
        old = select_policy_inputs(before, case)
        new = select_policy_inputs(after, case)
        if any(old[key] != new[key] for key in ('knowledge', 'guidance', 'style', 'policy_versions')):
            affected.add(case['case_id'])
    conversations = set()
    for draft in service.repo.list('draft'):
        if draft.get('case_id') not in affected or draft.get('status') not in ('pending', 'stale'):
            continue
        draft.update(status='stale', updated_at=now_ms())
        validation = draft.get('validation') or {}
        draft['validation'] = {**validation, 'ready': False, 'issues': list(dict.fromkeys(validation.get('issues', []) + ['依据或授权状态已变化，请重新复核']))}
        service.repo.put('draft', draft['draft_id'], draft, history=True)
        conversations.add(draft['conversation_id'])
    for case in service.repo.list('case'):
        if case['case_id'] in affected:
            conversations.add(case['conversation_id'])
    for confirmation in service.repo.list('confirmation'):
        if confirmation.get('conversation_id') in conversations:
            confirmation['invalidated'] = True
            service.repo.put('confirmation', confirmation['confirmation_id'], confirmation)


def save_policy(service, kind, title, content, product_scope, confirmed, public, policy_id=None,
                expected_revision=None, valid_until=None, *, category=None, enabled=None, usage=None,
                customer_wording=None, internal_notes=None, template_id=_UNSET, template_version=_UNSET, priority=None):
    _require_identity(service)
    if (kind not in ('policy', 'product') or not isinstance(title, str) or not title.strip()
            or not isinstance(content, str) or not content.strip() or len(content) > 50000
            or not isinstance(product_scope, list) or any(not isinstance(s, str) or not s.strip() for s in product_scope)
            or type(confirmed) is not bool or type(public) is not bool
            or (valid_until is not None and (type(valid_until) is not int or valid_until < 0))
            or (enabled is not None and type(enabled) is not bool)
            or (priority is not None and (type(priority) is not int or not 0 <= priority <= 100))
            or (category is not None and category not in _CATEGORIES)
            or (usage is not None and usage not in _USAGES)
            or any(v is not None and (not isinstance(v, str) or len(v) > 50000) for v in (customer_wording, internal_notes))):
        raise ServiceError('invalid_params', '资料字段无效')
    with service._lock, service.repo.transaction():
        previous = service.repo.get('policy', policy_id) if policy_id else None
        if policy_id and not previous:
            raise ServiceError('not_found', '资料不存在')
        if previous and previous['revision'] != expected_revision:
            raise ServiceError('revision_conflict', '资料版本已变化')
        if not previous and expected_revision is not None:
            raise ServiceError('revision_conflict', '资料版本已变化')
        old = normalize_policy(previous) if previous else {}
        # A legacy save still changes public; new callers specify usage/enabled.
        effective_usage = usage if usage is not None else (old.get('usage') if previous and public == old.get('public') else ('customer_reference' if public else 'human_only'))
        effective_enabled = enabled if enabled is not None else (old.get('enabled') if previous else public)
        policy = {**old, 'policy_id': policy_id or new_id('policy'), 'kind': kind, 'title': title.strip(),
                  'content': content, 'product_scope': list(product_scope), 'confirmed': confirmed,
                  'category': category or old.get('category') or ('product' if kind == 'product' else 'business'),
                  'enabled': effective_enabled, 'usage': effective_usage,
                  'priority': old.get('priority', 0) if priority is None else priority,
                  'customer_wording': old.get('customer_wording', '') if customer_wording is None else customer_wording,
                  'internal_notes': old.get('internal_notes', '') if internal_notes is None else internal_notes,
                  'template_id': old.get('template_id') if template_id is _UNSET else template_id,
                  'template_version': old.get('template_version') if template_version is _UNSET else template_version,
                  'revision': previous['revision'] + 1 if previous else 1, 'updated_at': now_ms(), 'valid_until': valid_until}
        policy = normalize_policy(policy)
        if (policy['template_id'] in ('T01', 'T02', 'T03') and policy['enabled']
                and policy['confirmed'] and policy['usage'] != 'human_only' and not _policy_content(policy)):
            raise ServiceError('invalid_params', '请先填写已核实的资料正文')
        before = service.repo.list('policy')
        service.repo.put('policy', policy['policy_id'], policy, history=True)
        _invalidate_affected(service, before, service.repo.list('policy'))
    service._changed()
    return policy


def _catalog():
    return json.loads(files('reception_service').joinpath('resources', 'builtin_policies.json').read_text(encoding='utf-8'))


def list_builtin_policies(service):
    with service._lock:
        adopted = {p.get('template_id'): p for p in service.repo.list('policy') if p.get('template_id')}
        items = []
        for entry in _catalog():
            current = adopted.get(entry['template_id'])
            items.append({**entry, 'adopted_policy_id': current['policy_id'] if current else None,
                          'enabled': normalize_policy(current)['enabled'] if current else False,
                          'available_update': bool(current and (current.get('template_version') or 0) < entry['template_version'])})
        return {'items': items}


def adopt_builtin_policy(service, template_id, content=None, product_scope=None, enabled=None,
                         confirmed=None, expected_revision=None, update=False):
    _require_identity(service)
    if (type(update) is not bool or (enabled is not None and type(enabled) is not bool)
            or (confirmed is not None and type(confirmed) is not bool)):
        raise ServiceError('invalid_params', '资料字段无效')
    with service._lock, service.repo.transaction():
        entry = next ( (p for p in _catalog() if p['template_id'] == template_id), None)
        if entry is None:
            raise ServiceError('not_found', '内置资料不存在')
        previous = next((p for p in service.repo.list('policy') if p.get('template_id') == template_id), None)
        if previous and not update:
            return normalize_policy(previous)
        if update and (not previous or expected_revision is None or expected_revision != previous['revision']):
            raise ServiceError('revision_conflict', '资料版本已变化')
        if not previous and expected_revision is not None:
            raise ServiceError('revision_conflict', '资料版本已变化')
        if update and content is None:
            raise ServiceError('invalid_params', '请提供审核后的资料正文')
        text = entry['content'] if content is None else content
        if entry['is_template'] and (not isinstance(text, str) or not _template_text(text)):
            raise ServiceError('invalid_params', '请先填写已核实的资料正文')
        if entry['is_template'] and _PLACEHOLDER.search(text):
            raise ServiceError('invalid_params', '资料正文不能包含待填写占位内容')
        local = normalize_policy(previous) if previous else None
        return save_policy(service, local['kind'] if local else entry['kind'], local['title'] if local else entry['title'], text,
                           product_scope if product_scope is not None else (local.get('product_scope', []) if local else entry['product_scope']),
                           confirmed if confirmed is not None else (local['confirmed'] if local else True),
                           local['public'] if local else entry['usage'] == 'customer_reference',
                           policy_id=local['policy_id'] if local else None,
                           expected_revision=expected_revision, valid_until=local.get('valid_until') if local else None,
                           category=local['category'] if local else entry['category'],
                           enabled=enabled if enabled is not None else (local['enabled'] if local else True),
                           usage=local['usage'] if local else entry['usage'],
                           template_id=template_id, template_version=entry['template_version'])
