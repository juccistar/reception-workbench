"""Pure in-memory legacy rehearsal; no filesystem discovery or live migration.

Callers supply the entire declared scope and the exact source bytes and indexed
operations.  Results never prove that an unlisted source does not exist.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


class LegacyBlocked(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def rehearse_import(*, account_id, declared_sources, source_bytes, operations):
    if not isinstance(account_id, str) or not account_id:
        raise LegacyBlocked('account_unverified')
    if not isinstance(declared_sources, list) or not declared_sources or any(not isinstance(s, str) or not s for s in declared_sources):
        raise LegacyBlocked('manifest_incomplete')
    if len(set(declared_sources)) != len(declared_sources) or not isinstance(source_bytes, dict) or set(declared_sources) != set(source_bytes):
        raise LegacyBlocked('source_missing_or_conflicting')
    if not isinstance(operations, list):
        raise LegacyBlocked('operation_index_missing')
    sources = {}
    for source in declared_sources:
        data = source_bytes[source]
        if not isinstance(data, bytes):
            raise LegacyBlocked('source_unreadable')
        sources[source] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': data}
    indexed = {}
    for row in operations:
        if not isinstance(row, dict) or row.get('account_id') != account_id or row.get('source') not in sources:
            raise LegacyBlocked('operation_identity_unverified')
        for field in ('operation_id', 'chat_id', 'status'):
            if not isinstance(row.get(field), str) or not row[field]:
                raise LegacyBlocked('operation_invalid')
        if row['status'] not in ('prepared', 'sending', 'unknown', 'accepted', 'failed', 'cancelled'):
            raise LegacyBlocked('operation_invalid')
        prior = indexed.get(row['operation_id'])
        if prior is not None and prior != row:
            raise LegacyBlocked('operation_conflict')
        indexed[row['operation_id']] = deepcopy(row)
    return {'format': 'wa-legacy-rehearsal-v1', 'account_id': account_id, 'sources': sources,
            'operations': [indexed[k] for k in sorted(indexed)], 'reception_enabled': False}


def backup_rehearsal(imported):
    if not isinstance(imported, dict) or imported.get('format') != 'wa-legacy-rehearsal-v1':
        raise LegacyBlocked('invalid_backup')
    sources = imported.get('sources')
    if not isinstance(sources, dict):
        raise LegacyBlocked('invalid_backup')
    result = deepcopy(imported)
    for value in result['sources'].values():
        if not isinstance(value, dict) or not isinstance(value.get('bytes'), bytes) or hashlib.sha256(value['bytes']).hexdigest() != value.get('sha256'):
            raise LegacyBlocked('source_changed')
    result['backup_sha256'] = hashlib.sha256(_canonical({
        'account_id': result['account_id'], 'sources': {k: v['sha256'] for k, v in result['sources'].items()},
        'operations': result['operations'], 'reception_enabled': result['reception_enabled']})).hexdigest()
    return result


def restore_rehearsal(backup, *, target_empty):
    if target_empty is not True or not isinstance(backup, dict):
        raise LegacyBlocked('target_not_empty')
    candidate = deepcopy(backup)
    expected = candidate.pop('backup_sha256', None)
    checked = backup_rehearsal(candidate)
    if not isinstance(expected, str) or checked['backup_sha256'] != expected:
        raise LegacyBlocked('backup_mismatch')
    result = rehearse_import(account_id=candidate['account_id'], declared_sources=list(candidate['sources']),
        source_bytes={key: value['bytes'] for key, value in candidate['sources'].items()}, operations=candidate['operations'])
    result['reception_enabled'] = False
    return result
