import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from reception_service.translation_provider import (
    ChatCompletionsTranslator, TranslationProviderError, normalize_base_url,
)


def reply(items=None, *, finish='stop', usage=None, message=None):
    if message is None:
        message = {'content': json.dumps({'items': items if items is not None else [{'id': 't1', 'text': '你好'}]})}
    return {'choices': [{'finish_reason': finish, 'message': message}], 'usage': usage}


def translator(handler):
    return ChatCompletionsTranslator('https://example.com/v1', 'sample-model', 'secret-key',
                                     transport=httpx.MockTransport(handler))


def source(key='private-source-id'):
    return {'key': key, 'text': 'Hello', 'speaker': 'buyer',
            'context': [{'speaker': 'seller', 'text': 'Earlier text'}]}


class TranslationProviderTests(unittest.TestCase):
    def test_request_mapping_context_usage_and_order(self):
        def handler(request):
            self.assertEqual(str(request.url), 'https://example.com/v1/chat/completions')
            self.assertEqual(request.headers['authorization'], 'Bearer secret-key')
            body = json.loads(request.content)
            self.assertEqual(set(body), {'model', 'messages', 'stream', 'response_format', 'max_tokens'})
            self.assertEqual(body['model'], 'sample-model')
            self.assertFalse(body['stream'])
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertEqual(body['max_tokens'], 4096)
            self.assertEqual([m['role'] for m in body['messages']], ['system', 'user'])
            self.assertNotIn('private-source-id', request.content.decode())
            sent = json.loads(body['messages'][1]['content'])['items']
            self.assertEqual(sent[0], {'id': 't1', 'text': 'Hello', 'speaker': 'buyer',
                                      'context': [{'speaker': 'seller', 'text': 'Earlier text'}]})
            self.assertEqual(sent[1]['id'], 't2')
            return httpx.Response(200, json=reply([{'id': 't2', 'text': '二'}, {'id': 't1', 'text': '一'}],
                                                   usage={'prompt_tokens': 5, 'completion_tokens': 2}))
        result = translator(handler).translate([source(), source('other')], 'req-1')
        self.assertEqual(result, {'items': [{'key': 'private-source-id', 'text': '一'}, {'key': 'other', 'text': '二'}],
                                  'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': None},
                                  'model': 'sample-model', 'request_id': 'req-1'})

    def test_invalid_response_variants(self):
        cases = [
            reply(message={'content': ''}), reply(message={'content': '  '}),
            reply(finish='length'), reply(message={'content': '{}', 'tool_calls': [{'id': 'x'}]}),
            reply([{'id': 't1', 'text': 'a'}, {'id': 't1', 'text': 'b'}]),
            reply([{'id': 't2', 'text': 'a'}]), reply([]),
            reply([{'id': 't1', 'text': '   '}]),
            reply([{'id': 't1', 'text': 'a' * 50001}]),
            reply(message={'content': '{"items":[],"items":[]}'}),
        ]
        for data in cases:
            with self.subTest(data=str(data)[:100]):
                with self.assertRaises(TranslationProviderError) as caught:
                    translator(lambda request: httpx.Response(200, json=data)).translate([source()], 'r')
                self.assertEqual(caught.exception.code, 'invalid_response')
        for content in (b'{"choices":[],"choices":[]}', b'x' * (1024 * 1024 + 1)):
            with self.assertRaises(TranslationProviderError) as caught:
                translator(lambda request: httpx.Response(200, content=content)).translate([source()], 'r')
            self.assertEqual(caught.exception.code, 'invalid_response')

    def test_status_errors_do_not_leak(self):
        for status in (400, 401, 403, 404, 429, 500):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, text='secret-key private-source-id server-secret', headers={'Retry-After': '9999'})
            with self.subTest(status=status), self.assertRaises(TranslationProviderError) as caught:
                translator(handler).translate([source()], 'r')
            err = caught.exception
            self.assertNotIn('secret', str(err))
            self.assertEqual(len(calls), 1)
            self.assertEqual(err.retryable, status in (429, 500))
            self.assertEqual(err.retry_after, 300 if status == 429 else None)
        self.assertNotIn('secret', str(TranslationProviderError('x', 'secret')))

    def test_cancellation_interrupts_body_and_closes_stream(self):
        started = threading.Event()
        closed = threading.Event()
        cancel = threading.Event()
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.sleep(60)
                yield b'never'
            async def aclose(self):
                closed.set()
        provider = translator(lambda request: httpx.Response(200, stream=SlowStream()))
        result = []
        def run():
            try:
                provider.translate([source()], 'r', cancel_event=cancel)
            except TranslationProviderError as exc:
                result.append(exc.code)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2))
        cancel.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ['cancelled'])
        self.assertTrue(closed.is_set())

    def test_endpoint_validation(self):
        for address, expected in [
            ('https://EXAMPLE.com/v1/', 'https://example.com/v1'),
            ('https://example.com/custom/chat/completions/', 'https://example.com/custom'),
            ('https://8.8.8.8', 'https://8.8.8.8'),
            ('http://127.0.0.1:3425/v1', 'http://127.0.0.1:3425/v1'),
            ('http://127.255.255.255/v1/chat/completions/', 'http://127.255.255.255/v1'),
            ('http://LOCALHOST.:3425/v1', 'http://localhost:3425/v1'),
            ('https://localhost/v1', 'https://localhost/v1'),
            ('https://127.0.0.1/v1', 'https://127.0.0.1/v1'),
            ('http://[::1]:3425/v1', 'http://[::1]:3425/v1'),
            ('https://[::1]/v1', 'https://[::1]/v1'),
        ]:
            with self.subTest(address=address):
                self.assertEqual(normalize_base_url(address), expected)
        for address in (
            'http://example.com', 'http://8.8.8.8', 'http://localhost.example.com',
            'http://foo.localhost', 'https://foo.localhost', 'http://localhost..',
            'http://localtest.me', 'https://user:pass@example.com', 'http://user@localhost',
            'https://10.0.0.1', 'http://192.168.1.1', 'https://169.254.1.1',
            'https://192.0.2.1', 'http://[::2]', 'https://[fe80::1]',
            'http://127.1', 'https://2130706433', 'http://0177.0.0.1',
            'http://0x7f.0.0.1', 'http://0x7f000001', 'http://127.0.0.1.',
            'ftp://localhost/v1', 'https://example.com/?x=1', 'http://localhost/?',
            'http://localhost/#', 'https://example.com/a/../v1',
            'https://example.com/%2e%2e', 'http://localhost/v1//',
        ):
            with self.subTest(address=address), self.assertRaises(TranslationProviderError):
                normalize_base_url(address)

    def test_local_http_request_and_redirect(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                requests.append((self.path, self.headers.get('Authorization'),
                                 self.headers.get('Content-Type'), json.loads(body)))
                if len(requests) == 1:
                    data = json.dumps(reply()).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(307)
                    self.send_header('Location', '/redirected')
                    self.send_header('Content-Length', '0')
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = ChatCompletionsTranslator(
                f'http://127.0.0.1:{server.server_port}/v1', 'sample-model', 'synthetic-key')
            result = provider.translate([source('synthetic-id')], 'request-1')
            self.assertEqual(result['items'], [{'key': 'synthetic-id', 'text': '你好'}])
            path, authorization, content_type, body = requests[0]
            self.assertEqual(path, '/v1/chat/completions')
            self.assertEqual(authorization, 'Bearer synthetic-key')
            self.assertEqual(content_type, 'application/json')
            self.assertEqual(body['model'], 'sample-model')
            self.assertEqual(json.loads(body['messages'][1]['content'])['items'][0]['text'], 'Hello')
            with self.assertRaises(TranslationProviderError) as caught:
                provider.translate([source('synthetic-id')], 'request-2')
            self.assertEqual(caught.exception.code, 'http_error')
            self.assertEqual([request[0] for request in requests],
                             ['/v1/chat/completions', '/v1/chat/completions'])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_network_failure_is_retryable(self):
        def handler(request):
            raise httpx.ConnectError('secret-key', request=request)
        with self.assertRaises(TranslationProviderError) as caught:
            translator(handler).translate([source()], 'r')
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn('secret-key', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
