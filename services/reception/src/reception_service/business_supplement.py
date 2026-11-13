"""Current seller-provided business terms; never a buyer fact or a history store."""
from __future__ import annotations

import hashlib
from .repository import encoded
from .errors import ServiceError
from .supplement_attachments import attachment_rows, public_attachment


def supplement_context(repo, conversation_id):
    note = repo.get('conversation_supplement', conversation_id) or {
        'conversation_id': conversation_id, 'body': '', 'revision': None, 'updated_at': None}
    result = {key: note.get(key) for key in ('conversation_id', 'body', 'revision', 'updated_at')}
    rows = attachment_rows(repo, conversation_id)
    if any(row.get('status') != 'ready' for row in rows):
        raise ServiceError('attachment_integrity', '业务补充附件读取或完整性校验失败，请移除或从备份恢复')
    result['attachments'] = [{**public_attachment(row, preview=False),
                              **({'text': row['text']} if 'text' in row else {})} for row in rows]
    result['source_id'] = ('business_note_' + hashlib.sha256(encoded(
        [conversation_id, result['revision'], result['body']]).encode('utf-8')).hexdigest()
        if isinstance(result['body'], str) and result['body'].strip() else None)
    return result


def supplement_sources(context):
    note = context.get('supplement') or {}
    cid = (context.get('case') or context).get('conversation_id')
    if (not isinstance(note, dict) or note.get('conversation_id') != cid
            or type(note.get('revision')) is not int or note['revision'] < 1):
        return {}
    result = {row['id']: row['text'] for row in note.get('attachments', [])
              if isinstance(row, dict) and isinstance(row.get('id'), str)
              and isinstance(row.get('text'), str) and row.get('origin') == 'business_supplement'}
    if isinstance(note.get('body'), str) and note['body'].strip():
        expected = 'business_note_' + hashlib.sha256(encoded(
            [cid, note['revision'], note['body']]).encode('utf-8')).hexdigest()
        if note.get('source_id') == expected:
            result[expected] = note['body']
    return result


def supplement_fingerprint(context):
    note = context.get('supplement') or {}
    return hashlib.sha256(encoded({key: note.get(key) for key in
        ('conversation_id', 'body', 'revision')}).encode('utf-8')).hexdigest()
