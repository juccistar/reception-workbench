"""Offline maintenance entry points for managed workspaces."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from .storage_guard import StorageError, preflight_workspace, validate_database, REPOSITORY_VERSION, LEDGER_VERSION
from .workspace_backup import (backup_workspace, restore_workspace, _files, _separate,
                               _validate_archive, _hash, _receipt_roots, RECEIPTS)
from .workspace_lock import WorkspaceLock, reject_links


def _path(value):
    if not isinstance(value, str) or not value or '\x00' in value or not os.path.isabs(value):
        raise StorageError('Absolute path required')
    if value.replace('\\', '/').startswith('//'):
        raise StorageError('Network paths are not supported')
    path = Path(os.path.abspath(value))
    reject_links(path)
    # Never select a volume root, system tree, or application installation.
    if path == path.parent:
        raise StorageError('Unsafe path')
    protected = [os.environ.get('WINDIR'), os.environ.get('ProgramFiles'),
                 os.environ.get('ProgramFiles(x86)'), str(Path(sys.executable).parent)]
    for item in protected:
        if item:
            parent = Path(os.path.abspath(item))
            if path == parent or parent in path.parents:
                raise StorageError('Unsafe path')
    return path


def _workspace(root, *, clean=True):
    if not root.is_dir():
        raise StorageError('Workspace does not exist')
    preflight_workspace(root)
    return _files(root, require_checkpoint=clean, check_references=clean,
                  receipt_roots=[str(root / 'live/workctl-state/chat-analysis/send-receipts')])


def _lock_readonly(root):
    return WorkspaceLock(root).acquire(invalidate_clean=False)


def _check_snapshot(root, safety):
    names = _workspace(root)
    manifest = _validate_archive(safety)
    hashes = {entry['path']: entry['sha256'] for entry in manifest['files']}
    if (set(names) - {'.receipt-roots.json'} != set(hashes) - {'.receipt-roots.json'}
            or set(_receipt_roots(safety)) != set([*_receipt_roots(root), str(root / RECEIPTS)])):
        raise StorageError('Workspace changed after backup')
    for name in names:
        if name != '.receipt-roots.json' and hashes.get(name) != _hash(root / name):
            raise StorageError('Workspace changed after backup')
    return names


def _archive_path(directory):
    root = _path(directory)
    if not root.is_dir():
        raise StorageError('Backup directory does not exist')
    return root / ('reception-backup-' + uuid.uuid4().hex)


def _ledgers(root):
    result = {}
    for name in ('demo/integrations-demo.sqlite3', 'demo/adapters-live.sqlite3', 'live/adapters-live.sqlite3'):
        path = root / name
        if not path.exists():
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            result[name] = {table: set(db.execute('SELECT * FROM ' + table).fetchall())
                            for table in ('operations', 'attempt_history', 'retry_grants')}
    return result


def _check_history(current, archive):
    old, restored = _ledgers(current), _ledgers(archive)
    for name, tables in restored.items():
        if name not in old and tables['retry_grants']:
            raise StorageError('Backup would restore old retry grants')
    for name, tables in old.items():
        if name not in restored:
            raise StorageError('Backup is missing current ledger')
        # Grants must be identical: an old grant may authorize a duplicate send.
        for table in tables:
            if table == 'retry_grants':
                valid = tables[table] == restored[name][table]
            else:
                valid = tables[table] <= restored[name][table]
            if not valid:
                raise StorageError('Backup conflicts with current operation history')


def run_maintenance(action, source, destination=None, archive=None, backup_dir=None, *, allow_new=False):
    root = _path(source)
    backup = None
    if action == 'prepare' and (not root.exists() or
            root.is_dir() and all(item.name == 'host' and item.is_dir() for item in root.iterdir())):
        if not allow_new:
            raise StorageError('Configured workspace does not exist')
        reject_links(root / 'host')
        return {'status': 'completed', 'action': action, 'data_root': str(root), 'backup': None, 'new': True}
    if action == 'open' and destination:
        target = _path(destination)
        _separate(root, target)
        safety = _archive_path(backup_dir)
        _separate(root, safety)
        _separate(target, safety)
        if not root.is_dir() or not target.is_dir():
            raise StorageError('Workspace does not exist')
        backup_workspace(root, safety)
        lock = _lock_readonly(root)
        try:
            if not lock.was_clean:
                raise StorageError('Workspace has no managed clean shutdown')
            _check_snapshot(root, safety)
            target_lock = _lock_readonly(target)
            try:
                if not target_lock.was_clean:
                    raise StorageError('Workspace has no managed clean shutdown')
                paths = _workspace(target)
                _check_history(root, target)
            finally:
                target_lock.release()
        finally:
            lock.release()
        return {'status': 'completed', 'action': action, 'data_root': str(target),
                'backup': str(safety), 'file_count': len(paths)}
    if action in ('prepare', 'inspect', 'open'):
        if not root.is_dir():
            raise StorageError('Workspace does not exist')
        lock = _lock_readonly(root)
        try:
            paths = _workspace(root, clean=False)
            recovery_backup = None
            if action == 'prepare' and lock.was_clean:
                from .storage_recovery import recover_wal
                recovery_backup = recover_wal(root, lock, backup_dir)
            if lock.was_clean:
                paths = _workspace(root)
            upgrades = []
            for name in paths:
                if not name.endswith('.sqlite3'):
                    continue
                kind = 'ledger' if name.endswith(('adapters-live.sqlite3', 'integrations-demo.sqlite3')) else 'repository'
                if validate_database(root / name, kind) != (LEDGER_VERSION if kind == 'ledger' else REPOSITORY_VERSION):
                    upgrades.append(name)
            if action == 'prepare' and upgrades and not lock.was_clean:
                raise StorageError('Upgrade requires clean shutdown and a safety backup; recover the workspace with the previous version first')
            cleanup_required = False
            if action == 'prepare' and lock.was_clean:
                from .reminder_cleanup import needs_reminder_cleanup
                cleanup_required = needs_reminder_cleanup(root, paths)
        finally:
            lock.release()
        cleanup = None
        if action == 'prepare' and (upgrades or cleanup_required):
            backup = backup_workspace(root, _archive_path(backup_dir))
        if action == 'prepare' and cleanup_required:
            # No content is deleted before a complete archive is validated and
            # every current source file is matched to that archive under lock.
            from .reminder_cleanup import clean_reminders
            with WorkspaceLock(root) as lock:
                if not lock.was_clean:
                    raise StorageError('Workspace has no managed clean shutdown')
                current_paths = _check_snapshot(root, backup)
                cleanup = clean_reminders(root, current_paths)
                _workspace(root)
                lock.mark_clean_shutdown()
        return {'status': 'completed', 'action': action, 'data_root': str(root), 'backup': str(backup) if backup else None, 'recovery_backup': str(recovery_backup) if recovery_backup else None, 'file_count': len(paths), 'upgrade_required': bool(upgrades), 'reminder_cleanup': cleanup}
    if action == 'backup':
        target = _path(destination)
        backup = backup_workspace(root, target)
        return {'status': 'completed', 'action': action, 'data_root': str(root), 'backup': str(backup)}
    if action in ('migrate', 'restore'):
        target = _path(destination)
        safety = _archive_path(backup_dir)
        _separate(root, target)
        _separate(target, safety)
        old_archive = _path(archive) if action == 'restore' else None
        if old_archive:
            _separate(old_archive, target)
            _validate_archive(old_archive)
            if not root.is_dir():
                raise StorageError('Workspace does not exist')
            with WorkspaceLock(root) as lock:
                if not lock.was_clean:
                    raise StorageError('Workspace has no managed clean shutdown')
                try:
                    _workspace(root)
                    _check_history(root, old_archive)
                finally:
                    lock.mark_clean_shutdown()
        backup_workspace(root, safety)
        # Keep the source locked until publishing; reject changes since safety snapshot.
        with WorkspaceLock(root) as lock:
            if not lock.was_clean:
                raise StorageError('Workspace has no managed clean shutdown')
            try:
                _check_snapshot(root, safety)
                chosen = old_archive or safety
                if old_archive:
                    _check_history(safety, old_archive)
                restore_workspace(chosen, target)
            finally:
                lock.mark_clean_shutdown()
        return {'status': 'completed', 'action': action, 'data_root': str(target), 'backup': str(safety)}
    raise StorageError('Unknown maintenance action')
