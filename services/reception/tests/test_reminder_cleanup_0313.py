"""Safety backup, exact cleanup, receipt preservation and restart idempotence."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from reception_service.repository import Repository
from reception_service.storage_guard import StorageError
from reception_service.storage_maintenance import run_maintenance
from reception_service.workspace_backup import backup_workspace
from reception_service.workspace_lock import WorkspaceLock
from reception_service.integrations.accio_native.vendor.adapters.common import Ledger


class ReminderCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'workspace'
        self.relative = 'live/sellers/synthetic/workspace.sqlite3'
        Repository(self.root / 'live/control.sqlite3').close()
        self.dbpath = self.root / self.relative
        repo = Repository(self.dbpath, 'synthetic-seller')
        self.case = {'case_id': 'case', 'seller_id': 'synthetic-seller', 'conversation_id': 'c', 'revision': 7,
            'summary': '旧采购事项摘要', 'requirement_items': [{'value': 'duplicate'}], 'last_changes': ['old'],
            'facts': {'quantity': {'value': '100 pcs', 'display_value': '100件', 'status': 'confirmed', 'evidence_ids': ['m']}},
            'product_ref': {'original_title': 'Old Platform Title', 'observed_product_id': '123'}}
        self.draft = {'draft_id': 'd', 'status': 'unknown', 'reply': 'Original reply',
            'validation': {'ready': False, 'issues': ['operation_unresolved'], 'warnings': ['旧质量提醒']},
            'quality_warnings': ['old'], 'supplement_issue_ids': ['stable'], 'source_versions': {'m': 'original'}}
        repo.put('case', 'case', self.case, history=True)
        repo.put('draft', 'd', self.draft, history=True)
        for kind in ('business_progress', 'assistance_task', 'assistance_mutation', 'assistance_start_mutation', 'requirement_proposal'):
            repo.put(kind, 'old', {'warning': '旧待核实提醒'}, history=True)
        self.protected = {
            'message': {'message_id': 'm', 'text': 'Original title and warnings in a real message', 'attachments': []},
            'image_asset': {'id': 'image', 'data': 'unmodified synthetic bytes'},
            'message_image': {'id': 'photo', 'message_id': 'm'},
            'operation': {'operation_id': 'operation', 'status': 'unknown', 'submission_started': True,
                          'retry_attempt': 2, 'intent': {'reply': 'Original reply', 'client_request_id': 'stable-key'}},
            'outbox': {'id': 'outbox', 'status': 'pending'},
            'continuity_plan': {'id': 'plan', 'supplement_issue_ids': ['stable'], 'result_version': 8, 'status': 'unknown'},
            'supplement_followup': {'conversation_id': 'c', 'issue_ids': ['stable'], 'source_message_id': 'm',
                'source_text_sha256': 'original', 'business_note_source_id': 'note-original', 'status': 'pending'},
            'conversation_supplement': {'body': 'Current note', 'revision': 2},
        }
        for kind, value in self.protected.items():
            repo.put(kind, 'protected', value, history=True)
        repo.close()
        self.ledger = self.root / 'live/adapters-live.sqlite3'
        Ledger(self.ledger).initialize()
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?)', ('original-operation', 'original-key', 'send', '{}', '{"status":"unknown"}'))
            db.execute('INSERT INTO attempt_history(operation_id,result,stamp) VALUES (?,?,?)', ('original-operation', 'unknown', 123))
        receipt = self.root / 'live/workctl-state/chat-analysis/send-receipts/original.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_bytes(b'{"operation_id":"original-operation","status":"unknown"}')
        with WorkspaceLock(self.root) as lock:
            lock.mark_clean_shutdown()

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, path, kind, identifier, table='entities'):
        with closing(sqlite3.connect(path)) as db:
            row = db.execute(f'SELECT data FROM {table} WHERE kind=? AND id=?', (kind, identifier)).fetchone()
            return json.loads(row[0]) if row else None

    def test_full_verified_backup_precedes_exact_current_and_history_cleanup(self):
        ledger_bytes = self.ledger.read_bytes()
        receipt_path = self.root / 'live/workctl-state/chat-analysis/send-receipts/original.json'
        receipt_bytes = receipt_path.read_bytes()
        result = run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        archive = Path(result['backup'])
        self.assertTrue((archive / 'manifest.json').is_file())
        self.assertEqual(self.read(archive / self.relative, 'case', 'case'), self.case)
        self.assertIsNotNone(self.read(archive / self.relative, 'assistance_task', 'old'))
        self.assertGreater(result['reminder_cleanup']['deleted_reminder_records'], 0)
        for table in ('entities', 'revisions'):
            self.assertIsNone(self.read(self.dbpath, 'business_progress', 'old', table))
            self.assertIsNone(self.read(self.dbpath, 'assistance_task', 'old', table))
            case = self.read(self.dbpath, 'case', 'case', table)
            self.assertNotIn('summary', case)
            self.assertNotIn('original_title', case['product_ref'])
            self.assertEqual(case['facts']['quantity'], {'value': '100 pcs', 'evidence_ids': ['m']})
            draft = self.read(self.dbpath, 'draft', 'd', table)
            self.assertNotIn('warnings', draft['validation'])
            self.assertEqual(draft['validation']['issues'], ['operation_unresolved'])
            self.assertEqual(draft['status'], 'unknown')
            self.assertEqual(draft['supplement_issue_ids'], ['stable'])
            for kind, expected in self.protected.items():
                self.assertEqual(self.read(self.dbpath, kind, 'protected', table), expected, kind)
        self.assertEqual(self.ledger.read_bytes(), ledger_bytes)
        self.assertEqual(receipt_path.read_bytes(), receipt_bytes)
        self.assertTrue((self.root / '.reception-clean-shutdown').is_file())
        again = run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        self.assertIsNone(again['reminder_cleanup'])
        self.assertIsNone(again['backup'])

    def test_missing_backup_destination_refuses_without_deleting_data(self):
        before = self.dbpath.read_bytes()
        with self.assertRaises(StorageError):
            run_maintenance('prepare', str(self.root))
        self.assertEqual(self.dbpath.read_bytes(), before)
        self.assertIsNotNone(self.read(self.dbpath, 'business_progress', 'old'))

    def test_backup_failure_does_not_clean_anything(self):
        with patch('reception_service.storage_maintenance.backup_workspace', side_effect=StorageError('synthetic backup failure')):
            with self.assertRaises(StorageError):
                run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        self.assertEqual(self.read(self.dbpath, 'case', 'case'), self.case)
        self.assertIsNotNone(self.read(self.dbpath, 'assistance_task', 'old'))

    def test_changed_source_after_backup_refuses_cleanup(self):
        def change_after_backup(root, destination):
            result = backup_workspace(root, destination)
            with closing(sqlite3.connect(self.dbpath)) as db, db:
                db.execute('UPDATE entities SET data=? WHERE kind=? AND id=?',
                           (json.dumps(dict(self.protected['message'], text='new original message')), 'message', 'protected'))
            return result
        with patch('reception_service.storage_maintenance.backup_workspace', side_effect=change_after_backup):
            with self.assertRaisesRegex(StorageError, 'changed after backup'):
                run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        self.assertIsNotNone(self.read(self.dbpath, 'business_progress', 'old'))
        self.assertEqual(self.read(self.dbpath, 'case', 'case'), self.case)

    def test_readonly_inspect_does_not_apply_content_cleanup(self):
        before = self.dbpath.read_bytes()
        run_maintenance('inspect', str(self.root))
        self.assertEqual(self.dbpath.read_bytes(), before)
        self.assertEqual(self.read(self.dbpath, 'case', 'case'), self.case)

    def test_busy_workspace_cannot_be_cleaned(self):
        with WorkspaceLock(self.root):
            with self.assertRaises((OSError, StorageError)):
                run_maintenance('prepare', str(self.root), backup_dir=str(self.base))
        self.assertIsNotNone(self.read(self.dbpath, 'business_progress', 'old'))


if __name__ == '__main__':
    unittest.main()
