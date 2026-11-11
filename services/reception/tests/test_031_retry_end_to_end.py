"""Offline service/SQLite/worker retry regressions with synthetic receipts."""
import copy
import time
import unittest

import test_continuity_end_to_end as fixtures
from reception_service.errors import ServiceError
from reception_service.repository import now_ms


class RetryEndToEnd(unittest.TestCase):
    wait = fixtures.ContinuityEndToEndTests.wait
    output = fixtures.ContinuityEndToEndTests.output
    install = fixtures.ContinuityEndToEndTests.install
    enable = fixtures.ContinuityEndToEndTests.enable
    accepted = fixtures.ContinuityEndToEndTests.accepted

    def setUp(self):
        from unittest.mock import patch
        self.clock_offset = 0
        def clock():
            return now_ms() + self.clock_offset
        for module in ('reception_service.service', 'reception_service.continuity'):
            mock = patch(module + '.now_ms', side_effect=clock)
            mock.start()
            self.addCleanup(mock.stop)
        fixtures.ContinuityEndToEndTests.setUp(self)

    def operations(self):
        return self.service.repo.list('operation', conversation_id=self.cid)

    def accelerate(self, op, delay):
        # Advance the clock while leaving the durable operation evidence intact.
        with self.service._lock:
            self.clock_offset += delay + 50

    def test_three_real_sends_and_stop(self):
        self.install()
        calls = []
        def send(intent, authorization):
            self.assertTrue(authorization())
            calls.append(copy.deepcopy(intent))
            return dict(status='rejected', submission_started=False, error_code='timeout', stage='cli')
        self.service.integrations.send = send
        self.enable()
        for n, delay in ((1, 5000), (2, 15000)):
            self.wait(lambda: len(self.operations()) == n and self.operations()[-1]['status'] == 'rejected')
            op = self.operations()[-1]
            self.wait(lambda: any(j.get('retry_attempt') == n and j['status'] == 'deferred' for j in self.service.list_jobs()))
            job = next(j for j in self.service.list_jobs() if j.get('retry_attempt') == n)
            self.assertEqual(job['retry_at'] - op['updated_at'], delay)
            self.assertIn('最多3次', job['message'])
            self.assertEqual(job['error']['code'], 'timeout')
            self.assertEqual(len(calls), n)
            self.accelerate(op, delay)
        self.wait(lambda: len(self.operations()) == 3 and self.operations()[-1]['status'] == 'rejected')
        self.wait(lambda: any(j.get('retry_status') == 'exhausted' and j['status'] == 'failed' for j in self.service.list_jobs()))
        job = next(j for j in self.service.list_jobs() if j.get('retry_status') == 'exhausted')
        self.assertIn('停止自动重试', job['message'])
        time.sleep(.35)
        self.assertEqual(len(calls), 3)
        self.assertFalse(any(j['kind'] == 'automatic_send' and j['status'] == 'deferred'
                             for j in self.service.list_jobs()))
        self.assertFalse(self.service.get_conversation(self.cid)['conversation']['human_owned'])

    def test_accepted_third_attempt_once(self):
        self.install()
        original = self.service.integrations.send
        calls = []
        def send(intent, authorization):
            calls.append(copy.deepcopy(intent))
            if len(calls) < 3:
                self.assertTrue(authorization())
                return dict(status='rejected', submission_started=False, error_code='rate_limit', stage='cli')
            return original(intent, authorization)
        self.service.integrations.send = send
        self.enable()
        for n, delay in ((1, 5000), (2, 15000)):
            self.wait(lambda: len(self.operations()) == n and self.operations()[-1]['status'] == 'rejected')
            self.wait(lambda: any(j.get('retry_attempt') == n and j['status'] == 'deferred' for j in self.service.list_jobs()))
            self.accelerate(self.operations()[-1], delay)
        self.wait(lambda: self.accepted())
        time.sleep(.25)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.accepted()), 1)

    def test_unclassified_and_permanent_stop_first(self):
        for code in ('adapter_error', 'exit', 'auth'):
            with self.subTest(code=code):
                if code != 'adapter_error':
                    self.service.inject_demo(self.cid, 'A new synthetic question ' + code)
                calls = []
                def send(intent, authorization):
                    self.assertTrue(authorization())
                    calls.append(intent)
                    return dict(status='rejected', submission_started=False, error_code=code, stage='cli')
                self.service.integrations.send = send
                self.install()
                if code == 'adapter_error':
                    self.enable()
                self.wait(lambda: calls)
                self.wait(lambda: any(j.get('retry_status') == 'blocked' and j['status'] == 'failed'
                                      and j.get('source_message_id') == calls[0]['source_message_id'] for j in self.service.list_jobs()))
                time.sleep(.15)
                self.assertEqual(len(calls), 1)
                job = next(j for j in self.service.list_jobs() if j.get('retry_status') == 'blocked'
                           and j.get('source_message_id') == calls[0]['source_message_id'])
                self.assertIn('停止本条自动重试', job['message'])

    def test_legacy_nine_and_new_draft_survive_restart(self):
        from reception_service.service import ReceptionService
        self.install()
        calls = []
        def reject(intent, authorization):
            calls.append(intent)
            return dict(status='rejected', submission_started=False, error_code='backend_timeout', stage='cli')
        self.service.integrations.send = reject
        self.enable()
        self.wait(lambda: len(self.operations()) == 1 and self.operations()[0]['status'] == 'rejected')
        self.wait(lambda: any(j.get('retry_attempt') == 1 and j['status'] == 'deferred' for j in self.service.list_jobs()))
        self.service.set_reception(False)
        with self.service._lock:
            op = self.operations()[0]
            job = self.service.repo.get('job', op['job_id'])
            for index in range(2, 10):
                synthetic_job = dict(job, job_id='legacy-job-%d' % index, status='deferred')
                synthetic_op = dict(op, operation_id='legacy-op-%d' % index, job_id=synthetic_job['job_id'])
                self.service.repo.put('job', synthetic_job['job_id'], synthetic_job)
                self.service.repo.put('operation', synthetic_op['operation_id'], synthetic_op)
            draft = self.service.repo.get('draft', op['draft_id'])
            draft.update(draft_id='regenerated-synthetic-draft', status='pending', retry_after=0)
            self.service.repo.put('draft', draft['draft_id'], draft)
        self.service.close()
        self.service = ReceptionService(self.root)
        self.addCleanup(self.service.close)
        self.service.settings.update(merge_seconds=0, poll_seconds=3600)
        self.service._next_poll = time.monotonic() + 3600
        self.service.integrations.send = reject
        self.install()
        self.enable()
        self.wait(lambda: self.service.repo.get('draft', 'regenerated-synthetic-draft')['status'] == 'exhausted')
        time.sleep(.25)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.operations()), 9)
        self.assertFalse(any(j['kind'] == 'automatic_send' and j['status'] == 'deferred'
                             for j in self.service.list_jobs()))
        self.service.inject_demo(self.cid, 'A genuinely new buyer question about delivery.')
        self.wait(lambda: len(calls) == 2)
        self.assertNotEqual(calls[0]['source_message_id'], calls[1]['source_message_id'])

    def test_presend_guard_exception_does_not_requeue(self):
        self.install()
        original = self.service._task_automatic_send
        calls = []
        def fail_guard(params, job_id, epoch, facade):
            calls.append(params)
            raise ServiceError('synthetic_guard', 'Synthetic pre-send guard', stage='send')
        self.service._task_automatic_send = fail_guard
        self.enable()
        self.wait(lambda: any(j.get('error', {}).get('code') == 'synthetic_guard' for j in self.service.list_jobs()))
        time.sleep(.35)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.operations(), [])
        draft = self.service.repo.get('draft', calls[0]['draft_id'])
        self.assertEqual(draft['status'], 'blocked')
        self.service._task_automatic_send = original

    def test_unknown_only_reconciles_and_other_customer_continues(self):
        self.install()
        original = self.service.integrations.send
        sends, reads = [], []
        def send(intent, authorization):
            sends.append(copy.deepcopy(intent))
            if intent['conversation_id'] == self.cid:
                self.assertTrue(authorization())
                return dict(status='unknown', submission_started=True)
            return original(intent, authorization)
        def reconcile(operation_id):
            reads.append(operation_id)
            return dict(status='unknown', submission_started=True)
        self.service.integrations.send = send
        self.service.integrations.reconcile_operation = reconcile
        self.service.set_scope('selected', [self.cid, 'demo-sofia'], 1)
        self.service.set_reception(True, auto_send=True)
        self.service._next_poll = time.monotonic() + 3600
        self.wait(lambda: any(o['status'] == 'unknown' for o in self.operations()))
        self.wait(lambda: reads)
        self.wait(lambda: any(o['status'] == 'accepted' for o in self.service.repo.list('operation', conversation_id='demo-sofia')))
        time.sleep(.25)
        self.assertEqual(len([i for i in sends if i['conversation_id'] == self.cid]), 1)
        self.assertEqual(set(reads), {self.operations()[0]['operation_id']})
        self.assertFalse(self.service.get_conversation(self.cid)['conversation']['human_owned'])

    def assert_retry_then_unknown(self, continuity=False):
        from reception_service.integrations.live import LiveIntegration
        def generate(context, attempt):
            if continuity and attempt == 1:
                raise ServiceError('timeout', 'Synthetic generation timeout', stage='analysis')
            return self.output(context)
        self.install(generate)
        original = self.service.integrations.send_chat
        calls, reads = [], []
        def send(intent, authorization):
            if continuity and intent.get('continuity_phase') != 'final':
                return original(intent, authorization)
            self.assertTrue(authorization())
            calls.append(copy.deepcopy(intent))
            if len(calls) == 1:
                return dict(status='rejected', submission_started=False, error_code='timeout', stage='cli')
            return LiveIntegration._send_result(dict(status='rejected', error_code='timeout', stage='cli'))
        def reconcile(operation_id):
            reads.append(operation_id)
            return dict(status='unknown', submission_started=True)
        self.service.integrations.reconcile_operation = reconcile
        if continuity:
            self.service.integrations.send_chat = send
        else:
            self.service.integrations.send = send
        def attempts():
            return [o for o in self.operations() if not continuity or
                    o.get('intent', {}).get('continuity_phase') == 'final']
        self.enable()
        self.wait(lambda: len(attempts()) == 1 and attempts()[0]['status'] == 'rejected')
        first = copy.deepcopy(attempts()[0])
        self.wait(lambda: self.service.repo.get('job', first['job_id'])['status'] == 'deferred')
        self.accelerate(first, 5000)
        self.wait(lambda: len(attempts()) == 2 and attempts()[-1]['status'] == 'unknown')
        self.wait(lambda: self.service.repo.get('job', first['job_id'])['status'] == 'cancelled')
        old_job = self.service.repo.get('job', first['job_id'])
        self.assertEqual(old_job['retry_status'], 'blocked')
        self.assertIsNone(old_job['retry_at'])
        self.assertIn('原发送仍需核查', old_job['message'])
        self.assertEqual(self.service.repo.get('operation', first['operation_id']), first)
        self.wait(lambda: reads)
        self.assertEqual(set(reads), {attempts()[-1]['operation_id']})
        time.sleep(.2)
        self.assertEqual(len(calls), 2)
        kind = 'continuity_send' if continuity else 'automatic_send'
        self.assertFalse(any(j['kind'] == kind and j['status'] == 'deferred' for j in self.service.list_jobs()))

    def test_retry_then_incomplete_receipt_clears_wait_and_only_reconciles(self):
        self.assert_retry_then_unknown()

    def test_continuity_retry_then_unknown_clears_wait_and_only_reconciles(self):
        self.assert_retry_then_unknown(continuity=True)

    def test_continuity_three_refusals_stop(self):
        def generate(context, attempt):
            if attempt == 1:
                raise ServiceError('timeout', 'Synthetic timeout', stage='analysis')
            return self.output(context)
        self.install(generate)
        original = self.service.integrations.send_chat
        calls = []
        def send(intent, authorization):
            if intent.get('continuity_phase') == 'final':
                self.assertTrue(authorization())
                calls.append(intent)
                return dict(status='rejected', submission_started=False, error_code='timeout', stage='cli')
            return original(intent, authorization)
        self.service.integrations.send_chat = send
        self.enable()
        def finals():
            return [o for o in self.operations() if o.get('intent', {}).get('continuity_phase') == 'final']
        for n, delay in ((1, 5000), (2, 15000)):
            self.wait(lambda: len(finals()) == n and finals()[-1]['status'] == 'rejected')
            self.wait(lambda: any(j.get('retry_attempt') == n and j['status'] == 'deferred' for j in self.service.list_jobs()))
            job = next(j for j in self.service.list_jobs() if j.get('retry_attempt') == n)
            self.assertEqual(job['retry_at'] - finals()[-1]['updated_at'], delay)
            self.accelerate(finals()[-1], delay)
        self.wait(lambda: len(finals()) == 3 and finals()[-1]['status'] == 'rejected')
        self.wait(lambda: any(j.get('retry_status') == 'exhausted' and j['status'] == 'failed' for j in self.service.list_jobs()))
        time.sleep(.25)
        self.assertEqual(len(calls), 3)
        self.assertFalse(any(j['kind'] == 'continuity_send' and j['status'] == 'deferred'
                             for j in self.service.list_jobs()))


if __name__ == '__main__':
    unittest.main()
