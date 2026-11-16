"""Synthetic regression checks for bounded reads and ownership-safe cleanup."""
import asyncio
import base64
import hashlib
import json
from contextlib import closing
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from reception_service.repository import Repository
from reception_service.storage_guard import REPOSITORY_SCHEMA, _expected, _signature
from reception_service.supplement_attachments import attachment_projections, attachment_rows, parse_attachment
from reception_service.model_providers.runtime import ModelRuntime


class PerformanceTests(unittest.TestCase):
    def test_legacy_database_preserves_schema_and_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workspace.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                for statement in REPOSITORY_SCHEMA:
                    db.execute(statement)
                db.execute('PRAGMA user_version=2')
                db.execute('INSERT INTO entities VALUES (?,?,?)', ('meta', 'seller', '{"seller_id":"seller"}'))
                for i in range(120):
                    key = 'm%03d' % i
                    db.execute('INSERT INTO entities VALUES (?,?,?)', ('message', key, json.dumps(
                        {'conversation_id': 'c', 'timestamp_ms': i // 3, 'message_id': key})))
                db.commit()
                db.execute('PRAGMA journal_mode=WAL').fetchone()
                schema_before = _signature(db)
            hash_before = hashlib.sha256(path.read_bytes()).hexdigest()
            repo = Repository(path, 'seller')
            try:
                first = repo.message_page('c', 50)
                second = repo.message_page('c', 50, (first[0]['timestamp_ms'], first[0]['message_id']))
                self.assertEqual([r['message_id'] for r in second + first], ['m%03d' % i for i in range(20, 120)])
                self.assertEqual(_signature(repo.db), schema_before)
            finally:
                repo.close()
            with closing(sqlite3.connect(path)) as db:
                schema_after = _signature(db)
                self.assertEqual(schema_after, _expected(REPOSITORY_SCHEMA))
            hash_after = hashlib.sha256(path.read_bytes()).hexdigest()
            schema_hash = hashlib.sha256(repr(schema_before).encode()).hexdigest()
            self.assertEqual(schema_hash, hashlib.sha256(repr(schema_after).encode()).hexdigest())
            print(f'legacy fixture schema sha256={schema_hash}; database before={hash_before}; after={hash_after}')

    def test_new_database_has_cursor_index(self):
        repo = Repository(None)
        self.addCleanup(repo.close)
        plan = repo.db.execute("EXPLAIN QUERY PLAN SELECT data FROM entities WHERE kind='message' AND json_extract(data,'$.conversation_id')='c' ORDER BY COALESCE(json_extract(data,'$.timestamp_ms'),-1) DESC,json_extract(data,'$.message_id') DESC LIMIT 50")
        self.assertIn('entities_message_cursor', ' '.join(str(row) for row in plan))

    def test_projection_cache_invalidates_and_model_rechecks(self):
        repo = Repository(None)
        self.addCleanup(repo.close)
        item, original = parse_attachment('a.txt', base64.b64encode(b'hello').decode())
        item.update(conversation_id='c', seller_id='s', original_b64=original)
        repo.put('supplement_attachment', item['id'], item)
        with patch('reception_service.supplement_attachments.base64.b64decode', wraps=base64.b64decode) as decode:
            first = attachment_projections(repo, 'c')
            self.assertEqual(decode.call_count, 1)
            attachment_projections(repo, 'c')
            self.assertEqual(decode.call_count, 1)
            attachment_rows(repo, 'c')
            self.assertEqual(decode.call_count, 2)
            item['original_b64'] = base64.b64encode(b'wrong').decode()
            repo.put('supplement_attachment', item['id'], item)
            self.assertEqual(attachment_projections(repo, 'c')[0]['status'], 'failed')
        self.assertEqual(first[0]['status'], 'ready')

    def test_projection_detects_external_changes_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workspace.sqlite3'
            repo = Repository(path)
            try:
                item, original = parse_attachment('a.txt', base64.b64encode(b'hello').decode())
                item.update(conversation_id='c', seller_id='', original_b64=original)
                repo.put('supplement_attachment', item['id'], item)
                self.assertEqual(attachment_projections(repo, 'c')[0]['status'], 'ready')
                try:
                    with repo.transaction():
                        repo.db.execute("DELETE FROM entities WHERE kind='supplement_attachment'")
                        self.assertEqual(attachment_projections(repo, 'c'), [])
                        raise RuntimeError('rollback')
                except RuntimeError:
                    pass
                self.assertEqual(attachment_projections(repo, 'c')[0]['status'], 'ready')
                with closing(sqlite3.connect(path)) as other:
                    other.execute("DELETE FROM entities WHERE kind='supplement_attachment'")
                    other.commit()
                self.assertEqual(attachment_projections(repo, 'c'), [])
            finally:
                repo.close()

    def test_release_does_not_block_loop_or_release_early(self):
        entered = threading.Event()
        release = threading.Event()
        event_loop_ran = threading.Event()
        class Budget:
            def __init__(self, **kwargs): pass
            async def acquire(self, purpose, heavy): return 'token'
            def release(self, token):
                entered.set()
                release.wait(5)
        runtime = ModelRuntime(cleanup_timeout=3, budget_factory=Budget)
        def run():
            runtime.run('reception', lambda: asyncio.sleep(0), deadline=5)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            runtime._loop.call_soon_threadsafe(event_loop_ran.set)
            self.assertTrue(event_loop_ran.wait(1))
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(5)
            runtime.close()
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
