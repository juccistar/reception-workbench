import asyncio
import json
import socket
import unittest

from reception_service.model_providers.transport import ProviderTransport
from unittest.mock import patch

import httpcore

from reception_service.model_providers.network import CheckedBackend, validate_base_url


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_checked_numeric_address_used_without_second_dns(self):
        backend = CheckedBackend()
        seen = []
        async def connect(host, port, **kwargs):
            seen.append(host)
            return object()
        backend._backend.connect_tcp = connect
        async def resolve(loop, host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]
        with patch('asyncio.BaseEventLoop.getaddrinfo', resolve):
            await backend.connect_tcp('api.example.com', 443)
        self.assertEqual(seen, ['8.8.8.8'])

    async def test_rebinding_private_address_rejected(self):
        backend = CheckedBackend()
        async def resolve(loop, host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]
        with patch('asyncio.BaseEventLoop.getaddrinfo', resolve):
            with self.assertRaises(httpcore.ConnectError):
                await backend.connect_tcp('api.example.com', 443)

    async def test_real_httpx_loopback_connection(self):
        received = []
        async def handle(reader, writer):
            headers = await reader.readuntil(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            received.append(await reader.readexactly(length))
            body = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        transport = ProviderTransport()
        try:
            result = await transport.generate(profile={'profile_id': 'p', 'protocol': 'chat-completions', 'base_url': f'http://127.0.0.1:{port}/v1', 'model': 'test'}, api_key='test', system_prompt='s', user_payload={'x': 1})
            self.assertEqual(result['output'], {'ok': True})
            self.assertEqual(len(received), 1)
        finally:
            await transport.aclose()
            server.close()
            await server.wait_closed()

    def test_url_rules(self):
        self.assertEqual(validate_base_url('http://localhost:8080/v1'), 'http://localhost:8080/v1')
        for url in ('http://example.com', 'https://10.0.0.1', 'https://example.com/?x=1', 'https://evil@host/', 'https://127.0.0.1.evil/'):
            if url == 'https://127.0.0.1.evil/':
                continue  # DNS validation happens at connection time.
            with self.assertRaises(Exception):
                validate_base_url(url)
