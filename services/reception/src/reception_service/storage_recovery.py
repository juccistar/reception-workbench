"""Recover a clean workspace's residual WAL from a verified raw snapshot."""
from contextlib import contextmanager, closing
import ctypes
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from .storage_guard import StorageError, preflight_workspace
from .workspace_backup import _files, _hash, RECEIPTS
from .workspace_lock import MARKER, reject_links


@contextmanager
def _write_guard(paths):
    if os.name != 'nt':
        raise StorageError('WAL recovery requires Windows write guards', code='storage_recovery_unavailable')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handles = []
    try:
        for path in paths:
            reject_links(path)
            handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise StorageError('Workspace file is in use', code='storage_busy')
            handles.append(handle)
        yield
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)


def recover_wal(root, lock, backup_dir):
    """Caller holds WorkspaceLock and has verified its original clean marker."""
    from .storage_maintenance import _path
    from .workspace_backup import _separate
    root = Path(root)
    names = _files(root, require_checkpoint=False, check_references=False)
    databases = [name for name in names if name.endswith('.sqlite3')]
    affected = [name for name in databases if (root / (name + '-wal')).is_file() and (root / (name + '-wal')).stat().st_size]
    if not affected:
        return None
    if not lock.was_clean:
        raise StorageError('Recovery requires clean shutdown', code='storage_recovery_required')
    directory = _path(backup_dir)
    if not directory.is_dir():
        raise StorageError('Backup directory does not exist', code='storage_backup_failed')
    backup = directory / ('reception-raw-recovery-' + uuid.uuid4().hex)
    _separate(root, backup)
    inventory = set(names)
    inventory.add(MARKER)
    for name in databases:
        for suffix in ('-wal', '-shm'):
            path = root / (name + suffix)
            reject_links(path)
            if path.exists():
                inventory.add(name + suffix)
    def current_inventory():
        result = set(_files(root, require_checkpoint=False, check_references=False))
        for name in databases:
            for suffix in ('-wal', '-shm'):
                if (root / (name + suffix)).exists():
                    result.add(name + suffix)
        if (root / MARKER).exists():
            result.add(MARKER)
        return result

    report = {'format': 'raw-recovery-snapshot-v1', 'source': str(root), 'status': 'snapshotting', 'files': [], 'checkpoints': []}
    def save():
        with (backup / 'raw-manifest.json').open('w', encoding='utf-8') as out:
            json.dump(report, out, ensure_ascii=False, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
    connections = []
    started = False
    try:
        backup.mkdir()
        save()
        with _write_guard([root / name for name in sorted(inventory)]):
            for name in sorted(inventory):
                dest = backup / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / name, dest)
                with dest.open('r+b') as stream:
                    os.fsync(stream.fileno())
                digest = _hash(dest)
                if digest != _hash(root / name):
                    raise StorageError('Snapshot verification failed', code='storage_backup_failed')
                report['files'].append({'path': name, 'sha256': digest})
            report['status'] = 'snapshot_verified'
            save()
            expected = {}
            with tempfile.TemporaryDirectory(prefix='reception-normalize-') as temp:
                for index, name in enumerate(affected):
                    copied = Path(temp) / str(index) / 'database.sqlite3'
                    copied.parent.mkdir()
                    shutil.copyfile(backup / name, copied)
                    shutil.copyfile(backup / (name + '-wal'), Path(str(copied) + '-wal'))
                    with closing(sqlite3.connect(copied, isolation_level=None)) as db:
                        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)] or tuple(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()) != (0, 0, 0):
                            raise StorageError('Snapshot WAL is invalid', code='storage_recovery_failed')
                    expected[name] = _hash(copied)
            if current_inventory() != inventory or any(_hash(root / item['path']) != item['sha256'] for item in report['files']):
                raise StorageError('Workspace changed during snapshot', code='storage_busy')
        original = {item['path']: item['sha256'] for item in report['files']}
        affected_files = {name + suffix for name in affected for suffix in ('', '-wal', '-shm')}
        protected = [root / name for name in inventory - affected_files - {MARKER}]
        with _write_guard(protected):
            lock.invalidate_clean_shutdown()
            started = True
            report['status'] = 'source_connection_phase_started'
            save()
            for name in affected:
                db = sqlite3.connect((root / name).resolve().as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=1)
                connections.append((name, db))
                if db.execute('PRAGMA locking_mode=EXCLUSIVE').fetchone()[0].lower() != 'exclusive':
                    raise StorageError('Exclusive lock unavailable', code='storage_busy')
                db.execute('BEGIN EXCLUSIVE')
                db.execute('PRAGMA schema_version').fetchone()
                db.execute('COMMIT')
            logical_names = lambda entries: {name for name in entries if name != MARKER and not name.endswith('-shm')}
            if logical_names(current_inventory()) != logical_names(inventory):
                raise StorageError('Workspace inventory changed', code='storage_busy')
            for name, digest in original.items():
                if name != MARKER and not name.endswith('-shm') and _hash(root / name) != digest:
                    raise StorageError('Workspace changed before recovery', code='storage_busy')
            report['status'] = 'checkpoint_in_progress'
            save()
            for name, db in connections:
                result = tuple(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
                report['checkpoints'].append({'database': name, 'result': result})
                save()
                if result != (0, 0, 0):
                    raise StorageError('Checkpoint busy or incomplete', code='storage_recovery_incomplete')
            if any(_hash(root / name) != expected[name] for name in affected):
                raise StorageError('Recovered database differs from snapshot', code='storage_recovery_incomplete')
            for _, db in connections:
                db.close()
            connections.clear()
            final = [root / name for name in affected]
            final += [root / (name + suffix) for name in affected for suffix in ('-wal', '-shm') if (root / (name + suffix)).exists()]
            with _write_guard(final):
                if any(_hash(root / name) != expected[name] for name in affected):
                    raise StorageError('Recovered database changed', code='storage_busy')
                if set(_files(root, receipt_roots=[str(root / RECEIPTS)])) != set(names):
                    raise StorageError('Workspace inventory changed', code='storage_recovery_incomplete')
                preflight_workspace(root)
                report['status'] = 'checkpoint_verified'
                save()
                lock.mark_clean_shutdown()
        return backup
    except StorageError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise StorageError('WAL recovery failed', code='storage_recovery_incomplete' if started else 'storage_backup_failed') from exc
    finally:
        for _, db in connections:
            db.close()
