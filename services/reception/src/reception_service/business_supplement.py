"""Current seller-provided business terms; never a buyer fact or a history store."""
from __future__ import annotations

import hashlib
from .repository import encoded


def supplement_context(repo, conversation_id):
    note = repo.get('conversation_supplement', conversation_id) or {
        'conversation_id': conversation_id, 'body': '', 'revision': None, 'updated_at': None}
    result = {key: note.get(key) for key in ('conversation_id', 'body', 'revision', 'updated_at')}
    result['source_id'] = ('business_note_' + hashlib.sha256(encoded(
        [conversation_id, result['revision'], result['body']]).encode('utf-8')).hexdigest()
        if isinstance(result['body'], str) and result['body'].strip() else None)
    return result


def supplement_sources(context):
    note = context.get('supplement') or {}
    cid = (context.get('case') or context).get('conversation_id')
    if (not isinstance(note, dict) or note.get('conversation_id') != cid
            or not isinstance(note.get('body'), str) or not note['body'].strip()
            or type(note.get('revision')) is not int or note['revision'] < 1):
        return {}
    expected = 'business_note_' + hashlib.sha256(encoded(
        [cid, note['revision'], note['body']]).encode('utf-8')).hexdigest()
    return {expected: note['body']} if note.get('source_id') == expected else {}


def supplement_fingerprint(context):
    note = context.get('supplement') or {}
    return hashlib.sha256(encoded({key: note.get(key) for key in
        ('conversation_id', 'body', 'revision')}).encode('utf-8')).hexdigest()
