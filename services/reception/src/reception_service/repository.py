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
        self._image_metadata_cache = {}
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
            self.db.execute('PRAGMA journal_mode=WAL').fetchone()
            self.db.execute('PRAGMA synchronous=FULL')
            # Create TEMP tracking after WAL is established. SQLite can retain a
            # TEMP schema read lock across a journal-mode change, preventing an
            # immediate checkpoint/close before the first ordinary data query.
            with self.transaction():
                self.db.execute('CREATE TEMP TABLE image_entity_versions (kind TEXT, id TEXT, version INTEGER NOT NULL, PRIMARY KEY(kind,id))')
                for action in ('INSERT', 'UPDATE', 'DELETE'):
                    row = 'OLD' if action == 'DELETE' else 'NEW'
                    self.db.execute(f"""CREATE TEMP TRIGGER image_version_{action.lower()} AFTER {action} ON main.entities
                        WHEN {row}.kind IN ('image_asset','supplement_attachment') BEGIN
                        INSERT INTO image_entity_versions VALUES ({row}.kind, {row}.id, 1)
                        ON CONFLICT(kind,id) DO UPDATE SET version=version+1; END""")
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

    def image_transfer_metadata(self, kind: str, key: str):
        """Fixed projection and transactional generation for cached image records."""
        if kind not in ('image_asset', 'supplement_attachment'):
            raise ValueError('Unsupported image record')
        with self.lock:
            external_version = self.db.execute('PRAGMA data_version').fetchone()[0]
            generation = self.db.execute('SELECT version FROM image_entity_versions WHERE kind=? AND id=?', (kind, key)).fetchone()
            stamp = (external_version, generation[0] if generation else 0)
            cached = self._image_metadata_cache.get((kind, key))
            if not self.db.in_transaction and cached and cached['version'] == stamp:
                return dict(cached)
            row = self.db.execute("""SELECT json_extract(data,'$.sha256'), json_extract(data,'$.size'),
                json_extract(data,'$.mime'), json_extract(data,'$.media_type'),
                json_extract(data,'$.seller_id'), json_extract(data,'$.conversation_id'),
                json_extract(data,'$.buyer_id'), json_extract(data,'$.id'),
                json_extract(data,'$.status'), json_extract(data,'$.origin'),
                json_type(data,'$.original_b64'), length(json_extract(data,'$.original_b64'))
                FROM entities e WHERE kind=? AND id=?""", (kind, key)).fetchone()
            if row is None:
                self._image_metadata_cache.pop((kind, key), None)
                return None
            names = ('sha256', 'size', 'mime', 'media_type', 'seller_id', 'conversation_id',
                     'buyer_id', 'id', 'status', 'origin', 'content_type', 'content_length')
            result = dict(zip(names, row), version=stamp)
            if not self.db.in_transaction:
                if len(self._image_metadata_cache) >= 128:
                    self._image_metadata_cache.clear()
                self._image_metadata_cache[(kind, key)] = result
            return dict(result)

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

    def message_page(self, conversation_id: str, limit: int, before: tuple[int, str] | None = None) -> list[dict]:
        """Return the newest page in display order, before an exclusive composite cursor.

        The caller must first establish ownership of the conversation. LIMIT bounds
        returned and decoded rows; without an index SQLite may still scan and sort.
        """
        if not isinstance(conversation_id, str) or not conversation_id or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Invalid message page')
        if before is not None and (not isinstance(before, tuple) or len(before) != 2 or type(before[0]) is not int or not isinstance(before[1], str)):
            raise ValueError('Invalid message cursor')
        timestamp = "COALESCE(json_extract(data,'$.timestamp_ms'),-1)"
        message_id = "json_extract(data,'$.message_id')"
        clause = '' if before is None else f' AND ({timestamp}, {message_id}) < (?, ?)'
        params = ('message', conversation_id, *(before or ()), limit)
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM entities WHERE kind=? AND json_extract(data,'$.conversation_id')=?"
                + clause + f' ORDER BY {timestamp} DESC, {message_id} DESC LIMIT ?', params).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

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
