"""Task-specific model projection; the full host context remains authoritative."""
from __future__ import annotations

from copy import deepcopy
import json
import re

_URL = re.compile(r'https?://[^\s<>"\']+', re.I)
_SKU_IDENTIFIER = re.compile(r'\bsku\s*(?:id|code|number|no\.?)?\s*[:#=]\s*[a-z0-9][a-z0-9_-]*|\bsku\s+(?=[a-z0-9_-]*\d)[a-z0-9][a-z0-9_-]*', re.I)
_MEDIA_KEYS = re.compile(r'^(?:attachments?|images?|image_?url|imageUrl|thumbnail.*|cover.*|sku(?:s|_.*|[A-Z].*)?|product_?url|productUrl|url|link|links|card|card_?fields|cardFields|raw|raw_json|metadata|media|photo.*)$', re.I)


def _safe_text(value):
    text = value.strip()
    if re.match(r'^\[card\]|^this is a (?:single )?product card\b', text, re.I):
        return ''
    if text.startswith(('{', '[')):
        try:
            parsed, end = json.JSONDecoder().raw_decode(text)
        except (ValueError, TypeError):
            parsed, end = None, 0
        if isinstance(parsed, (dict, list)):
            safe = _model_safe(parsed)
            if isinstance(safe, dict) and safe.get('type') in ('product_card', 'product_reference'):
                prefix = safe.get('text', safe.get('message', ''))
            else:
                prefix = json.dumps(safe, ensure_ascii=False)
            suffix = _safe_text(text[end:]) if text[end:].strip() else ''
            return (prefix + ('\n' if prefix and suffix else '') + suffix).strip()
    return _SKU_IDENTIFIER.sub('', _URL.sub('', value))


def _model_safe(value):
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [_model_safe(item) for item in value]
    if isinstance(value, dict):
        keys = {str(k).casefold().replace('_', '') for k in value}
        card_type = str(value.get('cardType', value.get('card_type', value.get('type', '')))).casefold()
        if (card_type in ('product_card', 'productcard') or card_type == 'product'
                and (bool(keys.intersection({'cardtype', 'fields', 'productid', 'producturl', 'sku', 'skulist', 'imageurl'})))):
            return {'type': 'product_card'}
        if (keys.intersection({'productid', 'producturl', 'skuimages', 'skulist', 'skuinfo'})
                or 'sku' in keys and keys.intersection({'name', 'title', 'price', 'image', 'imageurl'})):
            # Keep explicitly supplied message text, never catalogue properties.
            return {'type': 'product_reference', **{k: _model_safe(value[k]) for k in ('text', 'message')
                                                     if isinstance(value.get(k), str)}}
        return {key: _model_safe(item) for key, item in value.items()
                if isinstance(key, str) and not _MEDIA_KEYS.match(key)}
    return deepcopy(value)

from .errors import ServiceError


MODEL_CONTEXT_CHARS = 120_000
MODEL_REQUEST_CHARS = 140_000


def model_json(context):
    """Budget the actual projection while preserving the full local evidence."""
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
        parts = []
        size = 0
        for part in encoder.iterencode(project_model_payload(context)):
            size += len(part)
            if size > MODEL_CONTEXT_CHARS:
                raise ServiceError('context_too_long', '模型输入超出单次预算，未截断证据', stage='context')
            parts.append(part)
        return ''.join(parts)
    except ServiceError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise ServiceError('invalid_context', '请求内容无法编码', stage='context') from None


def model_request(instructions, context, suffix=''):
    request = instructions + model_json(context) + suffix
    if len(request) > MODEL_REQUEST_CHARS:
        raise ServiceError('context_too_long', '完整请求超出单次预算，未截断证据', stage='context')
    return request

PAYLOAD_VERSION = 'reception-payload-0.3.16'


def _pick(row, keys):
    return {key: deepcopy(row[key]) for key in keys if key in row}


def _semantic(value):
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    if isinstance(value, dict):
        excluded = {'source_excerpts', 'evidence_versions', 'source_version', 'revision',
                    'seller_id', 'buyer_id', 'conversation_id', 'fingerprint', 'hash'}
        return {key: _semantic(item) for key, item in value.items() if key not in excluded}
    return deepcopy(value)


def _without_product_links_or_images(value):
    if isinstance(value, dict):
        return {key: _without_product_links_or_images(item) for key, item in value.items()
                if isinstance(key, str) and not re.search(r'url|link|image|photo|thumbnail|cover|主图', key, re.I)}
    if isinstance(value, list):
        return [_without_product_links_or_images(item) for item in value]
    if isinstance(value, str):
        return _URL.sub('', value)
    return deepcopy(value)


