"""Grounded analysis shared by local demo and the independent native AI call."""
from __future__ import annotations

import json
import re
import time

from ..errors import ServiceError
from ..photos import photo_context, photo_reply_issues, is_photo_placeholder
from .accio_native.vendor.fact_rules import is_complete
from .accio_native.vendor.models import FACT_LABELS
from .accio_native.vendor.adapters.tool_signals import output_has_tools
from .reply_policy import (NUMBER, commitment_supported, has_seller_commitment,
                           numbers_needing_evidence, numeric_value, seller_commitments)

FIELDS = frozenset(FACT_LABELS)


def fail(code, message, stage='schema'):
    raise ServiceError(code, message, stage=stage)


def context_messages(context):
    if not isinstance(context, dict) or not isinstance(context.get('case'), dict):
        fail('invalid_context', '接待上下文缺少采购事项', 'context')
    case = context['case']
    recent = context.get('recent_messages', context.get('messages', []))
    historical = context.get('historical_evidence', [])
    if not isinstance(recent, list) or not isinstance(historical, list):
        fail('invalid_context', '接待上下文消息格式无效', 'context')
    messages = historical + recent
    seen = {}
    for message in messages:
        if not isinstance(message, dict) or any(
                not isinstance(case.get(key), str) or not case[key] or message.get(key) != case[key]
                for key in ('seller_id', 'buyer_id', 'conversation_id')):
            fail('identity_mismatch', '接待上下文包含不属于本客户的消息', 'context')
        if (not isinstance(message.get('message_id'), str) or not message['message_id']
                or message.get('role') not in ('buyer', 'seller')
                or not isinstance(message.get('text'), str)):
            fail('invalid_context', '接待上下文消息格式无效', 'context')
        previous = seen.get(message['message_id'])
        if previous is not None and previous != message:
            fail('invalid_context', '接待上下文包含不同版本的同一消息', 'context')
        seen[message['message_id']] = message
    if not recent:
        fail('context_empty', '没有可供生成的当前消息', 'context')
    return list(seen.values()), recent


def current_message(context, recent):
    """Resolve the reply source without rewriting the actual conversation history."""
    if 'continuation' not in context:
        return recent[-1]
    meta = context['continuation']
    keys = {'phase', 'source_message_id', 'acknowledgement_message_id'}
    if (not isinstance(meta, dict) or set(meta) != keys or meta.get('phase') != 'final'
            or any(not isinstance(meta.get(key), str) or not meta[key].strip()
                   for key in ('source_message_id', 'acknowledgement_message_id'))):
        fail('invalid_continuation', '续答来源信息无效', 'context')
    if len(recent) < 2:
        fail('invalid_continuation', '续答来源或确认消息不存在', 'context')
    source, ack = recent[-2:]
    if (source['role'] != 'buyer' or source['message_id'] != meta['source_message_id']
            or ack['role'] != 'seller' or ack.get('own') is not True
            or ack['message_id'] != meta['acknowledgement_message_id']
            or source['message_id'] == ack['message_id']):
        fail('invalid_continuation', '续答来源或最新确认消息已变化', 'context')
    return source


