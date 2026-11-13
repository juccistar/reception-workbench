"""Ordinary handwritten chat: synthetic identities, temporary DBs and mocks only."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_core import MockFacade
from reception_service.errors import ServiceError
from reception_service.repository import encoded, new_id, now_ms
from reception_service.service import ReceptionService


class ChatFacade(MockFacade):
    def __init__(self):
        super().__init__()
        self.seller = '10001'
        self.chat_intents = []
        self.chat_started = threading.Event()
        self.chat_release = threading.Event()
        self.block_chat = False
        self.chat_result = None
        self.authorization = None

    def send_chat(self, intent, *, authorization):
        self.chat_intents.append(copy.deepcopy(intent))
        self.authorization = authorization
        if self.block_chat:
            self.chat_started.set()
            if not self.chat_release.wait(5):
                raise RuntimeError('synthetic chat wait timed out')
        self.observed_authorization = authorization()
        if not self.observed_authorization:
            return {'status': 'rejected', 'submission_started': False}
        if isinstance(self.chat_result, Exception):
            raise self.chat_result
        if self.chat_result is not None:
            return copy.deepcopy(self.chat_result)
        return {'status': 'accepted', 'message_id': new_id('platform'), 'submission_started': True,
                'timestamp_ms': 1_900_000_000_000}


class ManualChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.facade = ChatFacade()
        self.factory = patch('reception_service.service.build_integrations', return_value=self.facade)
        self.factory.start()
        self.schedule = ReceptionService._schedule
        self.scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        self.scheduler.start()
        self.service = ReceptionService(self.root, 'live', seller_id=self.facade.seller)
        self.assertEqual(self.wait_job(self.service.call('check_connection'))['status'], 'succeeded')
        self.cid = 'synthetic-chat-customer'
        self.ingest()
        self.case_id = self.service.get_conversation(self.cid)['case']['case_id']

    def tearDown(self):
        self.facade.release.set()
        self.facade.chat_release.set()
        self.service.close()
        self.scheduler.stop()
        self.factory.stop()
        self.temp.cleanup()

    def wait_job(self, ack):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.repo.get('job', ack['job_id'])
            if job and job['status'] not in ('queued', 'running'):
                return job
            threading.Event().wait(0.005)
        self.fail('synthetic job did not complete')

    def ingest(self, cid=None, role='buyer', text='Hello', mid=None, stamp=None, buyer='10002'):
        cid = cid or self.cid
        info = {'conversation_id': cid, 'buyer_id': buyer, 'customer_name': 'Synthetic customer'}
        self.service._ingest(info, [{'message_id': mid or new_id('source'), 'seller_id': self.facade.seller,
            'buyer_id': buyer, 'conversation_id': cid, 'role': role, 'text': text,
            'timestamp_ms': now_ms() if stamp is None else stamp, 'attachments': []}], has_more=False, full=True)
        return self.service.repo.get('conversation', cid)

    def save(self, body='A handwritten follow-up with no procurement evidence.', cid=None, mutation=None):
        cid = cid or self.cid
        previous = self.service.repo.get('chat_draft', cid)
        return self.service.call('save_chat_draft', {'conversation_id': cid, 'body': body,
            'expected_revision': previous['revision'] if previous else None,
            'client_mutation_id': mutation or new_id('mutation')})

    def params(self, draft, request=None):
        return {'conversation_id': draft['conversation_id'], 'draft_id': draft['draft_id'],
                'expected_revision': draft['revision'], 'client_request_id': request or new_id('request')}

    def send(self, draft, request=None):
        return self.wait_job(self.service.call('send_chat', self.params(draft, request)))

    def start_auto(self):
        for name in ('messages', 'ai', 'send_preview'):
            self.service.checks[name]['status'] = 'passed'
        self.service.set_scope('selected', [self.cid, 'synthetic-other'] if self.service.repo.get('conversation', 'synthetic-other')
                               else [self.cid], 10)
        self.service.set_reception(True, True)
        self.service._next_poll = time.monotonic() + 100
        self.service.settings['merge_seconds'] = 0

    def switch_case(self):
        conversation = self.service.repo.get('conversation', self.cid)
        case = self.service._new_case(conversation)
        conversation['case_id'] = case['case_id']
        self.service.repo.put('conversation', self.cid, conversation)
        return case

    def test_no_case_is_not_ownership_error_and_manual_chat_sends(self):
        conversation = self.service.repo.get('conversation', self.cid)
        conversation['case_id'] = None
        self.service.repo.put('conversation', self.cid, conversation)
        detail = self.service.get_conversation(self.cid)
        self.assertIsNone(detail['case'])
        self.assertNotIn('case', [b['code'] for b in detail['conversation']['reception_status']['blockers']])
        draft = self.save('Manual message without a procurement case.')
        self.assertIsNone(draft['case_id'])
        self.assertEqual(self.send(draft)['status'], 'succeeded')
        self.assertEqual(self.facade.chat_intents[-1]['reply'], draft['body'])

    def test_foreign_case_still_reports_ownership_error(self):
        conversation = self.service.repo.get('conversation', self.cid)
        case = self.service.repo.get('case', self.case_id)
        case['buyer_id'] = 'foreign-buyer'
        self.service.repo.put('case', self.case_id, case)
        from reception_service.reception_state import ReceptionProjection
        status = ReceptionProjection(self.service).status(conversation)
        self.assertIn('case', [block['code'] for block in status['blockers']])
        with self.assertRaises(ServiceError):
            self.service.get_conversation(self.cid)

    def test_sync_yields_to_queued_human_before_reading_next_customer(self):
        self.ingest(cid='synthetic-other', buyer='10003')
        draft = self.save()
        started, release = threading.Event(), threading.Event()
        order = []
        infos = [self.service.repo.get('conversation', cid) for cid in (self.cid, 'synthetic-other')]
        self.facade.read_recent_conversations = lambda *args, **kwargs: {'items': infos, 'has_more': False, 'next_cursor': None}
        def read(seller, buyer, cid, **kwargs):
            order.append(cid)
            if cid == self.cid:
                started.set()
                if not release.wait(5):
                    raise AssertionError('synthetic read release missing')
            else:
                self.assertEqual(len(self.facade.chat_intents), 1, 'human request waited behind the other customer')
            return {'messages': self.service._messages(cid), 'has_more': False, 'next_cursor': None}
        self.facade.read_message_page = read
        synced = self.service.call('sync_messages')
        self.assertTrue(started.wait(3))
        sent = self.service.call('send_chat', self.params(draft))
        release.set()
        self.assertEqual(self.wait_job(sent)['status'], 'succeeded')
        self.assertEqual(self.wait_job(synced)['status'], 'succeeded')
        self.assertEqual(order, [self.cid, 'synthetic-other'])
        self.assertEqual(self.service._queue.unfinished_tasks, 0)

    def test_clean_close_marks_never_started_chat_unsubmitted_and_keeps_body(self):
        import json
        import sqlite3
        from contextlib import closing
        draft = self.save('Retain this unsent human message.')
        self.facade.block_read = True
        self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(3))
        ack = self.service.call('send_chat', self.params(draft))
        database = self.service.repo.path
        closer = threading.Thread(target=self.service.close)
        closer.start()
        self.assertTrue(self.service._stop.wait(3))
        self.facade.release.set()
        closer.join(5)
        self.assertFalse(closer.is_alive())
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            job = json.loads(db.execute("SELECT data FROM entities WHERE kind='job' AND id=?", (ack['job_id'],)).fetchone()[0])
            operation = json.loads(db.execute("SELECT data FROM entities WHERE kind='operation'").fetchone()[0])
            saved = json.loads(db.execute("SELECT data FROM entities WHERE kind='chat_draft' AND id=?", (self.cid,)).fetchone()[0])
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(operation['status'], 'rejected')
        self.assertIs(operation['submission_started'], False)
        self.assertEqual(saved['body'], draft['body'])
        self.assertEqual(saved['status'], 'rejected')
        self.assertEqual(self.facade.chat_intents, [])
        # The prior version left this exact never-started/cancelled pair prepared.
        operation['status'] = 'prepared'
        saved['status'] = 'sending'
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE entities SET data=? WHERE kind='operation' AND id=?", (json.dumps(operation), operation['operation_id']))
            db.execute("UPDATE entities SET data=? WHERE kind='chat_draft' AND id=?", (json.dumps(saved), self.cid))
            db.commit()
        self.service = ReceptionService(self.root, 'live', seller_id=self.facade.seller)
        self.service._activate_seller(self.facade.seller)
        recovered = self.service.repo.list('operation')[0]
        self.assertEqual(recovered['status'], 'rejected')
        self.assertIs(recovered['submission_started'], False)
        self.assertEqual(self.service.get_conversation(self.cid)['chat_draft']['body'], draft['body'])
        self.assertEqual(self.save('Explicit new human edit after definite cancellation')['status'], 'pending')

    def test_newly_received_automatic_source_does_not_enqueue_full_body_poll(self):
        self.start_auto()
        self.ingest(stamp=self.service._started_at_ms + 1)
        self.service._next_poll = 0
        captured = []
        with patch.object(self.service, '_enqueue', side_effect=lambda kind, params, **kwargs: captured.append(kind)):
            self.schedule(self.service)
        self.assertEqual(captured, ['generate_draft'])

    def test_paused_live_worker_leaves_receiving_to_summary_monitor(self):
        self.assertFalse(self.service.get_snapshot()['reception']['running'])
        self.assertEqual(self.service.checks['messages']['status'], 'passed')
        self.service._next_poll = 0
        captured = []
        with patch.object(self.service, '_enqueue', side_effect=lambda kind, params, **kwargs: captured.append((kind, params, kwargs))):
            self.schedule(self.service)
        self.assertEqual(captured, [])
        self.assertEqual(self.facade.generate_calls, 0)

    def test_begin_is_fast_idempotent_customer_local_and_keeps_waiting_quote(self):
        self.ingest(cid='synthetic-other', buyer='10003')
        self.start_auto()
        before_epoch, revoked = self.service._epoch, self.facade.revoked
        case = self.service.repo.get('case', self.case_id)
        case.update(state='waiting_quote', missing=[])
        self.service.repo.put('case', self.case_id, case)
        legacy = self.service.save_draft(self.case_id, 'Thank you.', None, 'synthetic-ai-draft')
        other_case = self.service.get_conversation('synthetic-other')['case']
        other_draft = self.service.save_draft(other_case['case_id'], 'Thank you.', None, 'other-legacy')
        confirmation = {'confirmation_id': 'synthetic-confirm', 'conversation_id': self.cid, 'invalidated': False}
        self.service.repo.put('confirmation', confirmation['confirmation_id'], confirmation)
        conversation = self.service.repo.get('conversation', self.cid)
        self.facade.block_read = True
        read = self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(1))
        started = time.monotonic()
        first = self.service.call('begin_manual_edit', {'conversation_id': self.cid})
        second = self.service.begin_manual_edit(self.cid)
        self.assertLess(time.monotonic() - started, 0.5)
        current = self.service.repo.get('conversation', self.cid)
        self.assertEqual(current['revision'], conversation['revision'] + 1)
        # Projection capture time is observational; repeating takeover must leave
        # every business field and the projected state unchanged.
        for snapshot in (first, second):
            for item in snapshot['conversations']:
                item['reception_status'].pop('observed_at')
        self.assertEqual(first, second)
        self.assertTrue(current['human_owned'])
        self.assertTrue(next(c for c in first['conversations'] if c['conversation_id'] == self.cid)['human_owned'])
        self.assertFalse(self.service.repo.get('conversation', 'synthetic-other')['human_owned'])
        self.assertTrue(first['reception']['running'])
        self.assertTrue(first['reception']['auto_send'])
        self.assertEqual(self.service._epoch, before_epoch)
        self.assertEqual(self.facade.revoked, revoked)
        self.assertEqual(self.service.repo.get('case', self.case_id), case)
        self.assertIn(case, first['handoff_cases'])
        self.assertEqual(self.service.repo.get('draft', legacy['draft_id'])['status'], 'stale')
        self.assertEqual(self.service.repo.get('draft', other_draft['draft_id']), other_draft)
        self.assertTrue(self.service.repo.get('confirmation', confirmation['confirmation_id'])['invalidated'])
        self.facade.release.set()
        self.wait_job(read)

    def test_paused_failed_ai_closed_and_latest_seller_do_not_gate_chat(self):
        self.service.checks['ai']['status'] = 'failed'
        self.service.set_reception(False)
        self.ingest(role='seller', text='Previous seller reply', stamp=now_ms() + 1)
        case = self.service.repo.get('case', self.case_id)
        case.update(state='closed', facts={}, missing=['product'])
        self.service.repo.put('case', self.case_id, case)
        draft = self.save('Price is $42; I will follow up tomorrow.')
        result = self.send(draft)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['result']['status'], 'accepted')
        self.assertEqual(self.service.repo.get('case', self.case_id), case)
        self.assertEqual(self.service.repo.list('outbox'), [])
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.send_calls, 0)
        intent = self.facade.chat_intents[0]
        self.assertEqual(intent['purpose'], 'chat')
        self.assertEqual(intent['send_mode'], 'manual')
        self.assertEqual(intent['reply'], draft['body'])
        self.assertNotIn('epoch', intent)
        self.assertNotIn('budget_reserved', intent)
        message = next(m for m in self.service.get_messages(self.cid)['items'] if m['text'] == draft['body'])
        self.assertEqual(message['timestamp_ms'], 1_900_000_000_000)
        self.assertEqual(self.service._budget()['used'], 0)

    def test_chat_and_reconcile_work_with_no_case_or_source(self):
        self.service.repo.db.execute("DELETE FROM entities WHERE kind IN ('case','case_origin','message','policy')")
        self.assertIsNone(self.service.get_conversation(self.cid)['case'])
        draft = self.save()
        self.assertIsNone(draft['case_id'])
        self.assertIsNone(draft['source_message_id'])
        self.facade.chat_result = {'status': 'unknown', 'submission_started': True}
        job = self.send(draft)
        op = job['result']['operation_id']
        self.assertEqual(job['status'], 'unknown')
        with patch.object(self.facade, 'reconcile_operation', return_value={
                'status': 'accepted', 'message_id': 'synthetic-reconciled', 'submission_started': True}, create=True):
            checked = self.wait_job(self.service.call('reconcile_operation', {'operation_id': op}))
        self.assertEqual(checked['status'], 'succeeded')
        self.assertEqual(checked['result']['status'], 'accepted')
        self.assertEqual(self.service.get_conversation(self.cid)['chat_draft']['status'], 'accepted')
        self.assertEqual(self.service.get_messages(self.cid)['items'], [])
        self.assertEqual(self.service.repo.list('case'), [])
        self.assertEqual(self.service.repo.list('outbox'), [])

    def test_save_mutation_and_edit_revision_are_independent_of_context(self):
        draft = self.save('', mutation='synthetic-first-mutation')
        self.assertEqual(draft['purpose'], 'chat')
        self.assertEqual(self.service.save_chat_draft(self.cid, '', None, 'synthetic-first-mutation'), draft)
        with self.assertRaises(ServiceError) as conflict:
            self.service.save_chat_draft(self.cid, 'Changed', None, 'synthetic-first-mutation')
        self.assertEqual(conflict.exception.code, 'mutation_conflict')
        self.ingest(text='Unrelated new buyer message', stamp=now_ms() + 10)
        self.service.save_policy('policy', 'Synthetic', 'New unrelated policy', [], True, True)
        self.switch_case()
        self.assertEqual(self.service.get_conversation(self.cid)['chat_draft'], draft)
        updated = self.save('New body')
        self.assertEqual(updated['revision'], draft['revision'] + 1)
        with self.assertRaises(ServiceError) as conflict:
            self.service.save_chat_draft(self.cid, 'Lost edit', draft['revision'], 'synthetic-stale-edit')
        self.assertEqual(conflict.exception.code, 'revision_conflict')
        self.assertEqual(self.send(updated)['status'], 'succeeded')

    def test_body_format_and_only_nonempty_body_can_send(self):
        for text in ('\x00', 'Hello\x01world', '\x7f', '\x85', '\ud800', 'x' * 50_001):
            with self.subTest(text=repr(text[:10])), self.assertRaises(ServiceError):
                self.save(text)
        draft = self.save(' \n\t')
        with self.assertRaises(ServiceError):
            self.service.call('send_chat', self.params(draft))
        draft = self.save('x' * 50_000 + '')
        self.assertEqual(len(draft['body']), 50_000)
        self.save('Hello\nWorld\tThanks\r\n你好')
        self.assertEqual(self.facade.chat_intents, [])

    def test_real_concurrent_request_retries_claim_exactly_one_job_and_operation(self):
        draft = self.save()
        params = self.params(draft, 'synthetic-stable-request')
        self.facade.block_chat = True
        barrier = threading.Barrier(13)
        def retry():
            barrier.wait(3)
            return self.service.call('send_chat', params)
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(retry) for _ in range(12)]
            barrier.wait(3)
            acks = [future.result(3) for future in futures]
        self.assertTrue(self.facade.chat_started.wait(1))
        self.assertTrue(all(ack == acks[0] for ack in acks))
        self.assertEqual(len(self.service.repo.list('operation')), 1)
        operation = self.service.repo.list('operation')[0]
        self.assertEqual(operation['intent']['reply'], draft['body'])
        self.assertEqual(operation['draft_revision'], draft['revision'])
        self.assertEqual(operation['buyer_id'], '10002')
        self.assertEqual(operation['seller_id'], '10001')
        changed = {**params, 'expected_revision': draft['revision'] + 1}
        with self.assertRaises(ServiceError) as conflict:
            self.service.call('send_chat', changed)
        self.assertEqual(conflict.exception.code, 'request_conflict')
        with self.assertRaises(ServiceError):
            self.service.call('send_chat', self.params(draft))
        self.facade.chat_release.set()
        self.assertEqual(self.wait_job(acks[0])['status'], 'succeeded')
        self.assertEqual(self.service.call('send_chat', params), acks[0])
        self.assertEqual(len(self.facade.chat_intents), 1)
        self.assertEqual(len([j for j in self.service.list_jobs() if j['kind'] == 'send_chat']), 1)

    def test_concurrent_different_requests_cannot_both_claim_same_conversation(self):
        draft = self.save()
        self.facade.block_chat = True
        barrier = threading.Barrier(3)
        def attempt():
            barrier.wait(3)
            try:
                return self.service.call('send_chat', self.params(draft))
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            barrier.wait(3)
            results = [future.result(3) for future in futures]
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn('send_unresolved', results)
        self.facade.chat_release.set()
        self.assertEqual(self.wait_job(next(r for r in results if isinstance(r, dict)))['status'], 'succeeded')
        self.assertEqual(len(self.facade.chat_intents), 1)

    def test_followups_including_explicit_same_body_are_allowed_after_resave(self):
        for text in ('Synthetic hello', 'Synthetic hello', 'One more detail', 'Proactive follow-up'):
            draft = self.save(text)
            params = self.params(draft)
            ack = self.service.call('send_chat', params)
            self.assertEqual(self.wait_job(ack)['status'], 'succeeded')
            self.assertEqual(self.service.call('send_chat', params), ack)
            with self.assertRaises(ServiceError) as caught:
                self.service.call('send_chat', self.params(draft))
            self.assertEqual(caught.exception.code, 'draft_not_sendable')
        self.assertEqual(len(self.facade.chat_intents), 4)
        self.assertEqual(len({i['operation_id'] for i in self.facade.chat_intents}), 4)
        self.assertEqual(self.service.repo.list('outbox'), [])

    def test_queued_chat_ignores_global_pause_and_uses_bound_text(self):
        self.start_auto()
        self.facade.block_read = True
        read = self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(1))
        draft = self.save('Synthetic queued exact body')
        ack = self.service.call('send_chat', self.params(draft))
        self.assertEqual(self.service.repo.list('operation')[0]['status'], 'prepared')
        self.service.set_reception(False)
        self.ingest(text='New buyer input after send claim', stamp=now_ms() + 20)
        self.switch_case()
        self.service.save_policy('policy', 'Synthetic', 'New policy', [], True, True)
        self.facade.release.set()
        self.wait_job(read)
        self.assertEqual(self.wait_job(ack)['status'], 'succeeded')
        self.assertEqual(self.facade.chat_intents[0]['reply'], draft['body'])
        self.assertTrue(self.facade.observed_authorization)

    def test_final_authorization_survives_pause_new_source_closed_case_and_resume(self):
        draft = self.save()
        self.facade.block_chat = True
        ack = self.service.call('send_chat', self.params(draft))
        self.assertTrue(self.facade.chat_started.wait(1))
        self.service.set_reception(False)
        self.ingest(text='Do not contact me.', stamp=now_ms() + 20)
        case = self.service.repo.get('case', self.case_id)
        self.assertEqual(case['state'], 'closed')
        self.service.takeover(self.cid, False)
        self.facade.chat_release.set()
        self.assertEqual(self.wait_job(ack)['status'], 'succeeded')
        self.assertEqual(self.service.repo.get('case', self.case_id), case)

    def test_facade_replacement_revokes_queued_intent(self):
        self.facade.block_read = True
        read = self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(1))
        ack = self.service.call('send_chat', self.params(self.save()))
        replacement = ChatFacade()
        with patch('reception_service.service.build_integrations', return_value=replacement):
            self.service.save_settings(poll_seconds=40)
        self.facade.release.set()
        self.wait_job(read)
        self.assertEqual(self.wait_job(ack)['result']['status'], 'rejected')
        self.assertEqual(self.facade.chat_intents, [])
        self.assertEqual(replacement.chat_intents, [])

    def test_final_authorization_rejects_changed_identity_owner_text_version_and_shutdown(self):
        changes = ('verified', 'seller', 'buyer', 'facade', 'body', 'revision', 'operation', 'shutdown')
        for change in changes:
            with self.subTest(change=change):
                cid = 'synthetic-fence-' + change
                self.ingest(cid=cid)
                draft = self.save(cid=cid)
                self.facade.chat_started.clear()
                self.facade.chat_release.clear()
                self.facade.block_chat = True
                ack = self.service.call('send_chat', self.params(draft))
                self.assertTrue(self.facade.chat_started.wait(1))
                operation = self.service.repo.list('operation', conversation_id=cid)[0]
                if change == 'verified':
                    self.service.account_verified = False
                elif change == 'seller':
                    self.service.seller_id = 'synthetic-foreign-seller'
                elif change == 'buyer':
                    c = self.service.repo.get('conversation', cid)
                    c['buyer_id'] = 'synthetic-foreign-buyer'
                    self.service.repo.put('conversation', cid, c)
                elif change == 'facade':
                    self.service.integrations = ChatFacade()
                elif change in ('body', 'revision'):
                    d = self.service.repo.get('chat_draft', cid)
                    d[change] = 'Changed text' if change == 'body' else d['revision'] + 1
                    self.service.repo.put('chat_draft', cid, d)
                elif change == 'operation':
                    operation['intent']['reply'] = 'Synthetic changed operation'
                    self.service.repo.put('operation', operation['operation_id'], operation)
                else:
                    self.service.call('shutdown')
                self.facade.chat_release.set()
                result = self.wait_job(ack)
                self.assertEqual(result['result']['status'], 'rejected', result)
                self.assertFalse(self.facade.observed_authorization)
                self.service.account_verified = True
                self.service.seller_id = self.facade.seller
                self.service.integrations = self.facade

    def test_missing_receipt_evidence_stays_unknown_and_exception_never_retries(self):
        invalid = ({'status': 'accepted', 'message_id': 'synthetic-id', 'submission_started': 1},
                   {'status': 'accepted', 'message_id': ' ', 'submission_started': True},
                   {'status': 'accepted', 'message_id': 42, 'submission_started': True},
                   {'status': 'accepted', 'submission_started': True},
                   {'status': 'rejected'}, {'status': 'rejected', 'submission_started': True},
                   {'status': 'accepted', 'message_id': 'synthetic-id', 'submission_started': False},
                   {'status': 'unknown'}, [], RuntimeError('synthetic transport failure'))
        for index, receipt in enumerate(invalid):
            with self.subTest(receipt=receipt):
                cid = f'synthetic-unknown-{index}'
                self.ingest(cid=cid)
                draft = self.save(cid=cid)
                params = self.params(draft)
                self.facade.chat_result = receipt
                ack = self.service.call('send_chat', params)
                job = self.wait_job(ack)
                self.assertEqual(job['status'], 'unknown', job)
                self.assertEqual(job['result']['status'], 'unknown')
                self.assertEqual(self.service.call('send_chat', params), ack)
                self.assertEqual(self.service.get_conversation(cid)['chat_draft']['status'], 'unknown')
                next_draft = self.save('New attempt', cid=cid)
                with self.assertRaises(ServiceError) as caught:
                    self.service.call('send_chat', self.params(next_draft))
                self.assertEqual(caught.exception.code, 'send_unresolved')
                self.assertEqual(self.service.repo.get('operation', job['result']['operation_id'])['intent']['reply'], draft['body'])
        self.assertEqual(len(self.facade.chat_intents), len(invalid))

    def test_rejected_allows_explicit_new_draft(self):
        self.facade.chat_result = {'status': 'rejected', 'submission_started': False}
        draft = self.save()
        self.assertEqual(self.send(draft)['result']['status'], 'rejected')
        rebound = self.save(draft['body'])
        self.assertNotEqual(rebound['draft_id'], draft['draft_id'])
        self.facade.chat_result = None
        self.assertEqual(self.send(rebound)['status'], 'succeeded')

    def test_unknown_fences_chat_and_automatic_generation_across_cases(self):
        self.facade.chat_result = {'status': 'unknown', 'submission_started': True}
        draft = self.save()
        job = self.send(draft)
        operation_id = job['result']['operation_id']
        self.service.takeover(self.cid, False)
        case = self.switch_case()
        self.start_auto()
        self.ingest(text='A new procurement request', stamp=self.service._started_at_ms + 10)
        case = self.service.repo.get('case', case['case_id'])
        self.assertIsNone(self.service._automatic_source(self.cid))
        with self.assertRaises(ServiceError) as caught:
            self.service.call('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']})
        self.assertEqual(caught.exception.code, 'send_unresolved')
        automatic = self.service.save_draft(case['case_id'], 'Thank you.', None, new_id('mutation'))
        with self.assertRaises(ServiceError):
            self.service._validate_send(automatic['draft_id'], automatic['revision'])
        self.schedule(self.service)
        self.assertEqual(self.facade.generate_calls, 0)
        self.assertEqual(self.facade.send_calls, 0)
        outbox = next(o for o in self.service.list_outbox() if o['operation_id'] == operation_id)
        self.assertEqual(outbox['purpose'], 'chat')
        self.assertEqual(outbox['status'], 'unknown')
        self.assertNotIn('intent', outbox)
        self.assertNotIn(draft['body'], str(outbox))

    def test_legacy_unknown_send_also_fences_new_chat(self):
        operation = {'operation_id': 'synthetic-legacy-operation', 'case_id': self.case_id,
                     'conversation_id': self.cid, 'status': 'unknown'}
        self.service.repo.put('operation', operation['operation_id'], operation)
        draft = self.save()
        with self.assertRaises(ServiceError) as caught:
            self.service.call('send_chat', self.params(draft))
        self.assertEqual(caught.exception.code, 'send_unresolved')
        self.assertEqual(self.service.repo.get('operation', operation['operation_id']), operation)

    def test_restart_prepared_and_sending_are_unknown_and_same_request_keeps_ack(self):
        draft = self.save()
        params = self.params(draft)
        self.facade.block_read = True
        read = self.service.call('sync_messages')
        self.assertTrue(self.facade.started.wait(1))
        ack = self.service.call('send_chat', params)
        # Capture a true abrupt boundary before performing orderly test cleanup.
        crashed_operation = self.service.repo.list('operation')[0]
        crashed_job = self.service.repo.get('job', ack['job_id'])
        crashed_draft = self.service.repo.get('chat_draft', self.cid)
        database = self.service.repo.path
        self.service.call('shutdown')
        self.facade.release.set()
        self.wait_job(read)
        self.service.close()
        import json
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(database)) as db:
            for kind, key, value in (('operation', crashed_operation['operation_id'], crashed_operation),
                                     ('job', ack['job_id'], crashed_job), ('chat_draft', self.cid, crashed_draft)):
                db.execute('UPDATE entities SET data=? WHERE kind=? AND id=?', (json.dumps(value), kind, key))
            db.commit()
        # Run both persisted crash boundaries. Neither may become retryable.
        for status in ('prepared', 'sending'):
            self.service = ReceptionService(self.root, 'live', seller_id=self.facade.seller)
            self.assertEqual(self.wait_job(self.service.call('check_connection'))['status'], 'succeeded')
            operation = self.service.repo.list('operation')[0]
            if status == 'sending':
                operation['status'] = 'sending'
                self.service.repo.put('operation', operation['operation_id'], operation)
                self.service._recover()
            self.assertEqual(self.service.repo.list('operation')[0]['status'], 'unknown')
            self.assertEqual(self.service.get_conversation(self.cid)['chat_draft']['status'], 'unknown')
            self.assertEqual(self.service.call('send_chat', params), ack)
            self.assertEqual(self.service.repo.get('job', ack['job_id'])['status'], 'unknown')
            next_draft = self.save('Next text')
            with self.assertRaises(ServiceError) as caught:
                self.service.call('send_chat', self.params(next_draft))
            self.assertEqual(caught.exception.code, 'send_unresolved')
            self.assertEqual(self.service.repo.list('operation')[0]['intent']['reply'], draft['body'])
            self.assertEqual(self.facade.chat_intents, [])
            if status == 'prepared':
                self.service.repo.put('chat_draft', self.cid, crashed_draft)
                self.service.close()

    def test_reconciliation_ignores_case_source_and_does_not_overwrite_newer_draft(self):
        self.facade.chat_result = {'status': 'unknown', 'submission_started': True}
        draft = self.save()
        op_id = self.send(draft)['result']['operation_id']
        newer = self.service.get_conversation(self.cid)['chat_draft']
        newer.update(body='Synthetic newer edit', revision=draft['revision'] + 1, status='pending')
        self.service.repo.put('chat_draft', self.cid, newer)
        self.service.repo.db.execute("DELETE FROM entities WHERE kind IN ('case','case_origin','message')")
        before = self.service.get_messages(self.cid)
        with patch.object(self.facade, 'reconcile_operation', return_value={
                'status': 'accepted', 'message_id': 'synthetic-final', 'submission_started': True}, create=True):
            receipt = self.service.reconcile_operation(op_id)
        self.assertEqual(receipt['status'], 'accepted')
        self.assertEqual(self.service.get_conversation(self.cid)['chat_draft'], newer)
        self.assertEqual(self.service.get_messages(self.cid), before)
        self.assertEqual(self.service.repo.list('outbox'), [])

    def test_reconcile_validates_conversation_owner_before_external_lookup(self):
        self.facade.chat_result = {'status': 'unknown', 'submission_started': True}
        op_id = self.send(self.save())['result']['operation_id']
        operation = self.service.repo.get('operation', op_id)
        for field in ('seller_id', 'buyer_id', 'conversation_id'):
            with self.subTest(field=field):
                self.service.repo.put('operation', op_id, {**operation, field: 'synthetic-foreign'})
                with patch.object(self.facade, 'reconcile_operation', create=True) as lookup:
                    with self.assertRaises(ServiceError):
                        self.service.reconcile_operation(op_id)
                    lookup.assert_not_called()
        self.service.repo.put('operation', op_id, operation)

    def test_accepted_without_platform_time_waits_for_real_sync(self):
        self.facade.chat_result = {'status': 'accepted', 'message_id': 'synthetic-time-missing', 'submission_started': True}
        before = self.service.get_messages(self.cid)
        draft = self.save()
        self.assertEqual(self.send(draft)['status'], 'succeeded')
        self.assertEqual(self.service.get_messages(self.cid), before)
        self.assertEqual(self.service.repo.list('outbox'), [])
        self.ingest(role='seller', text=draft['body'], mid='synthetic-time-missing', stamp=1_900_000_000_010)
        self.assertEqual(self.service.get_conversation(self.cid)['chat_draft']['status'], 'accepted')
        self.assertEqual(self.service.get_messages(self.cid)['items'][-1]['timestamp_ms'], 1_900_000_000_010)

    def test_demo_can_simulate_history_timestamp(self):
        self.service.close()
        self.facade.seller = 'demo-seller'
        self.service = ReceptionService(self.root, 'demo')
        self.cid = 'demo-emma'
        self.facade.chat_result = {'status': 'accepted', 'message_id': 'synthetic-demo-send', 'submission_started': True}
        draft = self.save()
        self.assertEqual(self.send(draft)['status'], 'succeeded')
        message = next(m for m in self.service.get_messages(self.cid)['items'] if m['message_id'] == 'synthetic-demo-send')
        self.assertIs(type(message['timestamp_ms']), int)

    def test_blocked_ai_return_never_overwrites_manual_text_case_or_outbox(self):
        case = self.service.repo.get('case', self.case_id)
        self.facade.block_generate = True
        ack = self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']})
        self.assertTrue(self.facade.started.wait(1))
        draft = self.save('My exact handwritten body')
        self.facade.release.set()
        self.assertEqual(self.wait_job(ack)['status'], 'stale')
        self.assertEqual(self.service.get_conversation(self.cid)['chat_draft'], draft)
        self.assertEqual(self.service.get_conversation(self.cid)['case'], case)
        self.assertIsNone(self.service.get_conversation(self.cid)['draft'])
        self.assertEqual(self.service.repo.list('outbox'), [])
        self.assertEqual(self.service.repo.get('ai_result', ack['job_id'])['status'], 'stale')

    def test_failed_blocked_ai_cannot_reset_human_owned_case_to_needs_human(self):
        entered, release = threading.Event(), threading.Event()
        def fail(context, request_id):
            entered.set()
            release.wait(5)
            raise ServiceError('synthetic_ai_failure', 'Synthetic AI failure')
        case = self.service.repo.get('case', self.case_id)
        with patch.object(self.facade, 'generate', side_effect=fail):
            ack = self.service.call('generate_draft', {'case_id': self.case_id, 'expected_case_revision': case['revision']})
            self.assertTrue(entered.wait(1))
            self.service.begin_manual_edit(self.cid)
            release.set()
            self.assertEqual(self.wait_job(ack)['status'], 'failed')
        self.assertEqual(self.service.repo.get('case', self.case_id), case)

    def test_other_customer_continues_and_explicit_resume_does_not_replay_old_intent(self):
        self.ingest(cid='synthetic-other', buyer='10003')
        self.start_auto()
        self.save()
        other_case = self.service.get_conversation('synthetic-other')['case']
        job = self.wait_job(self.service.call('generate_draft', {
            'case_id': other_case['case_id'], 'expected_case_revision': other_case['revision']}))
        self.assertEqual(job['status'], 'succeeded', job)
        legacy = self.service.save_draft(self.case_id, 'Thank you.', None, 'synthetic-old-intent')
        self.service.begin_manual_edit(self.cid)
        # An idempotent begin must not create a new revision; explicitly resuming
        # must invalidate every old AI/manual procurement confirmation instead.
        before = self.service.repo.get('case', self.case_id)
        snapshot = self.service.takeover(self.cid, False)
        self.assertFalse(next(c for c in snapshot['conversations'] if c['conversation_id'] == self.cid)['human_owned'])
        self.assertEqual(self.service.repo.get('case', self.case_id), before)
        self.assertEqual(self.service.repo.get('draft', legacy['draft_id'])['status'], 'stale')
        revision = self.service.repo.get('conversation', self.cid)['revision']
        self.service.takeover(self.cid, False)
        self.assertEqual(self.service.repo.get('conversation', self.cid)['revision'], revision)
        self.assertEqual(self.facade.chat_intents, [])
        self.assertEqual(self.facade.send_calls, 0)

    def test_unverified_identity_and_foreign_conversation_cannot_claim_chat(self):
        self.service.account_verified = False
        for operation in (lambda: self.service.begin_manual_edit(self.cid), lambda: self.save()):
            with self.assertRaises(ServiceError):
                operation()
        self.service.account_verified = True
        draft = self.save()
        conversation = self.service.repo.get('conversation', self.cid)
        conversation['seller_id'] = 'synthetic-foreign-seller'
        self.service.repo.put('conversation', self.cid, conversation)
        with self.assertRaises(ServiceError):
            self.service.call('send_chat', self.params(draft))
        self.assertEqual(self.facade.chat_intents, [])
        self.assertEqual(self.service.repo.list('operation'), [])

    def test_reconciliation_without_evidence_remains_unknown_then_explicit_rejection_allows_new_save(self):
        self.facade.chat_result = {'status': 'unknown', 'submission_started': True}
        operation_id = self.send(self.save())['result']['operation_id']
        for result in ({'status': 'accepted', 'message_id': ' ', 'submission_started': True},
                       {'status': 'accepted', 'message_id': 'synthetic-mid', 'submission_started': 1},
                       {'status': 'rejected', 'submission_started': True}, {'status': 'rejected'}, None):
            with self.subTest(result=result), patch.object(self.facade, 'reconcile_operation', return_value=result, create=True):
                self.assertEqual(self.service.reconcile_operation(operation_id)['status'], 'unknown')
                self.assertEqual(self.service.get_conversation(self.cid)['chat_draft']['status'], 'unknown')
        with patch.object(self.facade, 'reconcile_operation', return_value={
                'status': 'rejected', 'submission_started': False}, create=True):
            self.assertEqual(self.service.reconcile_operation(operation_id)['status'], 'rejected')
        self.assertEqual(self.service.get_conversation(self.cid)['chat_draft']['status'], 'rejected')
        self.facade.chat_result = None
        self.assertEqual(self.send(self.save('Explicit new follow-up'))['status'], 'succeeded')

    def test_concurrent_same_save_mutation_advances_edit_version_only_once(self):
        barrier = threading.Barrier(7)
        def save():
            barrier.wait(3)
            return self.service.save_chat_draft(self.cid, 'Synthetic exact edit', None, 'synthetic-shared-edit')
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(save) for _ in range(6)]
            barrier.wait(3)
            drafts = [future.result(3) for future in futures]
        self.assertTrue(all(d == drafts[0] for d in drafts))
        self.assertEqual(drafts[0]['revision'], 1)
        self.assertEqual(len(self.service.repo.history('chat_draft', self.cid)), 1)

    def test_ai_quality_warning_keeps_safe_recovery_diagnostics(self):
        def generate(context, request_id):
            self.facade.last_ai_diagnostics = {'result_source': 'completed_record', 'elapsed_ms': 5000,
                'recovery_enabled': True, 'recovery_status': 'completed', 'raw_reply': 'PRIVATE SENTINEL'}
            return {'action': 'human', 'reply': 'Our price is $25.', 'facts': {}, 'question_keys': [],
                    'conflicts': [], 'reply_sources': ['synthetic-ai-check-message']}
        with patch.object(self.facade, 'generate', side_effect=generate):
            checked = self.wait_job(self.service.call('check_ai'))
        self.assertEqual(checked['status'], 'succeeded', checked)
        self.assertEqual(checked['diagnostics']['result_source'], 'completed_record')
        self.assertEqual(checked['diagnostics']['elapsed_ms'], 5000)
        self.assertNotIn('PRIVATE', str(checked))

    def test_diagnostic_updates_preserve_prior_process_facts_without_raw_data(self):
        from reception_service.errors import safe_diagnostics
        checked = self.wait_job(self.service.call('check_ai'))
        self.service._job_update(checked['job_id'], diagnostics={'result_source': 'completed_record', 'raw': 'PRIVATE SENTINEL'})
        self.service._job_update(checked['job_id'], diagnostics={})
        result = self.service.repo.get('job', checked['job_id'])
        self.assertEqual(result['diagnostics']['result_source'], 'completed_record')
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(safe_diagnostics({'validation_reason': 'PRIVATE SENTINEL'}), {})

    def test_ai_check_and_generation_keep_safe_completed_record_diagnostics(self):
        self.facade.last_ai_diagnostics = {'result_source': 'completed_record', 'cli_timed_out': True, 'recovery_status': 'completed'}
        checked = self.wait_job(self.service.call('check_ai'))
        self.assertEqual(checked['status'], 'succeeded', checked)
        for key, value in self.facade.last_ai_diagnostics.items():
            self.assertEqual(checked['diagnostics'][key], value)
        case = self.service.repo.get('case', self.case_id)
        generated = self.wait_job(self.service.call('generate_draft', {
            'case_id': self.case_id, 'expected_case_revision': case['revision']}))
        self.assertEqual(generated['status'], 'succeeded', generated)
        for key, value in self.facade.last_ai_diagnostics.items():
            self.assertEqual(generated['diagnostics'][key], value)


if __name__ == '__main__':
    unittest.main()
