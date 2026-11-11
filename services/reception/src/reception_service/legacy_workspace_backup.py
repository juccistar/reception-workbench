"""One-time offline export of pre-lock-protocol workspaces on Windows.

The caller must independently establish that every owning process and operation
has stopped. File sharing restrictions and inventories are additional safeguards,
not a distributed transaction or proof of shutdown.
"""
import ctypes
from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .storage_guard import StorageError
from .workspace_lock import WorkspaceLock, reject_links
from .workspace_backup import (MANIFEST, PROVENANCE, RECEIPTS, _files, _hash, _publish,
                               _receipt_roots, _separate, _target, _validate_archive,
                               _write_provenance)


@dataclass(frozen=True)
class LegacyShutdownEvidence:
    # Concrete operator observations, not merely a clean-shutdown marker.
    stopped_processes: str
    stopped_operations: str


def _inventory(root):
    names = _files(root, require_checkpoint=False, check_references=False)
    all_names = list(names)
    for name in names:
        if name.endswith('.sqlite3'):
            for suffix in ('-wal', '-shm'):
                sidecar = root / (name + suffix)
                reject_links(sidecar)
                if sidecar.exists():
                    if not sidecar.is_file():
                        raise StorageError('Invalid database sidecar')
                    all_names.append(name + suffix)
    return names, sorted(all_names)


class _ReadOnlyHandles:
    """Hold handles denying writes/deletes, including existing SQLite sidecars."""
    def __init__(self):
        self.handles = []

    def __enter__(self):
        if os.name != 'nt':
            raise StorageError('Legacy export requires Windows file-sharing safeguards')
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                            wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        return self

    def add(self, path):
        # FILE_SHARE_READ only: an existing or subsequent write/delete handle fails.
        handle = self.kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise StorageError('Workspace file is in use or cannot be protected')
        self.handles.append(handle)

    def __exit__(self, *args):
        for handle in reversed(self.handles):
            self.kernel.CloseHandle(handle)


def _snapshot(root, names):
    result = {}
    for name in names:
        path = root / name
        reject_links(path)
        before = path.stat()
        digest = _hash(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise StorageError('Workspace changed during export')
        result[name] = (after.st_size, after.st_mtime_ns, digest)
    return result


def export_legacy_workspace(data_root, destination, *, evidence):
    """Export after independently verifying stopped owners and pending operations.

    Evidence fields describe the actual checks performed; this function cannot
    independently verify them. It never creates or alters a source clean marker.
    """
    if (not isinstance(evidence, LegacyShutdownEvidence)
            or not isinstance(evidence.stopped_processes, str)
            or not evidence.stopped_processes.strip()
            or not isinstance(evidence.stopped_operations, str)
            or not evidence.stopped_operations.strip()):
        raise StorageError('Verified process and operation shutdown evidence is required')
    root = Path(os.path.abspath(data_root))
    target = _target(destination)
    reject_links(root)
    _separate(root, target)
    if not root.is_dir():
        raise StorageError('Workspace does not exist')
    lock = WorkspaceLock(root)
    lock.acquire(invalidate_clean=False)
    try:
        names, protected = _inventory(root)
        with _ReadOnlyHandles() as handles:
            for name in protected:
                handles.add(root / name)
            if _inventory(root) != (names, protected):
                raise StorageError('Workspace inventory changed during export')
            before = _snapshot(root, protected)
            stage = Path(tempfile.mkdtemp(prefix='.reception-legacy-backup-', dir=target.parent))
            try:
                with tempfile.TemporaryDirectory(prefix='reception-legacy-private-') as private:
                    entries = []
                    for index, name in enumerate(names):
                        if name == PROVENANCE:
                            continue
                        source, dest = root / name, stage / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if name.endswith('.sqlite3'):
                            copy = Path(private) / (str(index) + '.sqlite3')
                            shutil.copyfile(source, copy)
                            wal = root / (name + '-wal')
                            if wal.exists() and wal.stat().st_size:
                                if wal.stat().st_size < 32:
                                    raise StorageError('Invalid SQLite WAL')
                                shutil.copyfile(wal, Path(str(copy) + '-wal'))
                            try:
                                with closing(sqlite3.connect(copy)) as db, closing(sqlite3.connect(dest)) as out:
                                    db.backup(out)
                            except sqlite3.Error as exc:
                                raise StorageError('Invalid database') from exc
                        else:
                            shutil.copy2(source, dest)
                            if dest.stat().st_mtime_ns != before[name][1]:
                                raise StorageError('Receipt timestamp precision could not be preserved')
                        s = dest.stat()
                        entries.append({'path': name, 'size': s.st_size,
                                        'sha256': _hash(dest), 'mtime_ns': s.st_mtime_ns})
                if (root / RECEIPTS).is_dir():
                    (stage / RECEIPTS).mkdir(parents=True, exist_ok=True)
                _write_provenance(stage, [*_receipt_roots(root), str(root / RECEIPTS)], entries)
                (stage / MANIFEST).write_text(json.dumps({'format': 1, 'files': entries}, sort_keys=True), encoding='utf-8')
                _validate_archive(stage)
                if _inventory(root) != (names, protected) or _snapshot(root, protected) != before:
                    raise StorageError('Workspace changed during export')
                _publish(stage, target)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
    finally:
        lock.release()
    return target