def knowledge_rows(context):
    rows = context.get('knowledge', [])
    if not isinstance(rows, list):
        fail('invalid_context', '知识资料格式无效', 'context')
    case = context['case']
    product = (case.get('facts', {}).get('product', {}).get('value') or '').casefold()
    procurement = str(case.get('procurement_key') or '').casefold()
    allowed = []
    for row in rows:
        if not isinstance(row, dict):
            fail('invalid_context', '知识资料格式无效', 'context')
        if row.get('confirmed') is not True or row.get('public') is not True:
            continue
        expiry = row.get('valid_until')
        if expiry is not None and (type(expiry) is not int or expiry <= time.time_ns() // 1_000_000):
            continue
        scopes = row.get('product_scope') or []
        if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
            fail('invalid_context', '知识资料适用范围无效', 'context')
        if scopes and not any(scope.casefold() in product or scope.casefold() == procurement for scope in scopes):
            continue
        if not isinstance(row.get('policy_id'), str) or not isinstance(row.get('content'), str):
            fail('invalid_context', '知识资料缺少来源或正文', 'context')
        allowed.append(row)
    return allowed


def prepare_context(context):
    messages, recent = context_messages(context)
    current_message(context, recent)
    knowledge = knowledge_rows(context)
    photo = context.get('photo_context')
    observations = photo.get('observations', []) if isinstance(photo, dict) else []
    prepared = dict(context, knowledge=knowledge, photo_context=photo_context(messages, observations))
    # JSON encoding also prevents accidental execution of attachment content.
    try:
        encoded = json.dumps(prepared, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        fail('invalid_context', '接待上下文无法编码', 'context')
    if len(encoded) > 120_000:
        fail('context_too_long', '接待上下文超出单次生成预算，未截断证据', 'context')
    return prepared, messages, recent


def _conversation_sources(body, sources, recent):
    # Never replace supplied (possibly foreign/invalid) IDs. Only an ordinary
    # conversational reply may attach to the latest real buyer message here.
    if not sources and body.strip() and not has_seller_commitment(body):
        latest = next((row for row in reversed(recent) if row['role'] == 'buyer'), None)
        if latest is not None:
            return [latest['message_id']]
    return sources


def reply_grounding_issues(body, sources, context):
    """Check actual assertions while allowing ordinary customer conversation."""
    messages, recent = context_messages(context)
    knowledge = knowledge_rows(context)
    sources = _conversation_sources(body, sources, recent)
    allowed = {row['message_id']: row['text'] for row in messages}
    allowed.update({row['policy_id']: row['content'] for row in knowledge})
    photo = context.get('photo_context')
    observations = photo_context(messages, photo.get('observations', []) if isinstance(photo, dict) else [])['observations']
    allowed.update({row['observation_id']: '\n'.join([row['description'], *row['visible_text'], *row['uncertainties']])
                    for row in observations})
    issues = photo_reply_issues(body, messages, observations, sources)
    if not sources or any(not isinstance(source, str) or source not in allowed for source in sources):
        issues.append('正文来源缺失或超出本客户及适用资料范围')
    observation_numbers = {row['observation_id']: '\n'.join(row['visible_text']) for row in observations}
    cited = '\n'.join(observation_numbers.get(source, allowed[source]) for source in sources
                      if isinstance(source, str) and source in allowed)
    cited_numbers = {numeric_value(number) for number in NUMBER.findall(cited)}
    for number in dict.fromkeys(numbers_needing_evidence(body)):
        if numeric_value(number) not in cited_numbers:
            issues.append(f'数值 {number} 缺少引用依据')
    policy_contents = [row['content'] for row in knowledge if row['policy_id'] in sources]
    for clause in seller_commitments(body):
        if not commitment_supported(clause, policy_contents):
            issues.append('费用、报价、折扣、库存或履约承诺缺少适用已确认公开卖家资料依据')
    return list(dict.fromkeys(issues))


def quality_warnings(body, sources, context):
    warnings = []
    for issue in reply_grounding_issues(body, sources, context):
        if issue.startswith('数值 '):
            code, message = 'number_unverified', '正文中的数字缺少资料依据，请核实。'
        elif '卖家资料依据' in issue:
            code, message = 'commitment_unverified', '正文中的商业条件缺少适用资料依据，请核实。'
        elif '正文来源' in issue:
            code, message = 'sources_missing', '正文来源不完整或不适用，请核实。'
        else:
            code, message = 'photo_unverified', '图片相关描述需要核实。'
        warning = {'code': code, 'message': message}
        if warning not in warnings:
            warnings.append(warning)
    return warnings


def invalid_reply_body(body):
    if not isinstance(body, str):
        return True
    text = body.strip()
    return bool(text and (text.startswith(('{', '[', '```', '<tool', '<function')) or
                          output_has_tools(text)))


def validate_analysis(raw, context):
    context, messages, recent = prepare_context(context)
    if output_has_tools(raw):
        fail('tool_approval', '生成结果包含工具或审批信号，已阻断', 'answer')
    if not isinstance(raw, dict) or raw.get('action') not in ('ask', 'wait_quote', 'human', 'skip'):
        fail('schema_action', '生成结果的接待动作无效')
    if not isinstance(raw.get('reply'), str):
        fail('answer_non_json', 'AI 未返回完整且明确的回复正文', 'answer')
    raw = dict(raw)
    repairs = []
    defaults = {'summary': '', 'procurement_key': context['case'].get('procurement_key') or 'main',
                'new_case': False, 'question_keys': [], 'missing': [], 'conflicts': [],
                'reply_sources': [], 'facts': {}}
    for key, value in defaults.items():
        if key not in raw:
            raw[key] = value
            repairs.append(key)
    if any(not isinstance(raw.get(key), str) for key in ('summary', 'procurement_key')):
        fail('schema_text', '生成结果的采购事项无效')
    if not raw['procurement_key'].strip():
        raw['procurement_key'] = context['case'].get('procurement_key') or 'main'
        repairs.append('procurement_key')
    if (len(raw['reply']) > 1200 or invalid_reply_body(raw['reply'])
            or (raw['action'] != 'skip' and not raw['reply'].strip())):
        fail('answer_non_json', 'AI 未返回完整且明确的回复正文', 'answer')
    if type(raw.get('new_case')) is not bool:
        fail('schema_procurement', '生成结果的采购事项标记无效')
    for key in ('question_keys', 'missing', 'conflicts', 'reply_sources'):
        if not isinstance(raw.get(key), list) or any(not isinstance(value, str) for value in raw[key]):
            fail('schema_lists', '生成结果的列表字段无效')
    warnings = []
    fact_issues = []
    for key in ('question_keys', 'missing'):
        if set(raw[key]) - FIELDS:
            raw[key] = [value for value in raw[key] if value in FIELDS]
            warnings.append({'code': 'invalid_fact_key', 'message': '部分采购字段无效，已忽略。'})
    if len(raw['question_keys']) > 2 or len(set(raw['question_keys'])) != len(raw['question_keys']):
        warnings.append({'code': 'question_repeated', 'message': '提问可能重复或过多，请核实。'})
        raw['question_keys'] = list(dict.fromkeys(raw['question_keys']))[:2]
    evidence = {row['message_id']: row['text'] for row in messages if row['role'] == 'buyer' and not is_photo_placeholder(row['text'])}
    facts = raw.get('facts')
    if not isinstance(facts, dict):
        fail('schema_facts', '生成结果的采购事实无效')
    checked = {}
    for key, fact in facts.items():
        ids = fact.get('evidence_ids') if isinstance(fact, dict) else None
        value = fact.get('value') if isinstance(fact, dict) else None
        if (key not in FIELDS or not isinstance(value, str) or not value.strip()
                or not isinstance(ids, list) or not ids
                or any(not isinstance(mid, str) or mid not in evidence for mid in ids)
                or any(value not in evidence[mid] for mid in ids)):
            fact_issues.append({'code': 'fact_unverified', 'message': '采购事实缺少有效买家原文依据，已忽略。',
                                'field': key if key in FIELDS else ''})
            continue
        checked[key] = {'value': value, 'evidence_ids': list(dict.fromkeys(ids))}
        display = fact.get('display_value')
        if isinstance(display, str) and display.strip() and len(display) <= 2000:
            checked[key]['display_value'] = display.strip()
    latest = current_message(context, recent)
    if latest['role'] == 'buyer' and is_photo_placeholder(latest['text']):
        # A photo event without pixels cannot establish a new product/summary.
        raw = dict(raw, new_case=False, procurement_key=context['case'].get('procurement_key') or 'main',
                   summary=context['case'].get('summary', ''))
    case = {} if raw['new_case'] else context['case']
    values = {key: value.get('value', '') for key, value in (case.get('facts') or {}).items() if isinstance(value, dict)}
    values.update({key: fact['value'] for key, fact in checked.items()})
    questions = case.get('questions') or {}
    if any(is_complete(key, values.get(key, '')) or questions.get(key, 0) >= 2 for key in raw['question_keys']):
        warnings.append({'code': 'question_repeated', 'message': '正文可能重复询问已知需求，请核实。'})
        raw['question_keys'] = [key for key in raw['question_keys']
                                if not is_complete(key, values.get(key, '')) and questions.get(key, 0) < 2]
    required = context.get('required_fields', ['product', 'quantity'])
    if not isinstance(required, list) or any(key not in FIELDS for key in required):
        fail('invalid_context', '采购必填字段配置无效', 'context')
    if raw['action'] == 'wait_quote' and any(not is_complete(key, values.get(key, '')) for key in ['product', *required]):
        raw['action'] = 'ask'
        warnings.append({'code': 'facts_incomplete', 'message': '采购必要事实尚不完整，请继续核实。'})
    sources = {row['message_id']: row['text'] for row in messages}
    knowledge = knowledge_rows(context)
    sources.update({row['policy_id']: row['content'] for row in knowledge})
    sources.update({row['observation_id']: row['description'] for row in context['photo_context']['observations']})
    reply_sources = _conversation_sources(raw['reply'], raw['reply_sources'], recent)
    warnings.extend(quality_warnings(raw['reply'], reply_sources, context))
    raw['reply_sources'] = [source for source in reply_sources if source in sources]
    latest_text = '\n'.join(row['text'] for row in messages if row['role'] == 'buyer')
    if re.search(r'logo|印标|标志', values.get('customization', '') + latest_text, re.I) and re.search(
            r'(?:do you (?:want|need)|would you like|是否需要|需要.*吗).*logo', raw['reply'], re.I):
        warnings.append({'code': 'question_repeated', 'message': '客户已提出 Logo 需求，请避免重复询问。'})
    result = {key: (checked if key == 'facts' else raw[key]) for key in (
        'action', 'facts', 'question_keys', 'reply', 'summary', 'missing', 'conflicts',
        'procurement_key', 'new_case', 'reply_sources')}
    assistance_requests = []
    if re.search(r'\b(?:still\s+(?:checking|confirming)|(?:check|confirm|verify)\w*\s+(?:this\s+)?with\s+(?:our|the)\s+team)\b|'
                 r'(?:仍在|正在|还在).{0,12}(?:团队|业务员).{0,8}(?:确认|核实)|'
                 r'(?:团队|业务员).{0,8}(?:确认|核实)', raw['reply'], re.I):
        assistance_requests.append({'kind': 'verify_followup', 'detail': '回复中提及需要团队核实，请核实后跟进。', 'fact_keys': []})
    result.update(warnings=warnings, fact_issues=fact_issues, repairs=repairs,
                  assistance_requests=assistance_requests)
    return result
