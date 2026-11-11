"""Classify persisted seller ownership using private, read-only SQLite copies.

Only the dedicated seller metadata row and unbound-store row counts are queried.
Customer entities, revisions, settings values and credentials are never decoded.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from .workspace_lock import reject_links as _reject_io_links


def _io_path(path: Path) -> Path:
    # Private metadata inspection/byte copies only; SQLite opens the short copy.
    return Path('\\\\?\\' + str(path)) if os.name == 'nt' else path


def reject_links(path: Path):
    _reject_io_links(_io_path(path))


_ID = re.compile(r'[1-9][0-9]{0,127}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_MAX_DATABASE = 2 * 1024 * 1024 * 1024


@contextmanager
def _private_database(database: Path):
    reject_links(database)
    physical = _io_path(database)
    if not physical.is_file() or physical.stat().st_size > _MAX_DATABASE:
        raise ValueError('invalid_identity_store')
    wal = Path(str(database) + '-wal')
    reject_links(wal)
    reject_links(Path(str(database) + '-shm'))
    with tempfile.TemporaryDirectory(prefix='reception-owner-') as directory:
        copied = Path(directory) / 'identity.sqlite3'
        shutil.copyfile(physical, copied)
        physical_wal = _io_path(wal)
        if physical_wal.exists() and physical_wal.stat().st_size:
            if not physical_wal.is_file() or not 32 <= physical_wal.stat().st_size <= _MAX_DATABASE:
                raise ValueError('invalid_identity_store')
            shutil.copyfile(physical_wal, Path(str(copied) + '-wal'))
        connection = sqlite3.connect(copied)
        try:
            connection.execute('PRAGMA query_only=ON')
            yield connection
        finally:
            connection.close()


def _table(connection, name):
    if connection.execute('SELECT type FROM sqlite_master WHERE name=?', (name,)).fetchall() != [('table',)]:
        raise ValueError('invalid_identity_store')


def _seller_metadata(database: Path) -> str:
    with _private_database(database) as connection:
        _table(connection, 'entities')
        rows = connection.execute(
            "SELECT json_extract(data,'$.seller_id') FROM entities WHERE kind='meta' AND id='seller'"
        ).fetchall()
        if len(rows) != 1 or not isinstance(rows[0][0], str) or not _ID.fullmatch(rows[0][0]):
            raise ValueError('missing_identity_metadata')
        return rows[0][0]


def _unbound_is_empty(live: Path) -> bool:
    # Missing seller databases must never hide an existing send ledger.
    names = {item.name for item in live.iterdir()}
    allowed = {'sellers', 'workctl-state'}
    for name in ('control.sqlite3', 'adapters-live.sqlite3'):
        allowed.update((name, name + '-wal', name + '-shm'))
    if names - allowed:
        return False
    native = live / 'workctl-state'
    reject_links(native)
    if native.exists():
        if not native.is_dir() or {item.name for item in native.iterdir()} - {'chat-analysis'}:
            return False
        analysis = native / 'chat-analysis'
        reject_links(analysis)
        if analysis.exists():
            if not analysis.is_dir() or {item.name for item in analysis.iterdir()} - {'send-receipts'}:
                return False
            receipts = analysis / 'send-receipts'
            reject_links(receipts)
            if receipts.exists() and (not receipts.is_dir() or any(receipts.iterdir())):
                return False
    for name in ('control.sqlite3', 'adapters-live.sqlite3'):
        database = live / name
        if not database.exists() and any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-shm')):
            return False
    control = live / 'control.sqlite3'
    if control.exists():
        with _private_database(control) as connection:
            _table(connection, 'entities')
            if connection.execute("SELECT count(*) FROM entities WHERE kind IS NULL OR kind<>'meta'").fetchone()[0]:
                return False
            _table(connection, 'revisions')
            if connection.execute("SELECT count(*) FROM revisions WHERE kind IS NULL OR kind<>'meta'").fetchone()[0]:
                return False
            rows = connection.execute("SELECT json_extract(data,'$.seller_id') FROM entities WHERE kind='meta' AND id='seller'").fetchall()
            if rows != [('',)]:
                return False
    ledger = live / 'adapters-live.sqlite3'
    if control.exists() and (not ledger.exists() or not (native / 'chat-analysis' / 'send-receipts').is_dir()):
        return False
    if ledger.exists():
        if not control.exists():
            return False
        with _private_database(ledger) as connection:
            for name in ('operations', 'attempt_history', 'retry_grants'):
                _table(connection, name)
                if connection.execute('SELECT count(*) FROM ' + name).fetchone()[0]:
                    return False
    return True


def inspect_launch_ownership(data_root: Path) -> dict:
    """Return a bounded classification, never raw SQL values or exceptions."""
    try:
        root = Path(data_root).absolute()
        if str(root).startswith(('\\\\', '//')):
            return {'status': 'unknown'}
        reject_links(root)
        if not root.is_dir():
            return {'status': 'missing'}
        live = root / 'live'
        reject_links(live)
        allowed = {'live', 'demo', 'host', '.reception-workspace.lock', '.reception-clean-shutdown'}
        known_root_entries = all(item.name in allowed for item in root.iterdir())
        if not live.exists():
            return {'status': 'empty' if known_root_entries else 'unknown'}
        if not live.is_dir() or (live / 'workspace.sqlite3').exists():
            return {'status': 'unknown'}
        sellers = live / 'sellers'
        reject_links(sellers)
        if not sellers.exists():
            return {'status': 'empty' if known_root_entries and _unbound_is_empty(live) else 'unknown'}
        entries = list(sellers.iterdir())
        if len(entries) > 128:
            return {'status': 'unknown'}
        ids = []
        for entry in entries:
            reject_links(entry)
            if not _io_path(entry).is_dir() or not _HASH.fullmatch(entry.name):
                return {'status': 'unknown'}
            workspace = entry / 'workspace.sqlite3'
            reject_links(workspace)
            physical_workspace = _io_path(workspace)
            if physical_workspace.is_file() and physical_workspace.stat().st_size == 0:
                return {'status': 'unknown', 'reason': 'zero_byte_workspace'}
            seller = _seller_metadata(workspace)
            if hashlib.sha256(seller.encode('utf-8')).hexdigest() != entry.name:
                return {'status': 'unknown'}
            ids.append(seller)
        if not ids:
            return {'status': 'empty' if known_root_entries and _unbound_is_empty(live) else 'unknown'}
        if len(ids) != 1:
            return {'status': 'multiple'}
        return {'status': 'owned', 'sellerId': ids[0]}
    except Exception:
        return {'status': 'unknown'}
