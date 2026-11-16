"""Grounded, customer-local AI context and conservative reply validation."""
from __future__ import annotations

import math
import re
from .repository import encoded
from .errors import ServiceError
from .integrations.analysis import invalid_reply_body, context_messages, current_message
from .conversation_style import social_intent
from .photos import is_photo_placeholder
from .policy_library import select_policy_inputs
from .collection_rules import effective_required_fields, collection_rules
from .model_payload import model_json
from .reception_context import shared_context, semantic_fingerprint
from .integrations.reception_prompts import PROMPT_VERSION

FIELDS = ('product', 'quantity', 'specifications', 'customization', 'destination',
          'delivery', 'budget', 'packaging', 'contact')
REQUIREMENT_FIELDS = ('product', 'product_id', 'product_url', 'product_images', 'quantity',
                      'specifications', 'customization', 'destination', 'delivery', 'budget',
                      'packaging', 'certifications', 'contact')


def validated_requirements(raw, context):
    """Accept only model values backed by sources in this customer's frozen context."""
    if not isinstance(raw, dict):
        return {}
    owner = context.get('case') or context
    messages = {m['message_id']: m for m in context.get('historical_evidence', []) + context.get('recent_messages', [])
                if isinstance(m, dict) and isinstance(m.get('message_id'), str)
                and all(m.get(k) == owner.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id'))}
    from .requirement_media import _media_id
    reads = {r.get('image_id') for r in (context.get('photo_context') or {}).get('read_images', []) if isinstance(r, dict)}
    manifest = {ref.get('alias'): ref for ref in context.get('image_manifest', [])
                if isinstance(ref, dict) and ref.get('image_id') in reads
                and ref.get('origin') != 'business_supplement'
                and ref.get('message_id') in messages
                and (ref.get('role') is None or ref.get('role') == messages[ref['message_id']].get('role'))}
    image_ids = {ref.get('image_id'): ref for ref in manifest.values()}
    media_ids = {_media_id('customer_image', ref['message_id']): ref for ref in manifest.values()}
    result = {}
    for key in REQUIREMENT_FIELDS:
        if key == 'product_url':
            continue  # The product library derives it, never the model.
        row = raw.get(key)
        if not isinstance(row, dict):
            continue
        value, ids = row.get('value'), row.get('evidence_ids')
        if not isinstance(ids, list) or any(not isinstance(i, str) or i not in messages for i in ids):
            continue
        if key == 'product_images':
            if not isinstance(value, list) or any(not isinstance(i, str) or i not in manifest and i not in image_ids and i not in media_ids for i in value):
                continue
            refs = [manifest.get(i) or image_ids.get(i) or media_ids[i] for i in value]
            if any(ref.get('message_id') not in ids for ref in refs):
                continue
            value = list(dict.fromkeys(_media_id('customer_image', ref['message_id']) for ref in refs))
        elif value is not None and (not isinstance(value, str) or len(value) > 10000):
            continue
        elif isinstance(value, str):
            value = value.strip() or None
        if value and not ids:
            continue
        if key == 'product_id' and value:
            from .product_identity import product_kind
            kind = product_kind(value)
            if kind == 'alibaba':
                if not any(messages[mid].get('role') == 'buyer' and
                           re.search(r'(?<![0-9])' + re.escape(value) + r'(?![0-9])', messages[mid].get('text', ''))
                           for mid in ids):
                    continue
            elif kind == 'custom':
                product = result.get('product') or (owner.get('requirements') or {}).get('product') or {}
                product_ids = product.get('evidence_ids') if isinstance(product, dict) else None
                if (not isinstance(product.get('value'), str) or not product['value'].strip()
                        or not isinstance(product_ids, list) or not product_ids
                        or not any(mid in messages and messages[mid].get('role') == 'buyer'
                                   and messages[mid].get('text', '').strip()
                                   and not social_intent(messages[mid]['text']) for mid in product_ids)):
                    continue
                previous = (owner.get('requirements') or {}).get('product_id') or {}
                prior_id = previous.get('value') if isinstance(previous, dict) else None
                if product_kind(prior_id) == 'custom':
                    value = prior_id
            else:
                continue
        excerpts = row.get('source_excerpts', {})
        if not isinstance(excerpts, dict) or any(mid not in ids or not isinstance(text, str)
                or not text or text not in messages[mid].get('text', '') for mid, text in excerpts.items()):
            continue
        checked = {'value': value, 'evidence_ids': list(dict.fromkeys(ids))}
        if excerpts:
            checked['source_excerpts'] = dict(excerpts)
        from .procurement import evidence_snapshot
        checked['evidence_versions'] = evidence_snapshot(checked, messages)['evidence_versions']
        result[key] = checked
    return result
# Stable metadata only; shared executable wording lives in reception_prompts.
RULES = ['prompt_version=' + PROMPT_VERSION]


def missing_fields(facts: dict, required: list[str]) -> list[str]:
    placeholders = {'', 'unknown', 'unspecified', 'not sure', "i don't know", 'tbd', 'n/a', 'null', 'none',
                    'not decided', 'undecided', 'not specified', 'to be confirmed',
                    '待确认', '未知', '不确定', '不知道', '未指定', '未提供', '待定'}
    result = []
    for key in dict.fromkeys(['product'] + required):
        fact = facts.get(key, {})
        value = fact.get('value', '')
        valid = isinstance(value, str) and value.strip().casefold() not in placeholders and fact.get('status') == 'confirmed'
        if valid and key == 'quantity':
            numbers = re.findall(r'(?<![\w.])-?\d+(?:[.,]\d+)*', value)
            unit = re.search(r'\b(?:pieces?|pcs?|units?|sets?|pairs?|cartons?|boxes|box|dozens?|kg|kilograms?|g|grams?|'
                             r'tons?|tonnes?|lit(?:er|re)s?|ml|met(?:er|re)s?|m|rolls?|packs?|bags?|bottles?)\b|'
                             r'件|个|套|双|箱|打|千克|公斤|克|吨|升|米|卷|包|袋|瓶', value, re.I)
            valid = bool(numbers and unit) and all(math.isfinite(float(n.replace(',', ''))) and float(n.replace(',', '')) > 0 for n in numbers)
        if not valid:
            result.append(key)
    return result


def missing_fields_for_case(facts: dict, settings: dict, case: dict) -> list[str]:
    if case.get('requirements_format') == 'ai-v1':
        values = {key: {'value': row.get('value'), 'status': 'confirmed'}
                  for key, row in (case.get('requirements') or {}).items() if isinstance(row, dict)}
        return missing_fields(values, effective_required_fields(settings, case))
    values = dict(facts)
    ref = case.get('product_ref') or {}
    if (not values.get('product') and ref.get('product_id') and not ref.get('source_invalid')
            and ref.get('suitability') == 'applicable'):
        values['product'] = {'value': ref['product_id'], 'status': 'confirmed'}
    return missing_fields(values, effective_required_fields(settings, case))


def applicable_policies(policies: list[dict], case: dict) -> list[dict]:
    return select_policy_inputs(policies, case)['knowledge']


def build_context(repo, case: dict, settings: dict, budget: dict, *, continuation=None) -> dict:
    conversation = repo.get('conversation', case['conversation_id'])
    result = shared_context(repo, case, case, task_mode='automatic', continuation=continuation, settings=settings)
    recent = result['recent_messages']
    result.update(rules=RULES, case=case, history_coverage=repo.get('coverage', case['conversation_id']),
                  budget=budget, required_fields=effective_required_fields(settings, case), conversation_revision=conversation['revision'],
                  conversation_intent=social_intent(recent[-1]['text']) if recent and recent[-1]['role'] == 'buyer' and not recent[-1].get('attachments') else None)
    if continuation is not None:
        result['continuation'] = dict(continuation) if isinstance(continuation, dict) else continuation
        _, checked_recent = context_messages(result)
        source = current_message(result, checked_recent)
        result['reply_target'] = {'source_message_id': source['message_id']}
        result['conversation_intent'] = (social_intent(source['text'])
                                         if not source.get('attachments') else None)
    # Reject oversized input explicitly; never silently remove source evidence.
    model_json(result)
    # Usage is telemetry, not evidence. A real call increments it after context
    # construction and must not invalidate its own otherwise unchanged reply.
    result['fingerprint'] = semantic_fingerprint(result)
    return result


def validate_reply(body: str, sources: list[str], context: dict, *, max_chars=1200) -> dict:
    messages = context['recent_messages'] + context['historical_evidence']
    issues = []
    if not isinstance(body, str) or not body.strip():
        issues.append('回复正文为空')
    elif len(body) > max_chars or invalid_reply_body(body):
        issues.append('回复正文格式无效')
    return {'ready': not issues, 'issues': list(dict.fromkeys(issues)), 'sources': sources}


def reply_validation_reason(validation):
    for issue in validation.get('issues', []):
        if issue.startswith('数值 '):
            return 'number_unverified'
        if '卖家资料依据' in issue:
            return 'commitment_unverified'
        if '正文来源' in issue:
            return 'sources_missing'
        if '正文为空' in issue:
            return 'reply_empty'
        if '重复询问' in issue:
            return 'question_repeated'
    return 'reply_unverified'


def validated_facts(output: dict, context: dict, *, warnings=None) -> dict:
    from .procurement import buyer_fact_text_allowed
    messages = {m['message_id']: m for m in context['recent_messages'] + context['historical_evidence']}
    facts = {}
    for key, fact in output.get('facts', {}).items():
        ids = fact.get('evidence_ids') if isinstance(fact, dict) else None
        value = fact.get('value') if isinstance(fact, dict) else None
        ambiguous = any(isinstance(item, dict) and item.get('field') == key and item.get('value') == value
                        and item.get('expression', item.get('intent')) in ('question', 'alternative', 'option', 'negated', 'undecided')
                        for item in (output.get('requirement_items') if isinstance(output.get('requirement_items'), list) else []))
        valid = (not ambiguous and key in FIELDS and isinstance(value, str) and bool(value.strip())
                 and isinstance(ids, list) and bool(ids) and all(isinstance(source, str) for source in ids))
        if valid:
            valid = all(source in messages and messages[source]['role'] == 'buyer'
                        and not is_photo_placeholder(messages[source]['text'])
                        and buyer_fact_text_allowed(key, messages[source]['text'], value)
                        and value.casefold() in messages[source]['text'].casefold()
                        and all(messages[source].get(k) == context['case'].get(k)
                                for k in ('seller_id', 'buyer_id', 'conversation_id')) for source in ids)
        if not valid:
            if warnings is not None:
                warnings.append({'code': 'fact_ungrounded', 'message': '该需求字段缺少有效买家原文，未保存为确认事实。',
                                 'field': str(key)[:100]})
            continue
        facts[key] = {'value': value.strip(), 'evidence_ids': list(dict.fromkeys(ids)), 'status': 'confirmed'}
        display = fact.get('display_value')
        if isinstance(display, str) and display.strip() and len(display) <= 2000:
            facts[key]['display_value'] = display.strip()
    return facts
