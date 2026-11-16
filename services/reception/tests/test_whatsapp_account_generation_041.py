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
from unittest.mock import patch
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
        context = json.loads(kwargs['user_payload'])['context']
        source = context['whatsapp_messages'][-1]['source_id']
        return {'output': {'reply_messages': [{'body': 'Synthetic response ' + str(i), 'reply_sources': [source]} for i in range(self.parts)],
                           'wa_summary': 'Synthetic customer summary'}}

    async def aclose(self):
        pass


class AccountGenerationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.enterContext(patch('reception_service.model_request_budget._directory',
                                return_value=Path(self.temp.name) / 'model-budget'))
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

    def test_generation_uses_configured_model_without_capability_precheck(self):
        settings = self.packet['settings']
        for capability in settings['profiles'][0]['capabilities'].values():
            capability['status'] = 'unknown'
        Path(self.packet['authority_file']).write_text(json.dumps({**settings, 'encrypted_keys': {}}), encoding='utf-8')
        self.service._configure_models(**self.packet)
        self.service.set_whatsapp_reception(True, [MERCHANT], ACCOUNT, 'epoch')
        self.ingest()
        operation = self.generate()
        self.assertEqual(self.prepare(operation)['body'], 'Synthetic response 0')

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

    def test_disabled_images_do_not_block_generation_and_message_count_applies(self):
        self.ingest(mid='buyer', image=True)
        request = self.event('whatsapp.context.requested')
        lease = self.lease(request)
        lease['context'].update(reception_images_enabled=False, context_message_count=1)
        self.service.call('_wa_set_merchant_context', {'request': request, 'context': lease})
        self.event('whatsapp.send.requested')
        payload = json.loads(self.provider.calls[-1]['user_payload'])['context']
        self.assertEqual(len(payload['whatsapp_messages']), 1)
        self.assertEqual(payload['image_transport'], 'none')

    def test_unchecked_model_can_generate_with_current_configuration(self):
        self.service.set_whatsapp_reception(False)
        for capability in self.packet['settings']['profiles'][0]['capabilities'].values():
            capability['status'] = 'unknown'
        authority = Path(self.packet['authority_file'])
        authority.write_text(json.dumps({**self.packet['settings'], 'encrypted_keys': {}}), encoding='utf-8')
        self.service._configure_models(**self.packet)
        self.service.set_whatsapp_reception(True, [MERCHANT], ACCOUNT, 'epoch')
        self.ingest()
        self.assertTrue(self.generate())

    def test_cancelled_generation_retries_with_new_supplement(self):
        self.provider.release.clear()
        self.ingest()
        request = self.event('whatsapp.context.requested')
        self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
        self.assertTrue(self.provider.started.wait(5))
        ctx = self.service.get_whatsapp_context(CHAT)
        self.service._wa_save_supplement(CHAT, MERCHANT, ctx['binding_revision'], 0, 'new details', ACCOUNT, 'epoch')
        deadline = time.monotonic() + 5
        while self.service._generation_active and time.monotonic() < deadline:
            time.sleep(.01)
        with self.service.repo.lock:
            attempt = self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))
            self.assertEqual(attempt['status'], 'failed')
            self.assertEqual(attempt['count'], 1)
            row = self.service.repo.get('conversation', _key(ACCOUNT, CHAT))
            row['retry_at_ms'] = 0
            self.service.repo.put('conversation', _key(ACCOUNT, CHAT), row)
            self.provider.release.set()
            self.service._schedule_account_replies()
        self.assertTrue(self.generate())
        self.assertEqual(len(self.provider.calls), 2)
        self.assertIn('new details', self.provider.calls[-1]['user_payload'])

    def test_cleanup_timeout_releases_obsolete_attempt_only_after_worker_finishes(self):
        from reception_service.model_providers.owned_sync import owned_sync
        from reception_service.model_providers.runtime import ModelRuntime
        self.service.router._runtime = ModelRuntime(cleanup_timeout=.15)
        release = threading.Event()
        started = threading.Event()
        original = self.provider.generate
        async def blocked(**kwargs):
            kwargs['before_send']()
            self.provider.calls.append(kwargs)
            started.set()
            await owned_sync(lambda: release.wait(5))
            return {'output': {}}
        self.provider.generate = blocked
        try:
            self.ingest()
            request = self.event('whatsapp.context.requested')
            self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
            self.assertTrue(started.wait(3))
            ctx = self.service.get_whatsapp_context(CHAT)
            self.service._wa_save_supplement(CHAT, MERCHANT, ctx['binding_revision'], 0, 'new details', ACCOUNT, 'epoch')
            job = self.service._generation_active
            job['thread'].join(3)
            self.assertFalse(job['thread'].is_alive())
            with self.service.repo.lock:
                attempt = self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))
                self.assertEqual((attempt['status'], attempt['count']), ('unknown', 1))
                self.assertIs(self.service._generation_active, job)
                self.assertEqual(self.service.repo.list('operation'), [])
                self.service._schedule_account_replies()
                self.assertEqual(len(self.service._generation_requests), 0)
                row = self.service.repo.get('conversation', _key(ACCOUNT, CHAT))
                row['retry_at_ms'] = 0
                self.service.repo.put('conversation', _key(ACCOUNT, CHAT), row)
            self.provider.generate = original
            release.set()
            request = self.event('whatsapp.context.requested')
            with self.service.repo.lock:
                attempt = self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))
                self.assertEqual((attempt['status'], attempt['count']), ('failed', 1))
                self.assertEqual(self.service.repo.list('operation'), [])
            self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
            self.assertTrue(self.event('whatsapp.send.requested')['operation_id'])
            self.assertEqual(len(self.provider.calls), 2)
            self.assertIn('new details', self.provider.calls[-1]['user_payload'])
            self.assertEqual(self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))['count'], 2)
        finally:
            release.set()

    def test_settled_timeout_then_supplement_change_retries(self):
        from reception_service.model_providers.owned_sync import owned_sync
        from reception_service.model_providers.runtime import ModelRuntime
        self.service.router._runtime = ModelRuntime(cleanup_timeout=.15, budget_directory=Path(self.temp.name) / 'budget')
        release, started = threading.Event(), threading.Event()
        original_generate = self.service.router.generate
        original_provider = self.provider.generate
        async def blocked(**kwargs):
            kwargs['before_send']()
            self.provider.calls.append(kwargs)
            started.set()
            await owned_sync(lambda: release.wait(15))
            return {'output': {}}
        self.provider.generate = blocked
        self.service.router.generate = lambda *args, **kwargs: original_generate(*args, **{**kwargs, 'deadline': time.monotonic() + 4})
        try:
            self.ingest()
            request = self.event('whatsapp.context.requested')
            self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
            self.assertTrue(started.wait(6))
            job = self.service._generation_active
            job['thread'].join(8)
            self.assertFalse(job['thread'].is_alive())
            self.assertEqual(self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))['status'], 'unknown')
            release.set()
            deadline = time.monotonic() + 3
            while self.service._generation_active and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNone(self.service._generation_active)
            self.assertEqual(self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))['status'], 'unknown')
            self.provider.generate = original_provider
            self.service.router.generate = original_generate
            ctx = self.service.get_whatsapp_context(CHAT)
            self.service._wa_save_supplement(CHAT, MERCHANT, ctx['binding_revision'], 0, 'later details', ACCOUNT, 'epoch')
            with self.service.repo.lock:
                row = self.service.repo.get('conversation', _key(ACCOUNT, CHAT))
                row['retry_at_ms'] = 0
                self.service.repo.put('conversation', _key(ACCOUNT, CHAT), row)
                self.service._schedule_account_replies()
            self.assertTrue(self.generate())
            self.assertEqual(len(self.provider.calls), 2)
            self.assertIn('later details', self.provider.calls[-1]['user_payload'])
            self.assertEqual(self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))['count'], 2)
        finally:
            release.set()

    def _settings_change_cancels_blocked_generation(self, setting, value, image=False):
        import hashlib
        import sqlite3
        from contextlib import closing
        from reception_service.model_providers.owned_sync import owned_sync
        from reception_service.model_providers.runtime import ModelRuntime
        budget = Path(self.temp.name) / 'settings-budget'
        self.service.router._runtime = ModelRuntime(cleanup_timeout=.15, budget_directory=budget)
        release, started = threading.Event(), threading.Event()
        async def blocked(**kwargs):
            kwargs['before_send']()
            started.set()
            await owned_sync(lambda: release.wait(10))
            return {'output': {'reply_messages': [{'body': 'late', 'reply_sources': []}], 'wa_summary': 'late'}}
        self.provider.generate = blocked
        try:
            self.ingest(image=image)
            if image:
                fields = {'account_id': ACCOUNT, 'connection_epoch': 'epoch', 'chat_id': CHAT, 'message_id': 'buyer', 'source_version': _key(ACCOUNT, 'epoch', CHAT, 'buyer', '10')}
                tid = self.service.call('_wa_media_begin', {**fields, 'mime_type': 'image/png'})['transfer_id']
                self.service.call('_wa_media_chunk', {**fields, 'transfer_id': tid, 'offset': 0, 'data_base64': base64.b64encode(PNG).decode()})
                self.service.call('_wa_media_finish', {**fields, 'transfer_id': tid, 'size': len(PNG), 'sha256': hashlib.sha256(PNG).hexdigest()})
            request = self.event('whatsapp.context.requested')
            initial = self.lease(request)
            initial['context'].update(reception_images_enabled=True, context_message_count=50)
            self.service.call('_wa_set_merchant_context', {'request': request, 'context': initial})
            self.assertTrue(started.wait(5))
            job = self.service._generation_active
            unrelated = dict(request, merchant_id=str(uuid.uuid4()))
            self.assertFalse(self.service.call('_wa_check_generation_context', {'request': unrelated, 'context': None, 'error': 'unavailable'})['active'])
            self.assertFalse(job['cancel'].is_set())
            changed = self.lease(request, 'b' * 64)
            changed['context'].update(initial['context'])
            changed['context'][setting] = value
            self.assertFalse(self.service.call('_wa_check_generation_context', {'request': request, 'context': changed})['active'])
            self.assertTrue(job['cancel'].is_set())
            job['thread'].join(3)
            self.assertFalse(job['thread'].is_alive())
            self.assertIs(self.service._generation_active, job)
            with closing(sqlite3.connect(budget / 'budget.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT heavy FROM requests WHERE granted=1').fetchall(), [(int(image),)])
            self.assertEqual(self.service.repo.list('operation'), [])
            release.set()
            deadline = time.monotonic() + 5
            while self.service._generation_active is job and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNot(self.service._generation_active, job)
            self.assertEqual(self.service.repo.list('operation'), [])
        finally:
            release.set()

    def test_image_setting_change_cancels_and_holds_image_budget(self):
        self._settings_change_cancels_blocked_generation('reception_images_enabled', False, image=True)

    def test_message_count_change_cancels_and_holds_budget(self):
        self._settings_change_cancels_blocked_generation('context_message_count', 10)

    def test_normal_close_settles_inflight_and_restart_keeps_attempt_count(self):
        from reception_service.model_providers.owned_sync import owned_sync
        from reception_service.model_providers.runtime import ModelRuntime
        self.service.router._runtime = ModelRuntime(cleanup_timeout=2, budget_directory=Path(self.temp.name) / 'budget')
        release, started = threading.Event(), threading.Event()
        async def blocked(**kwargs):
            kwargs['before_send']()
            started.set()
            await owned_sync(lambda: release.wait(5))
            return {'output': {}}
        self.provider.generate = blocked
        self.ingest()
        request = self.event('whatsapp.context.requested')
        self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
        self.assertTrue(started.wait(3))
        self.assertEqual(self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))['count'], 1)
        errors = []
        def close():
            try:
                self.service.close()
            except Exception as exc:
                errors.append(exc)
        closer = threading.Thread(target=close)
        closer.start()
        try:
            deadline = time.monotonic() + 2
            while not self.service._generation_closed and time.monotonic() < deadline:
                time.sleep(.01)
        finally:
            release.set()
            closer.join(4)
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        root, account_id = self.service.root, self.service.workspace_id
        self.service = WhatsAppAccountService(root, account_id, self.events.put)
        self.assertFalse(self.service.enabled)
        attempt = self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))
        self.assertEqual((attempt['status'], attempt['count']), ('failed', 1))
        self.service._wa_set_account(ACCOUNT, 'epoch', 'connected')
        self.service._configure_models(**self.packet)
        self.provider = Provider()
        self.service.router._transport = self.provider
        self.service._wa_set_legacy_status(format='wa-legacy-verification-v1', account_id=ACCOUNT, manifest_hash='a' * 64,
                                           verified_at_ms=time.time_ns() // 1_000_000, sources=[])
        self.service.set_whatsapp_reception(True, [MERCHANT], ACCOUNT, 'epoch')
        self.assertTrue(self.generate())
        self.assertEqual(self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))['count'], 2)

    def test_close_timeout_does_not_claim_unfinished_attempt_is_settled(self):
        from reception_service.model_providers.owned_sync import owned_sync
        from reception_service.model_providers.runtime import ModelRuntime
        self.service.router._runtime = ModelRuntime(cleanup_timeout=.15, budget_directory=Path(self.temp.name) / 'budget')
        release, started = threading.Event(), threading.Event()
        async def blocked(**kwargs):
            kwargs['before_send']()
            started.set()
            await owned_sync(lambda: release.wait(5))
            return {'output': {}}
        self.provider.generate = blocked
        try:
            self.ingest()
            request = self.event('whatsapp.context.requested')
            self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
            self.assertTrue(started.wait(3))
            with self.assertRaises(ServiceError) as error:
                self.service.close()
            self.assertEqual(error.exception.code, 'model_cleanup_timeout')
            attempt = self.service.repo.get('generation_attempt', _key(ACCOUNT, CHAT, 'buyer'))
            self.assertEqual((attempt['status'], attempt['count'], attempt['runtime_settled']), ('in_flight', 1, False))
        finally:
            release.set()
            self.service.router._runtime._thread.join(4)

    def test_runtime_completion_during_close_does_not_touch_closed_repository(self):
        from reception_service.model_providers.owned_sync import owned_sync
        from reception_service.model_providers.runtime import ModelRuntime
        self.service.router._runtime = ModelRuntime(cleanup_timeout=.5)
        release, started = threading.Event(), threading.Event()
        async def blocked(**kwargs):
            kwargs['before_send']()
            started.set()
            await owned_sync(lambda: release.wait(5))
            return {'output': {}}
        self.provider.generate = blocked
        self.ingest()
        request = self.event('whatsapp.context.requested')
        self.service.call('_wa_set_merchant_context', {'request': request, 'context': self.lease(request)})
        self.assertTrue(started.wait(3))
        errors = []
        def close():
            try:
                self.service.close()
            except Exception as exc:
                errors.append(exc)
        closer = threading.Thread(target=close)
        closer.start()
        try:
            deadline = time.monotonic() + 2
            while not self.service._generation_closed and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.service._generation_closed)
        finally:
            release.set()
            closer.join(3)
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(self.service.repo.db)
        # tearDown expects to close an open service.
        self.service.close = lambda: None

    def test_supplement_is_revision_checked_and_segment_isolated(self):
        self.ingest()
        ctx = self.service.get_whatsapp_context(CHAT)
        params = dict(chat_id=CHAT, merchant_id=MERCHANT, binding_revision=ctx['binding_revision'],
                      expected_revision=0, body='private note', expected_account_id=ACCOUNT, expected_connection_epoch='epoch')
        self.assertEqual(self.service._wa_save_supplement(**params)['revision'], 1)
        with self.assertRaises(ServiceError):
            self.service._wa_save_supplement(**params)
        self.assertEqual(self.service.get_whatsapp_context(CHAT)['wa_supplement']['body'], 'private note')
        self.service.save_whatsapp_binding(CHAT, str(uuid.uuid4()), None, ctx['binding_revision'], ACCOUNT, 'epoch')
        self.assertEqual(self.service.get_whatsapp_context(CHAT)['wa_supplement']['body'], '')

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

    def test_translation_only_configuration_does_not_revoke_reception(self):
        from copy import deepcopy
        packet = deepcopy(self.packet)
        packet['settings']['directory_revision'] += 1
        translation = dict(packet['settings']['profiles'][0], profile_id='translation', credential_ref='translation-credential', name='Translation API')
        packet['settings']['profiles'].append(translation)
        packet['settings']['routes']['translation'] = {'profile_id': 'translation', 'route_revision': 1}
        packet['credentials']['translation'] = 'synthetic-translation-only'
        Path(packet['authority_file']).write_text(json.dumps({**packet['settings'], 'encrypted_keys': {}}), encoding='utf-8')
        self.service.call('_configure_models', packet)
        self.assertTrue(self.service.enabled)
        self.assertEqual(self.service.allowed, [MERCHANT])
        self.assertEqual(self.provider.calls, [])

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

    def test_cancelling_unsent_part_then_rebinding_does_not_reuse_old_pending_source(self):
        other = '22222222-2222-4222-8222-222222222222'
        self.service.set_whatsapp_reception(True, [MERCHANT, other], ACCOUNT, 'epoch')
        self.ingest()
        operation = self.generate()
        self.ingest('next-buyer')
        self.service.call('save_whatsapp_binding', {'chat_id': CHAT, 'merchant_id': other, 'conversation_id': None,
                          'expected_revision': 0, 'expected_account_id': ACCOUNT, 'expected_connection_epoch': 'epoch'})
        state = self.service.get_whatsapp_state()
        self.assertEqual(state['bindings'][0]['merchant_id'], other)
        self.assertTrue(state['conversations'][0]['reception_paused'])
        self.assertFalse(state['conversations'][0]['pending'])
        self.assertEqual(self.service.get_whatsapp_context(CHAT)['whatsapp_messages'], [])
        self.assertEqual(self.service.repo.get('operation', operation)['status'], 'cancelled')
        self.assertEqual(state['reply_batches'][0]['pending_operation_ids'], [])
        self.service._schedule_account_replies()
        self.assertEqual(len(self.provider.calls), 1)

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
        context = json.loads(call['user_payload'])['context']
        self.assertEqual(context['whatsapp_messages'][0]['text'], 'Please quote this product')
        self.assertEqual(context['image_transport'], 'inline')
        self.assertEqual(len(self.provider.calls), 1)


if __name__ == '__main__':
    unittest.main()
