"""Shared customer-local context projections; never a long-lived AI memory."""
from __future__ import annotations

import copy
import hashlib
import re

from .photos import direct_photo_context, with_photo_notice, has_photo, pending_photo_message_ids
from .repository import encoded
from .business_supplement import supplement_context
from .policy_library import select_policy_inputs
from .collection_rules import collection_rules
from .integrations.reception_prompts import PROMPT_VERSION

_TOPICS = (
    re.compile(r'\b(?:photo|photos|picture|pictures|image|images|video)\b|图片|照片|视频', re.I),
    re.compile(r'\b(?:sample|samples|shipping|ship|address|delivery)\b|样品|寄送|地址|交期', re.I),
    re.compile(r'\b(?:price|quote|quotation|cost|budget|payment)\b|价格|报价|费用|预算', re.I),
    re.compile(r'\b(?:custom|customization|logo|print|fabric|size|color|colour)\b|定制|面料|尺码|颜色|印花', re.I),
)
_CARD = re.compile(r'\[card\]|\b(?:address|contact|product) card\b|地址卡|联系卡|商品卡|This is a .*card', re.I)


def dialogue_window(messages, case, continuation=None, pending_message_ids=(), limit=50):
    """Select recent text messages and missing, explicitly referenced evidence."""
    limit = limit if type(limit) is int and 1 <= limit <= 200 else 50
    def references(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('evidence_ids', 'source_message_ids', 'referenced_message_ids') and isinstance(child, list):
                    yield from (item for item in child if isinstance(item, str))
                elif key in ('source_message_id', 'referenced_message_id', 'reply_to_message_id') and isinstance(child, str):
                    yield child
                else:
                    yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)
    text_rows = [m for m in messages if m.get('role') in ('buyer', 'seller')
                 and isinstance(m.get('text'), str) and m['text'].strip()
                 and m['text'].strip() != '[image]'
                 and not m['text'].strip().casefold().startswith('[card]')]
    selected = {m['message_id'] for m in text_rows[-limit:]}
    required = set(references(case)) | set(references(continuation)) | set(pending_message_ids)
    required.update(references([m for m in messages if m['message_id'] in selected]))
    # Include associated cards and images in the chronological window without
    # consuming text slots; older attachments enter only through references.
    start = (next((i for i, m in enumerate(messages) if m['message_id'] in selected), 0)
             if len(text_rows) > limit else 0)
    selected.update(m['message_id'] for m in messages[start:]
                    if m.get('attachments') or has_photo(m)
                    or m.get('text', '').strip().casefold().startswith('[card]'))
    recent = [m for m in messages if m['message_id'] in selected]
    historical = [m for m in messages if m['message_id'] in required and m['message_id'] not in selected]
    return recent, historical


def add_image_references(context):
    """Short aliases project chronology; they never overwrite raw message text."""
    messages = {row['message_id']: row for row in context.get('historical_evidence', []) + context.get('recent_messages', [])}
    ordered = sorted(messages.values(), key=lambda m: (m.get('timestamp_ms') or -1, m['message_id']))
    refs = {row['message_id']: row for row in (context.get('photo_context') or {}).get('direct_images', [])}
    manifest, view = [], []
    for message in ordered:
        ref = refs.get(message['message_id'])
        image_refs = []
        if ref:
            alias = 'p' + str(len(manifest) + 1)
            manifest.append({'alias': alias, 'image_id': ref['image_id'], 'message_id': message['message_id'],
                             'role': message['role'], 'sender_id': message['seller_id'] if message['role'] == 'seller' else message['buyer_id'],
                             'timestamp_ms': message.get('timestamp_ms'), 'source_version': ref['source_version'],
                             'image_sha256': ref['image_sha256'], 'availability': 'ready_for_read'})
            image_refs.append({'alias': alias, 'image_id': ref['image_id']})
        view.append({'message_id': message['message_id'], 'role': message['role'],
                     'timestamp_ms': message.get('timestamp_ms'), 'text': message['text'],
                     **({'image_refs': image_refs} if image_refs else {})})
    for ref in context.get('supplement_image_refs', []):
        manifest.append({'alias': 'p' + str(len(manifest) + 1), 'image_id': ref['image_id'],
                         'attachment_id': ref['attachment_id'], 'origin': 'business_supplement',
                         'role': 'seller', 'sender_id': ref['seller_id'], 'image_sha256': ref['image_sha256'],
                         'availability': 'ready_for_read'})
    context.update(image_manifest=manifest, message_view=view, image_transport='read_compat' if manifest else 'none')
    return context