def project_model_payload(context: dict, task_mode: str | None = None) -> dict:
    if not isinstance(context, dict):
        return deepcopy(context)
    mode = task_mode or context.get('task_mode') or 'automatic'
    if mode == 'whatsapp_reply':
        # The resolver's private references and cached media metadata are never
        # part of model input; only the explicitly attached manifest is exposed.
        result = deepcopy(context)
        result.pop('model_image_refs', None)
        for row in result.get('whatsapp_messages', []):
            if isinstance(row, dict) and isinstance(row.get('media'), dict):
                row['media'] = {key: row['media'][key] for key in ('status',) if key in row['media']}
        return result
    result = {'payload_version': PAYLOAD_VERSION, 'task_mode': mode}
    common = ('reply_target', 'knowledge', 'style', 'supplement', 'message_semantics', 'collection_rules')
    for key in common:
        if key in context:
            result[key] = deepcopy(context[key])
    for key in ('knowledge', 'style'):
        if isinstance(result.get(key), list):
            result[key] = [_pick(row, ('source_id', 'title', 'content', 'product_scope', 'valid_until'))
                           for row in result[key] if isinstance(row, dict)]
    if isinstance(result.get('supplement'), dict):
        result['supplement'] = _pick(result['supplement'], ('source_id', 'body', 'attachments'))
    rows = context.get('message_view')
    if not isinstance(rows, list):
        rows = context.get('historical_evidence', []) + context.get('recent_messages', [])
    seen = set()
    timeline = []
    for row in sorted((row for row in rows if isinstance(row, dict)),
                      key=lambda r: (r.get('timestamp_ms') or -1, r.get('message_id') or '')):
        mid = row.get('message_id')
        if not mid or mid in seen:
            continue
        seen.add(mid)
        projected = _pick(row, ('message_id', 'role', 'timestamp_ms', 'text'))
        body = projected.get('text')
        if isinstance(body, str) and (_URL.search(body) or _safe_text(body) != body):
            projected['media_notice'] = 'product_or_media_metadata_filtered'
        if body == '[image]' and not any(isinstance(ref, dict) and ref.get('message_id') == mid
                                         for ref in context.get('image_manifest', [])):
            projected['text'] = ''
            projected['media_notice'] = 'image_not_available_to_this_request'
        timeline.append(projected)
    result['message_view'] = timeline
    if mode != 'polish':
        for key in ('guidance', 'continuation', 'required_fields', 'conversation_intent'):
            if key in context:
                result[key] = deepcopy(context[key])
        if isinstance(result.get('guidance'), list):
            result['guidance'] = [_pick(row, ('source_id', 'title', 'content', 'product_scope', 'valid_until'))
                                  for row in result['guidance'] if isinstance(row, dict)]
        case = context.get('case')
        if isinstance(case, dict):
            result['case'] = _pick(case, ('procurement_key', 'questions', 'state'))
        if 'followup_context' in context:
            result['followup_context'] = deepcopy(context['followup_context'])
        if isinstance(case, dict):
            result['requirements'] = _semantic({key: row for key, row in (case.get('requirements') or {}).items()
                                                if key != 'product_url'})
        if isinstance(context.get('conversation_summary'), dict):
            result['conversation_summary'] = context['conversation_summary'].get('body', '')
        if 'case_catalog' in context:
            result['case_catalog'] = []
            for entry in context['case_catalog']:
                row = _pick(entry, ('case_ref', 'state', 'procurement_key'))
                row['requirements'] = _semantic({key: value for key, value in entry['requirements'].items() if key != 'product_url'})
                for key in ('supplier_context', 'product_context'):
                    if key in entry:
                        row[key] = _without_product_links_or_images(entry[key])
                for key in ('knowledge', 'guidance', 'style'):
                    row[key] = [_pick(item, ('source_id', 'title', 'content', 'product_scope', 'valid_until')) for item in entry.get(key, [])]
                result['case_catalog'].append(row)
            result.update(_pick(context, ('current_case_ref', 'selected_case_ref')))
    elif isinstance(context.get('unsent_reply'), dict):
        result['unsent_reply'] = _pick(context['unsent_reply'], ('body',))
    manifest = context.get('image_manifest')
    if isinstance(manifest, list) and manifest:
        result['image_manifest'] = [_pick(row, ('alias', 'image_id', 'message_id', 'attachment_id', 'origin', 'role'))
                                    for row in manifest if isinstance(row, dict)]
        result['image_transport'] = context.get('image_transport', 'none')
    projected = _model_safe(result)
    # These are our typed requirement records, not platform card metadata.
    # Preserve their product_id/media IDs after removing the derived URL above.
    if mode != 'polish':
        for key in ('requirements', 'case_catalog'):
            if key in result:
                projected[key] = deepcopy(result[key])
    if isinstance(result.get('supplement'), dict):
        # Merchant-provided attachment text is business data, not product-card metadata.
        projected['supplement'] = deepcopy(result['supplement'])
    if 'image_manifest' in result:
        projected['image_manifest'] = deepcopy(result['image_manifest'])
        projected['image_transport'] = result['image_transport']
    for original, row in zip(timeline, projected['message_view']):
        if isinstance(original.get('text'), str):
            row['text'] = original['text']
            if _URL.search(row['text']):
                row['contains_product_reference'] = True
                if row.get('media_notice') == 'product_or_media_metadata_filtered' and row['text'] == original['text']:
                    row.pop('media_notice')
    if mode != 'polish':
        if 'supplier_context' in context:
            projected['supplier_context'] = _without_product_links_or_images(context['supplier_context'])
        if 'product_context' in context:
            projected['product_context'] = _without_product_links_or_images(context['product_context'])
    elif isinstance(context.get('unsent_reply'), dict) and isinstance(context['unsent_reply'].get('body'), str):
        # Polishing must preserve literal links and conditions in the frozen text.
        projected['unsent_reply']['body'] = context['unsent_reply']['body']
    return projected
