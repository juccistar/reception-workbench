"""Offline backup of account state and verification of declared legacy sources."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time

from .storage_guard import StorageError
from .workspace_lock import WorkspaceLock, reject_links, LOCK, MARKER
from .workspace_backup import backup_workspace, _hash, _target, _separate, _publish, _unique, _normal_path, _validate_archive

MAX_FILES = 100000
MAX_BYTES = 4 * 1024 * 1024 * 1024
MANIFEST = 'account-manifest.json'


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _inventory(root, *, archive=False):
    entries = []
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            reject_links(Path(directory) / name)
        for name in files:
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if rel in (LOCK, MARKER) or (archive and rel == MANIFEST):
                continue
            if not path.is_file() or name.endswith(('-wal', '-shm')):
                raise StorageError('Unexpected or uncheckpointed account file')
            parts = PurePosixPath(rel).parts
            is_database = len(parts) == 3 and parts[0] == 'whatsapp-accounts' and parts[2] == 'account.sqlite3'
            is_ledger = len(parts) == 2 and parts[0] == 'send-ledger' and len(parts[1]) == 70 and parts[1].endswith('.jsonl') and all(c in '0123456789abcdef' for c in parts[1][:-6])
            is_legacy = len(parts) >= 3 and parts[0] == 'legacy-archives'
            if not (is_database or is_ledger or is_legacy):
                raise StorageError('Unexpected account file')
            size = path.stat().st_size
            total += size
            if total > MAX_BYTES or len(entries) >= MAX_FILES:
                raise StorageError('Account backup exceeds limits')
            entries.append({'path': rel, 'size': size, 'sha256': _hash(path)})
    entries.sort(key=lambda e: e['path'])
    databases = [e for e in entries if e['path'].endswith('/account.sqlite3')]
    if not databases:
        raise StorageError('Account database missing')
    for entry in databases:
        db = sqlite3.connect(_normal_path(root / entry['path']).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise StorageError('Account database corrupt')
            db.execute('SELECT kind,id,data FROM records LIMIT 1').fetchall()
            db.execute('SELECT id,value FROM metadata LIMIT 1').fetchall()
        finally:
            db.close()
    return entries


def _validate(root):
    reject_links(root)
    manifest = json.loads((root / MANIFEST).read_text(encoding='utf-8'), object_pairs_hook=_unique)
    if not isinstance(manifest, dict) or set(manifest) != {'format', 'files'} or manifest['format'] != 'wa-account-backup-v1':
        raise StorageError('Invalid account manifest')
    if manifest['files'] != _inventory(root, archive=True):
        raise StorageError('Account archive mismatch')
    return manifest


def backup_account(root, destination):
    root = Path(os.path.abspath(root))
    reject_links(root)
    if not root.is_dir():
        raise StorageError('Account root missing')
    target = _target(destination)
    _separate(root, target)
    lock = WorkspaceLock(root).acquire(invalidate_clean=False)
    try:
        if not lock.was_clean:
            raise StorageError('Account root was not closed cleanly')
        before = _inventory(root)
        stage = Path(tempfile.mkdtemp(prefix='.wa-backup-', dir=target.parent))
        try:
            for item in before:
                dest = stage / item['path']
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / item['path'], dest)
            manifest = {'format': 'wa-account-backup-v1', 'files': before}
            (stage / MANIFEST).write_bytes(_canonical(manifest))
            if _inventory(root) != before:
                raise StorageError('Account root changed during backup')
            _validate(stage)
            _publish(stage, target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return manifest
    finally:
        lock.release()


def restore_account(archive, destination):
    source = Path(os.path.abspath(archive))
    target = _target(destination)
    _separate(source, target)
    manifest = _validate(source)
    stage = Path(tempfile.mkdtemp(prefix='.wa-restore-', dir=target.parent))
    try:
        for item in manifest['files']:
            dest = stage / item['path']
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / item['path'], dest)
        if _inventory(stage) != manifest['files'] or _validate(source) != manifest:
            raise StorageError('Account archive changed during restore')
        with WorkspaceLock(stage) as lock:
            lock.mark_clean_shutdown()
        _publish(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'destination': str(target), 'reception_enabled': False}


def _legacy_operations(snapshot, seller, account):
    seller_hash = hashlib.sha256(seller.encode('utf-8')).hexdigest()
    dbpath = snapshot / 'live' / 'sellers' / seller_hash / 'workspace.sqlite3'
    if not dbpath.is_file():
        raise StorageError('Seller database missing')
    db = sqlite3.connect(_normal_path(dbpath).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        row = db.execute("SELECT data FROM entities WHERE kind='meta' AND id='seller'").fetchone()
        if not row or json.loads(row[0]).get('seller_id') != seller:
            raise StorageError('Seller identity mismatch')
        pending = []
        known_ledgers = set()
        for kind, key, text in db.execute("SELECT kind,id,data FROM entities WHERE kind LIKE 'wa_%'"):
            value = json.loads(text, object_pairs_hook=_unique)
            # Older summary/ACK cache rows need not contain account metadata.
            # Their bytes stay in the complete archive; only operation ownership
            # can establish whether a send fence belongs to this account.
            if kind != 'wa_operation':
                continue
            if not isinstance(value, dict) or not isinstance(value.get('account_id'), str) or not value['account_id']:
                raise StorageError('Legacy account unknown')
            if value.get('seller_id') != seller:
                raise StorageError('Legacy seller mismatch')
            if value.get('operation_id') != key:
                raise StorageError('Invalid operation identity')
            known_ledgers.add(hashlib.sha256(key.encode()).hexdigest() + '.jsonl')
            if value['account_id'] != account:
                continue
            op = value
            if op.get('operation_id') != key or any(not isinstance(op.get(k), str) or not op[k] for k in ('operation_id', 'connection_epoch', 'chat_id', 'body', 'status')):
                raise StorageError('Invalid operation')
            if op['status'] not in ('prepared', 'sending', 'unknown', 'accepted', 'failed', 'cancelled', 'rejected'):
                raise StorageError('Invalid operation status')
            ledger = snapshot / 'live' / 'whatsapp-ledgers' / seller_hash / 'send-ledger' / (hashlib.sha256(key.encode()).hexdigest() + '.jsonl')
            if ledger.exists():
                lines = ledger.read_text(encoding='utf-8').splitlines(keepends=True)
                if not lines or not lines[0].endswith('\n') or len(lines) > 2:
                    raise StorageError('Invalid ledger')
                intent = json.loads(lines[0], object_pairs_hook=_unique)
                if any(intent.get(k) != op.get(k) for k in ('operation_id', 'account_id', 'connection_epoch', 'chat_id', 'body')):
                    raise StorageError('Ledger intent conflict')
                if len(lines) == 2 and lines[1].endswith('\n'):
                    result = json.loads(lines[1], object_pairs_hook=_unique)
                    if not isinstance(result, dict) or not isinstance(result.get('message_id'), str) or type(result.get('ack')) is not int:
                        raise StorageError('Invalid ledger result')
                    if op.get('message_id') and op['message_id'] != result['message_id']:
                        raise StorageError('Ledger result conflict')
            elif op.get('submission_started') is True:
                raise StorageError('Submitted operation ledger missing')
            if op['status'] in ('prepared', 'sending', 'unknown'):
                pending.append(op)
        ledger_dir = snapshot / 'live' / 'whatsapp-ledgers' / seller_hash / 'send-ledger'
        if ledger_dir.exists() and any(p.name not in known_ledgers for p in ledger_dir.iterdir()):
            raise StorageError('Unassociated ledger')
        return pending
    finally:
        db.close()


def verify_legacy_sources(account_id, sources, archive_directory):
    if not isinstance(account_id, str) or not account_id or not isinstance(sources, list) or len(sources) > 1000:
        raise StorageError('Invalid legacy declaration')
    archive = Path(os.path.abspath(archive_directory))
    reject_links(archive)
    archive.mkdir(parents=True, exist_ok=True)
    results, seen, inventories = [], {}, []
    for source in sources:
        if not isinstance(source, dict) or any(not isinstance(source.get(k), str) or not source[k] for k in ('source_id', 'merchant_id', 'seller_id', 'data_root', 'state_directory', 'registry_directory')) or source['source_id'] in seen:
            raise StorageError('Invalid legacy declaration')
        sid = source['source_id']
        seen[sid] = len(results)
        result = {'source_id': sid, 'status': 'blocked', 'pending_operations': []}
        results.append(result)
        snapshot = None
        try:
            _separate(Path(os.path.abspath(source['data_root'])), archive)
            snapshot = Path(tempfile.mkdtemp(prefix='.wa-source-', dir=archive))
            snapshot.rmdir()
            backup_workspace(source['data_root'], snapshot)
            manifest = _validate_archive(snapshot)
            ops = _legacy_operations(snapshot, source['seller_id'], account_id)
            if _validate_archive(snapshot) != manifest:
                raise StorageError('Legacy snapshot changed')
            # Ignore timestamps and provenance location when deriving stable content identity.
            files = [{'path': e['path'], 'sha256': e['sha256'], 'size': e['size']} for e in manifest['files']]
            digest = _digest(files)
            dest = archive / digest
            if dest.exists():
                old = _validate_archive(dest)
                if [{'path': e['path'], 'sha256': e['sha256'], 'size': e['size']} for e in old['files']] != files:
                    raise StorageError('Archived source conflict')
            else:
                os.rename(snapshot, dest)
            inventories.append({'source_id': sid, 'archive_hash': digest})
            result.update(status='verified', pending_operations=ops)
        except (StorageError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            pass
        finally:
            if snapshot is not None and snapshot.exists():
                shutil.rmtree(snapshot)
    by_op = {}
    for result in results:
        for op in result['pending_operations']:
            by_op.setdefault(op['operation_id'], []).append((op, result))
    conflicts = set()
    for rows in by_op.values():
        if any(op != rows[0][0] for op, _ in rows[1:]):
            conflicts.update(row['source_id'] for _, row in rows)
    for result in results:
        if result['source_id'] in conflicts:
            result.update(status='blocked', pending_operations=[])
    return {'format': 'wa-legacy-verification-v1', 'account_id': account_id,
            'manifest_hash': _digest({'account_id': account_id, 'sources': results, 'archives': inventories}),
            'verified_at_ms': time.time_ns() // 1_000_000, 'sources': results}
