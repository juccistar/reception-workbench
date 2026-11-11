"""Activity projection against temporary SQLite stores; no external integrations."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reception_service.repository import Repository
from reception_service.sync_activity import visible_jobs


class SyncActivityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Repository(Path(temp.name) / 'activity.sqlite3', 'synthetic-seller')
        self.addCleanup(self.repo.close)

    def job(self, job_id, *, kind='sync_messages', status='failed', created_at=1000, **fields):
        job = {
            'job_id': job_id, 'kind': kind, 'status': status,
            'conversation_id': 'synthetic-customer', 'case_id': 'synthetic-case',
            'stage': kind, 'message': '合成任务', 'created_at': created_at,
            'started_at': created_at, 'completed_at': created_at + 10,
            **fields,
        }
        self.repo.put('job', job_id, job, history=True)
        return job

    def test_thousands_of_routine_background_jobs_are_filtered_before_json_decode(self):
        statuses = ('succeeded', 'queued', 'running', 'deferred', 'waiting', 'pending', 'cancelled', 'stale')
        with self.repo.transaction():
            for index in range(4000):
                flag = {'background_sync': True} if index % 2 else {'sync_generation': 0}
                self.job(f'routine-{index}', status=statuses[(index // 2) % len(statuses)],
                         created_at=index, result={'message': '后台原始正文'}, **flag)
            business = self.job('business', kind='generate_draft', status='running', created_at=1)
            self.job('one-error', background_sync=True, error={'code': 'timeout'})
        with patch.object(self.repo, 'list', side_effect=AssertionError('must not decode raw job list')):
            with patch('reception_service.sync_activity.json.loads', wraps=json.loads) as decode:
                jobs = visible_jobs(self.repo)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(jobs[1], business)
        self.assertEqual([job['job_id'] for job in jobs], ['sync-error:timeout', 'business'])
        self.assertEqual(len(self.repo.list('job')), 4002)

    def test_same_code_merges_across_customers_with_first_and_last_times_and_stable_id(self):
        self.job('latest-completion', background_sync=True, created_at=3000, completed_at=9000,
                 conversation_id='customer-b', error={'code': 'timeout', 'message': 'private B'})
        self.job('first-error', background_sync=True, created_at=1000, completed_at=1200,
                 conversation_id='customer-a', error={'code': 'timeout', 'message': 'private A'})
        self.job('unknown-result', background_sync=True, status='unknown', created_at=5000,
                 completed_at=8000, conversation_id='customer-c', error={'code': 'timeout'})
        row, = visible_jobs(self.repo)
        self.assertEqual(row['job_id'], 'sync-error:timeout')
        self.assertEqual(row['kind'], 'sync_messages')
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(row['conversation_id'])
        self.assertIsNone(row['case_id'])
        self.assertIsNone(row['started_at'])
        self.assertTrue(row['background_sync'])
        self.assertEqual(row['created_at'], 1000)
        self.assertEqual(row['completed_at'], 9000)
        self.assertEqual(row['diagnostics'], {'count': 3, 'last_job_id': 'latest-completion'})
        self.assertIn('累计 3 次', row['message'])
        self.assertIn('最后发生时间：', row['message'])
        self.assertNotIn('9000', row['message'])
        self.assertEqual(row['error'], {'code': 'timeout', 'message': '后台消息读取异常，请检查连接后重试'})
        self.assertEqual(visible_jobs(self.repo), [row])

        self.job('later-error', background_sync=True, created_at=10000, completed_at=11000,
                 error={'code': 'timeout'})
        self.job('later-success', background_sync=True, status='succeeded', created_at=12000)
        updated, = visible_jobs(self.repo)
        self.assertEqual(updated['job_id'], row['job_id'])
        self.assertEqual(updated['created_at'], 1000)
        self.assertEqual(updated['completed_at'], 11000)
        self.assertEqual(updated['diagnostics'], {'count': 4, 'last_job_id': 'later-error'})

    def test_different_trusted_codes_stay_separate_and_sort_by_first_created_time(self):
        self.job('timeout-old', background_sync=True, created_at=1000, completed_at=50000,
                 error={'code': 'timeout'})
        self.job('page-error', background_sync=True, created_at=3000, error={'code': 'invalid_page'})
        business = self.job('manual', kind='send_chat', created_at=2000)
        rows = visible_jobs(self.repo)
        self.assertEqual([row['job_id'] for row in rows],
                         ['sync-error:invalid_page', 'manual', 'sync-error:timeout'])
        self.assertEqual(rows[1], business)
        self.assertEqual([row['diagnostics']['count'] for row in (rows[0], rows[2])], [1, 1])

    def test_legacy_incomplete_failures_are_hidden_but_structured_failures_are_summarized(self):
        with self.repo.transaction():
            for index in range(100):
                error = ({}, {'error': None}, {'error': {}}, {'error': {'code': None}},
                         {'error': {'code': ''}})[index % 5]
                self.job(f'old-incomplete-{index}', sync_generation=0,
                         result={'changed_count': 0}, **error)
            self.job('old-null-count', sync_generation=None, result={'changed_count': None})
            self.job('old-real-error', sync_generation=1, error={'code': 'timeout'},
                     result={'changed_count': 0})
            self.job('new-real-error', background_sync=True, error={'code': 'timeout'})
        rows = visible_jobs(self.repo)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['job_id'], 'sync-error:timeout')
        self.assertEqual(rows[0]['diagnostics']['count'], 2)
        self.assertEqual(len(self.repo.list('job')), 103)

    def test_manual_reads_ai_sends_and_other_business_jobs_remain_exactly_intact(self):
        expected = []
        for index, status in enumerate(('queued', 'running', 'succeeded', 'deferred', 'failed', 'unknown')):
            expected.append(self.job(f'explicit-manual-{index}', background_sync=False, sync_generation=0,
                                     status=status, created_at=index,
                                     error={'code': 'timeout', 'message': '手动读取原记录'},
                                     result={'changed_count': 0}))
        expected.append(self.job('unmarked-manual', status='succeeded', created_at=20))
        expected.append(self.job('present-null-flag', background_sync=None, sync_generation=0, created_at=21))
        expected.append(self.job('non-boolean-flag', background_sync=1, sync_generation=0, created_at=22))
        for index, kind in enumerate(('generate_draft', 'check_ai', 'send_chat', 'automatic_send',
                                      'confirm_send', 'preview_send', 'reconcile_operation',
                                      'fill_history', 'check_connection', 'run_outbox')):
            expected.append(self.job(f'business-{index}', kind=kind, created_at=30 + index,
                                     automatic=True, background_sync=True,
                                     result={'operation_id': 'original-receipt', 'status': 'unknown'},
                                     diagnostics={'stage': 'original-stage'}))
        self.job('hidden-routine', background_sync=True, status='succeeded', created_at=100)
        self.assertEqual(visible_jobs(self.repo), sorted(expected, key=lambda job: job['created_at'], reverse=True))

    def test_missing_or_unsafe_codes_and_raw_exception_fields_never_leak_into_summary(self):
        secret = 'PRIVATE_TOKEN_客户原文_C:/secret.txt'
        unsafe_codes = (secret, 'timeout\n' + secret, 'private_token_abc', 'x' * 200,
                        '', None, 123, True, {'raw': secret}, [secret])
        for index, code in enumerate(unsafe_codes):
            self.job(f'unsafe-{index}', background_sync=True,
                     message=secret, stage=secret, error={'code': code, 'message': secret},
                     result={'message': secret}, diagnostics={'stdout': secret, 'code': secret})
        self.job('no-code', background_sync=True, error={'message': secret})
        self.job('unknown-no-code', background_sync=True, status='unknown', message=secret)
        self.job('legacy-real-no-code', sync_generation=0, message=secret)
        self.job('legacy-unknown', sync_generation=0, status='unknown', result={'changed_count': 0})
        self.job('new-failure-with-result', background_sync=True, result={'changed_count': 0})
        rows = visible_jobs(self.repo)
        by_code = {row['error']['code']: row for row in rows}
        self.assertEqual(set(by_code), {'sync_failed', 'sync_unknown'})
        self.assertEqual(by_code['sync_failed']['diagnostics']['count'], len(unsafe_codes) + 3)
        self.assertEqual(by_code['sync_unknown']['diagnostics']['count'], 2)
        text = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn(secret, text)
        self.assertNotIn('private_token_abc', text)
        self.assertNotIn('stdout', text)
        self.assertEqual(by_code['sync_failed']['error']['message'], by_code['sync_unknown']['error']['message'])

    def test_projection_is_read_only_preserving_rows_revisions_and_receipts(self):
        original = self.job('original-failure', background_sync=True,
                            error={'code': 'timeout', 'message': '原始失败正文'}, result={'receipt': '原始回执'})
        self.job('old-incomplete', sync_generation=1, result={'changed_count': 0})
        self.job('routine-success', background_sync=True, status='succeeded')
        self.repo.put('operation', 'send-receipt', {'status': 'unknown', 'message': '原始发送回执'}, history=True)
        self.repo.put('outbox', 'registration-receipt', {'status': 'failed', 'message': '原始登记回执'}, history=True)
        with self.repo.lock:
            entities = self.repo.db.execute('SELECT kind,id,data FROM entities ORDER BY kind,id').fetchall()
            history = self.repo.db.execute('SELECT * FROM revisions ORDER BY sequence').fetchall()
            changes = self.repo.db.total_changes
            self.repo.db.execute('PRAGMA query_only=ON')
        first = visible_jobs(self.repo)
        self.assertEqual(visible_jobs(self.repo), first)
        with self.repo.lock:
            self.assertEqual(self.repo.db.total_changes, changes)
            self.assertEqual(self.repo.db.execute('SELECT kind,id,data FROM entities ORDER BY kind,id').fetchall(), entities)
            self.assertEqual(self.repo.db.execute('SELECT * FROM revisions ORDER BY sequence').fetchall(), history)
            self.assertFalse(self.repo.db.in_transaction)
        self.assertEqual(self.repo.get('job', original['job_id']), original)
        self.assertEqual(self.repo.history('job', original['job_id']), [original])
        self.assertEqual(len(self.repo.list('job')), 3)

    def test_empty_store_and_unknown_jobs_without_completion_time(self):
        self.assertEqual(visible_jobs(self.repo), [])
        self.repo.put('not-a-job', 'ignore-me', {'kind': 'sync_messages', 'background_sync': True, 'status': 'failed'})
        self.assertEqual(visible_jobs(self.repo), [])
        self.job('unknown', background_sync=True, status='unknown', created_at=12345,
                 completed_at=None, error={'code': 'native_failed'})
        row, = visible_jobs(self.repo)
        self.assertEqual(row['job_id'], 'sync-error:native_failed')
        self.assertEqual(row['created_at'], 12345)
        self.assertEqual(row['completed_at'], 12345)
        self.assertEqual(row['diagnostics']['last_job_id'], 'unknown')


if __name__ == '__main__':
    unittest.main()
