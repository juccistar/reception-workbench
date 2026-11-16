"""Synthetic selection-to-wire checks using real context, router and transport.

Only the HTTP provider is replaced. Dedicated cases also cover explicitly bound
supplement inputs and independent account generation without a real service.
"""
import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

import httpx
from PIL import Image

from reception_service.image_assets import decode_image
from reception_service.integrations.live import LiveIntegration
from reception_service.integrations.reception_prompts import AUTOMATIC, MANUAL, POLISH, REQUIREMENTS, WHATSAPP, IMAGE_NATIVE
from reception_service.model_providers.transport import ProviderTransport
from reception_service.model_router import ModelRouter
from reception_service.photos import direct_image_id, photo_version
from reception_service.requirement_media import _media_id
from reception_service.whatsapp_account_generation import AccountGenerationMixin


class NativeImageRequests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stream = io.BytesIO()
        Image.new('RGBA', (3, 2), (255, 255, 255, 0)).save(stream, format='PNG')
        self.original = stream.getvalue()
        self.asset = decode_image(self.original, 'image/png')
        self.requests = []
        self.answer = {'reply_messages': [{'body': 'Hello', 'reply_sources': []}], 'wa_summary': '问候'}
        self.router = ModelRouter()
        self.addCleanup(self.router.close)
        self.facade = LiveIntegration.__new__(LiveIntegration)
        self.facade.settings = {}
        self.facade._query_state = threading.local()
        self.facade.configure_model_router(self.router)

    def configure(self, protocol):
        caps = {name: {'status': 'passed', 'checked_at': 1} for name in
                ('text', 'structure', 'single_image', 'multi_image', 'combined')}
        settings = {'format': 'model-settings-v1', 'directory_revision': 1,
                    'profiles': [{'profile_id': 'p', 'protocol': protocol, 'base_url': 'https://example.com/v1',
                                  'model': 'test', 'credential_ref': 'c', 'has_key': True, 'config_revision': 1,
                                  'credential_revision': 1, 'capability_revision': 1, 'capabilities': caps}],
                    'routes': {'reception': {'profile_id': 'p', 'route_revision': 1},
                               'translation': {'profile_id': None, 'route_revision': 0}}}
        path = Path(self.tmp.name) / 'settings.json'
        path.write_text(json.dumps(settings), encoding='utf8')
        self.router.configure({'format': 'model-runtime-v1', 'authority_file': str(path),
                               'settings': settings, 'credentials': {'p': 'test-key'}})
        def respond(request):
            self.requests.append(json.loads(request.content))
            answer = json.dumps(self.answer)
            if protocol == 'anthropic':
                return httpx.Response(200, json={'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                    'model': 'test', 'content': [{'type': 'text', 'text': answer}], 'stop_reason': 'end_turn',
                    'stop_sequence': None, 'usage': {'input_tokens': 2, 'output_tokens': 3}})
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': answer}}], 'model': 'test'})
        self.router._transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(respond))

    def fixture(self):
        message = dict(seller_id='s', buyer_id='b', conversation_id='c', message_id='m', role='buyer',
                       own=False, text='[image]', timestamp_ms=1, attachments=[])
        ref = {k: message[k] for k in ('seller_id', 'buyer_id', 'conversation_id', 'message_id', 'role')}
        ref.update(source_version=photo_version(message), image_sha256=self.asset['sha256'],
                   image_id=direct_image_id(message, self.asset['sha256']))
        context = {'case': {'seller_id': 's', 'buyer_id': 'b', 'conversation_id': 'c'},
                   'message_view': [message], 'photo_context': {'direct_images': [ref]},
                   'image_manifest': [{'image_id': ref['image_id'], 'alias': 'p1'}]}
        return context, [{'message': message, 'asset': self.asset}], ref

    def inspect(self, protocol, expected_manifest, attached=True, *, prompt=None):
        body = self.requests[-1]
        if protocol == 'anthropic':
            system, blocks = body['system'], body['messages'][0]['content']
        else:
            system, blocks = body['messages'][0]['content'], body['messages'][1]['content']
        envelope = json.loads(blocks[0]['text'])
        self.assertEqual(list(envelope)[-1], 'context')
        payload = envelope['context']
        if prompt is not None:
            self.assertEqual(system, prompt + ('\n' + IMAGE_NATIVE if attached else ''))
        if attached:
            self.assertIn(IMAGE_NATIVE, system)
            self.assertEqual(payload['image_transport'], 'inline')
            self.assertEqual(payload['image_manifest'], expected_manifest)
            image = blocks[1]
            encoded = image['source']['data'] if protocol == 'anthropic' else image['image_url']['url'].split(',', 1)[1]
            self.assertEqual(hashlib.sha256(base64.b64decode(encoded)).hexdigest(), hashlib.sha256(self.original).hexdigest())
            self.assertEqual(base64.b64decode(encoded), self.original)
            self.assertEqual(len(blocks), 2)
        else:
            self.assertNotIn(IMAGE_NATIVE, system)
            self.assertNotIn('image_manifest', payload)
            self.assertEqual(len(blocks), 1)
        return payload

    def test_reviewed_native_text_matches_generated_constant(self):
        root = Path(__file__).resolve().parents[3]
        text = (root / 'docs' / '接待提示词审核稿.md').read_text(encoding='utf8')
        block = text.split('### P05-A：', 1)[1].split('```text\n', 1)[1].split('\n```', 1)[0]
        self.assertEqual(IMAGE_NATIVE, block.strip() + '\n')

    def test_business_requests_and_text_only(self):
        for protocol in ('chat-completions', 'anthropic'):
            with self.subTest(protocol=protocol):
                self.configure(protocol)
                for mode, prompt in [('automatic', AUTOMATIC), ('manual', MANUAL), ('requirements', REQUIREMENTS)]:
                    context, inputs, ref = self.fixture()
                    context['task_mode'] = mode
                    self.facade._business_query(prompt, context, inputs)
                    payload = self.inspect(protocol, [{'alias': 'p1', 'image_id': ref['image_id'], 'role': 'buyer', 'message_id': 'm', 'media_id': _media_id('customer_image', 'm')}], prompt=prompt)
                    self.assertEqual(payload['message_view'][0]['text'], '[image]')
                context, _, _ = self.fixture()
                context.update(photo_context={}, image_manifest=[])
                self.facade._business_query(MANUAL, context, [])
                payload = self.inspect(protocol, None, attached=False, prompt=MANUAL)
                self.assertEqual(payload['message_view'][0]['media_notice'], 'image_not_available_to_this_request')

    def test_selected_message_original_reaches_the_same_native_request(self):
        from reception_service.message_photos import MessagePhotosMixin
        from reception_service.reception_context import shared_context
        from reception_service.repository import Repository, encoded
        for protocol in ('chat-completions', 'anthropic'):
            self.configure(protocol)
            for mode, prompt, has_case in [('manual', MANUAL, False), ('manual', MANUAL, True),
                                          ('automatic', AUTOMATIC, True), ('requirements', REQUIREMENTS, True)]:
                with self.subTest(protocol=protocol, mode=mode, has_case=has_case):
                    repo = Repository(None, 's')
                    self.addCleanup(repo.close)
                    owner = {'seller_id': 's', 'buyer_id': 'b', 'conversation_id': 'c',
                             'case_id': 'active' if has_case else None}
                    repo.put('conversation', 'c', owner)
                    message = dict(seller_id='s', buyer_id='b', conversation_id='c', message_id='m',
                                   role='buyer', own=False, text='[image]', timestamp_ms=1, attachments=[])
                    repo.put('message', encoded(['c', 'm']), message)
                    repo.put('image_asset', self.asset['sha256'], self.asset)
                    repo.put('message_image', encoded(['c', 'm']), {
                        **{key: message[key] for key in ('seller_id', 'buyer_id', 'conversation_id', 'message_id')},
                        'source_version': photo_version(message), 'sha256': self.asset['sha256'], 'status': 'ready'})
                    case = None
                    if has_case:
                        case = {**owner, 'revision': 1, 'state': 'collecting', 'requirements_format': 'ai-v1',
                                'requirements': {'customization': {'value': '按买家原图定制', 'evidence_ids': ['m']}}}
                        repo.put('case', 'active', case)
                        repo.put('case', 'old', {**case, 'case_id': 'old', 'state': 'stopped', 'requirements': {}})
                    context = shared_context(repo, owner, case, task_mode=mode)
                    if case:
                        context['case'] = case
                    class PhotoOwner(MessagePhotosMixin):
                        account_verified = True
                        seller_id = 's'
                        _lock = threading.RLock()
                        def _require(self, kind, key):
                            value = self.repo.get(kind, key)
                            if value is None:
                                raise AssertionError('missing synthetic record')
                            return value
                    photo_owner = PhotoOwner()
                    photo_owner.repo = repo
                    inputs = photo_owner._direct_photo_inputs(context)
                    self.facade._business_query(prompt, context, inputs)
                    ref = context['photo_context']['direct_images'][0]
                    payload = self.inspect(protocol, [{'alias': 'p1', 'image_id': ref['image_id'], 'role': 'buyer', 'message_id': 'm', 'media_id': _media_id('customer_image', 'm')}], prompt=prompt)
                    self.assertEqual(payload['message_view'][0]['text'], '[image]')
                    self.assertEqual(payload['message_view'][0]['image_refs'], [{'alias': 'p1', 'image_id': ref['image_id']}])
                    self.assertNotIn('media_notice', payload['message_view'][0])

    def test_auto_and_manual_validate_real_image_input_and_source_guards(self):
        from unittest.mock import patch
        from reception_service.errors import ServiceError
        from reception_service.reception_context import add_image_references
        from reception_service.requirement_changes import validate_changes
        from reception_service.photos import with_direct_photo_reads

        self.configure('chat-completions')
        self.facade.resolve_identity = lambda seller: seller
        context, inputs, ref = self.fixture()
        message = inputs[0]['message']
        buyer_text = {**message, 'message_id': 'm2', 'text': 'I need blue glasses', 'timestamp_ms': 2}
        context.update(seller_id='s', buyer_id='b', conversation_id='c', recent_messages=[message, buyer_text],
                       historical_evidence=[], case_catalog=[], knowledge=[], task_mode='automatic')
        context['case'].update(state='collecting', procurement_key='main')
        context['photo_context']['mode'] = 'direct'
        add_image_references(context)
        self.answer = {'action': 'ask', 'reply_messages': [{'body': 'Noted.', 'reply_sources': ['m2']}],
            'requirement_changes': [{'action': 'create', 'case_ref': 'new:1', 'trigger_evidence_ids': ['m2'],
                'set_fields': {'product': {'value': '蓝色眼镜', 'evidence_ids': ['m2'],
                                           'source_excerpts': {'m2': 'blue glasses'}},
                               'product_images': {'value': [_media_id('customer_image', 'm')],
                                                  'evidence_ids': ['m'], 'source_excerpts': {}}}}]}
        with patch('reception_service.integrations.live.target'):
            for mode in ('automatic', 'manual'):
                context['task_mode'] = mode
                if mode == 'automatic':
                    result = self.facade.generate(context, 'synthetic-auto', photo_inputs=inputs)
                else:
                    result = self.facade.generate_manual(context, 'synthetic-manual', photo_inputs=inputs)
                self.assertTrue(result['requirement_changes'])
                blocks = self.requests[-1]['messages'][1]['content']
                payload = json.loads(blocks[0]['text'])['context']
                self.assertEqual(payload['message_view'][0]['text'], '[image]')
                self.assertEqual(payload['message_view'][0]['image_refs'][0]['alias'], 'p1')
                self.assertEqual(payload['image_manifest'][0]['media_id'], _media_id('customer_image', 'm'))
        checked = with_direct_photo_reads(context, [ref])
        import copy
        # A text-bearing image also keeps the exact source body in the view.
        text_context = copy.deepcopy(context)
        text_context['recent_messages'][0]['text'] = 'blue glasses'
        add_image_references(text_context)
        from reception_service.model_payload import project_model_payload
        text_view = project_model_payload(text_context)['message_view'][0]
        self.assertEqual(text_view['text'], 'blue glasses')
        self.assertEqual(text_view['image_refs'][0]['alias'], 'p1')
        forged = copy.deepcopy(self.answer)
        forged['requirement_changes'][0]['set_fields']['product']['source_excerpts']['m2'] = 'blue glasses\n[图片 p1]'
        with self.assertRaises(ServiceError):
            validate_changes(forged, checked)
        with self.assertRaises(ServiceError):
            validate_changes(self.answer, with_direct_photo_reads(context, []))
        foreign = copy.deepcopy(self.answer)
        foreign['requirement_changes'][0]['set_fields']['product_images']['value'] = [_media_id('customer_image', 'foreign')]
        with self.assertRaises(ServiceError):
            validate_changes(foreign, checked)
        foreign_source = copy.deepcopy(self.answer)
        foreign_source['requirement_changes'][0]['set_fields']['product']['evidence_ids'] = ['foreign-message']
        foreign_source['requirement_changes'][0]['set_fields']['product']['source_excerpts'] = {'foreign-message': 'blue glasses'}
        with self.assertRaises(ServiceError):
            validate_changes(foreign_source, checked)

    def test_polish_only_sends_supplement(self):
        for protocol in ('chat-completions', 'anthropic'):
            with self.subTest(protocol=protocol):
                self.configure(protocol)
                context, inputs, _ = self.fixture()
                attachment = dict(id='supp', origin='business_supplement', seller_id='s', buyer_id='b', conversation_id='c', sha256=self.asset['sha256'])
                ref = dict(origin='business_supplement', attachment_id='supp', image_id='supp', image_sha256=self.asset['sha256'], seller_id='s', buyer_id='b', conversation_id='c', supplement_revision=1)
                context.update(task_mode='polish', supplement_image_refs=[ref], unsent_reply={'body': 'Hello'})
                context['image_manifest'].append({'alias': 'p2', 'image_id': 'supp', 'origin': 'business_supplement'})
                inputs.append({'attachment': attachment, 'asset': self.asset})
                self.facade._business_query(POLISH, context, inputs)
                payload = self.inspect(protocol, [{'alias': 'p2', 'image_id': 'supp', 'role': 'seller', 'origin': 'business_supplement', 'attachment_id': 'supp'}], prompt=POLISH)
                self.assertEqual(payload['message_view'][0]['media_notice'], 'image_not_available_to_this_request')

    def test_independent_whatsapp_request(self):
        for protocol in ('chat-completions', 'anthropic'):
            with self.subTest(protocol=protocol):
                self.configure(protocol)
                class Repo:
                    lock = threading.RLock()
                    def __init__(self):
                        self.rows = {}
                        self.db = self
                    def get(self, kind, key, default=None): return self.rows.get((kind, key), default)
                    def put(self, kind, key, value): self.rows[kind, key] = value
                    def execute(self, sql): pass
                class Host(AccountGenerationMixin):
                    def _generation_current(self, job): return True
                    def get_whatsapp_context(self, chat, message_count=50):
                        assert message_count == 50
                        return {'task_mode': 'whatsapp_reply', 'whatsapp_messages': [{'source_id': 'wa1', 'role': 'buyer'}]}
                    def _account_resolve_image(self, *args): return asset
                    def _account_key(self, *args): return ':'.join(args)
                    def _account_create_part(self, batch): return 'op'
                    def _schedule_account_replies(self): pass
                    def _changed(self): pass
                    def emit(self, event): pass
                asset = self.asset
                host = Host()
                host.repo, host.router = Repo(), self.router
                host._init_account_generation()
                ref = dict(source_id='wa1', source_version='v1', image_id='wa-image', sha256=asset['sha256'], role='buyer')
                job = dict(chat_id='chat', account_id='account', connection_epoch=1, source_key='key',
                           refs=[ref], statuses=[{'source_id': 'wa1', 'media': {'source_version': 'v1'}}],
                           merchant_context={'context': {}, 'seller_id': 's', 'revision': 1, 'instance_id': 'inst'},
                           cancel=threading.Event(), merchant_id='merchant', segment_id='seg', binding_revision=1,
                           pending_message_id='wa1', source_fingerprint='fp', message_count=50)
                host._run_account_generation(job)
                self.assertEqual(host.repo.rows['generation_attempt', 'key']['status'], 'completed')
                self.inspect(protocol, [{'alias': 'p1', 'image_id': 'wa-image', 'source_id': 'wa1', 'role': 'buyer'}], prompt=WHATSAPP)


if __name__ == '__main__':
    unittest.main()
