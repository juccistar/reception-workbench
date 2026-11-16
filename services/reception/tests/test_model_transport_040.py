import asyncio
import threading
import hashlib
import json
import base64
import io
import random
import unittest

from PIL import Image

import httpx

from reception_service.model_providers.transport import ProviderTransport


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.response = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}], 'usage': {'total_tokens': 7}}
        self.status = 200
        async def handler(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.response, headers={'request-id': 'req-test'})
        self.transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(handler))
        self.profile = {'profile_id': 'p', 'protocol': 'chat-completions', 'base_url': 'http://localhost:8080/v1', 'model': 'test', 'config_revision': 1, 'credential_revision': 1}

    async def asyncTearDown(self):
        await self.transport.aclose()

    async def call(self, **kwargs):
        return await self.transport.generate(profile=self.profile, api_key='private', system_prompt='system', user_payload={'text': '你好'}, **kwargs)

    async def test_resolved_originals_reach_both_protocols_within_budget(self):
        from reception_service.model_inputs import resolve_whatsapp_images
        from reception_service.model_media import OwnedInput, estimate_input_reservation
        from reception_service.media_resources import PROCESS_MEDIA_BUDGET
        random_pixels = random.Random(9).randbytes(1400 * 1400 * 3)
        image = Image.frombytes('RGB', (1400, 1400), random_pixels)
        stream = io.BytesIO()
        image.save(stream, format='PNG')
        image.close()
        large = stream.getvalue()
        self.assertGreater(len(large), 5 * 1024 * 1024)
        self.assertLess(len(large), 6 * 1024 * 1024)
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
        small = stream.getvalue()
        for data, count in ((large, 4), (small, 12)):
            digest = hashlib.sha256(data).hexdigest()
            asset = {'original_b64': base64.b64encode(data).decode(), 'mime': 'image/png', 'sha256': digest}
            refs = [dict(source_id=f's{i}', source_version='v', image_id=f'i{i}', sha256=digest, role='buyer') for i in range(count)]
            context = {'model_image_refs': refs, 'whatsapp_messages': [
                {'source_id': ref['source_id'], 'role': 'buyer', 'media': {'source_version': 'v'}} for ref in refs]}
            before = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
            owner = OwnedInput(estimate_input_reservation(context, None))
            try:
                images, manifest = resolve_whatsapp_images(context, lambda ref: asset, owner=owner)
                self.assertEqual(len(manifest), count)
                for protocol in ('chat-completions', 'anthropic'):
                    self.profile['protocol'] = protocol
                    self.response = ({'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
                        if protocol == 'chat-completions' else
                        {'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': 'test',
                         'content': [{'type': 'text', 'text': '{"ok":true}'}], 'stop_reason': 'end_turn',
                         'stop_sequence': None, 'usage': {'input_tokens': 2, 'output_tokens': 3}})
                    result = await self.call(images=images)
                    self.assertEqual(result['output'], {'ok': True})
                    body = json.loads(self.requests.pop().content)
                    blocks = body['messages'][-1]['content'][1:]
                    self.assertEqual(len(blocks), count)
                    for block in blocks:
                        encoded = (block['image_url']['url'].split(',', 1)[1] if protocol == 'chat-completions'
                                   else block['source']['data'])
                        self.assertEqual(hashlib.sha256(base64.b64decode(encoded)).hexdigest(), digest)
            finally:
                owner.close()
            self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], before)

    async def test_chat_request_and_digest(self):
        result = await self.call()
        request = self.requests[0]
        body = json.loads(request.content)
        self.assertEqual(body['messages'][1]['content'][0]['text'], '{"text":"你好"}')
        self.assertEqual(result['encoded_body_sha256'], hashlib.sha256(request.content).hexdigest())
        self.assertEqual(result['output'], {'ok': True})
        self.assertEqual(result['request_id'], 'req-test')

    async def test_anthropic_sdk_request(self):
        self.profile['protocol'] = 'anthropic'
        self.response = {'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': 'test', 'content': [{'type': 'text', 'text': '{"ok":true}'}], 'stop_reason': 'end_turn', 'stop_sequence': None, 'usage': {'input_tokens': 2, 'output_tokens': 3}}
        result = await self.call()
        request = self.requests[0]
        self.assertEqual(request.url.path, '/v1/messages')
        self.assertEqual(request.headers['x-api-key'], 'private')
        self.assertEqual(json.loads(request.content)['system'], 'system')
        self.assertEqual(result['output'], {'ok': True})
        self.assertEqual(result['encoded_body_sha256'], hashlib.sha256(request.content).hexdigest())

    async def test_anthropic_environment_headers_cannot_escape(self):
        from unittest.mock import patch
        self.profile['protocol'] = 'anthropic'
        self.response = {'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': 'test', 'content': [{'type': 'text', 'text': '{}'}], 'stop_reason': 'end_turn', 'stop_sequence': None, 'usage': {'input_tokens': 2, 'output_tokens': 3}}
        with patch.dict('os.environ', {'ANTHROPIC_CUSTOM_HEADERS': 'Authorization: Bearer injected\nx-api-key: injected\nx-secret: leaked'}):
            await self.call()
        headers = self.requests[0].headers
        self.assertEqual(headers['x-api-key'], 'private')
        self.assertNotIn('authorization', headers)
        self.assertNotIn('x-secret', headers)

    async def test_oversized_text_rejected_before_send(self):
        with self.assertRaises(Exception) as exc:
            await self.transport.generate(profile=self.profile, api_key='private', system_prompt='s', user_payload='x' * (4 * 1024 * 1024 + 1))
        self.assertEqual(exc.exception.code, 'model_invalid_input')
        self.assertFalse(self.requests)

    async def test_cancel_waits_for_callback_worker_before_releasing_transport(self):
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(5)
        task = asyncio.create_task(self.call(before_send=blocked))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(.05)
            self.assertFalse(task.done())
            self.assertFalse(self.requests)
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.requests)

    async def test_callback_failure_prevents_sdk_upload(self):
        self.profile['protocol'] = 'anthropic'
        def reject():
            raise RuntimeError('cancel')
        with self.assertRaisesRegex(RuntimeError, 'cancel'):
            await self.call(before_send=reject)
        self.assertEqual(self.requests, [])

    async def test_reject_truncated_or_refused_responses(self):
        for reason in ('length', 'content_filter', 'tool_calls'):
            self.response['choices'][0]['finish_reason'] = reason
            with self.assertRaises(Exception) as ctx:
                await self.call()
            self.assertEqual(ctx.exception.code, 'model_invalid_response')

    async def test_limit_and_no_replay_after_upload(self):
        self.response = {'data': 'x' * (1024 * 1024)}
        with self.assertRaises(Exception):
            await self.call()
        self.assertEqual(len(self.requests), 1)

    async def test_sdk_unknown_upload_is_not_replayed(self):
        await self.transport.aclose()
        async def handler(request):
            self.requests.append(request)
            raise httpx.ReadError('connection lost after upload', request=request)
        self.transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(handler))
        self.profile['protocol'] = 'anthropic'
        with self.assertRaises(Exception) as ctx:
            await self.call()
        self.assertEqual(ctx.exception.code, 'model_network_error')
        self.assertEqual(len(self.requests), 1)

    async def test_compressed_response_rejected(self):
        await self.transport.aclose()
        class RawStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'bad'
        async def handler(request):
            return httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=RawStream())
        self.transport = ProviderTransport(_transport_factory=lambda: httpx.MockTransport(handler))
        with self.assertRaises(Exception) as ctx:
            await self.call()
        self.assertEqual(ctx.exception.code, 'model_invalid_response')

    async def test_bad_url_rejected_before_request(self):
        self.profile['base_url'] = 'http://127.0.0.1@evil.example/v1'
        with self.assertRaises(Exception):
            await self.call()
        self.assertFalse(self.requests)