def shared_context(repo, owner, case, *, task_mode, continuation=None, settings=None):
    raw = [m for m in repo.list('message', conversation_id=owner['conversation_id'])
           if all(m.get(key) == owner.get(key) for key in ('seller_id', 'buyer_id', 'conversation_id'))]
    messages = sorted((with_photo_notice(m) for m in raw), key=lambda m: (m.get('timestamp_ms') or -1, m['message_id']))
    from .requirement_media import get_reception_source_images
    from .requirement_changes import case_catalog
    catalogue = case_catalog(repo, owner) if task_mode != 'polish' else []
    settings = settings or {}
    images_enabled = settings.get('reception_images_enabled', True)
    sources = get_reception_source_images(repo, owner, case, raw, settings=settings) if task_mode != 'polish' else []
    source_ids = {m['message_id'] for m in sources}
    recent, historical = dialogue_window(messages, {'selected': case or {}, 'catalogue': catalogue}, continuation, source_ids,
                                         limit=settings.get('context_message_count', 50))
    policies = select_policy_inputs(repo.list('policy'), case or {'facts': {}, 'procurement_key': ''})
    knowledge = policies['knowledge']
    result = {'prompt_version': PROMPT_VERSION, 'task_mode': task_mode,
              'collection_rules': collection_rules(settings, case),
              'recent_messages': recent, 'historical_evidence': historical, 'knowledge': knowledge,
              'guidance': policies['guidance'], 'style': policies['style'],
              'policy_versions': policies['policy_versions'],
              'style_conflicts': policies.get('style_conflicts', []),
              'supplement': supplement_context(repo, owner['conversation_id']),
              'conversation_summary': repo.get('conversation_summary', owner['conversation_id']),
              'photo_context': direct_photo_context(repo, [m for m in historical + recent if images_enabled and m['message_id'] in source_ids])}
    result['supplement_image_refs'] = [
        {'origin': 'business_supplement', 'attachment_id': row['id'], 'image_id': row['id'],
         'image_sha256': row['sha256'], 'seller_id': owner['seller_id'], 'buyer_id': owner['buyer_id'],
         'conversation_id': owner['conversation_id'], 'supplement_revision': result['supplement']['revision']}
        for row in result['supplement']['attachments'] if images_enabled and row['media_type'].startswith('image/')]
    latest = next((m for m in reversed(recent) if m['role'] == 'buyer'), None)
    result['reply_target'] = {'source_message_id': latest['message_id'] if latest else None}
    if task_mode != 'polish':
        conversation = repo.get('conversation', owner['conversation_id']) or {}
        result['case_catalog'] = catalogue
        result['current_case_ref'] = next((row['case_ref'] for row in catalogue if row['case_id'] == conversation.get('case_id')), None)
        result['selected_case_ref'] = next((row['case_ref'] for row in catalogue if case and row['case_id'] == case.get('case_id')), None)
    return add_image_references(result)


def semantic_fingerprint(context):
    from .model_payload import project_model_payload
    model = project_model_payload(context)
    model.pop('followup_context', None)  # Derived progress is applied by this same generation.
    semantic = {'model': model,
                'prompt_version': context.get('prompt_version'),
                'conversation_revision': context.get('conversation_revision'),
                'case_revision': (context.get('case') or {}).get('revision'),
                'policy_versions': context.get('policy_versions'),
                'product_images': (context.get('photo_context') or {}).get('direct_images', []),
                'history_coverage': context.get('history_coverage')}
    coverage = semantic.get('history_coverage')
    if isinstance(coverage, dict):
        # Display wording and audit time do not change the history read. Status,
        # frontier and cursor remain part of the frozen generation basis.
        semantic['history_coverage'] = {key: value for key, value in coverage.items()
                                        if key not in ('updated_at', 'message')}
    return hashlib.sha256(encoded(semantic).encode('utf-8')).hexdigest()
