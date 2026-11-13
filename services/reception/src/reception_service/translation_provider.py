"""Independent, single-attempt Chat Completions translation provider."""
import asyncio
import ipaddress
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import httpx


_MAX_BODY = 1024 * 1024
_MAX_TRANSLATION = 50000
_MAX_RETRY_AFTER = 300
from .integrations.reception_prompts import TRANSLATION

_SYSTEM_PROMPT = TRANSLATION


class TranslationProviderError(Exception):
    """Provider failure with safe text and machine-readable retry information.

    The message argument is accepted for the public interface, but deliberately
    not displayed: callers must never accidentally expose untrusted text.
    """
    def __init__(self, code, message, *, retryable=False, retry_after=None):
        super().__init__('翻译服务请求失败')
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


def _error(code, *, retryable=False, retry_after=None):
    return TranslationProviderError(code, '', retryable=retryable, retry_after=retry_after)


def normalize_base_url(base_url):
    """Validate an HTTPS or loopback HTTP endpoint and remove the API suffix.

    Hostnames are not resolved here; only literal IP addresses can be checked
    for public routing without introducing a DNS lookup or a rebinding race.
    """
    try:
        if not isinstance(base_url, str) or base_url != base_url.strip() or any(ord(c) < 33 or ord(c) == 127 for c in base_url):
            raise ValueError
        parts = urlsplit(base_url)
        host = parts.hostname
        port = parts.port
        scheme = parts.scheme.lower()
        if (scheme not in ('http', 'https') or not host or parts.username is not None
                or parts.password is not None or '?' in base_url or '#' in base_url or '\\' in base_url
                or '%' in parts.netloc or port == 0):
            raise ValueError
        host = host.lower()
        if host.endswith('.'):
            host = host[:-1]
        if not host or host.endswith('.localhost'):
            raise ValueError
        loopback = host == 'localhost'
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Reject alternate numeric IP notations accepted by some resolvers.
            if (':' in host or re.fullmatch(r'[0-9.]+', host)
                    or any(re.fullmatch(r'0x[0-9a-f]+', label) for label in host.split('.'))
                    or not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*', host)
                    or any(not label or label.startswith('-') or label.endswith('-') for label in host.split('.'))):
                raise ValueError
        else:
            # A trailing dot on an IP literal is not its canonical spelling.
            if parts.hostname.endswith('.'):
                raise ValueError
            loopback = address.is_loopback
            if not loopback and not address.is_global:
                raise ValueError
        if scheme == 'http' and not loopback:
            raise ValueError
        path = parts.path
        if any(segment in ('.', '..') for segment in path.split('/')) or '%' in path or '//' in path:
            raise ValueError
        path = path.rstrip('/')
        if path.endswith('/chat/completions'):
            path = path[:-len('/chat/completions')]
        netloc = '[' + host + ']' if ':' in host else host
        if port is not None:
            netloc += ':' + str(port)
        return urlunsplit((scheme, netloc, path, '', ''))
    except (ValueError, TypeError):
        raise _error('invalid_config') from None


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _parse_json(data):
    return json.loads(data, object_pairs_hook=_unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _retry_after(value):
    if not value:
        return None
    try:
        if re.fullmatch(r'[0-9]+', value.strip()):
            return min(int(value.strip()), _MAX_RETRY_AFTER)
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return min(max(0, (date - datetime.now(timezone.utc)).total_seconds()), _MAX_RETRY_AFTER)
    except (ValueError, TypeError, OverflowError):
        return None


class ChatCompletionsTranslator:
    def __init__(self, base_url, model, api_key, *, transport=None):
        self.base_url = normalize_base_url(base_url)
        if not isinstance(model, str) or not model.strip() or not isinstance(api_key, str) or not api_key.strip():
            raise _error('invalid_config')
        self.model = model
        self._api_key = api_key
        self._transport = transport

    def translate(self, items, request_id, *, cancel_event=None):
        """Translate one batch synchronously; call from a thread without an event loop."""
        if cancel_event is not None and cancel_event.is_set():
            raise _error('cancelled')
        if not isinstance(items, list) or not items:
            raise _error('invalid_input')
        outgoing = []
        keys = []
        for index, item in enumerate(items, 1):
            if (not isinstance(item, dict) or not isinstance(item.get('key'), str)
                    or not item['key'] or item['key'] in keys
                    or not isinstance(item.get('text'), str) or not item['text'].strip()
                    or not isinstance(item.get('speaker'), str) or not isinstance(item.get('context'), list)):
                raise _error('invalid_input')
            context = []
            for entry in item['context']:
                if not isinstance(entry, dict) or not isinstance(entry.get('speaker'), str) or not isinstance(entry.get('text'), str):
                    raise _error('invalid_input')
                context.append({'speaker': entry['speaker'], 'text': entry['text']})
            keys.append(item['key'])
            outgoing.append({'id': f't{index}', 'text': item['text'], 'speaker': item['speaker'], 'context': context})
        payload = {'model': self.model, 'messages': [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps({'items': outgoing}, ensure_ascii=False)},
        ], 'stream': False, 'response_format': {'type': 'json_object'}, 'max_tokens': 4096}
        if urlsplit(self.base_url).hostname == 'api.deepseek.com':
            # This is an official DeepSeek parameter, not a generic compatibility field.
            payload['thinking'] = {'type': 'disabled'}
        return asyncio.run(self._run(payload, keys, request_id, cancel_event))

    async def _run(self, payload, keys, request_id, cancel_event):
        task = asyncio.create_task(self._request(payload, keys, request_id))
        try:
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise _error('cancelled')
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise _error('timeout', retryable=True)
                done, _ = await asyncio.wait({task}, timeout=min(0.05, remaining))
                if done:
                    if cancel_event is not None and cancel_event.is_set():
                        raise _error('cancelled')
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _request(self, payload, keys, request_id):
        try:
            timeout = httpx.Timeout(20, connect=5)
            async with httpx.AsyncClient(transport=self._transport, trust_env=False, follow_redirects=False, timeout=timeout) as client:
                async with client.stream('POST', self.base_url + '/chat/completions', json=payload,
                                         headers={'Authorization': 'Bearer ' + self._api_key}) as response:
                    status = response.status_code
                    if status in (400, 401, 403, 404):
                        raise _error('invalid_config')
                    if status == 429:
                        raise _error('rate_limited', retryable=True, retry_after=_retry_after(response.headers.get('retry-after')))
                    if status >= 500:
                        raise _error('server_error', retryable=True)
                    if status != 200:
                        raise _error('http_error')
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > _MAX_BODY:
                            raise _error('invalid_response')
            return self._decode(body, keys, request_id)
        except httpx.TimeoutException:
            raise _error('timeout', retryable=True) from None
        except (httpx.InvalidURL, httpx.LocalProtocolError, UnicodeError):
            raise _error('invalid_config') from None
        except httpx.RequestError:
            raise _error('network_error', retryable=True) from None

    def _decode(self, body, keys, request_id):
        try:
            data = _parse_json(body)
            choices = data['choices']
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if choice.get('finish_reason') != 'stop':
                raise ValueError
            message = choice['message']
            if message.get('tool_calls') or message.get('function_call'):
                raise ValueError
            content = message['content']
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            translated = _parse_json(content)['items']
            if not isinstance(translated, list) or len(translated) != len(keys):
                raise ValueError
            result = {}
            expected = {f't{i}' for i in range(1, len(keys) + 1)}
            for entry in translated:
                identifier, text = entry['id'], entry['text']
                if (not isinstance(identifier, str) or identifier not in expected or identifier in result
                        or not isinstance(text, str) or not text.strip() or len(text) > _MAX_TRANSLATION):
                    raise ValueError
                result[identifier] = text
            usage = data.get('usage')
            if not isinstance(usage, dict):
                usage = {}
            counts = {name: usage.get(name) if type(usage.get(name)) is int and usage[name] >= 0 else None
                      for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
            reported = data.get('model')
            reported = reported if isinstance(reported, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', reported) else self.model
            provider_id = data.get('id')
            provider_id = provider_id if isinstance(provider_id, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', provider_id) else request_id
            return {'items': [{'key': key, 'text': result[f't{i}']} for i, key in enumerate(keys, 1)],
                    'usage': counts, 'model': reported, 'request_id': provider_id}
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError, RecursionError):
            raise _error('invalid_response') from None
