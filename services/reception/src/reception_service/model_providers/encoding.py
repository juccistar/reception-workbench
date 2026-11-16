"""Encode validated in-memory model inputs and parse complete JSON responses."""
from __future__ import annotations

import base64
import hashlib
import json

from ..errors import ServiceError

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 20000
MAX_JSON_STRING = 262144
MAX_REQUEST_RESERVATION = 512 * 1024 * 1024
MAX_INPUT_TEXT = 4 * 1024 * 1024
_MIMES = frozenset(('image/jpeg', 'image/png', 'image/gif', 'image/webp'))


def failure(code='model_invalid_response'):
    return ServiceError(code, '模型请求失败，请检查模型配置后重试', stage='model')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def parse_json(raw):
    """Parse one bounded JSON object without accepting duplicate keys or NaN."""
    try:
        if not isinstance(raw, str) or len(raw) > MAX_RESPONSE_BYTES or len(raw.encode('utf-8')) > MAX_RESPONSE_BYTES:
            raise ValueError()
        depth = 0
        in_string = escaped = False
        for char in raw:
            if in_string:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in '[{':
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ValueError()
            elif char in ']}':
                depth -= 1
        def reject_constant(_value):
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError()
        stack = [value]
        count = 0
        while stack:
            item = stack.pop()
            count += 1
            if count > MAX_JSON_NODES:
                raise ValueError()
            if isinstance(item, str) and len(item) > MAX_JSON_STRING:
                raise ValueError()
            if isinstance(item, dict):
                count += len(item)
                if count > MAX_JSON_NODES or any(len(key) > MAX_JSON_STRING for key in item):
                    raise ValueError()
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise failure() from None


def validate_request_budget(system_prompt, user_payload, images):
    """Reject oversized inputs before Base64 or JSON request allocation."""
    try:
        stack = [system_prompt, user_payload]
        count = chars = 0
        while stack:
            item = stack.pop()
            count += 1
            if count > MAX_JSON_NODES:
                raise ValueError()
            if isinstance(item, str):
                chars += len(item)
                if chars > MAX_INPUT_TEXT:
                    raise ValueError()
            elif isinstance(item, dict):
                stack.extend(item.keys())
                stack.extend(item.values())
            elif isinstance(item, (list, tuple)):
                stack.extend(item)
        raw_bytes = sum(len(item['data']) for item in images)
        encoded = sum(4 * ((len(item['data']) + 2) // 3) for item in images)
        # Blocks, potentially four-byte-wide JSON text, and UTF-8 wire bytes
        # can coexist in the HTTP and SDK serializers.
        reservation = raw_bytes + 6 * encoded + 32 * chars
        if raw_bytes > 256 * 1024 * 1024 or reservation > MAX_REQUEST_RESERVATION:
            raise ValueError()
        return reservation
    except (KeyError, TypeError, ValueError, RecursionError):
        raise failure('model_invalid_input') from None


def image_blocks(images, protocol):
    """Return provider blocks and metadata derived from precisely encoded bytes."""
    if protocol not in ('chat-completions', 'anthropic'):
        raise failure('model_invalid_input')
    blocks, manifest = [], []
    aliases = set()
    try:
        for item in images:
            if not isinstance(item, dict):
                raise ValueError()
            data, mime, alias = item['data'], item['mime'], item['alias']
            if not isinstance(data, bytes) or not data or mime not in _MIMES:
                raise ValueError()
            if not isinstance(alias, str) or not alias or alias in aliases:
                raise ValueError()
            aliases.add(alias)
            digest = hashlib.sha256(data).hexdigest()
            if item['sha256'] != digest:
                raise ValueError()
            for key in ('image_id', 'source_id', 'source_version', 'role'):
                if not isinstance(item[key], str) or not item[key]:
                    raise ValueError()
            for key in ('width', 'height'):
                if type(item[key]) is not int or item[key] <= 0:
                    raise ValueError()
            encoded = base64.b64encode(data).decode('ascii')
            if protocol == 'chat-completions':
                block = {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{encoded}'}}
            else:
                block = {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': encoded}}
            blocks.append(block)
            manifest.append({key: item[key] for key in ('alias', 'image_id', 'source_id', 'source_version', 'role', 'mime', 'sha256', 'width', 'height')})
    except (KeyError, TypeError, ValueError):
        raise failure('model_invalid_input') from None
    return blocks, manifest


def chat_result(response):
    """Extract only a complete, non-refused, single-choice text response."""
    try:
        choices = response['choices']
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError()
        choice = choices[0]
        message = choice['message']
        if choice['finish_reason'] != 'stop' or message.get('refusal') or message.get('tool_calls') or message.get('function_call'):
            raise ValueError()
        return parse_json(message['content'])
    except (KeyError, TypeError, ValueError):
        raise failure() from None


def anthropic_result(response):
    """Extract text blocks from a complete SDK response; reject all other blocks."""
    try:
        if response.stop_reason != 'end_turn' or not response.content:
            raise ValueError()
        if any(block.type != 'text' or not isinstance(block.text, str) for block in response.content):
            raise ValueError()
        return parse_json(''.join(block.text for block in response.content))
    except (AttributeError, TypeError, ValueError):
        raise failure() from None
