import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from reception_service.errors import ServiceError
from reception_service.whatsapp_account_service import WhatsAppAccountService, _key


class AccountCoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = str(uuid.uuid4())
        self.events = []
        self.service = WhatsAppAccountService(Path(self.temp.name), self.workspace, self.events.append)
        self.service._wa_set_account('account', 'epoch', 'connected')

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def ingest(self, chat='chat', mid='message', history=False, outgoing=False, body='hello'):
        return self.service._wa_ingest('account', 'epoch', [{'id': mid, 'chat_id': chat, 'body': body,
            'timestamp_ms': 1, 'is_history': history, 'is_group': False, 'from_me': outgoing}])

    def test_disconnected_projection_keeps_owner_unknown(self):
        self.ingest()
        self.service.repo.put('operation', 'op', {'operation_id': 'op', 'account_id': 'account',
            'connection_epoch': 'epoch', 'chat_id': 'chat', 'status': 'unknown'})
        state = self.service._wa_set_account(None, None, 'disconnected')
        self.assertFalse(state['connected'])
        self.assertEqual(state['account_id'], 'account')
        self.assertEqual(state['pending_operations'][0]['operation_id'], 'op')
        self.assertEqual(state['legacy']['pending_count'], 1)
        self.assertEqual(state['conversations'][0]['chat_id'], 'chat')

    def test_default_is_only_for_new_chats_and_unbind_stays_unbound(self):
        s = self.service
        s.save_whatsapp_default_merchant('merchant-a', 0, 'account', 'epoch')
        self.ingest()
        s.save_whatsapp_binding('chat', None, None, 0, 'account', 'epoch')
        s.save_whatsapp_default_merchant('merchant-b', 1, 'account', 'epoch')
        self.ingest(mid='second')
        self.ingest(chat='new', mid='third')
        bindings = {b['chat_id']: b for b in s.get_whatsapp_state()['bindings']}
        self.assertIsNone(bindings['chat']['merchant_id'])
        self.assertEqual(bindings['new']['merchant_id'], 'merchant-b')

    def test_history_does_not_freeze_default_before_first_live_buyer_message(self):
        s = self.service
        s.save_whatsapp_default_merchant('old', 0, 'account', 'epoch')
        self.ingest(history=True)
        self.assertIsNone(s.get_whatsapp_state()['bindings'][0]['merchant_id'])
        s.save_whatsapp_default_merchant('new', 1, 'account', 'epoch')
        self.ingest(mid='live')
        self.assertEqual(s.get_whatsapp_state()['bindings'][0]['merchant_id'], 'new')
        s.save_whatsapp_default_merchant('later', 2, 'account', 'epoch')
        self.ingest(mid='another-live')
        self.assertEqual(s.get_whatsapp_state()['bindings'][0]['merchant_id'], 'new')

    def test_metadata_discovery_and_manual_outgoing_do_not_assign_default(self):
        s = self.service
        s.save_whatsapp_default_merchant('old', 0, 'account', 'epoch')
        s._wa_ingest('account', 'epoch', [{'id': 'metadata:chat', 'chat_id': 'chat', 'body': '', 'timestamp_ms': 0,
            'is_history': True, 'is_group': False, 'from_me': False, 'metadata_only': True}])
        self.ingest(mid='manual', outgoing=True)
        s.save_whatsapp_default_merchant('new', 1, 'account', 'epoch')
        self.assertIsNone(s.get_whatsapp_state()['bindings'][0]['merchant_id'])
        self.ingest(mid='live')
        self.assertEqual(s.get_whatsapp_state()['bindings'][0]['merchant_id'], 'new')
        self.assertTrue(s.get_whatsapp_state()['conversations'][0]['human_owned'])

    def test_rebinding_pauses_contact_until_explicit_resume(self):
        s = self.service
        self.ingest()
        s.save_whatsapp_binding('chat', 'new', None, 0, 'account', 'epoch')
        row = s.get_whatsapp_state()['conversations'][0]
        self.assertFalse(row['pending'])
        self.assertTrue(row['reception_paused'])
        self.assertFalse(row['human_owned'])
        self.ingest(mid='after-binding')
        self.assertTrue(s.get_whatsapp_state()['conversations'][0]['reception_paused'])
        s.takeover_whatsapp('chat', False, 'account', 'epoch')
        self.assertFalse(s.get_whatsapp_state()['conversations'][0]['reception_paused'])

    def test_segment_does_not_include_previous_messages(self):
        self.ingest()
        self.service.save_whatsapp_binding('chat', 'new-merchant', None, 0, 'account', 'epoch')
        self.ingest(mid='new-message', body='new content')
        result = self.service.get_whatsapp_context('chat')
        self.assertEqual([m['text'] for m in result['whatsapp_messages']], ['new content'])

    def test_supplement_is_revision_checked_and_isolated_by_contact_and_segment(self):
        s = self.service
        self.ingest()
        s.save_whatsapp_binding('chat', 'merchant-a', None, 0, 'account', 'epoch')
        note = s._wa_save_supplement('chat', 'merchant-a', 1, 0, 'private note', 'account', 'epoch')
        self.assertEqual(note, {'body': 'private note', 'revision': 1})
        self.assertEqual(s.get_whatsapp_context('chat')['wa_supplement']['body'], 'private note')
        with self.assertRaises(ServiceError):
            s._wa_save_supplement('chat', 'merchant-a', 1, 0, 'stale', 'account', 'epoch')
        self.ingest(chat='other', mid='other-message')
        self.assertEqual(s.get_whatsapp_context('other')['wa_supplement']['body'], '')
        s.save_whatsapp_binding('chat', 'merchant-b', None, 1, 'account', 'epoch')
        self.assertEqual(s.get_whatsapp_context('chat')['wa_supplement']['body'], '')
        with self.assertRaises(ServiceError):
            s._wa_save_supplement('chat', 'merchant-a', 1, 1, 'wrong merchant', 'account', 'epoch')

    def test_unknown_fence_queues_binding_until_exact_receipt(self):
        self.ingest()
        s = self.service
        operation = {'operation_id': 'op', 'account_id': 'account', 'connection_epoch': 'epoch',
                     'chat_id': 'chat', 'merchant_id': None, 'status': 'unknown', 'submission_started': True,
                     'message_id': None, 'ack': None}
        s.repo.put('operation', 'op', operation)
        binding = s.save_whatsapp_binding('chat', 'merchant', None, 0, 'account', 'epoch')
        self.assertIsNone(binding['merchant_id'])
        self.assertEqual(binding['requested_binding']['merchant_id'], 'merchant')
        s._wa_transport_result('op', 'account', 'epoch', 'submitted', 'mid', 0)
        self.assertIsNone(s.get_whatsapp_state()['bindings'][0]['merchant_id'])
        s._wa_ack('account', 'epoch', 'mid', 1)
        self.assertEqual(s.get_whatsapp_state()['bindings'][0]['merchant_id'], 'merchant')
        self.assertEqual(s.get_whatsapp_state()['pending_operations'], [])

    def test_context_reply_must_match_original_request_and_binding(self):
        self.ingest()
        s = self.service
        binding = s.save_whatsapp_binding('chat', 'merchant', None, 0, 'account', 'epoch')
        s.repo.put('operation', 'op', {'operation_id': 'op', 'account_id': 'account', 'connection_epoch': 'epoch',
            'chat_id': 'chat', 'merchant_id': 'merchant', 'status': 'prepared', 'segment_id': binding['segment_id'],
            'binding_revision': binding['revision']})
        request = s._wa_get_context_request('op')
        with self.assertRaises(ServiceError):
            s._wa_set_merchant_context(dict(request, merchant_id='other'), None)
        request = s._wa_get_context_request('op')
        s.save_whatsapp_binding('chat', None, None, binding['revision'], 'account', 'epoch')
        import time
        now = time.time_ns() // 1_000_000
        lease = {'format': 'wa-merchant-context-v1', 'merchant_id': 'merchant', 'conversation_id': None,
                 'seller_id': 'seller', 'data_root': 'root', 'instance_id': 'instance', 'revision': 'r',
                 'observed_at_ms': now, 'expires_at_ms': now + 10000, 'context': {}}
        with self.assertRaises(ServiceError):
            s._wa_set_merchant_context(request, lease)

    def test_prepared_intent_is_unknown_before_body_release(self):
        import time
        self.ingest()
        s = self.service
        binding = s.save_whatsapp_binding('chat', 'merchant', None, 0, 'account', 'epoch')
        s.takeover_whatsapp('chat', False, 'account', 'epoch')
        self.ingest(mid='new-segment-message')
        s.enabled, s.allowed = True, ['merchant']
        class Router:
            def assert_current(self, snapshot): pass
            def close(self): pass
        s.router = Router()
        snapshot = {'purpose': 'reception', 'profile_id': 'p', 'route_revision': 1, 'config_revision': 1,
                    'credential_revision': 1, 'capability_revision': 1, 'required_capabilities': ['text']}
        op = {'operation_id': 'op', 'account_id': 'account', 'connection_epoch': 'epoch', 'chat_id': 'chat',
              'merchant_id': 'merchant', 'status': 'prepared', 'segment_id': binding['segment_id'],
              'binding_revision': binding['revision'], 'pending_message_id': 'new-segment-message', 'context_revision': 'r',
              'model_snapshot': snapshot, 'body': 'frozen'}
        s.repo.put('operation', 'op', op)
        now = time.time_ns() // 1_000_000
        s.context_leases['op'] = {'revision': 'r', 'expires_at_ms': now + 10000}
        self.assertEqual(s._wa_prepare_send('op')['body'], 'frozen')
        self.assertEqual(s.repo.get('operation', 'op')['status'], 'unknown')
        self.assertTrue(s.repo.get('operation', 'op')['submission_started'])
        with self.assertRaises(ServiceError):
            s._wa_prepare_send('op')

    def test_legacy_manifest_retains_unresolved_operations_on_empty_refresh(self):
        import time
        s = self.service
        legacy_op = {'operation_id': 'old', 'account_id': 'account', 'connection_epoch': 'previous',
                     'chat_id': 'chat', 'status': 'unknown', 'submission_started': True, 'body': 'frozen'}
        packet = {'format': 'wa-legacy-verification-v1', 'account_id': 'account', 'manifest_hash': 'a' * 64,
                  'verified_at_ms': time.time_ns() // 1_000_000,
                  'sources': [{'source_id': 'old-source', 'status': 'verified', 'pending_operations': [legacy_op]}]}
        self.assertEqual(s._wa_set_legacy_status(**packet)['pending_count'], 1)
        packet['sources'] = []
        with self.assertRaises(ServiceError):
            s._wa_set_legacy_status(**packet)
        self.assertEqual(s.get_whatsapp_state()['legacy']['pending_count'], 1)
        self.assertEqual(s.repo.get('operation', 'old')['body'], 'frozen')
        with self.assertRaises(ServiceError):
            s._wa_set_legacy_status(format='wa-legacy-verification-v1', account_id='other', manifest_hash='a' * 64,
                                    verified_at_ms=packet['verified_at_ms'], sources=[])

    def test_root_lock_excludes_second_account_service_and_marks_clean_on_close(self):
        from reception_service.workspace_lock import WorkspaceLock, MARKER
        with self.assertRaises(OSError):
            WorkspaceLock(Path(self.temp.name)).acquire()
        self.assertFalse((Path(self.temp.name) / MARKER).exists())
        self.service.close()
        self.assertTrue((Path(self.temp.name) / MARKER).exists())
        self.service = WhatsAppAccountService(Path(self.temp.name), self.workspace, self.events.append)
        self.assertFalse((Path(self.temp.name) / MARKER).exists())

    def test_model_sync_is_idempotent_and_invalid_sync_revokes(self):
        class Router:
            def configure(self, packet):
                if packet.get('bad'):
                    raise ValueError('invalid')
            def close(self): pass
        self.service.router = Router()
        self.service._configure_models(format='synthetic')
        self.service.enabled = True
        self.service.allowed = ['merchant']
        self.service._configure_models(format='synthetic')
        self.assertTrue(self.service.enabled)
        with self.assertRaises(ValueError):
            self.service._configure_models(format='synthetic', bad=True)
        self.assertFalse(self.service.enabled)
        self.assertEqual(self.service.allowed, [])

    def test_writeback_contract_cannot_be_enabled_or_create_records(self):
        changes = self.service.repo.db.total_changes
        capabilities = self.service.get_whatsapp_writeback_capabilities()
        self.assertFalse(capabilities['available'])
        self.assertFalse(capabilities['enabled'])
        for method in ('_wa_writeback_preview', '_wa_writeback_submit'):
            for request in ({}, {'enabled': True}, {'format': 'wa-writeback-plan-v1', 'target': {'merchant_id': 'other'}}, {'format': 'old-protocol', 'preview_id': 'expired'}):
                with self.assertRaises(ServiceError) as caught:
                    self.service.call(method, request)
                self.assertEqual(caught.exception.code, 'capability_unavailable')
        self.assertEqual(self.service.repo.db.total_changes, changes)
        self.assertEqual(self.service.repo.list('operation'), [])

    def test_private_revoke_needs_no_identity(self):
        self.service.enabled = True
        self.service.allowed = ['merchant']
        self.service.set_whatsapp_reception(False)
        self.assertFalse(self.service.enabled)
        self.assertEqual(self.service.allowed, [])

    def test_restart_and_changed_account_remain_blocked(self):
        with self.assertRaises(ServiceError):
            self.service.set_whatsapp_reception(True, ['merchant'], 'account', 'epoch')
        with self.assertRaises(ServiceError):
            self.service._wa_set_account('other', 'epoch2', 'connected')
        self.assertFalse(self.service.get_whatsapp_state()['connected'])

    def test_history_and_metadata_do_not_set_pending(self):
        self.ingest(history=True)
        self.assertFalse(self.service.get_whatsapp_state()['conversations'][0]['pending'])

    def test_malformed_batch_is_not_partially_saved(self):
        with self.assertRaises(ServiceError):
            self.service._wa_ingest('account', 'epoch', [{'id': 'x', 'chat_id': 'chat', 'body': '', 'timestamp_ms': 1,
                'is_history': True, 'is_group': False, 'from_me': False}, {}])
        self.assertEqual(self.service.get_whatsapp_state()['conversations'], [])

    def test_cli_handshake_does_not_import_merchant_service(self):
        script = ("import runpy,sys; "
                  "sys.argv=['reception_service','--whatsapp-account','--workspace-id',sys.argv[1],'--data-dir',sys.argv[2]]; "
                  "runpy.run_module('reception_service',run_name='__main__')")
        env = dict(os.environ)
        source = str(Path(__file__).resolve().parents[1] / 'src')
        env['PYTHONPATH'] = source
        # Import guard fails immediately if the account process imports merchant service.
        guard = ("import builtins; orig=builtins.__import__; "
                 "builtins.__import__=lambda name,*a,**k: (_ for _ in ()).throw(RuntimeError('merchant import')) if name in ('service','reception_service.service') else orig(name,*a,**k); ")
        with tempfile.TemporaryDirectory() as cli_root:
            proc = subprocess.run([sys.executable, '-c', guard + script, self.workspace, cli_root],
                input='{"id":1,"method":"get_whatsapp_state"}\n{"id":2,"method":"shutdown"}\n',
                text=True, encoding='utf8', capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(replies[0]['result']['workspace_id'], self.workspace)
        self.assertFalse(replies[0]['result']['enabled'])
        self.assertEqual(replies[1]['id'], 2)


if __name__ == '__main__':
    unittest.main()
