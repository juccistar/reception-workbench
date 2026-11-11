"""Safe diagnostic projection must not recover/mutate or export customer content."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/read-live-checks.py'
spec = importlib.util.spec_from_file_location('reception_live_checks_reader', SCRIPT)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class LiveChecksReaderTests(unittest.TestCase):
    def test_missing_workspace_is_not_created_or_reported_as_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'not-created'
            report = reader.read_checks(root, '123456789')
            self.assertFalse(root.exists())
            self.assertFalse(report['database_present'])
            self.assertEqual(report['current_reception_status'], 'not_observed')
            self.assertEqual(report['checks'], [])

    def test_projects_only_check_receipts_without_mutating_the_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seller = '123456789'
            database = root / 'live/sellers' / hashlib.sha256(seller.encode()).hexdigest() / 'workspace.sqlite3'
            database.parent.mkdir(parents=True)
            job = {'job_id': 'check-1', 'kind': 'check_ai', 'status': 'failed', 'stage': 'answer',
                   'created_at': 1000, 'started_at': 1100, 'completed_at': 1200,
                   'error': {'code': 'cli_unclassified_error', 'message': 'PRIVATE CUSTOMER TEXT'},
                   'diagnostics': {'exit_code': 70, 'elapsed_ms': 180000, 'stderr': 'PRIVATE TOKEN'},
                   'result': {'reply': 'PRIVATE CUSTOMER TEXT'}}
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE entities(kind TEXT,id TEXT,data TEXT)')
                connection.execute('INSERT INTO entities VALUES(?,?,?)', ('job', 'check-1', json.dumps(job)))
                connection.execute('INSERT INTO entities VALUES(?,?,?)', ('job', 'customer-generation', json.dumps({**job, 'kind': 'generate_draft'})))
                connection.execute('INSERT INTO entities VALUES(?,?,?)', ('message', 'buyer-message', json.dumps({'text': 'PRIVATE CUSTOMER TEXT'})))
            connection.close()
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            report = reader.read_checks(root, seller)
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
            self.assertEqual(len(report['checks']), 1)
            self.assertEqual(report['checks'][0]['error_code'], 'cli_unclassified_error')
            self.assertEqual(report['checks'][0]['diagnostics'], {'elapsed_ms': 180000, 'exit_code': 70})
            self.assertNotIn('PRIVATE', json.dumps(report))
            self.assertEqual(report['current_reception_status'], 'not_observed')

    def test_rejects_non_numeric_seller_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            for seller in ('../elsewhere', '１２３', '0', ''):
                with self.subTest(seller=seller), self.assertRaises(ValueError):
                    reader.read_checks(Path(temporary), seller)
