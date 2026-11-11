"""Cooperative process lock and clean-shutdown evidence for managed roots.

This does not exclude older applications or other non-cooperating writers.
"""
import os
from pathlib import Path
import stat
import threading

_children = {}
_children_lock = threading.Lock()

def track_native_child(process):
    with _children_lock:
        _children[id(process)] = process

def release_native_child(process):
    with _children_lock:
        if process.poll() is not None:
            _children.pop(id(process), None)

def native_children_stopped():
    with _children_lock:
        for key, process in list(_children.items()):
            if process.poll() is not None:
                del _children[key]
        return not _children

MARKER = '.reception-clean-shutdown'
LOCK = '.reception-workspace.lock'
MAGIC = b'reception-managed-clean-v1\n'

def reject_links(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Symlink or reparse point is not allowed')

class WorkspaceLock:
    def __init__(self, data_root):
        self.root = Path(os.path.abspath(data_root))
        self.handle = None
        self.was_clean = False

    def acquire(self, *, invalidate_clean=True):
        reject_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / LOCK
        reject_links(lock_path)
        self.handle = open(lock_path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0)
                if not self.handle.read(1):
                    self.handle.write(b'0'); self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker = self.root / MARKER
            reject_links(marker)
            self.was_clean = marker.is_file() and marker.read_bytes() == MAGIC
            if invalidate_clean:
                self.invalidate_clean_shutdown()
            return self
        except BaseException:
            self.handle.close()
            self.handle = None
            raise

    def invalidate_clean_shutdown(self):
        if self.handle is None:
            raise RuntimeError('Workspace lock is not held')
        marker = self.root / MARKER
        reject_links(marker)
        marker.unlink(missing_ok=True)

    def mark_clean_shutdown(self):
        if self.handle is None:
            raise RuntimeError('Workspace lock is not held')
        marker = self.root / MARKER
        reject_links(marker)
        with open(marker, 'xb') as out:
            out.write(MAGIC)
            out.flush()
            os.fsync(out.fileno())

    def release(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()
