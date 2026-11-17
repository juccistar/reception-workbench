import copy
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from reception_service import reception_context, whatsapp, whatsapp_account_generation
from reception_service.conversation_assistance import ConversationAssistanceMixin
from reception_service.whatsapp_account_generation import AccountGenerationMixin
from reception_service.whatsapp_account_service import WhatsAppAccountService, _key
from reception_service.errors import ServiceError


class ManualFixture(ConversationAssistanceMixin):
    repo = object()
    settings = {}
    seller_id = 'seller'

    def _chat_owner(self, cid):
        return {'seller_id': 'seller', 'buyer_id': 'buyer', 'conversation_id': cid}

    def _with_case_catalog_context(self, context):
        pass


class Repo:
    def __init__(self):
        self.rows = {}

    def get(self, kind, key, default=None):
        return self.rows.get((kind, key), default)

    def list_owned(self, *args):
        return [{'id': 'm1', 'timestamp_ms': 1, 'from_me': False}]

    def list(self, kind):
        return [value for (row_kind, _), value in self.rows.items() if row_kind == kind]

    def put(self, kind, key, value):
        self.rows[kind, key] = value


class AccountFixture(AccountGenerationMixin):
    def __init__(self):
        self.repo = Repo()
        self._generation_closed = False
        self.enabled = self.connected = True
        self.account, self.epoch = 'account', 'epoch'
        self.allowed = {'merchant'}
        self.repo.rows[('binding', self._account_key('account', 'chat'))] = {
            'merchant_id': 'merchant', 'revision': 1, 'segment_id': 'segment'}
        self.repo.rows[('conversation', self._account_key('account', 'chat'))] = {
            'pending': True, 'pending_message_id': 'm1'}

    def _account_key(self, *parts):
        return repr(parts)


class PromptRevisionTests(unittest.TestCase):
    def test_automatic_fingerprint_covers_revision_and_missing_old_field(self):
        context = {'task_mode': 'automatic', 'prompt_version': 'revision-a', 'recent_messages': []}
        before = reception_context.semantic_fingerprint(context)
        self.assertEqual(before, reception_context.semantic_fingerprint(copy.deepcopy(context)))
        self.assertNotEqual(before, reception_context.semantic_fingerprint({**context, 'prompt_version': 'revision-b'}))
        del context['prompt_version']
        self.assertNotEqual(before, reception_context.semantic_fingerprint(context))

    def test_manual_and_polish_fingerprints_cover_revision(self):
        fixture = ManualFixture()
        def context(*args, **kwargs):
            return {'prompt_version': reception_context.PROMPT_VERSION, 'knowledge': [],
                    'task_mode': kwargs['task_mode'], 'recent_messages': []}
        with patch('reception_service.conversation_assistance.shared_context', side_effect=context):
            for body in (None, 'Please check'):
                with self.subTest(body=body):
                    args = ('conversation', None, body, 1 if body else None)
                    _, before = fixture._manual_context(*args)
                    self.assertEqual(before, fixture._manual_context(*args)[1])
                    with patch.object(reception_context, 'PROMPT_VERSION', 'another-revision'):
                        self.assertNotEqual(before, fixture._manual_context(*args)[1])

    def test_legacy_whatsapp_fingerprint_covers_revision(self):
        class Fixture(whatsapp.WhatsAppMixin):
            def get_whatsapp_context(self, chat_id, *, exclude_message_ids=()):
                return {'messages': ['hello'], 'excluded': list(exclude_message_ids)}
        fixture = Fixture()
        before = fixture._wa_fingerprint('chat')
        self.assertEqual(before, fixture._wa_fingerprint('chat'))
        with patch.object(whatsapp, 'PROMPT_VERSION', 'another-revision'):
            self.assertNotEqual(before, fixture._wa_fingerprint('chat'))

    def test_account_generation_freezes_version_without_changing_source_budget(self):
        fixture = AccountFixture()
        source = fixture._account_source_fingerprint('account', 'chat', 'segment')
        job = {'cancel': threading.Event(), 'account_id': 'account', 'connection_epoch': 'epoch',
               'merchant_id': 'merchant', 'chat_id': 'chat', 'segment_id': 'segment',
               'binding_revision': 1, 'pending_message_id': 'm1', 'source_fingerprint': source,
               'prompt_version': whatsapp_account_generation.PROMPT_VERSION}
        self.assertTrue(fixture._generation_current(job))
        self.assertTrue(fixture._generation_current(job))
        with patch.object(whatsapp_account_generation, 'PROMPT_VERSION', 'another-revision'):
            self.assertFalse(fixture._generation_current(job))
            self.assertEqual(source, fixture._account_source_fingerprint('account', 'chat', 'segment'))
        del job['prompt_version']
        self.assertFalse(fixture._generation_current(job))

    def test_account_send_rejects_old_and_missing_revision(self):
        class Fixture(AccountGenerationMixin):
            _wa_prepare_send = WhatsAppAccountService._wa_prepare_send
            def __init__(self, version):
                self.repo = Repo()
                self.enabled, self.allowed = True, {'merchant'}
                self.context_leases = {'op': {'revision': 1, 'expires_at_ms': 9999999999999}}
                self.cancelled = False
                self.repo.put('binding', _key('account', 'chat'), {'merchant_id': 'merchant', 'revision': 1, 'segment_id': 'segment'})
                self.repo.put('conversation', _key('account', 'chat'), {'pending': True, 'pending_message_id': 'm1'})
                self.repo.put('operation', 'op', {'operation_id': 'op', 'status': 'prepared', 'submission_started': False,
                    'account_id': 'account', 'connection_epoch': 'epoch', 'chat_id': 'chat', 'merchant_id': 'merchant',
                    'segment_id': 'segment', 'binding_revision': 1, 'pending_message_id': 'm1',
                    'context_revision': 1, 'batch_id': 'batch', 'prompt_version': version})
            def _identity(self, *args):
                pass
            def _cancel_account_prepared(self, op):
                self.cancelled = True
                op['status'] = 'cancelled'
        for version in (None, 'old-revision'):
            fixture = Fixture(version)
            with self.assertRaises(ServiceError) as error:
                fixture._wa_prepare_send('op')
            self.assertEqual(error.exception.code, 'source_changed')
            self.assertTrue(fixture.cancelled)
        fixture = Fixture(None)
        fixture.repo.get('operation', 'op')['status'] = 'unknown'
        with self.assertRaises(ServiceError):
            fixture._wa_prepare_send('op')
        self.assertEqual(fixture.repo.get('operation', 'op')['status'], 'unknown')
        self.assertFalse(fixture.cancelled)


