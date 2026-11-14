"""Grounded analysis shared by local demo and the independent native AI call."""
from __future__ import annotations

import json
import re
import time

from ..errors import ServiceError
from ..reply_messages import normalize_reply_output
from ..model_payload import model_json
from ..business_supplement import supplement_sources
from ..procurement import buyer_fact_text_allowed
from ..photos import photo_context, photo_reply_issues, is_photo_placeholder, scoped_photo_context, direct_photo_sources
from .accio_native.vendor.fact_rules import is_complete
from .accio_native.vendor.models import FACT_LABELS
from .accio_native.vendor.adapters.tool_signals import output_has_tools
from .generation_metadata import checked_metadata
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
    if isinstance(meta, dict) and meta.get('phase') == 'supplement':
        keys = {'phase', 'source_message_id', 'latest_message_id', 'business_note_source_id', 'issue_ids'}
        candidates = (context.get('historical_evidence') or []) + recent
        source = next((m for m in candidates if m.get('message_id') == meta.get('source_message_id')), None)
        if (set(meta) != keys or not source or source.get('role') != 'buyer' or not recent
                or recent[-1].get('role') != 'seller' or recent[-1].get('message_id') != meta.get('latest_message_id')
                or not isinstance(meta.get('issue_ids'), list) or not meta['issue_ids']
                or any(not isinstance(i, str) or not i for i in meta['issue_ids'])
                or meta.get('business_note_source_id') not in supplement_sources(context)):
            fail('invalid_continuation', '业务补充续答来源已变化', 'context')
        return source
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
    product = ((case.get('requirements') or {}).get('product', {}).get('value')
               or case.get('facts', {}).get('product', {}).get('value') or '').casefold()
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
    prepared = dict(context, knowledge=knowledge, photo_context=scoped_photo_context(messages, photo))
    # JSON encoding also prevents accidental execution of attachment content.
    try:
        encoded = json.dumps(prepared, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        fail('invalid_context', '接待上下文无法编码', 'context')
    model_json(prepared)
    return prepared, messages, recent


def _conversation_sources(body, sources, recent):
    # Never replace supplied (possibly foreign/invalid) IDs. Only an ordinary
    # conversational reply may attach to the latest real buyer message here.
    if not sources and body.strip() and not has_seller_commitment(body):
        latest = next((row for row in reversed(recent) if row['role'] == 'buyer'), None)
        if latest is not None:
            return [latest['message_id']]
    return sources


def invalid_reply_body(body):
    if not isinstance(body, str):
        return True
    text = body.strip()
    return bool(text and (text.startswith(('{', '[', '```', '<tool', '<function')) or
                          output_has_tools(text)))


def validate_analysis(raw, context, *, allow_legacy=False):
    context, messages, recent = prepare_context(context)
    if output_has_tools(raw):
        fail('tool_approval', '生成结果包含工具或审批信号，已阻断', 'answer')
    if not isinstance(raw, dict) or raw.get('action') not in ('ask', 'wait_quote', 'human', 'skip'):
        fail('schema_action', '生成结果的接待动作无效')
    if allow_legacy and raw['action'] == 'skip' and 'reply_messages' not in raw and not raw.get('reply'):
        raw = dict(raw, reply_messages=[])
    try:
        raw = normalize_reply_output(raw, allow_legacy=allow_legacy, allow_empty=raw['action'] == 'skip')
    except ValueError:
        fail('answer_non_json', 'AI 未返回完整且明确的回复正文', 'answer')
    if (raw['action'] == 'skip' and raw['reply_messages'] or
            any(invalid_reply_body(item['body']) for item in raw['reply_messages']) or invalid_reply_body(raw['reply'])):
        fail('answer_non_json', 'AI 未返回完整且明确的回复正文', 'answer')
    allowed = {m['message_id'] for m in messages} | set(supplement_sources(context))
    from ..requirement_changes import reply_scopes
    scopes = reply_scopes(raw, context)
    for scope in scopes:
        allowed.update(row.get('policy_id') or row.get('source_id') for row in knowledge_rows(dict(scope, case=scope.get('case') or scope)))
    allowed.update(direct_photo_sources(context.get('photo_context') or {}))
    allowed.update(row['image_id'] for row in context.get('supplement_image_reads', [])
                   if row.get('origin') == 'business_supplement')
    for scope in scopes:
        supplier = scope.get('supplier_context') or {}
        if supplier.get('usable') is True and supplier.get('customer_wording') and isinstance(supplier.get('source_id'), str):
            allowed.add(supplier['source_id'])
    from ..requirement_changes import validate_changes
    validate_changes(raw, context)
    from ..context import validate_reply
    for item in raw['reply_messages']:
        item['reply_sources'] = [source for source in _conversation_sources(item['body'], item['reply_sources'], recent) if source in allowed]
        if not validate_reply(item['body'], item['reply_sources'], context)['ready']:
            fail('answer_non_json', 'AI 未返回有效回复正文', 'answer')
    raw = normalize_reply_output(raw, allow_empty=raw['action'] == 'skip')
    if raw['reply_messages']:
        validation = validate_reply(raw['reply'], raw['reply_sources'], context)
        # Display separators are not part of the combined message-body budget.
        if not validation['ready'] and not (len(raw['reply']) > 1200 and validation['issues'] == ['回复正文格式无效']):
            fail('answer_non_json', 'AI 未返回有效回复正文', 'answer')
    return {'action': raw['action'], 'reply': raw['reply'], 'reply_messages': raw['reply_messages'],
            'reply_sources': raw['reply_sources'],
            **{key: raw[key] for key in ('requirement_changes', 'reply_case_refs') if key in raw},
            'conversation_summary': raw.get('conversation_summary') if isinstance(raw.get('conversation_summary'), str) else None,
            'facts': {}, 'question_keys': [], 'missing': [], 'conflicts': [], 'repairs': [],
            'procurement_key': context['case'].get('procurement_key') or 'main'}
