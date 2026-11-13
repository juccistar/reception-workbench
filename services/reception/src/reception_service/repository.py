"""Small, transactional SQLite store. No external I/O occurs inside transactions."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex}'


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class Repository:
    def __init__(self, path: Path | None, seller_id: str = ''):
        from .storage_guard import REPOSITORY_SCHEMA, REPOSITORY_VERSION, validate_database, check_sqlite_path
        check_sqlite_path(path)
        version = validate_database(path, 'repository') if path is not None else None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path) if path else ':memory:', check_same_thread=False,
                                  isolation_level=None, timeout=5)
        try:
            with self.transaction():
                if version is None:
                    for statement in REPOSITORY_SCHEMA:
                        self.db.execute(statement)
                existing = self.get('meta', 'seller')
                if existing is not None and existing['seller_id'] != seller_id:
                    from .errors import ServiceError
                    raise ServiceError('seller_mismatch', '数据库卖家身份不匹配')
                self.db.execute(f'PRAGMA user_version={REPOSITORY_VERSION}')
                self.put('meta', 'seller', {'seller_id': seller_id})
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            nested = self.db.in_transaction
            if not nested:
                self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self
                if not nested:
                    self.db.execute('COMMIT')
            except BaseException:
                if not nested:
                    self.db.execute('ROLLBACK')
                raise

    def get(self, kind: str, key: str, default=None):
        with self.lock:
            row = self.db.execute('SELECT data FROM entities WHERE kind=? AND id=?', (kind, key)).fetchone()
            return json.loads(row[0]) if row else default

    def list(self, kind: str, **filters) -> list[dict]:
        # Filter ownership in SQLite so opening one customer never decodes another
        # customer's entire message history. The JSON paths are a fixed whitelist.
        if set(filters) - {'conversation_id', 'case_id'}:
            raise ValueError('Unsupported storage filter')
        where = ''.join(" AND json_extract(data,'$." + key + "')=?" for key in filters)
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM entities WHERE kind=?' + where + ' ORDER BY rowid',
                (kind, *filters.values()))]

    def put(self, kind: str, key: str, value: dict, *, history: bool = False):
        with self.lock:
            payload = encoded(value)
            if history:
                self.db.execute('INSERT INTO revisions(kind,id,data,saved_at) VALUES(?,?,?,?)',
                                (kind, key, payload, now_ms()))
            self.db.execute('INSERT INTO entities(kind,id,data) VALUES(?,?,?) '
                            'ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data', (kind, key, payload))

    def history(self, kind: str, key: str) -> list[dict]:
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT data FROM revisions WHERE kind=? AND id=? ORDER BY sequence', (kind, key))]

    def compact_supplements(self):
        """Remove only note history; deduplication retains hashes, never old bodies."""
        import hashlib
        with self.transaction():
            if self.get('meta', 'supplement_storage', {}).get('format') == 2:
                return
            self.db.execute("DELETE FROM revisions WHERE kind IN ('conversation_supplement','supplement_mutation')")
            rows = self.db.execute("SELECT id,data FROM entities WHERE kind='supplement_mutation'").fetchall()
            for key, payload in rows:
                record = json.loads(payload)
                request, result = record.get('request'), record.get('result')
                if isinstance(request, dict) and isinstance(result, dict):
                    self.put('supplement_mutation', key, {
                        'request_hash': hashlib.sha256(encoded(request).encode('utf-8')).hexdigest(),
                        'conversation_id': request.get('conversation_id'),
                        'revision': result.get('revision'), 'updated_at': result.get('updated_at')})
                elif not isinstance(record.get('request_hash'), str):
                    self.db.execute("DELETE FROM entities WHERE kind='supplement_mutation' AND id=?", (key,))
            self.put('meta', 'supplement_storage', {'format': 2})

    def close(self):
        from .storage_guard import StorageError
        with self.lock:
            error = None
            try:
                mode = self.db.execute('PRAGMA journal_mode').fetchone()[0].lower()
                if mode == 'wal':
                    result = tuple(self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
                    if result != (0, 0, 0):
                        raise StorageError('SQLite checkpoint busy or incomplete', code='storage_busy')
            except Exception as exc:
                error = exc
            try:
                self.db.close()
            except Exception as exc:
                error = error or exc
            if error:
                raise error