class PromptUpgradeRecoveryTests(unittest.TestCase):
    @contextmanager
    def upgraded(self, *, outcome=None, count=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'account'
            workspace = str(uuid.uuid4())
            events = []
            service = WhatsAppAccountService(root, workspace, events.append)
            account, chat, segment = 'account', 'chat', 'segment'
            source_key = _key(account, chat, 'buyer')
            service.repo.put('message', 'buyer', {'account_id': account, 'chat_id': chat,
                'segment_id': segment, 'id': 'buyer', 'timestamp_ms': 1, 'from_me': False})
            fingerprint = service._account_source_fingerprint(account, chat, segment)
            basis = {'account_id': account, 'connection_epoch': 'epoch', 'chat_id': chat,
                     'merchant_id': 'merchant', 'pending_message_id': 'buyer', 'segment_id': segment,
                     'binding_revision': 1, 'source_fingerprint': fingerprint, 'batch_id': 'batch'}
            parts = [{'body': 'Old reply', 'status': 'prepared', 'part_index': 0,
                      'operation_id': 'head', 'message_id': None}]
            head = {**basis, 'operation_id': 'head', 'part_index': 0, 'body': 'Old reply',
                    'status': 'prepared', 'submission_started': False, 'message_id': None,
                    'ack': None, 'owner_run_id': 'previous-process'}
            if outcome in ('unknown', 'accepted'):
                head.update(status=outcome, submission_started=True)
                parts[0]['status'] = outcome
            if outcome == 'accepted':
                head.update(message_id='platform-id', ack=1)
                parts[0]['message_id'] = 'platform-id'
                parts.append({'body': 'Old tail', 'status': 'prepared', 'part_index': 1,
                              'operation_id': 'tail', 'message_id': None})
                service.repo.put('operation', 'tail', {**basis, 'operation_id': 'tail', 'part_index': 1,
                    'body': 'Old tail', 'status': 'prepared', 'submission_started': False,
                    'message_id': None, 'ack': None, 'owner_run_id': 'previous-process'})
            if outcome in ('ambiguous_accepted_batch', 'ambiguous_unknown_part'):
                parts[0]['status'] = 'accepted' if outcome == 'ambiguous_accepted_batch' else 'unknown'
            if outcome == 'ambiguous_part_ack':
                parts[0]['ack'] = 1
            if outcome in ('other_accepted_batch', 'other_unknown_part', 'other_part_ack'):
                other_part = {'body': 'Other reply', 'status': 'planned', 'part_index': 0,
                              'operation_id': 'missing-operation', 'message_id': None}
                if outcome == 'other_unknown_part':
                    other_part['status'] = 'unknown'
                if outcome == 'other_part_ack':
                    other_part['ack'] = 1
                service.repo.put('reply_batch', 'other-batch', {**basis, 'batch_id': 'other-batch',
                    'parts': [other_part], 'next_index': 0,
                    'status': 'accepted' if outcome == 'other_accepted_batch' else 'prepared'})
            service.repo.put('operation', 'head', head)
            service.repo.put('reply_batch', 'batch', {**basis, 'parts': parts,
                'next_index': 1 if outcome == 'accepted' else 0,
                'status': 'accepted' if outcome == 'ambiguous_accepted_batch' else 'prepared'})
            attempt = {'status': 'completed', 'owner_run_id': 'previous-process',
                       'attempt_id': 'original-attempt', 'source_fingerprint': fingerprint}
            if count is not None:
                attempt['count'] = count
            service.repo.put('generation_attempt', source_key, attempt)
            service.repo.put('binding', _key(account, chat), {'merchant_id': 'merchant',
                'revision': 1, 'segment_id': segment})
            service.repo.put('conversation', _key(account, chat), {'account_id': account,
                'chat_id': chat, 'pending': True, 'pending_message_id': 'buyer'})
            # An old owner survives ordinary cancellation, as after an abrupt
            # previous-process exit. The old batch deliberately has no revision.
            service.close()
            database = root / 'whatsapp-accounts' / workspace / 'account.sqlite3'
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as stored:
                stored.execute('PRAGMA query_only=ON')
                row = stored.execute("SELECT data FROM records WHERE kind='reply_batch' AND id='other-batch'").fetchone()
                self._other_batch_before_recovery = json.loads(row[0]) if row else None
            events.clear()
            service = WhatsAppAccountService(root, workspace, events.append)
            service.account, service.epoch = account, 'epoch'
            service.enabled = service.connected = True
            service.allowed = {'merchant'}
            service._arm_account_schedule = lambda *args: None
            try:
                yield service, events, source_key, fingerprint, head
            finally:
                service.close()

    def test_upgrade_retires_old_owner_unsent_batch_without_resetting_attempts(self):
        with self.upgraded() as (service, events, source_key, fingerprint, head):
            operation = service.repo.get('operation', 'head')
            self.assertEqual(operation['status'], 'cancelled')
            self.assertIs(operation['submission_started'], False)
            self.assertEqual(operation['body'], head['body'])
            self.assertEqual(service.repo.get('reply_batch', 'batch')['status'], 'stopped')
            attempt = service.repo.get('generation_attempt', source_key)
            self.assertEqual((attempt['status'], attempt['count']), ('failed', 1))
            self.assertEqual(attempt['attempt_id'], 'original-attempt')
            self.assertEqual(service._account_source_fingerprint('account', 'chat', 'segment'), fingerprint)
            service._schedule_account_replies()
            self.assertEqual(len(service._generation_requests), 1)
            self.assertEqual([event['event'] for event in events], ['whatsapp.context.requested'])
            self.assertEqual(service.repo.get('generation_attempt', source_key)['count'], 1)

    def test_upgrade_does_not_restore_exhausted_or_missing_attempt_budget(self):
        for count in (3, 0, None):
            with self.subTest(count=count), self.upgraded(count=count) as (service, events, source_key, _, _):
                self.assertEqual(service.repo.get('operation', 'head')['status'], 'cancelled')
                attempt = service.repo.get('generation_attempt', source_key)
                self.assertEqual(attempt.get('count'), count)
                self.assertEqual(attempt['status'], 'completed')
                service._schedule_account_replies()
                self.assertEqual(service._generation_requests, {})
                self.assertEqual(events, [])

    def test_upgrade_preserves_original_unknown_submission(self):
        with self.upgraded(outcome='unknown') as (service, events, source_key, _, head):
            self.assertEqual(service.repo.get('operation', 'head'), head)
            self.assertEqual(service.repo.get('generation_attempt', source_key)['status'], 'completed')
            service._schedule_account_replies()
            self.assertEqual(service._generation_requests, {})
            self.assertEqual(events, [])

    def test_upgrade_keeps_conflicting_accepted_or_unknown_batch_evidence(self):
        for outcome in ('ambiguous_accepted_batch', 'ambiguous_unknown_part', 'ambiguous_part_ack'):
            with self.subTest(outcome=outcome), self.upgraded(outcome=outcome) as (service, events, source_key, _, head):
                self.assertEqual(service.repo.get('operation', 'head'), head)
                self.assertEqual(service.repo.get('generation_attempt', source_key)['status'], 'completed')
                service._schedule_account_replies()
                self.assertEqual(service._generation_requests, {})
                self.assertEqual(events, [])

    def test_upgrade_checks_receipts_in_every_batch_for_the_same_source(self):
        for outcome in ('other_accepted_batch', 'other_unknown_part', 'other_part_ack'):
            with self.subTest(outcome=outcome), self.upgraded(outcome=outcome) as (service, events, source_key, _, _):
                self.assertEqual(service.repo.get('operation', 'head')['status'], 'cancelled')
                other = self._other_batch_before_recovery
                self.assertIsNotNone(other)
                self.assertEqual(service.repo.get('reply_batch', 'other-batch'), other)
                self.assertEqual(service.repo.get('generation_attempt', source_key)['status'], 'completed')
                service._schedule_account_replies()
                self.assertEqual(service._generation_requests, {})
                self.assertEqual(service.repo.get('reply_batch', 'other-batch'), other)
                self.assertEqual(events, [])

    def test_upgrade_stops_unsent_tail_but_keeps_accepted_head_without_regeneration(self):
        with self.upgraded(outcome='accepted') as (service, events, source_key, _, head):
            self.assertEqual(service.repo.get('operation', 'head'), head)
            self.assertEqual(service.repo.get('operation', 'tail')['status'], 'cancelled')
            attempt = service.repo.get('generation_attempt', source_key)
            self.assertEqual((attempt['status'], attempt['count']), ('completed', 1))
            service._schedule_account_replies()
            self.assertEqual(service._generation_requests, {})
            self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()
