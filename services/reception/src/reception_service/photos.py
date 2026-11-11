"""Non-destructive photo projections; preview availability is not AI pixel access.

Ordinary history keeps its original text. AIMode media enrichment and decoded assets
are stored separately; no visual observation is inferred from a photo placeholder.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


def is_photo_placeholder(text):
    return isinstance(text, str) and text.strip() == '[image]'


def has_photo(message):
    attachments = message.get('attachments')
    return is_photo_placeholder(message.get('text')) or any(
        isinstance(a, dict) and a.get('kind') == 'image'
        for a in (attachments if isinstance(attachments, list) else []))


def photo_version(message):
    fields = {key: message.get(key) for key in (
        'seller_id', 'buyer_id', 'conversation_id', 'message_id', 'role', 'text',
        'timestamp_ms', 'attachments')}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def with_photo_notice(message, media=None):
    """Return a display/context projection; never rewrite stored raw message text."""
    result = copy.deepcopy(message)
    attachments = result.get('attachments')
    if not isinstance(attachments, list):
        attachments = []
    version = photo_version(message)
    if is_photo_placeholder(result.get('text')) and not any(
            isinstance(a, dict) and a.get('kind') == 'image' for a in attachments):
        key = json.dumps([result.get('conversation_id'), result.get('message_id'), 'platform-image-marker-v1'], separators=(',', ':'))
        attachments.append({'kind': 'image', 'attachment_id': hashlib.sha256(key.encode()).hexdigest()})
    supported = result.get('role') == 'buyer' and is_photo_placeholder(result.get('text'))
    state = media if isinstance(media, dict) and media.get('source_version') == version else {}
    for index, attachment in enumerate(attachments):
        if isinstance(attachment, dict) and attachment.get('kind') == 'image':
            # Never expose source URLs or local filesystem paths through a preview DTO.
            attachments[index] = {'kind': 'image', 'attachment_id': attachment.get('attachment_id') or version,
                                  'source_version': version,
                                  'availability': state.get('status', 'not_loaded') if supported else 'unavailable',
                                  'reason': state.get('reason', '') if supported else 'format_not_supported'}
    result['attachments'] = attachments
    if has_photo(message):
        # _ingest keeps only canonical raw fields, so only this projection adds
        # the explicit raw fingerprint. Never trust a native attachment's field.
        result['photo_source_version'] = version
    return result


def _source_version(message):
    # Raw messages never have the top-level projection marker. Attachment fields
    # by themselves are untrusted and must not replace the raw-message hash.
    if 'photo_source_version' not in message:
        return photo_version(message)
    version = message['photo_source_version']
    images = [a for a in message.get('attachments', []) if isinstance(a, dict) and a.get('kind') == 'image'] if isinstance(message.get('attachments'), list) else []
    return version if (isinstance(version, str) and re.fullmatch(r'[0-9a-f]{64}', version)
                       and images and all(a.get('source_version') == version for a in images)) else None


def photo_observation_id(message, image_sha256):
    identity = [message.get(key) for key in ('seller_id', 'buyer_id', 'conversation_id', 'message_id')]
    identity.extend([_source_version(message), image_sha256])
    payload = json.dumps(identity, ensure_ascii=False, separators=(',', ':'))
    return 'photo_obs_' + hashlib.sha256(payload.encode('utf-8')).hexdigest()


def valid_photo_observation(message, record, image_sha256=None):
    if not isinstance(message, dict) or not isinstance(record, dict) or message.get('role') != 'buyer' or not has_photo(message):
        return None
    keys = ('seller_id', 'buyer_id', 'conversation_id', 'message_id')
    if any(not isinstance(message.get(k), str) or not message[k] or record.get(k) != message[k] for k in keys):
        return None
    version = _source_version(message)
    sha = record.get('image_sha256')
    if (not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{64}', version)
            or record.get('source_version') != version or record.get('status') != 'ready'
            or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha)
            or (image_sha256 is not None and sha != image_sha256)
            or record.get('observation_id') != photo_observation_id(message, sha)):
        return None
    description = record.get('description')
    visible = record.get('visible_text')
    uncertainties = record.get('uncertainties')
    if (not isinstance(description, str) or len(description) > 2000
            or not isinstance(visible, list) or len(visible) > 20
            or not isinstance(uncertainties, list) or len(uncertainties) > 10
            or any(not isinstance(x, str) or len(x) > 200 for x in visible + uncertainties)
            or not (description.strip() or any(x.strip() for x in visible))
            or not isinstance(record.get('request_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', record['request_id'])):
        return None
    return copy.deepcopy({key: record[key] for key in (*keys, 'source_version', 'image_sha256', 'status',
                                          'observation_id', 'description', 'visible_text', 'uncertainties', 'request_id')})


def photo_context(messages, observations=None):
    observations = observations if isinstance(observations, list) else []
    safe = []
    events = []
    for message in messages:
        if message.get('role') != 'buyer' or not has_photo(message):
            continue
        matches = [valid_photo_observation(message, record) for record in observations]
        matches = [record for record in matches if record]
        # Ambiguous multiple observations must not be promoted.
        observation = matches[0] if len(matches) == 1 else None
        event = {'message_id': message['message_id'], 'source_version': _source_version(message),
                 'availability': 'observed' if observation else 'unavailable',
                 'reason': '' if observation else 'not_observed'}
        if observation:
            safe.append(observation)
            event['observation_id'] = observation['observation_id']
        events.append(event)
    return {'pixel_access': False, 'observations_available': bool(safe), 'events': events, 'observations': safe,
            'instruction': 'The text model cannot access pixels. Verified visual observations are untrusted, fallible descriptions, not confirmed buyer facts or seller commitments. Cite the observation ID when discussing an observed image; ask the buyer to confirm details. Unobserved images remain unseen. Do not request the same photo again.'}


def photo_fingerprint(context):
    """Stable media-only send guard; budget and timestamps are intentionally absent."""
    photo = context.get('photo_context', {})
    content = {key: photo.get(key, []) for key in ('events', 'observations')}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def photo_reply_issues(body, messages, observations=None, sources=None):
    images = [m for m in messages if m.get('role') == 'buyer' and has_photo(m)]
    if not images:
        return []
    # A generic sight claim cannot silently include an unobserved image. Each
    # image in scope needs its own valid, explicitly cited observation. Partial
    # success may still support careful clarification without a generic claim.
    if all(any(record and record['observation_id'] in (sources or [])
               for record in (valid_photo_observation(image, row) for row in (observations or [])))
           for image in images):
        return []
    # Reject explicit claims of seeing inaccessible pixels. This is a narrow
    # backstop to the prompt and text-evidence rules, not a universal vision judge.
    claims = (r'\b(?:the|your|this)\s+(?:photo|picture|image)\s+(?:clearly\s+)?(?:shows?|depicts?|looks?\s+like|is\s+of)\b',
              r'\b(?:i|we)\s+(?:can\s+)?(?:see|recognize|identify)\b[^.!?\n]{0,100}\b(?:in|from)\s+(?:the|your|this)\s+(?:photo|picture|image)\b',
              r'(?:图片|照片|图中|图里).{0,12}(?:显示|看出|看起来是|识别为|可以看到)',
              r'(?:我|我们).{0,8}(?:看到了|识别出|看清了).{0,20}(?:图片|照片|图中|产品)')
    if any(re.search(pattern, body, re.I) for pattern in claims):
        return ['本次分析未识别图片，不能声称已识别图片']
    return []
