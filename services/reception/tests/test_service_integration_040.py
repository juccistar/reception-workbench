import base64
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image
from reception_service.errors import ServiceError
from reception_service.service import ReceptionService
from reception_service.reply_batches import freeze_reply_batch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test_support'))
from reception_test_support.service import _factory


class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = ReceptionService(self.root, mode='demo', _integration_factory=_factory, _fixture_initializer=lambda service: None)
        self.s.mode = 'live'
        self.path = self.root / 'authority.json'
        profile = {'profile_id': 'p', 'name': 'synthetic', 'protocol': 'chat-completions',
                   'base_url': 'https://example.com', 'model': 'synthetic', 'credential_ref': 'c', 'has_key': True,
                   'config_revision': 1, 'credential_revision': 1, 'capability_revision': 1,
                   'capabilities': {k: {'status': 'passed'} for k in ('text', 'structure', 'translation')}}
        self.settings = {'format': 'model-settings-v1', 'directory_revision': 1, 'profiles': [profile],
                         'routes': {k: {'profile_id': 'p', 'route_revision': 1} for k in ('reception', 'translation')}}
        self.configure()

    def configure(self):
        self.path.write_text(json.dumps({**self.settings, 'encrypted_keys': {}}), encoding='utf-8')
        return self.s.call('_configure_models', {'format': 'model-runtime-v1', 'authority_file': str(self.path),
                           'settings': self.settings, 'credentials': {'p': 'synthetic'}})

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_translation_activation_and_route_independence(self):
        s = self.s
        self.assertFalse(s.call('get_translation_info')['enabled'])
        with self.assertRaises(ServiceError):
            s.call('_set_translation_enabled', {'enabled': True, 'seller_id': 'wrong', 'data_root': str(self.root), 'revision': 0})
        args = {'enabled': True, 'seller_id': s.seller_id, 'data_root': str(self.root), 'revision': 0}
        self.assertTrue(s.call('_set_translation_enabled', args)['enabled'])
        with self.assertRaises(ServiceError):
            s.call('_set_translation_enabled', args)
        s._running = True
        self.settings['routes']['translation']['route_revision'] += 1
        self.assertEqual(self.configure()['affected'], ['translation'])
        self.assertTrue(s._running)
        self.path.unlink()
        self.assertFalse(s.call('get_translation_info')['status'] == 'ready')
        self.assertFalse(s.call('_set_translation_enabled', {**args, 'enabled': False, 'revision': 1})['enabled'])

    def test_invalid_configuration_disables_both_purposes_until_valid_packet(self):
        s = self.s
        with self.assertRaises(ServiceError):
            s.call('_configure_models', {'format': 'invalid'})
        with self.assertRaises(ServiceError):
            s._freeze_reception_model()
        self.assertEqual(s.call('get_translation_info')['status'], 'not_configured')
        self.assertEqual(s.call('get_snapshot')['checks']['ai']['status'], 'blocked')
        self.configure()
        self.assertEqual(s._freeze_reception_model()['profile_id'], 'p')

    def test_whatsapp_dispatch_and_stale_submit(self):
        s = self.s
        facade = s.integrations
        facade.resolve_identity = lambda seller: seller
        facade.configure_whatsapp_image_resolver = lambda resolver: None
        def generate(context, job_id, **kwargs):
            source = context['whatsapp_messages'][-1]['source_id']
            return {'reply_messages': [{'body': 'Hello', 'reply_sources': [source]}],
                    'wa_summary': '摘要', 'model_snapshot': s._freeze_reception_model()}
        facade.generate_whatsapp = generate
        s.checks['ai']['status'] = 'passed'
        s.call('_wa_set_account', {'account_id': 'a', 'connection_epoch': 'e', 'status': 'connected'})
        s.call('set_whatsapp_reception', {'enabled': True})
        s.call('_wa_ingest', {'account_id': 'a', 'connection_epoch': 'e', 'messages': [
            {'id': 'm', 'chat_id': 'c', 'body': 'Hello', 'timestamp_ms': 100, 'from_me': False,
             'name': 'Buyer', 'is_group': False, 'is_history': False}]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not s.repo.list('wa_operation'):
            time.sleep(.01)
        ops = s.repo.list('wa_operation')
        self.assertEqual(len(ops), 1, s.repo.list('job'))
        self.settings['routes']['reception']['route_revision'] += 1
        self.path.write_text(json.dumps({**self.settings, 'encrypted_keys': {}}), encoding='utf-8')
        with self.assertRaises(ServiceError):
            s.call('_wa_prepare_send', {'operation_id': ops[0]['operation_id']})
        self.assertEqual(s.repo.get('wa_operation', ops[0]['operation_id'])['status'], 'cancelled')

    def test_reception_change_stops_only_unsubmitted_parts(self):
        s = self.s
        batch = freeze_reply_batch([{'body': 'one'}, {'body': 'two'}, {'body': 'three'}],
                                   origin='automatic', conversation_id='c', parent_draft_id='parent', parent_revision=1)
        batch['parts'][0]['status'] = 'accepted'
        batch['parts'][1]['status'] = 'unknown'
        s.repo.put('draft', 'parent', {'draft_id': 'parent', 'revision': 1, 'status': 'sending'})
        s.repo.put('reply_batch', batch['batch_id'], batch)
        s.repo.put('operation', 'pending', {'operation_id': 'pending', 'status': 'unknown'})
        self.settings['routes']['reception']['route_revision'] += 1
        self.configure()
        result = s.repo.get('reply_batch', batch['batch_id'])
        self.assertEqual([p['status'] for p in result['parts']], ['accepted', 'unknown', 'cancelled'])
        self.assertEqual(result['status'], 'waiting_receipt')
        self.assertEqual(s.repo.get('operation', 'pending')['status'], 'unknown')
        self.assertEqual(s.checks['ai']['status'], 'not_checked')

    def test_started_generation_does_not_defer_ambiguous_failure(self):
        s = self.s
        job = {'job_id': 'job_attempt', 'kind': 'generate_draft', 'conversation_id': 'c',
               'status': 'running', 'source_message_id': 'm', 'source_text_sha256': 'hash',
               'external_started': True, 'created_at': 1}
        s.repo.put('job', job['job_id'], job)
        s._defer_generation_retry(job['job_id'], 'task_failed')
        result = s.repo.get('job', job['job_id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['retry_at'])
        self.assertEqual(result['retry_status'], 'exhausted')

    def test_close_timeout_keeps_repository_open_until_media_drains(self):
        s = self.s
        entered, release = threading.Event(), threading.Event()
        stop_media = s._stop_requirement_media
        def blocked_media():
            entered.set()
            release.wait(20)
            stop_media()
        s._stop_requirement_media = blocked_media
        try:
            with self.assertRaises(ServiceError):
                s.close()
            self.assertTrue(entered.is_set())
            self.assertFalse(s._closed)
            s.repo.put('test', 'still_open', {'ok': True})
            self.assertTrue(s.repo.get('test', 'still_open')['ok'])
        finally:
            release.set()
        s.close()
        self.assertTrue(s._closed)

    def test_cleanup_failure_revokes_procurement_and_preserves_receipt(self):
        s = self.s
        s._running = s._auto_send = s._wa_enabled = s._procurement_authorized = True
        old_epoch = s._procurement_epoch
        s.repo.put('operation', 'pending', {'operation_id': 'pending', 'status': 'unknown'})
        s._executor_health.fail('failed', 'model_cleanup_timeout')
        self.assertFalse(s._running or s._auto_send or s._wa_enabled or s._procurement_authorized)
        self.assertGreater(s._procurement_epoch, old_epoch)
        self.assertEqual(s._executor_health.snapshot()['error_code'], 'model_cleanup_timeout')
        self.assertEqual(s.repo.get('operation', 'pending')['status'], 'unknown')

    def test_image_private_rpc_and_retired_entry(self):
        s = self.s
        out = io.BytesIO()
        Image.new('RGB', (3, 3), 'red').save(out, 'PNG')
        data = out.getvalue()
        sha = hashlib.sha256(data).hexdigest()
        s.repo.put('conversation', 'test', {'conversation_id': 'test', 'seller_id': s.seller_id, 'buyer_id': 'buyer'})
        s.repo.put('supplement_attachment', 'att', {'id': 'att', 'seller_id': s.seller_id, 'buyer_id': 'buyer',
                   'conversation_id': 'test', 'status': 'ready', 'origin': 'business_supplement', 'sha256': sha,
                   'original_b64': base64.b64encode(data).decode(), 'size': len(data), 'mime': 'image/png'})
        ref = {'kind': 'supplement', 'conversation_id': 'test', 'attachment_id': 'att', 'sha256': sha}
        token = s.call('_image_prepare', {'reference': ref, 'copy': False})['token']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = s.call('_image_status', {'token': token})
            if status['status'] != 'pending':
                break
            time.sleep(.01)
        self.assertEqual(status['status'], 'ready')
        chunk = s.call('_image_transfer_chunk', {'token': token, 'offset': 0, 'sha256': sha})
        self.assertEqual(base64.b64decode(chunk['data_b64']), data)
        self.assertTrue(s.call('_image_cancel', {'token': token})['cancelled'])
        with self.assertRaises(ServiceError):
            s.call('_image_original_describe', {'reference': ref})


if __name__ == '__main__':
    unittest.main()
