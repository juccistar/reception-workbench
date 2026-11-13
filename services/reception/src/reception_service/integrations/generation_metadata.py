"""Bounded metadata from model output; it never grants executable actions."""
from __future__ import annotations

import json

from ..errors import ServiceError
from ..business_supplement import supplement_sources
from ..photos import direct_photo_sources

_FIELDS = frozenset(('product', 'quantity', 'specifications', 'customization', 'destination', 'delivery', 'budget', 'packaging', 'contact'))
_NOTE_CODES = frozenset(('commitment_unverified', 'progress_unverified', 'policy_conflict', 'internal_content', 'scope_unclear', 'style_review'))


def applied_policy_summaries(context):
    rows = []
    for field, usage in (('knowledge', 'customer_reference'), ('guidance', 'internal_guidance'), ('style', 'style')):
        for row in context.get(field, []):
            source = row.get('source_id') or row.get('policy_id')
            if isinstance(source, str):
                rows.append({'source_id': source, 'title': str(row.get('title') or '适用资料')[:200],
                             'revision': row.get('revision', 1), 'usage': usage})
    return rows


def checked_metadata(raw, context, *, manual=False):
    keys = ('reply_sources', 'applied_guidance_ids')
    try:
        size = len(json.dumps({key: raw.get(key) for key in keys}, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except (TypeError, ValueError, RecursionError):
        raise ServiceError('invalid_metadata', 'AI 附加信息格式无效', stage='answer') from None
    if size > 64 * 1024:
        raise ServiceError('metadata_too_large', 'AI 附加信息超过资源预算，未截断有效来源', stage='answer')
    warnings = []
    messages = context.get('historical_evidence', []) + context.get('recent_messages', [])
    message_ids = {row['message_id'] for row in messages if isinstance(row, dict) and isinstance(row.get('message_id'), str)}
    public_ids = {row.get('policy_id') for row in context.get('knowledge', []) if row.get('public') is True}
    public_ids.update(supplement_sources(context))
    public_ids.update(direct_photo_sources(context.get('photo_context') or {}))
    supplier = context.get('supplier_context') or {}
    if supplier.get('usable') is True and supplier.get('customer_wording') and isinstance(supplier.get('source_id'), str):
        public_ids.add(supplier['source_id'])
    allowed_sources = message_ids | public_ids
    reply_sources = [value for value in raw.get('reply_sources', []) if isinstance(value, str) and value in allowed_sources] if isinstance(raw.get('reply_sources', []), list) else []
    guidance_ids = {row.get('source_id') or row.get('policy_id') for row in context.get('guidance', []) + context.get('style', [])}
    ids = raw.get('applied_guidance_ids', [])
    applied = list(dict.fromkeys(value for value in ids if isinstance(value, str) and value in guidance_ids)) if isinstance(ids, list) else []
    return {'reply_sources': list(dict.fromkeys(reply_sources)), 'applied_guidance_ids': applied,
            'applied_policies': applied_policy_summaries(context)}
