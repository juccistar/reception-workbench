"""Single-attempt model transports with bounded, isolated HTTP pools."""
import asyncio
import contextvars
import hashlib
import json
import time
from collections import OrderedDict

import anthropic
import httpx

from .encoding import MAX_RESPONSE_BYTES, anthropic_result, chat_result, failure, image_blocks, parse_json, validate_request_budget
from .network import checked_transport, validate_base_url
from .owned_sync import owned_sync
from ..media_resources import PROCESS_MEDIA_BUDGET, ResourceLimitError

_call = contextvars.ContextVar('model_call')


class _Guard(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner = inner

    async def handle_async_request(self, request):
        try:
            return await self._handle(request)
        except Exception as exc:
            _call.get()['transport_error'] = exc
            raise

    async def _handle(self, request):
        state = _call.get()
        if state.get('anthropic_key') is not None:
            # The SDK merges ANTHROPIC_CUSTOM_HEADERS even with trust_env=False.
            # Restrict the wire headers and replace authentication after merging.
            allowed = {'host', 'content-length', 'content-type', 'accept', 'accept-encoding',
                       'user-agent', 'anthropic-version', 'x-api-key'}
            for name in list(request.headers):
                if name.lower() not in allowed:
                    del request.headers[name]
            request.headers['x-api-key'] = state['anthropic_key']
            request.headers['anthropic-version'] = '2023-06-01'
            request.headers['content-type'] = 'application/json'
            request.headers['host'] = request.url.netloc.decode('ascii')
        body = await request.aread()
        if state.get('anthropic_key') is not None:
            request.headers['content-length'] = str(len(body))
        state['hash'] = hashlib.sha256(body).hexdigest()
        request.headers['accept-encoding'] = 'identity'
        try:
            if state['callback']:
                await owned_sync(state['callback'])
        except BaseException as exc:
            state['callback_error'] = exc
            raise
        attempt = state.get('attempt')
        if attempt is not None:
            remaining = attempt.deadline - time.monotonic()
            if remaining <= 0:
                raise failure('model_timeout')
            # The durable claim precedes this boundary. A crash from here remains
            # uncertain; only an actual completed task may refund a zero-HTTP claim.
            async with asyncio.timeout(remaining):
                attempt.entered = True
                response = await self.inner.handle_async_request(request)
                attempt.status = response.status_code
                if response.status_code == 429:
                    from ..translation_provider import _retry_after
                    delay = _retry_after(response.headers.get('retry-after'))
                    attempt.retry_after = delay if delay is not None else 1.0
                return await self._read_response(response, request, state)
        response = await self.inner.handle_async_request(request)
        return await self._read_response(response, request, state)

    async def _read_response(self, response, request, state):
        try:
            # Reject compression rather than allowing an unbounded decoder allocation.
            if response.headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
                raise failure()
            data = bytearray()
            async for chunk in response.stream:
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise failure()
                data.extend(chunk)
            if 300 <= response.status_code < 400:
                raise failure('model_network_error')
            state['request_id'] = response.headers.get('request-id') or response.headers.get('x-request-id')
            if response.status_code < 400:
                parse_json(bytes(data).decode('utf-8'))
            return httpx.Response(response.status_code, headers=response.headers, content=bytes(data),
                                  extensions=response.extensions, request=request)
        finally:
            await response.aclose()

    async def aclose(self):
        await self.inner.aclose()


class ProviderTransport:
    def __init__(self, *, _transport_factory=None, max_pools=32):
        self._factory = _transport_factory or checked_transport
        self._max_pools = max_pools
        self._pools = OrderedDict()
        self._lock = asyncio.Lock()
        self._closed = False

    async def _acquire(self, key):
        async with self._lock:
            if self._closed:
                raise failure('model_unavailable')
            entry = self._pools.get(key)
            if entry is not None and len(entry) > 2:
                raise failure('model_unavailable')
            if entry is None:
                while len(self._pools) >= self._max_pools:
                    old = next((k for k, v in self._pools.items() if not v[1]), None)
                    if old is None:
                        raise failure('model_unavailable')
                    client, _ = self._pools.pop(old)
                    await client.aclose()
                client = httpx.AsyncClient(transport=_Guard(self._factory()), trust_env=False,
                                           follow_redirects=False, timeout=180)
                entry = [client, 0]
                self._pools[key] = entry
            self._pools.move_to_end(key)
            entry[1] += 1
            return entry

    async def invalidate_profile(self, profile_id):
        """Retire idle profile pools; active requests finish before retirement."""
        async with self._lock:
            for key, entry in list(self._pools.items()):
                if key[0] == profile_id:
                    if entry[1]:
                        if len(entry) == 2:
                            entry.append(True)
                    else:
                        del self._pools[key]
                        await entry[0].aclose()

    async def _release(self, key, entry):
        async with self._lock:
            entry[1] -= 1
            if len(entry) > 2 and entry[1] == 0:
                if self._pools.get(key) is entry:
                    del self._pools[key]
                await entry[0].aclose()

    async def aclose(self):
        async with self._lock:
            self._closed = True
            if any(entry[1] for entry in self._pools.values()):
                raise failure('model_unavailable')
            for client, _ in self._pools.values():
                await client.aclose()
            self._pools.clear()

    async def generate(self, *, profile, api_key, system_prompt, user_payload, images=(),
                       encoded_images=None, max_output_tokens=8192, before_send=None, _resource_lease=None, _attempt=None):
        reservation = validate_request_budget(system_prompt, user_payload, images)
        lease = None
        if _resource_lease is None:
            try:
                lease = await PROCESS_MEDIA_BUDGET.acquire_async(reservation, deadline=time.monotonic() + 180)
            except ResourceLimitError as error:
                raise failure('model_resource_over_limit' if error.code == 'resource_too_large' else 'model_resource_busy') from None
        try:
            return await self._generate_leased(profile=profile, api_key=api_key, system_prompt=system_prompt,
                user_payload=user_payload, images=images, encoded_images=encoded_images,
                max_output_tokens=max_output_tokens, before_send=before_send, attempt=_attempt)
        finally:
            if lease is not None:
                lease.close()

    async def _generate_leased(self, *, profile, api_key, system_prompt, user_payload, images,
                               encoded_images, max_output_tokens, before_send, attempt=None):
        try:
            protocol = profile['protocol']
            base = validate_base_url(profile['base_url'])
            model = profile['model']
            if protocol not in ('anthropic', 'chat-completions') or not isinstance(model, str) or not model.strip() or not isinstance(api_key, str) or not api_key.strip() or not isinstance(system_prompt, str) or type(max_output_tokens) is not int or max_output_tokens < 1:
                raise ValueError()
            validate_request_budget(system_prompt, user_payload, images)
            blocks = image_blocks(images, protocol)[0] if encoded_images is None else encoded_images
            text = user_payload if isinstance(user_payload, str) else json.dumps(user_payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            key = (profile['profile_id'], profile.get('config_revision'), profile.get('credential_revision'), protocol, base, hashlib.sha256(api_key.encode()).digest())
        except (KeyError, TypeError, ValueError):
            raise failure('model_invalid_input') from None
        entry = await self._acquire(key)
        state = {'callback': before_send, 'callback_error': None, 'attempt': attempt,
                 'anthropic_key': api_key if protocol == 'anthropic' else None}
        token = _call.set(state)
        try:
            if protocol == 'chat-completions':
                content = [{'type': 'text', 'text': text}, *blocks]
                response = await entry[0].post(base + '/chat/completions', headers={'authorization': 'Bearer ' + api_key},
                    json={'model': model, 'max_tokens': max_output_tokens, 'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': content}]})
                response.raise_for_status()
                data = parse_json(response.text)
                output = chat_result(data)
                usage = data.get('usage', {})
                stop = 'stop'
                model_result = data.get('model', model)
            else:
                sdk_base = base[:-3] if base.endswith('/v1') else base
                sdk = anthropic.AsyncAnthropic(api_key=api_key, base_url=sdk_base, max_retries=0, http_client=entry[0])
                response = await sdk.messages.create(model=model, max_tokens=max_output_tokens, system=system_prompt,
                    messages=[{'role': 'user', 'content': [{'type': 'text', 'text': text}, *blocks]}])
                output = anthropic_result(response)
                usage = response.usage.model_dump() if response.usage else {}
                stop = response.stop_reason
                model_result = response.model
            allowed = ('input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
            usage = {k: usage[k] for k in allowed if isinstance(usage, dict) and type(usage.get(k)) is int and 0 <= usage[k] <= 10**12}
            request_id = state.get('request_id')
            if not isinstance(request_id, str) or len(request_id) > 200 or not request_id.isascii() or not request_id.isprintable():
                request_id = None
            return {'output': output, 'usage': usage, 'request_id': request_id, 'model': model_result if isinstance(model_result, str) and len(model_result) <= 256 else model, 'stop_reason': stop, 'encoded_body_sha256': state['hash']}
        except Exception as exc:
            if state.get('callback_error') is not None:
                raise state['callback_error']
            from ..errors import ServiceError
            if isinstance(exc, ServiceError):
                raise
            if isinstance(state.get('transport_error'), ServiceError):
                raise state['transport_error']
            if isinstance(exc, (TimeoutError, httpx.TimeoutException, anthropic.APITimeoutError)):
                raise failure('model_timeout') from None
            status = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
            code = 'model_auth_error' if status in (401, 403) else 'model_rate_limited' if status == 429 else 'model_network_error'
            raise failure(code) from None
        finally:
            _call.reset(token)
            await self._release(key, entry)
