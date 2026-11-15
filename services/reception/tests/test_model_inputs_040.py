import base64
import io
import unittest
from unittest.mock import Mock

from PIL import Image

from reception_service.errors import ServiceError
from reception_service.image_assets import decode_image
from reception_service.model_media import prepare_model_images
from reception_service.photos import direct_image_id, photo_version
from reception_service.integrations.live import LiveIntegration


def fixture():
    stream = io.BytesIO()
    Image.new('RGB', (2, 3), 'red').save(stream, format='PNG')
    asset = decode_image(stream.getvalue(), 'image/png')
    message = dict(seller_id='s', buyer_id='b', conversation_id='c', message_id='m', role='buyer', own=False, text='[image]', timestamp_ms=1, attachments=[])
    ref = {k: message[k] for k in ('seller_id', 'buyer_id', 'conversation_id', 'message_id', 'role')}
    ref.update(source_version=photo_version(message), image_sha256=asset['sha256'], image_id=direct_image_id(message, asset['sha256']))
    context = {'photo_context': {'direct_images': [ref]}, 'image_manifest': [{'image_id': ref['image_id'], 'alias': 'p1'}]}
    return context, [{'message': message, 'asset': asset}]


class ModelInputTests(unittest.TestCase):
    def test_context_budget_error_remains_distinct_from_encoding_error(self):
        from reception_service.model_payload import model_json, MODEL_CONTEXT_CHARS
        with self.assertRaises(ServiceError) as too_long:
            model_json({'task_mode': 'whatsapp_reply', 'whatsapp_messages': [{'text': 'a' * MODEL_CONTEXT_CHARS}]})
        self.assertEqual(too_long.exception.code, 'context_too_long')
        with self.assertRaises(ServiceError) as invalid:
            model_json({'task_mode': 'whatsapp_reply', 'whatsapp_messages': [{'text': object()}]})
        self.assertEqual(invalid.exception.code, 'invalid_context')

    def test_original_bytes_and_binding(self):
        context, inputs = fixture()
        images, refs = prepare_model_images(context, inputs)
        self.assertEqual(images[0]['data'], base64.b64decode(inputs[0]['asset']['original_b64']))
        self.assertEqual((images[0]['alias'], images[0]['role'], images[0]['source_version']), ('p1', 'buyer', refs[0]['source_version']))
        self.assertEqual((images[0]['width'], images[0]['height']), (2, 3))

    def test_source_change_and_missing_bytes_rejected(self):
        context, inputs = fixture()
        inputs[0]['message']['text'] = 'changed'
        with self.assertRaises(ServiceError):
            prepare_model_images(context, inputs)
        with self.assertRaises(ServiceError):
            prepare_model_images(context, [])

    def test_router_receives_one_multimodal_request(self):
        context, inputs = fixture()
        context['image_manifest'].append({'alias': 'p99', 'image_id': 'unattached', 'role': 'buyer'})
        facade = LiveIntegration.__new__(LiveIntegration)
        import threading
        facade._query_state = threading.local()
        facade.settings = {}
        router = Mock()
        images, _ = prepare_model_images(context, inputs)
        evidence = [{key: images[0][key] for key in ('alias', 'image_id', 'source_id', 'source_version', 'role', 'sha256')}]
        router.generate.return_value = {'output': {'reply': 'ok'}, 'model_snapshot': {'profile_id': 'p'}, 'image_input_evidence': {'status': 'completed', 'images': evidence}}
        facade.configure_model_router(router)
        output, refs = facade._business_query('rules', context, inputs)
        self.assertEqual(output['answer']['reply'], 'ok')
        self.assertEqual(len(refs), 1)
        self.assertEqual(router.generate.call_count, 1)
        args, kwargs = router.generate.call_args
        import json
        manifest = json.loads(args[2])['image_manifest']
        self.assertEqual([(row['alias'], row['image_id']) for row in manifest],
                         [(image['alias'], image['image_id']) for image in kwargs['images']])
        self.assertEqual(args[0], 'reception')
        self.assertEqual(kwargs['required_capabilities'], ['text', 'structure', 'single_image', 'combined'])
        self.assertNotIn('original_b64', args[2])
        self.assertIsInstance(kwargs['images'][0]['data'], bytes)
        router.generate.return_value['image_input_evidence'] = {'status': 'completed', 'images': []}
        with self.assertRaises(ServiceError):
            facade._business_query('rules', context, inputs)

    def test_polish_preserves_supplement_images_but_excludes_chat_photos(self):
        import json
        import threading
        context, inputs = fixture()
        asset = inputs[0]['asset']
        attachment = dict(id='supplement-image', origin='business_supplement', seller_id='s',
                          buyer_id='b', conversation_id='c', sha256=asset['sha256'])
        ref = dict(origin='business_supplement', attachment_id=attachment['id'], image_id=attachment['id'],
                   image_sha256=asset['sha256'], seller_id='s', buyer_id='b', conversation_id='c', supplement_revision=1)
        context.update(task_mode='polish', supplement_image_refs=[ref], unsent_reply={'body': 'Hello', 'revision': 1})
        context['image_manifest'].append({'alias': 'p2', 'image_id': attachment['id'], 'origin': 'business_supplement'})
        inputs.append({'attachment': attachment, 'asset': asset})
        facade = LiveIntegration.__new__(LiveIntegration)
        facade.settings = {}
        facade._query_state = threading.local()
        router = Mock()
        def generate(*args, **kwargs):
            images = kwargs['images']
            return {'output': {'reply': 'Hello'}, 'image_input_evidence': {'status': 'completed', 'images': [
                {key: image[key] for key in ('alias', 'image_id', 'source_id', 'source_version', 'role', 'sha256')}
                for image in images]}}
        router.generate.side_effect = generate
        facade.configure_model_router(router)
        _, read_refs = facade._business_query('polish', context, inputs)
        args, kwargs = router.generate.call_args
        self.assertEqual(read_refs, [ref])
        self.assertEqual([(image['image_id'], image['role']) for image in kwargs['images']], [('supplement-image', 'seller')])
        self.assertEqual(json.loads(args[2])['image_manifest'], [{'alias': 'p2', 'image_id': 'supplement-image',
                         'role': 'seller', 'origin': 'business_supplement', 'attachment_id': 'supplement-image'}])
        self.assertNotIn('original_b64', args[2])

    def test_whatsapp_resolver_binds_each_image_to_current_source(self):
        from reception_service.model_inputs import resolve_whatsapp_images
        _, inputs = fixture()
        asset = inputs[0]['asset']
        refs = [dict(source_id='wa1', source_version='v1', image_id='i1', sha256=asset['sha256'], role='buyer'),
                dict(source_id='wa2', source_version='v2', image_id='i2', sha256=asset['sha256'], role='seller')]
        context = {'model_image_refs': refs, 'whatsapp_messages': [
            {'source_id': 'wa1', 'role': 'buyer', 'media': {'source_version': 'v1'}},
            {'source_id': 'wa2', 'role': 'seller', 'media': {'source_version': 'v2'}}]}
        images, manifest = resolve_whatsapp_images(context, lambda ref: asset)
        self.assertEqual([i['alias'] for i in images], ['p1', 'p2'])
        self.assertEqual([i['role'] for i in images], ['buyer', 'seller'])
        self.assertEqual(len(manifest), 2)
        context['whatsapp_messages'][0]['media']['source_version'] = 'changed'
        with self.assertRaises(ServiceError):
            resolve_whatsapp_images(context, lambda ref: asset)
        with self.assertRaises(ServiceError):
            resolve_whatsapp_images({'model_image_refs': refs, 'whatsapp_messages': []}, lambda ref: asset)
        self.assertEqual(resolve_whatsapp_images({}, None), ([], []))

    def test_whatsapp_facade_checks_and_propagates_model_snapshot(self):
        import threading
        facade = LiveIntegration.__new__(LiveIntegration)
        facade._query_state = threading.local()
        facade.settings = {}
        facade.resolve_identity = lambda seller: seller
        router = Mock()
        router.generate.return_value = {'output': {'reply_messages': [{'body': 'Hello', 'reply_sources': []}], 'wa_summary': '问候'},
                                        'model_snapshot': {'profile_id': 'p'}, 'image_input_evidence': {'status': 'completed', 'images': []}}
        facade.configure_model_router(router)
        result = facade.generate_whatsapp({'task_mode': 'whatsapp_reply', 'seller_id': 's', 'whatsapp_messages': []}, 'r')
        self.assertEqual(result['reply'], 'Hello')
        self.assertEqual(result['model_snapshot'], {'profile_id': 'p'})
        router.generate.return_value['output'] = {'reply_messages': [], 'wa_summary': '问候'}
        with self.assertRaises(ServiceError):
            facade.generate_whatsapp({'task_mode': 'whatsapp_reply', 'seller_id': 's', 'whatsapp_messages': []}, 'r')

    def test_whatsapp_request_manifest_matches_attached_images(self):
        import json
        import threading
        _, inputs = fixture()
        asset = inputs[0]['asset']
        refs = [dict(source_id='wa1', source_version='v1', image_id='i1', sha256=asset['sha256'], role='buyer'),
                dict(source_id='wa2', source_version='v2', image_id='i2', sha256=asset['sha256'], role='seller')]
        context = {'task_mode': 'whatsapp_reply', 'model_image_refs': refs, 'whatsapp_messages': [
            {'source_id': 'wa1', 'role': 'buyer', 'media': {'source_version': 'v1'}},
            {'source_id': 'wa2', 'role': 'seller', 'media': {'source_version': 'v2'}}]}
        facade = LiveIntegration.__new__(LiveIntegration)
        facade._query_state = threading.local()
        facade.settings = {}
        facade.configure_whatsapp_image_resolver(lambda ref: asset)
        router = Mock()
        def generate(*args, **kwargs):
            evidence = [{key: image[key] for key in ('alias', 'image_id', 'source_id', 'source_version', 'role', 'sha256')}
                        for image in kwargs['images']]
            return {'output': {}, 'image_input_evidence': {'status': 'completed', 'images': evidence}}
        router.generate.side_effect = generate
        facade.configure_model_router(router)
        from reception_service.integrations.reception_prompts import WHATSAPP
        facade._business_query(WHATSAPP, context, [])
        args, kwargs = router.generate.call_args
        manifest = json.loads(args[2])['image_manifest']
        self.assertEqual(manifest, [{key: image[key] for key in ('alias', 'image_id', 'source_id', 'role')}
                                    for image in kwargs['images']])
        self.assertIn('wa_summary', args[1])
        self.assertNotIn('requirement_changes', args[1])
        self.assertIn('whatsapp_messages', args[1])
        self.assertNotIn('read_compat', args[1])

    def test_existing_photo_deadline_consumes_generation_budget(self):
        from unittest.mock import patch
        import threading
        facade = LiveIntegration.__new__(LiveIntegration)
        facade._query_state = threading.local()
        facade.settings = {'image_prepare_seconds': 60}
        router = Mock()
        router.generate.return_value = {'output': {}}
        facade.configure_model_router(router)
        with patch('reception_service.integrations.live.time.monotonic', return_value=1040):
            facade._business_query('rules', {}, [], photo_deadline=1060)
        self.assertEqual(router.generate.call_args.kwargs['deadline'], 1180)

    def test_router_failure_does_not_fallback(self):
        facade = LiveIntegration.__new__(LiveIntegration)
        import threading
        facade._query_state = threading.local()
        facade.settings = {}
        facade.configure_model_router(None)
        with self.assertRaises(ServiceError):
            facade._business_query('rules', {}, [])


if __name__ == '__main__':
    unittest.main()
