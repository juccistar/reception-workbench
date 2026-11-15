"""Account service + real ModelRouter, with a synthetic provider and receipts."""
import asyncio
import base64
import json
from io import BytesIO
from PIL import Image
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
import uuid

from reception_service.errors import ServiceError
from reception_service.whatsapp_account_service import WhatsAppAccountService, _key


ACCOUNT = '10001@c.us'
CHAT = '20001@c.us'
MERCHANT = str(uuid.UUID('11111111-1111-4111-8111-111111111111'))
_image_buffer = BytesIO()
Image.new('RGB', (8, 8), (90, 120, 150)).save(_image_buffer, format='PNG')
PNG = _image_buffer.getvalue()


class Provider:
    def __init__(self, parts=1):
        self.parts = parts
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    async def generate(self, **kwargs):
        kwargs['before_send']()
        self.calls.append(kwargs)
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        context = json.loads(kwargs['user_payload'])
        source = context['whatsapp_messages'][-1]['source_id']
        return {'output': {'reply_messages': [{'body': 'Synthetic response ' + str(i), 'reply_sources': [source]} for i in range(self.parts)],
                           'wa_summary': 'Synthetic customer summary'}}

    async def aclose(self):
        pass


class AccountGenerationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = queue.Queue()
        self.service = WhatsAppAccountService(Path(self.temp.name) / 'account', str(uuid.uuid4()), self.events.put)
        self.service._wa_set_account(ACCOUNT, 'epoch', 'connected')
        self.service.save_whatsapp_default_merchant(MERCHANT, 0, ACCOUNT, 'epoch')
        caps = {key: {'status': 'passed', 'checked_at': 1} for key in ('text', 'structure', 'single_image', 'multi_image', 'combined', 'translation')}
        profile = {'profile_id': 'p', 'name': 'synthetic', 'protocol': 'chat-completions', 'base_url': 'https://synthetic.invalid',
                   'model': 'synthetic', 'credential_ref': 'c', 'has_key': True, 'config_revision': 1, 'credential_revision': 1,
                   'capability_revision': 1, 'capabilities': caps}
        settings = {'format': 'model-settings-v1', 'directory_revision': 1, 'profiles': [profile],
                    'routes': {'reception': {'profile_id': 'p', 'route_revision': 1}, 'translation': {'profile_id': None, 'route_revision': 0}}}
        authority = Path(self.temp.name) / 'models.json'
        authority.write_text(json.dumps({**settings, 'encrypted_keys': {}}), encoding='utf-8')
        self.packet = {'format': 'model-runtime-v1', 'authority_file': str(authority), 'settings': settings, 'credentials': {'p': 'synthetic-only'}}
        self.service._configure_models(**self.packet)
        self.provider = Provider()
        self.service.router._transport = self.provider
        self.service._wa_set_legacy_status(format='wa-legacy-verification-v1', account_id=ACCOUNT, manifest_hash='a' * 64,
                                           verified_at_ms=time.time_ns() // 1_000_000, sources=[])
        self.service.set_whatsapp_reception(True, [MERCHANT], ACCOUNT, 'epoch')

    def tearDown(self):
        self.provider.release.set()
        self.service.close()
        self.temp.cleanup()

    def ingest(self, mid='buyer', chat=CHAT, image=False, outgoing=False):
        return self.service.call('_wa_ingest', {'account_id': ACCOUNT, 'connection_epoch': 'epoch', 'messages': [
            {'id': mid, 'chat_id': chat, 'body': 'Please quote this product', 'timestamp_ms': 10,
             'from_me': outgoing, 'is_group': False, 'is_history': False, 'has_image': image}]})

    def event(self, name):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            if event['event'] == name:
                return event['data']
        self.fail('event missing: ' + name)

    def lease(self, request, revision='a' * 64):
        now = time.time_ns() // 1_000_000
        return {'format': 'wa-merchant-context-v1', 'merchant_id': MERCHANT, 'seller_id': '1234',
                'data_root': str(Path(self.temp.name) / 'merchant'), 'instance_id': '1' * 32,
                'conversation_id': request['conversation_id'], 'revision': revision, 'observed_at_ms': now, 'expires_at_ms': now + 5000,
                'context': {'knowledge': [], 'guidance': [], 'style': [], 'collection_rules': {}}}

    def generate(self):
        request = self.event('whatsapp.context.requested')
        self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
        return self.event('whatsapp.send.requested')['operation_id']

    def prepare(self, operation, revision='a' * 64):
        request = self.service.call('_wa_get_context_request', {'operation_id': operation})
        self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request, revision)})
        return self.service.call('_wa_prepare_send', {'operation_id': operation})

    def complete_parts(self, count):
        self.provider.parts = count
        self.ingest()
        operation = self.generate()
        ids = []
        for i in range(count):
            intent = self.prepare(operation)
            self.assertEqual(self.service.repo.get('operation', operation)['status'], 'unknown')
            self.assertEqual(intent['body'], 'Synthetic response ' + str(i))
            ids.append(operation)
            self.service.call('_wa_transport_result', {'operation_id': operation, 'account_id': ACCOUNT,
                'connection_epoch': 'epoch', 'status': 'submitted', 'message_id': 'sent-' + str(i), 'ack': 1})
            if i < count - 1:
                operation = self.event('whatsapp.send.requested')['operation_id']
        self.service.call('_wa_ack', {'account_id': ACCOUNT, 'connection_epoch': 'epoch', 'message_id': 'sent-0', 'ack': 2})
        state = self.service.get_whatsapp_state()
        self.assertFalse(state['conversations'][0]['pending'])
        self.assertFalse(state['conversations'][0]['human_owned'])
        self.assertEqual(state['pending_operations'], [])
        self.assertEqual(state['reply_batches'][0]['accepted'], count)
        self.assertEqual(state['reply_batches'][0]['status'], 'accepted')
        self.assertEqual(len(self.service.repo.list('operation')), count)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.service.get_whatsapp_context(CHAT)['wa_summary'], 'Synthetic customer summary')
        return ids

    def test_single_reply_is_accepted_only_by_exact_receipt(self):
        self.complete_parts(1)

    def test_two_replies_progress_without_another_model_call(self):
        self.complete_parts(2)

    def test_three_replies_and_duplicate_ack_do_not_duplicate_parts(self):
        self.complete_parts(3)

    def test_same_configuration_during_generation_keeps_authorization(self):
        self.ingest()
        request = self.event('whatsapp.context.requested')
        self.service.call('_configure_models', self.packet)
        self.assertTrue(self.service.enabled)
        self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
        self.prepare(self.event('whatsapp.send.requested')['operation_id'])

    def test_new_merchant_revision_stops_unsent_operation(self):
        self.ingest()
        operation = self.generate()
        with self.assertRaises(ServiceError):
            self.prepare(operation, 'b' * 64)
        self.assertEqual(self.service.repo.get('operation', operation)['status'], 'cancelled')
        self.assertFalse(self.service.repo.get('operation', operation)['submission_started'])

    def test_offline_merchant_does_not_block_another_contact(self):
        self.ingest()
        request = self.event('whatsapp.context.requested')
        self.ingest('other-message', '30001@c.us')
        self.service.call('_wa_set_merchant_context', {'request': request, 'context': None, 'error': 'merchant_unavailable'})
        next_request = self.event('whatsapp.context.requested')
        self.assertEqual(next_request['chat_id'], '30001@c.us')
        self.assertEqual(len(self.provider.calls), 0)

    def test_takeover_is_responsive_and_discards_late_model_result(self):
        self.provider.release.clear()
        self.ingest()
        request = self.event('whatsapp.context.requested')
        self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
        self.assertTrue(self.provider.started.wait(3))
        started = time.monotonic()
        self.service.call('takeover_whatsapp', {'chat_id': CHAT, 'enabled': True, 'expected_account_id': ACCOUNT, 'expected_connection_epoch': 'epoch'})
        self.assertLess(time.monotonic() - started, 0.5)
        self.provider.release.set()
        active = self.service._generation_active
        if active:
            active['thread'].join(5)
            self.assertFalse(active['thread'].is_alive())
        self.assertEqual(self.service.repo.list('operation'), [])

    def test_related_original_image_and_text_use_the_same_encoded_request(self):
        self.ingest(image=True)
        version = _key(ACCOUNT, 'epoch', CHAT, 'buyer', '10')
        fields = {'account_id': ACCOUNT, 'connection_epoch': 'epoch', 'chat_id': CHAT, 'message_id': 'buyer', 'source_version': version}
        tid = self.service.call('_wa_media_begin', {**fields, 'mime_type': 'image/png'})['transfer_id']
        self.service.call('_wa_media_chunk', {**fields, 'transfer_id': tid, 'offset': 0, 'data_base64': base64.b64encode(PNG).decode()})
        import hashlib
        self.service.call('_wa_media_finish', {**fields, 'transfer_id': tid, 'size': len(PNG), 'sha256': hashlib.sha256(PNG).hexdigest()})
        self.prepare(self.generate())
        call = self.provider.calls[0]
        self.assertEqual(call['images'][0]['data'], PNG)
        context = json.loads(call['user_payload'])
        self.assertEqual(context['whatsapp_messages'][0]['text'], 'Please quote this product')
        self.assertEqual(context['image_transport'], 'inline')
        self.assertEqual(len(self.provider.calls), 1)


if __name__ == '__main__':
    unittest.main()
