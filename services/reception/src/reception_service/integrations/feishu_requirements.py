"""Verify formal Chinese requirements against freshly read buyer sources."""
from __future__ import annotations

from copy import deepcopy

from ..context import REQUIREMENT_FIELDS, validated_requirements
from ..procurement import evidence_snapshot
from .accio_native.vendor.adapters.common import AdapterError
from .accio_native.vendor.registration_fields import build_registration_fields

LABELS = dict(product='采购内容', product_id='商品编号', product_url='商品链接', product_images='需求图片',
              quantity='数量', specifications='规格', customization='定制', destination='目的地',
              delivery='交期', budget='预算', packaging='包装', certifications='认证', contact='联系方式')


def verify_requirements(case, messages):
    rows = case.get('requirements')
    if not isinstance(rows, dict) or set(rows) - set(REQUIREMENT_FIELDS):
        raise AdapterError('正式需求结构无效')
    sources = {}
    for message in messages:
        if not isinstance(message, dict):
            raise AdapterError('买家来源无效')
        mid = message.get('message_id')
        if not isinstance(mid, str) or not mid or mid.startswith('missing-id-') or mid in sources:
            raise AdapterError('买家来源编号无效或重复')
        if message.get('role') != 'buyer' or any(message.get(k) != case.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id')) or not isinstance(message.get('text'), str):
            raise AdapterError('买家来源身份不匹配')
        sources[mid] = message
    active = {k: v for k, v in rows.items() if isinstance(v, dict) and v.get('value') not in (None, '', []) and k != 'product_url'}
    if not isinstance(active.get('product', {}).get('value'), str) or not active['product']['value'].strip():
        raise AdapterError('正式需求缺少采购内容')
    for row in active.values():
        ids, versions = row.get('evidence_ids'), row.get('evidence_versions')
        if (not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or
                any(not isinstance(mid, str) or mid not in sources for mid in ids) or
                not isinstance(versions, dict) or set(versions) != set(ids) or
                evidence_snapshot(row, sources)['evidence_versions'] != versions):
            raise AdapterError('正式需求来源已变化或不属于当前买家')
        excerpts = row.get('source_excerpts', {})
        if not isinstance(excerpts, dict) or any(mid not in ids or not isinstance(text, str) or not text or text not in sources[mid]['text'] for mid, text in excerpts.items()):
            raise AdapterError('正式需求原文片段无效')
    # Existing validator checks type, identity, excerpts, and special product IDs.
    # Image IDs refer to local media, so validate their provenance above and do
    # not fabricate a model image-read manifest here.
    check = {k: v for k, v in active.items() if k != 'product_images'}
    validated = validated_requirements(check, {'case': case, 'recent_messages': list(sources.values())})
    if set(validated) != set(check) or any(validated[k]['value'] != v['value'] or validated[k]['evidence_versions'] != v['evidence_versions'] for k, v in check.items()):
        raise AdapterError('正式需求字段核验失败')
    images = active.get('product_images')
    if images and (not isinstance(images['value'], list) or any(not isinstance(x, str) or not x for x in images['value'])):
        raise AdapterError('需求图片标识无效')
    summary = []
    for key in REQUIREMENT_FIELDS:
        row = active.get(key)
        if row:
            value = ('已关联%d张' % len(row['value'])) if key == 'product_images' else row['value']
            summary.append(LABELS[key] + '：' + value)
    # A derived platform link is not an independent buyer fact. The program
    # may show it only when it exactly matches the validated platform ID.
    from ..product_identity import product_link
    product_id = (active.get('product_id') or {}).get('value')
    url = (rows.get('product_url') or {}).get('value') if isinstance(rows.get('product_url'), dict) else None
    if url and product_id and url == product_link(product_id):
        summary.append(LABELS['product_url'] + '：' + url)
    return '\n'.join(summary)


def derived_fields(payload, mapping, policy):
    """Project typed columns from literal excerpts, never Chinese paraphrases."""
    case = deepcopy(payload['case'])
    facts = {}
    for key, row in (case.get('requirements') or {}).items():
        if not isinstance(row, dict):
            continue
        excerpts = row.get('source_excerpts') or {}
        if len(excerpts) == 1:
            mid, text = next(iter(excerpts.items()))
            facts[key] = {'value': text, 'evidence_ids': [mid]}
    case['facts'] = facts
    case['source_message_ids'] = [m['message_id'] for m in payload['verified_messages']]
    return build_registration_fields(dict(payload, case=case), mapping, policy)
