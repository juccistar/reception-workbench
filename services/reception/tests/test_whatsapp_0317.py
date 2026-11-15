"""Synthetic WhatsApp channel isolation and send ledger tests."""
import base64
import hashlib
import io
import random
import threading
import time
import unittest
from PIL import Image
from unittest.mock import patch

import test_product_library_0313 as fixtures
from reception_service.errors import ServiceError


class WhatsAppTests(unittest.TestCase):
    setUp = fixtures.ProductLibraryTests.setUp
    tearDown = fixtures.ProductLibraryTests.tearDown
    demand = fixtures.ProductLibraryTests.demand

    def setUpAccount(self):
        self.service._wa_set_account('wa-account', 'epoch-1', 'connected')

    def ingest(self, mid, chat='contact', body='Hello', from_me=False, is_history=False, is_group=False, **extra):
        return self.service._wa_ingest('wa-account', 'epoch-1', [{'id': mid, 'chat_id': chat, 'body': body,
            'timestamp_ms': 100, 'from_me': from_me, 'name': 'Customer', 'is_group': is_group,
            'is_history': is_history, **extra}])

    def wait_op(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ops = self.service.repo.list('wa_operation')
            if ops:
                return ops[-1]
            threading.Event().wait(.01)
        self.fail('WhatsApp reply was not generated')

    def generate_parts(self, context, *args, **kwargs):
        source = context['whatsapp_messages'][-1]['source_id']
        return {'reply_messages': [{'body': text, 'reply_sources': [source]} for text in ('First', 'Second', 'Third')], 'wa_summary': '摘要'}

    def test_image_only_buyer_is_pending_without_text_only_generation(self):
        self.setUpAccount()
        self.service.set_whatsapp_reception(True)
        self.ingest('image-1', body='', has_image=True)
        contact = self.service._wa_contact('contact')
        self.assertTrue(contact['pending'])
        self.assertEqual(contact['pending_message_id'], 'image-1')
        self.assertEqual(contact['error_code'], 'whatsapp_media_not_ready')
        row = self.service.get_whatsapp_context('contact')['whatsapp_messages'][-1]
        self.assertEqual((row['text'], row['role'], row['media']['status']), ('', 'buyer', 'pending'))
        self.assertFalse(self.service.repo.list('wa_operation'))

    def test_image_history_does_not_create_pending(self):
        self.setUpAccount()
        self.ingest('historical-image', body='', has_image=True, is_history=True)
        self.assertFalse(self.service._wa_contact('contact')['pending'])
        self.assertEqual(self.service.get_whatsapp_context('contact')['whatsapp_messages'][-1]['media']['status'], 'pending')

    def test_image_transfer_multichunk_wakes_original_pending(self):
        self.setUpAccount()
        self.ingest('image-transfer', body='', has_image=True)
        media = self.service.get_whatsapp_context('contact')['whatsapp_messages'][-1]['media']
        identity = dict(account_id='wa-account', connection_epoch='epoch-1', chat_id='contact', message_id='image-transfer', source_version=media['source_version'])
        image = Image.frombytes('RGB', (400, 400), random.Random(3).randbytes(400 * 400 * 3))
        output = io.BytesIO()
        image.save(output, format='PNG')
        data = output.getvalue()
        self.assertGreater(len(data), 256 * 1024)
        with patch.object(self.service.integrations, 'generate_whatsapp', side_effect=self.generate_parts):
            self.service.set_whatsapp_reception(True)
            transfer = self.service._wa_media_begin(**identity, mime_type='image/png')['transfer_id']
            for offset in range(0, len(data), 256 * 1024):
                chunk = data[offset:offset + 256 * 1024]
                self.assertEqual(self.service._wa_media_chunk(**identity, transfer_id=transfer, offset=offset, data_base64=base64.b64encode(chunk).decode())['next_offset'], offset + len(chunk))
            result = self.service._wa_media_finish(**identity, transfer_id=transfer, size=len(data), sha256=hashlib.sha256(data).hexdigest())
            self.assertEqual(result['status'], 'ready')
            op = self.wait_op()
            self.assertEqual(op['pending_message_id'], 'image-transfer')
            with self.assertRaises(ServiceError):
                self.service._wa_media_finish(**identity, transfer_id=transfer, size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def test_live_attempt_budget_is_bound_to_original_message(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        service = self.service
        service.mode = 'live'
        params = {'account_id': 'a', 'connection_epoch': 'e1', 'chat_id': 'c', 'pending_message_id': 'm',
                  'model_snapshot': {'profile_id': 'first'}, 'fingerprint': 'pending-image', 'context': {}}
        facade = Mock()
        def generate(_context, _job, **kwargs):
            kwargs['on_call_start']()
            raise ServiceError('synthetic_failure', 'failed')
        facade.generate_whatsapp.side_effect = generate
        with patch.object(service, '_wa_current', return_value=True), patch.object(service, '_ensure_identity'), \
                patch.object(service, '_assert_reception_model'), patch.object(service, '_job_phase', return_value=nullcontext()):
            with self.assertRaises(ServiceError) as raised:
                service._task_whatsapp_reply(params, 'job-0', 1, facade)
            self.assertEqual(raised.exception.code, 'synthetic_failure')
            params.update(connection_epoch='e2', fingerprint='ready-image', model_snapshot={'profile_id': 'second'})
            with self.assertRaises(ServiceError) as raised:
                service._task_whatsapp_reply(params, 'job-1', 1, facade)
            self.assertEqual(raised.exception.code, 'model_attempt_exhausted')
            from reception_service.model_attempts import chain_id
            key = chain_id(service.seller_id, 'reception', 'whatsapp',
                           {'account_id': 'a', 'chat_id': 'c', 'pending_message_id': 'm'})
            chain = service.repo.get('model_attempt_chain_v1', key)
            self.assertEqual(chain['attempts'], 1)
            self.assertEqual(chain['status'], 'unknown')
        service.mode = 'demo'

    def test_late_success_finishes_original_claim_without_applying(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        from reception_service.model_attempts import chain_id
        service = self.service
        service.mode = 'live'
        params = {'account_id': 'a', 'connection_epoch': 'e', 'chat_id': 'c', 'pending_message_id': 'm',
                  'model_snapshot': {}, 'fingerprint': 'f', 'context': {}}
        current = [True]
        facade = Mock()
        def generate(_context, _job, **kwargs):
            kwargs['on_call_start']()
            current[0] = False
            return {'reply': 'late'}
        facade.generate_whatsapp.side_effect = generate
        with patch.object(service, '_wa_current', side_effect=lambda *args: current[0]), \
                patch.object(service, '_ensure_identity'), patch.object(service, '_assert_reception_model'), \
                patch.object(service, '_job_phase', return_value=nullcontext()):
            with self.assertRaises(ServiceError) as raised:
                service._task_whatsapp_reply(params, 'late-job', 1, facade)
            self.assertEqual(raised.exception.code, 'cancelled')
        key = chain_id(service.seller_id, 'reception', 'whatsapp',
                       {'account_id': 'a', 'chat_id': 'c', 'pending_message_id': 'm'})
        self.assertEqual(service.repo.get('model_attempt_chain_v1', key)['status'], 'completed')
        self.assertFalse(service.repo.list('wa_draft'))
        self.assertFalse(service.repo.list('wa_operation'))
        service.mode = 'demo'

    def test_live_router_transmits_owned_image_and_evidence(self):
        import json
        from pathlib import Path
        import httpx
        from reception_service.integrations.live import LiveIntegration
        from reception_service.model_providers.transport import ProviderTransport
        from reception_test_support.model import configure_synthetic_model

        self.setUpAccount()
        self.ingest('model-image', body='', has_image=True)
        media = self.service.get_whatsapp_context('contact')['whatsapp_messages'][-1]['media']
        identity = dict(account_id='wa-account', connection_epoch='epoch-1', chat_id='contact', message_id='model-image', source_version=media['source_version'])
        output = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(output, format='PNG')
        data = output.getvalue()
        transfer = self.service._wa_media_begin(**identity, mime_type='image/png')['transfer_id']
        self.service._wa_media_chunk(**identity, transfer_id=transfer, offset=0, data_base64=base64.b64encode(data).decode())
        self.service._wa_media_finish(**identity, transfer_id=transfer, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        context = self.service.get_whatsapp_context('contact')
        row = context['whatsapp_messages'][-1]
        context['model_image_refs'] = [dict(source_id=row['source_id'], source_version=row['media']['source_version'], image_id=row['media']['media_id'], sha256=row['media']['sha256'], role=row['role'])]
        requests = []
        change_source = [False]
        def respond(request):
            requests.append(json.loads(request.content))
            if change_source[0]:
                self.service._wa_set_account('wa-account', 'epoch-2', 'connected')
            answer = {'reply_messages': [{'body': 'Hello', 'reply_sources': [row['source_id']]}], 'wa_summary': '图片'}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}], 'model': 'fixture'})
        configure_synthetic_model(self.service, Path(self.tmp.name) / 'model-authority.json')
        self.service._model_router._transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(respond))
        facade = LiveIntegration.__new__(LiveIntegration)
        facade._query_state = threading.local()
        facade.settings = {}
        facade.resolve_identity = lambda seller: seller
        facade.configure_model_router(self.service._model_router)
        facade.configure_whatsapp_image_resolver(self.service._wa_resolve_current_image)
        result = facade.generate_whatsapp(context, 'image-job')
        self.assertEqual(result['image_input_evidence']['images'][0]['source_version'], media['source_version'])
        self.assertEqual(result['image_input_evidence']['images'][0]['sha256'], hashlib.sha256(data).hexdigest())
        self.assertIn(base64.b64encode(data).decode(), json.dumps(requests[0]))
        self.assertEqual(result['reply_sources'], [row['source_id']])
        from contextlib import nullcontext
        self.service.mode = 'live'
        self.service._model_config_valid = True
        self.service._wa_enabled = True
        params = {'account_id': 'wa-account', 'connection_epoch': 'epoch-1', 'chat_id': 'contact',
                  'pending_message_id': 'model-image', 'fingerprint': self.service._wa_fingerprint('contact'),
                  'model_snapshot': self.service._freeze_reception_model(('text', 'structure', 'single_image', 'combined')),
                  'context': context}
        change_source[0] = True
        with patch.object(self.service, '_ensure_identity'), patch.object(self.service, '_job_phase', return_value=nullcontext()):
            with self.assertRaises(ServiceError):
                self.service._task_whatsapp_reply(params, 'stale-job', 1, facade)
        self.assertFalse(self.service.repo.list('wa_operation'))
        with self.assertRaises(ServiceError):
            facade.generate_whatsapp(context, 'old-image-job')
        self.assertEqual(len(requests), 2)
        self.service.mode = 'demo'

    def test_image_transfer_rejects_bad_offset_and_epoch(self):
        self.setUpAccount()
        self.ingest('image-transfer', has_image=True)
        media = self.service.get_whatsapp_context('contact')['whatsapp_messages'][-1]['media']
        identity = dict(account_id='wa-account', connection_epoch='epoch-1', chat_id='contact', message_id='image-transfer', source_version=media['source_version'])
        transfer = self.service._wa_media_begin(**identity, mime_type='image/png')['transfer_id']
        with self.assertRaises(ServiceError):
            self.service._wa_media_chunk(**identity, transfer_id=transfer, offset=1, data_base64='YQ==')
        transfer = self.service._wa_media_begin(**identity, mime_type='image/png')['transfer_id']
        self.service._wa_set_account('wa-account', 'epoch-2', 'connected')
        with self.assertRaises(ServiceError):
            self.service._wa_media_chunk(**identity, transfer_id=transfer, offset=0, data_base64='YQ==')

    def test_old_image_version_cannot_be_claimed_by_new_epoch(self):
        self.setUpAccount()
        self.ingest('old-image', has_image=True)
        version = self.service.get_whatsapp_context('contact')['whatsapp_messages'][-1]['media']['source_version']
        self.service._wa_set_account('wa-account', 'epoch-2', 'connected')
        with self.assertRaises(ServiceError):
            self.service._wa_media_begin('wa-account', 'epoch-2', 'contact', 'old-image', version, 'image/png')

    def test_image_transfer_rejects_bad_digest_and_bad_image(self):
        self.setUpAccount()
        self.ingest('invalid-image', has_image=True)
        media = self.service.get_whatsapp_context('contact')['whatsapp_messages'][-1]['media']
        identity = dict(account_id='wa-account', connection_epoch='epoch-1', chat_id='contact', message_id='invalid-image', source_version=media['source_version'])
        for digest in ('0' * 64, hashlib.sha256(b'not an image').hexdigest()):
            transfer = self.service._wa_media_begin(**identity, mime_type='image/png')['transfer_id']
            self.service._wa_media_chunk(**identity, transfer_id=transfer, offset=0, data_base64=base64.b64encode(b'not an image').decode())
            with self.assertRaises(ServiceError):
                self.service._wa_media_finish(**identity, transfer_id=transfer, size=12, sha256=digest)
        self.assertEqual(self.service.get_whatsapp_context('contact')['whatsapp_messages'][-1]['media']['status'], 'pending')
        self.assertFalse(self.service.repo.list('wa_media_asset'))

    def test_batch_advances_only_on_exact_ack_and_preserves_early_echo(self):
        self.setUpAccount()
        with patch.object(self.service.integrations, 'generate_whatsapp', side_effect=self.generate_parts):
            self.service.set_whatsapp_reception(True)
            self.ingest('incoming')
            first = self.wait_op()
            self.assertEqual(first['body'], 'First')
            self.service._wa_prepare_send(first['operation_id'])
            self.ingest('sent-1', body='First', from_me=True, ack=1)
            self.service._wa_transport_result(first['operation_id'], 'wa-account', 'epoch-1', 'submitted', 'sent-1', 0)
            ops = self.service.repo.list('wa_operation')
            second = next(op for op in ops if op['body'] == 'Second')
            self.assertTrue(self.service.get_whatsapp_state()['conversations'][0]['pending'])
            self.assertFalse(self.service.get_whatsapp_state()['conversations'][0]['human_owned'])
            self.service._wa_prepare_send(second['operation_id'])
            self.service._wa_transport_result(second['operation_id'], 'wa-account', 'epoch-1', 'unknown')
            self.assertEqual(len(self.service.repo.list('wa_operation')), 2)
            progress = self.service.get_whatsapp_state()['reply_batches'][0]
            self.assertEqual((progress['origin'], progress['channel'], progress['phase']), ('automatic', 'whatsapp', 'reply'))
            self.assertEqual(progress['chat_id'], 'contact')
            self.assertIsInstance(progress['created_at'], int)
            self.assertIsInstance(progress['updated_at'], int)
            self.assertEqual(progress['parts'], [
                {'part_index': 0, 'status': 'accepted', 'message_id': 'sent-1'},
                {'part_index': 1, 'status': 'unknown', 'message_id': None},
                {'part_index': 2, 'status': 'planned', 'message_id': None}])
            self.assertEqual(progress['pending_operation_ids'], [second['operation_id']])
            self.assertNotIn('body', str(progress))
            self.service._wa_transport_result(second['operation_id'], 'wa-account', 'epoch-1', 'submitted', 'sent-2', 1)
            third = next(op for op in self.service.repo.list('wa_operation') if op['body'] == 'Third')
            self.service._wa_prepare_send(third['operation_id'])
            self.service._wa_transport_result(third['operation_id'], 'wa-account', 'epoch-1', 'submitted', 'sent-3', 1)
            batch = self.service.repo.list('wa_reply_batch')[0]
            self.assertEqual(batch['status'], 'accepted')
            self.assertFalse(self.service.get_whatsapp_state()['conversations'][0]['pending'])

    def test_new_account_keeps_old_unknown_visible_without_exposing_contacts(self):
        self.setUpAccount()
        self.service.repo.put('wa_operation', 'old-unknown', {'operation_id': 'old-unknown',
            'seller_id': self.service.seller_id, 'account_id': 'wa-account', 'connection_epoch': 'epoch-1',
            'chat_id': 'private-old-contact', 'status': 'unknown', 'message_id': 'exact-old-id', 'ack': 0})
        self.service._wa_set_account('different-account', 'epoch-2', 'connected')
        state = self.service.get_whatsapp_state()
        self.assertEqual(state['pending_operations'], [])
        self.assertEqual(state['other_account_pending_count'], 1)
        self.assertEqual(state['other_account_pending_operations'], [{'operation_id': 'old-unknown', 'status': 'unknown'}])
        self.assertNotIn('private-old-contact', str(state))
        original = self.service.repo.get('wa_operation', 'old-unknown')
        self.assertEqual(original['connection_epoch'], 'epoch-1')
        self.assertEqual(original['status'], 'unknown')
        self.assertFalse(state['enabled'])

    def test_new_buyer_message_stops_prepared_tail_and_keeps_new_pending(self):
        self.setUpAccount()
        with patch.object(self.service.integrations, 'generate_whatsapp', side_effect=self.generate_parts):
            self.service.set_whatsapp_reception(True)
            self.ingest('incoming')
            first = self.wait_op()
            self.service._wa_prepare_send(first['operation_id'])
            self.service._wa_transport_result(first['operation_id'], 'wa-account', 'epoch-1', 'submitted', 'sent-1', 1)
            batch = self.service.repo.list('wa_reply_batch')[0]
            second_id = batch['parts'][1]['operation_ids'][0]
            self.ingest('new-incoming')
            batch = self.service.repo.get('wa_reply_batch', batch['batch_id'])
            self.assertEqual(batch['status'], 'stopped')
            self.assertEqual(self.service.repo.get('wa_operation', second_id)['status'], 'cancelled')
            contact = self.service.repo.get('wa_conversation', self.service._wa_contact_key('contact'))
            self.assertEqual(contact['pending_message_id'], 'new-incoming')
            self.assertTrue(contact['pending'])

    def test_restart_stops_batch_and_preserves_unknown(self):
        from pathlib import Path
        from reception_service.service import ReceptionService
        self.setUpAccount()
        with patch.object(self.service.integrations, 'generate_whatsapp', side_effect=self.generate_parts):
            self.service.set_whatsapp_reception(True)
            self.ingest('incoming')
            first = self.wait_op()
            self.service._wa_prepare_send(first['operation_id'])
        self.service.close()
        self.service = ReceptionService(Path(self.tmp.name))
        batch = self.service.repo.list('wa_reply_batch')[0]
        self.assertEqual(batch['status'], 'stopped')
        self.assertEqual([p['status'] for p in batch['parts']], ['unknown', 'cancelled', 'cancelled'])
        self.assertEqual(self.service.repo.get('wa_operation', first['operation_id'])['status'], 'unknown')

    def test_external_reply_during_unknown_stops_tail(self):
        self.setUpAccount()
        with patch.object(self.service.integrations, 'generate_whatsapp', side_effect=self.generate_parts):
            self.service.set_whatsapp_reception(True)
            self.ingest('incoming')
            op = self.wait_op()
            self.service._wa_prepare_send(op['operation_id'])
            self.ingest('human', body='Manual', from_me=True)
            self.service._wa_transport_result(op['operation_id'], 'wa-account', 'epoch-1', 'submitted', 'sent-1', 1)
            batch = self.service.repo.list('wa_reply_batch')[0]
            self.assertEqual(batch['status'], 'stopped')
            self.assertEqual([p['status'] for p in batch['parts']], ['accepted', 'cancelled', 'cancelled'])
            self.assertTrue(self.service.get_whatsapp_state()['conversations'][0]['human_owned'])
            self.assertEqual(len(self.service.repo.list('wa_operation')), 1)

    def test_binding_change_before_first_submit_replans_same_pending_message(self):
        self.setUpAccount()
        with patch.object(self.service.integrations, 'generate_whatsapp', side_effect=self.generate_parts):
            self.service.set_whatsapp_reception(True)
            self.ingest('binding-pending')
            original = self.wait_op()
            self.service.save_whatsapp_binding('contact', 'demo-emma', 0)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = [row for row in self.service.repo.list('wa_operation') if row['operation_id'] != original['operation_id']]
                if rows:
                    break
                threading.Event().wait(.01)
            self.assertTrue(rows, self.service.list_jobs())
            self.assertEqual(self.service.repo.get('wa_operation', original['operation_id'])['status'], 'cancelled')
            self.assertEqual(rows[0]['status'], 'prepared')
            contact = self.service._wa_contact('contact')
            self.assertTrue(contact['pending'])
            self.assertEqual(contact['pending_message_id'], 'binding-pending')

    def test_unbound_inbound_generates_and_ack_requires_exact_server_id(self):
        self.setUpAccount()
        self.service.set_whatsapp_reception(True)
        before = self.service.repo.list('case')
        self.ingest('in-1')
        op = self.wait_op()
        self.assertEqual(op['status'], 'prepared')
        self.assertEqual(self.service.repo.list('case'), before)
        self.assertIsNone(self.service.get_whatsapp_state()['bindings'][0]['conversation_id'])
        frozen = self.service._wa_prepare_send(op['operation_id'])
        self.assertEqual(frozen['chat_id'], 'contact')
        with self.assertRaises(ServiceError):
            self.service._wa_prepare_send(op['operation_id'])
        self.ingest('out-1', body='auto', from_me=True)
        self.assertFalse(self.service.get_whatsapp_state()['conversations'][0]['human_owned'])
        result = self.service._wa_transport_result(op['operation_id'], 'wa-account', 'epoch-1', 'submitted', 'out-1', 0)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.service._wa_ack('wa-account', 'epoch-1', 'other', 1), {'matched': False})
        self.assertEqual(self.service._wa_ack('wa-account', 'epoch-1', 'out-1', 1)['status'], 'accepted')
        self.assertFalse(self.service.get_whatsapp_state()['conversations'][0]['human_owned'])

    def test_context_budget_prioritizes_whatsapp_and_includes_required_old_evidence(self):
        from reception_service.model_payload import model_json
        case = self.demand()
        with self.service._lock, self.service.repo.transaction():
            stored = self.service.repo.get('case', case['case_id'])
            stored['requirements']['quantity']['evidence_ids'] = ['old-evidence']
            self.service.repo.put('case', case['case_id'], stored)
            owner = self.service.repo.get('conversation', 'demo-emma')
            for index in range(24):
                mid = 'old-evidence' if index == 0 else 'synthetic-' + str(index)
                self.service.repo.put('message', mid, {'message_id': mid, 'conversation_id': 'demo-emma',
                    'seller_id': self.service.seller_id, 'buyer_id': owner['buyer_id'], 'role': 'buyer',
                    'timestamp_ms': index + 1, 'text': 'i' * 3000})
        self.setUpAccount()
        self.ingest('metadata', body='', metadata_only=True)
        self.service.save_whatsapp_binding('contact', 'demo-emma', 0)
        for index in range(30):
            self.ingest('wa-' + str(index), body='w' * 3000, is_history=True)
        context = self.service.get_whatsapp_context('contact')
        self.assertEqual(len(context['whatsapp_messages']), 30)
        self.assertLessEqual(len(model_json(context)), 120000)
        self.assertIn('old-evidence', [m['message_id'] for m in context['international_background']['messages']])

    def test_saving_settings_revokes_whatsapp_and_cancels_prepared(self):
        self.setUpAccount()
        self.service.set_whatsapp_reception(True)
        self.ingest('incoming')
        op = self.wait_op()
        self.service.save_settings(probe_seconds=3)
        self.assertFalse(self.service.get_whatsapp_state()['enabled'])
        self.assertEqual(self.service.repo.get('wa_operation', op['operation_id'])['status'], 'cancelled')
        with self.assertRaises(ServiceError):
            self.service._wa_prepare_send(op['operation_id'])

    def test_full_queue_does_not_discard_incoming_message(self):
        self.setUpAccount()
        self.service.set_whatsapp_reception(True)
        with patch.object(self.service._queue, 'put', side_effect=ServiceError('task_queue_full', 'busy')):
            self.ingest('incoming')
        self.assertEqual(len(self.service.repo.list('wa_message')), 1)
        self.assertTrue(self.service.get_whatsapp_state()['conversations'][0]['pending'])
        with self.service._lock, self.service.repo.transaction():
            self.service._schedule_whatsapp()
        self.assertEqual(self.wait_op()['chat_id'], 'contact')

    def test_live_generation_uses_seller_identity_without_international_target(self):
        from reception_service.integrations.live import LiveIntegration
        from reception_service.model_payload import project_model_payload
        facade = object.__new__(LiveIntegration)
        context = {'task_mode': 'whatsapp_reply', 'seller_id': '123', 'whatsapp_messages': [
            {'source_id': 'wa-source', 'role': 'buyer', 'text': 'Hello', 'timestamp_ms': 1}]}
        self.assertEqual(project_model_payload(context)['whatsapp_messages'], context['whatsapp_messages'])
        with patch.object(facade, 'resolve_identity', return_value='123') as identity, \
                patch.object(facade, '_assistance_query', return_value={'reply_messages': [{'body': 'Hello',
                    'reply_sources': ['wa-source']}], 'wa_summary': '客户问候。', '_host_model_result': {}}):
            output = facade.generate_whatsapp(context, 'job')
        identity.assert_called_once_with('123')
        self.assertEqual(output['reply_sources'], ['wa-source'])

    def test_ack_arriving_before_transport_result_is_preserved(self):
        self.setUpAccount()
        self.service.set_whatsapp_reception(True)
        self.ingest('incoming')
        op = self.wait_op()
        self.service._wa_prepare_send(op['operation_id'])
        self.assertFalse(self.service._wa_ack('wa-account', 'epoch-1', 'exact-id', 1)['matched'])
        result = self.service._wa_transport_result(op['operation_id'], 'wa-account', 'epoch-1', 'submitted', 'exact-id', 0)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['ack'], 1)

    def test_from_me_echo_ack_before_transport_result_is_preserved(self):
        self.setUpAccount()
        self.service.set_whatsapp_reception(True)
        self.ingest('incoming')
        op = self.wait_op()
        self.service._wa_prepare_send(op['operation_id'])
        self.ingest('exact-id', body='auto', from_me=True, ack=1)
        result = self.service._wa_transport_result(op['operation_id'], 'wa-account', 'epoch-1', 'submitted', 'exact-id', 0)
        self.assertEqual(result['status'], 'accepted')
        self.assertFalse(self.service.get_whatsapp_state()['conversations'][0]['human_owned'])

    def test_restart_keeps_unknown_and_revokes_grant(self):
        from pathlib import Path
        from reception_service.service import ReceptionService
        self.setUpAccount()
        self.service.set_whatsapp_reception(True)
        self.ingest('incoming')
        op = self.wait_op()
        self.service._wa_prepare_send(op['operation_id'])
        self.service.close()
        self.service = ReceptionService(Path(self.tmp.name))
        state = self.service.get_whatsapp_state()
        self.assertFalse(state['connected'])
        self.assertFalse(state['enabled'])
        self.assertEqual(self.service.repo.get('wa_operation', op['operation_id'])['status'], 'unknown')
        self.service._wa_set_account('wa-account', 'epoch-2', 'connected')
        self.assertEqual(self.service.reconcile_whatsapp_operation(op['operation_id'])['status'], 'unknown')
        self.assertFalse(self.service.get_whatsapp_state()['enabled'])

    def test_metadata_history_and_group_do_not_trigger_reply(self):
        self.setUpAccount()
        self.service.set_whatsapp_reception(True)
        self.ingest('metadata-1', body='', metadata_only=True)
        self.ingest('history-1', is_history=True)
        self.ingest('group-1', chat='group', is_group=True)
        self.assertEqual(len(self.service.get_whatsapp_state()['conversations']), 1)
        self.assertFalse(self.service.get_whatsapp_state()['conversations'][0]['pending'])
        self.assertEqual(self.service.repo.list('job'), [])

    def test_binding_context_readonly_and_compare_and_swap(self):
        case = self.demand()
        before = self.service.repo.get('case', case['case_id'])
        self.setUpAccount()
        self.ingest('metadata', body='', metadata_only=True)
        binding = self.service.save_whatsapp_binding('contact', 'demo-emma', 0)
        self.assertEqual(binding['revision'], 1)
        context = self.service.get_whatsapp_context('contact')
        self.assertEqual(context['international_background']['requirements']['quantity']['value'], '100件')
        self.assertNotIn('product_url', context['international_background']['requirements'])
        with self.assertRaises(ServiceError):
            self.service.save_whatsapp_binding('contact', None, 0)
        self.assertEqual(self.service.repo.get('case', case['case_id']), before)
        self.assertEqual(self.service.save_whatsapp_binding('contact', None, 1)['revision'], 2)
        self.assertNotIn('international_background', self.service.get_whatsapp_context('contact'))

    def test_external_message_takes_over_and_reconnect_revokes(self):
        self.setUpAccount()
        self.service.set_whatsapp_reception(True)
        self.ingest('human', body='', from_me=True)
        self.assertTrue(self.service.get_whatsapp_state()['conversations'][0]['human_owned'])
        self.service._wa_set_account('wa-account', 'epoch-2', 'connected')
        self.assertFalse(self.service.get_whatsapp_state()['enabled'])
        with self.assertRaises(ServiceError):
            self.ingest('old-epoch')

    def test_changed_background_invalidates_prepared_send(self):
        self.demand()
        self.setUpAccount()
        self.ingest('meta', body='', metadata_only=True)
        self.service.save_whatsapp_binding('contact', 'demo-emma', 0)
        self.service.set_whatsapp_reception(True)
        self.ingest('incoming')
        op = self.wait_op()
        with self.service._lock, self.service.repo.transaction():
            row = self.service.repo.get('conversation_supplement', 'demo-emma') or {'conversation_id': 'demo-emma', 'revision': 0}
            row.update(body='changed', revision=(row.get('revision') or 0) + 1)
            self.service.repo.put('conversation_supplement', 'demo-emma', row)
        with self.assertRaises(ServiceError):
            self.service._wa_prepare_send(op['operation_id'])
        self.assertEqual(self.service.repo.get('wa_operation', op['operation_id'])['status'], 'cancelled')

    def test_stale_account_controls_cannot_authorize_or_change_another_connection(self):
        self.setUpAccount()
        self.ingest('metadata', body='', metadata_only=True)
        self.service._wa_set_account('wa-account', 'epoch-2', 'connected')
        stale = {'expected_account_id': 'wa-account', 'expected_connection_epoch': 'epoch-1'}
        for command in (
            lambda: self.service.set_whatsapp_reception(True, **stale),
            lambda: self.service.save_whatsapp_binding('contact', None, 0, **stale),
            lambda: self.service.takeover_whatsapp('contact', True, **stale),
        ):
            with self.assertRaises(ServiceError):
                command()
        self.assertFalse(self.service.get_whatsapp_state()['enabled'])
        self.assertFalse(self.service.get_whatsapp_state()['conversations'][0]['human_owned'])

    def test_binding_change_cancels_inflight_generation(self):
        self.setUpAccount()
        self.ingest('metadata', body='', metadata_only=True)
        entered, release = threading.Event(), threading.Event()
        original = self.service.integrations.generate_whatsapp
        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        try:
            with patch.object(self.service.integrations, 'generate_whatsapp', side_effect=slow):
                self.service.set_whatsapp_reception(True)
                self.ingest('incoming')
                self.assertTrue(entered.wait(2))
                self.service.save_whatsapp_binding('contact', 'demo-emma', 0)
                release.set()
                deadline = time.monotonic() + 3
                while self.service._generation_active and time.monotonic() < deadline:
                    threading.Event().wait(.01)
                self.assertFalse(self.service.repo.list('wa_operation'))
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
