"""Exact, offline 0.3.13 content cleanup after a verified full-workspace backup.

This module never touches messages, images, adapter ledgers, operations, intents,
receipts, outboxes, retry grants or continuation identifiers. Historical backups
remain historical. Call only while the complete workspace is exclusively locked.
"""
from __future__ import annotations

from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3

from .repository import encoded
from .storage_guard import StorageError
from .workspace_lock import reject_links

_REMINDER_KINDS = ('business_progress', 'assistance_task', 'assistance_mutation',
                   'assistance_start_mutation', 'requirement_proposal')
_PATHS = {
    'case': (('summary',), ('requirement_items',), ('last_changes',),
             ('quality_notes',), ('quality_warnings',), ('assistance_requests',),
             ('product_ref', 'original_title')),
    'draft': (('validation', 'warnings'), ('quality_notes',), ('quality_warnings',),
              ('analysis_metadata', 'quality_notes'), ('analysis_metadata', 'assistance_suggestions')),
    'manual_suggestion': (('quality_notes',), ('quality_warnings',), ('assistance_suggestions',)),
    'job': (('warnings',),),
    'product_catalog': (('original_title',), ('result', 'original_title')),
}
_KINDS = (*_REMINDER_KINDS, *_PATHS)
_SQL_KINDS = ','.join('?' for _ in _KINDS)


def _strip(kind, value):
    result = copy.deepcopy(value)
    for path in _PATHS.get(kind, ()):
        parent = result
        for part in path[:-1]:
            parent = parent.get(part) if isinstance(parent, dict) else None
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
    # Old facts stay evidence, never become canonical AI requirements. Remove
    # only the abandoned display/review/reminder fields, not original values.
    if kind == 'case' and isinstance(result.get('facts'), dict):
        for fact in result['facts'].values():
            if isinstance(fact, dict):
                for key in ('display_value', 'status', 'review_status', 'reviewed_at', 'reviewed_by', 'confirmed_by'):
                    fact.pop(key, None)
    return result


def _paths(root, names):
    for name in names:
        if name == 'demo/workspace.sqlite3' or (name.startswith('live/sellers/') and name.endswith('/workspace.sqlite3')):
            path = Path(root) / name
            reject_links(path)
            if not path.is_file():
                raise StorageError('Workspace database missing')
            yield path


def needs_reminder_cleanup(root, names):
    """Read only checkpointed databases, already checked by offline maintenance."""
    for path in _paths(root, names):
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            for table in ('entities', 'revisions'):
                rows = db.execute(f'SELECT kind,data FROM {table} WHERE kind IN ({_SQL_KINDS})', _KINDS)
                for kind, text in rows:
                    if kind in _REMINDER_KINDS:
                        return True
                    value = json.loads(text)
                    if _strip(kind, value) != value:
                        return True
    return False


def clean_reminders(root, names):
    """Apply the allowlist. The caller supplies the verified backup and lock."""
    counts = {'deleted_reminder_records': 0, 'updated_records': 0, 'databases': 0}
    for path in _paths(root, names):
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            try:
                for table, key in (('entities', 'rowid'), ('revisions', 'sequence')):
                    rows = db.execute(f'SELECT {key},kind,data FROM {table} WHERE kind IN ({_SQL_KINDS})', _KINDS).fetchall()
                    for identifier, kind, text in rows:
                        if kind in _REMINDER_KINDS:
                            db.execute(f'DELETE FROM {table} WHERE {key}=?', (identifier,))
                            counts['deleted_reminder_records'] += 1
                        else:
                            value = json.loads(text)
                            cleaned = _strip(kind, value)
                            if cleaned != value:
                                db.execute(f'UPDATE {table} SET data=? WHERE {key}=?', (encoded(cleaned), identifier))
                                counts['updated_records'] += 1
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            if db.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal':
                if tuple(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()) != (0, 0, 0):
                    raise StorageError('Cleanup checkpoint incomplete', code='storage_busy')
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise StorageError('Cleanup integrity check failed')
            counts['databases'] += 1
    return counts
