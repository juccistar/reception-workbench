"""Connection-time address enforcement for the shared model HTTP pool."""
import asyncio
import ipaddress
import socket

import httpcore
import httpx

from ..translation_provider import normalize_base_url, TranslationProviderError
from .encoding import failure


def validate_base_url(value):
    try:
        return normalize_base_url(value)
    except TranslationProviderError:
        raise failure('model_invalid_input') from None


class CheckedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        from httpcore._backends.auto import AutoBackend
        self._backend = AutoBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        loopback = host.lower().rstrip('.') == 'localhost'
        try:
            literal = ipaddress.ip_address(host)
            loopback = literal.is_loopback
        except ValueError:
            pass
        addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        resolved = []
        for item in addresses:
            address = ipaddress.ip_address(item[4][0])
            if (not address.is_loopback if loopback else not address.is_global):
                raise httpcore.ConnectError('address rejected')
            resolved.append(str(address))
        if not resolved:
            raise httpcore.ConnectError('no address')
        # Connect to the checked numeric address, while httpcore keeps the original
        # origin for TLS SNI and certificate verification. Never resolve host twice.
        return await self._backend.connect_tcp(resolved[0], port, timeout=timeout,
                                               local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError('unix socket rejected')

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


def checked_transport():
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    transport._pool._network_backend = CheckedBackend()
    return transport
