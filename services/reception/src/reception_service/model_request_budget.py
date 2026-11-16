"""Cross-process model request admission. The ledger never contains request data."""
from __future__ import annotations

import asyncio
import ctypes
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .model_providers.encoding import failure
from .model_providers.owned_sync import owned_sync


def _directory():
    override = os.environ.get('RECEPTION_MODEL_BUDGET_DIRECTORY')
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError('model budget directory must be absolute')
        return path
    if os.name == 'nt':
        from ctypes import wintypes
        class GUID(ctypes.Structure):
            _fields_ = [('a', wintypes.DWORD), ('b', wintypes.WORD), ('c', wintypes.WORD), ('d', ctypes.c_ubyte * 8)]
        folder = GUID(0xF1B32785, 0x6FBA, 0x4FCF, (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91))
        result = ctypes.c_void_p()
        shell = ctypes.windll.shell32
        shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        if shell.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(result)) != 0:
            raise OSError('cannot resolve LocalAppData KnownFolder')
        try:
            root = Path(ctypes.wstring_at(result.value))
        finally:
            ctypes.windll.ole32.CoTaskMemFree(result)
        return root / 'AlibabaReceptionWorkbenchHost' / 'model-budget'
    return Path.home() / '.local' / 'share' / 'AlibabaReceptionWorkbenchHost' / 'model-budget'


def process_identity(pid):
    """Return creation identity, None for confirmed dead, or raise if uncertain."""
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise OSError('cannot inspect process')
        try:
            creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise OSError('cannot inspect process times')
            if exit_time.dwLowDateTime or exit_time.dwHighDateTime:
                return None
            return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path('/proc/%d/stat' % pid).read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return fields[19]
    except FileNotFoundError:
        return None


class ModelRequestBudget:
    def __init__(self, directory=None, *, probe=process_identity, max_queue=128, per_owner=16):
        self.directory = Path(directory) if directory is not None else _directory()
        if not self.directory.is_absolute():
            raise ValueError('model budget directory must be absolute')
        self.probe = probe
        self.pid = os.getpid()
        self.created = probe(self.pid)
        if not self.created:
            raise OSError('cannot establish process identity')
        self.owner = '%s:%s' % (self.pid, self.created)
        self.max_queue, self.per_owner = max_queue, per_owner
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'budget.sqlite3'
        with closing(self._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL, owner TEXT NOT NULL, pid INTEGER NOT NULL, created TEXT NOT NULL, purpose TEXT NOT NULL, heavy INTEGER NOT NULL, granted INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS owners (owner TEXT PRIMARY KEY, last_grant INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS counter (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL);
                INSERT OR IGNORE INTO counter VALUES (1, 0);
            ''')

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=0.2, isolation_level=None)
        db.execute('PRAGMA busy_timeout=200')
        return db

    def _prune(self, db):
        for pid, created in db.execute('SELECT DISTINCT pid, created FROM requests').fetchall():
            try:
                actual = self.probe(pid)
            except (OSError, ValueError):
                continue  # Unknown is not evidence of death.
            if actual is None or str(actual) != created:
                db.execute('DELETE FROM requests WHERE pid=? AND created=?', (pid, created))
        db.execute('DELETE FROM owners WHERE owner NOT IN (SELECT owner FROM requests) AND owner NOT IN (SELECT owner FROM owners ORDER BY last_grant DESC LIMIT 1024)')

    def _step(self, token, purpose, heavy, enqueue):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            self._prune(db)
            if enqueue:
                total = db.execute('SELECT count(*) FROM requests WHERE granted=0').fetchone()[0]
                own = db.execute('SELECT count(*) FROM requests WHERE granted=0 AND owner=?', (self.owner,)).fetchone()[0]
                if total >= self.max_queue or own >= self.per_owner:
                    raise failure('model_resource_busy')
                db.execute('INSERT INTO requests(token,owner,pid,created,purpose,heavy) VALUES(?,?,?,?,?,?)', (token, self.owner, self.pid, str(self.created), purpose, int(heavy)))
            active = db.execute('SELECT purpose,heavy FROM requests WHERE granted=1').fetchall()
            if len(active) < 2:
                used = {row[0] for row in active}
                heavy_used = any(row[1] for row in active)
                candidates = db.execute('''SELECT r.token,r.owner,r.purpose,r.heavy FROM requests r LEFT JOIN owners o ON o.owner=r.owner WHERE r.granted=0 ORDER BY COALESCE(o.last_grant,0),r.seq''').fetchall()
                for candidate, owner, candidate_purpose, candidate_heavy in candidates:
                    if candidate_purpose in used or (candidate_heavy and heavy_used):
                        continue
                    if candidate == token:
                        db.execute('UPDATE requests SET granted=1 WHERE token=?', (token,))
                        db.execute('UPDATE counter SET value=value+1 WHERE id=1')
                        db.execute('INSERT OR REPLACE INTO owners VALUES (?, (SELECT value FROM counter WHERE id=1))', (owner,))
                        db.commit()
                        return True
                    break
            db.commit()
            return False

    def _remove(self, token):
        while True:
            try:
                with closing(self._connect()) as db:
                    db.execute('DELETE FROM requests WHERE token=?', (token,))
                return
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                    raise
                time.sleep(0.05)

    async def acquire(self, purpose, heavy=False):
        token = uuid.uuid4().hex
        enqueued = False
        try:
            while True:
                try:
                    granted = self._step(token, purpose, heavy, not enqueued)
                    enqueued = True
                    if granted:
                        return token
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                        raise
                await asyncio.sleep(0.05)
        except BaseException:
            if enqueued:
                await owned_sync(lambda: self._remove(token), complete_on_cancel=True)
            raise

    def release(self, token):
        self._remove(token)
