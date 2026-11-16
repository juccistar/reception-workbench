"""Isolated, synthetic capability probes. No merchant state is opened here."""
from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
import time
import zlib

from ..errors import ServiceError
from .encoding import image_blocks, parse_json
from .runtime import ModelRuntime

_LIMIT = 1024 * 1024
_CAPS = ('text', 'structure', 'single_image', 'multi_image', 'combined', 'translation')
_CODES = frozenset(('model_timeout', 'model_cancelled', 'model_invalid_response', 'model_invalid_input',
                    'model_network_error', 'model_auth_error', 'model_rate_limited', 'model_unavailable'))


def _png(rgb):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    pixels = b''.join(b'\x00' + bytes(rgb) * 48 for _ in range(48))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 48, 48, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def _image(alias, rgb):
    data = _png(rgb)
    return dict(alias=alias, image_id=alias, source_id=alias, source_version='1', role='buyer',
                mime='image/png', data=data, sha256=hashlib.sha256(data).hexdigest(), width=48, height=48)


def _valid(packet):
    if not isinstance(packet, dict) or packet.get('format') != 'model-check-v1' or packet.get('purpose') not in ('reception', 'translation'):
        return False
    p = packet.get('profile')
    if not isinstance(p, dict) or p.get('protocol') not in ('chat-completions', 'anthropic'):
        return False
    if not isinstance(p.get('profile_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', p['profile_id']):
        return False
    if any(not isinstance(p.get(k), str) or not p[k].strip() or len(p[k]) > 2048 for k in ('base_url', 'model')):
        return False
    if any(type(p.get(k)) is not int or not 0 <= p[k] <= 2**53 - 1 for k in ('config_revision', 'credential_revision')):
        return False
    key = packet.get('api_key')
    return isinstance(key, str) and bool(key.strip()) and len(key) <= 16384


def _result(packet):
    profile = packet.get('profile') if isinstance(packet, dict) and isinstance(packet.get('profile'), dict) else {}
    return {'format': 'model-check-result-v1', 'status': 'failed', 'code': 'model_invalid_input',
            'purpose': packet.get('purpose') if isinstance(packet, dict) and packet.get('purpose') in ('reception', 'translation') else None,
            'profile_id': profile.get('profile_id') if isinstance(profile.get('profile_id'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', profile['profile_id']) else None,
            'config_revision': profile.get('config_revision') if type(profile.get('config_revision')) is int and 0 <= profile['config_revision'] <= 2**53 - 1 else None,
            'credential_revision': profile.get('credential_revision') if type(profile.get('credential_revision')) is int and 0 <= profile['credential_revision'] <= 2**53 - 1 else None,
            'capabilities': {k: {'status': 'not_checked', 'checked_at': None} for k in _CAPS}}


def _business(value):
    from ..integrations.analysis import validate_analysis
    identity = {'seller_id': 's', 'buyer_id': 'b', 'conversation_id': 'c'}
    context = {'case': dict(identity, procurement_key='main'), 'recent_messages': [dict(identity, message_id='m1', role='buyer', text='Please identify the image.')], 'historical_evidence': []}
    if not isinstance(value.get('conversation_summary'), str) or not isinstance(value.get('requirement_changes'), list):
        return False
    try:
        validate_analysis(value, context)
        return True
    except (ValueError, TypeError, KeyError, ServiceError):
        return False


def run_check(packet, transport=None, *, cancel=None, deadline=80):
    """Run probes through an injected async transport, never exposing its output.

    transport.generate receives profile, api_key, system_prompt, user_payload,
    images and encoded_images as keyword arguments and returns a parsed JSON object
    or a JSON string. It must implement aclose() for network resource cleanup.
    """
    result = _result(packet)
    if not _valid(packet):
        return result
    if transport is None:
        from .transport import ProviderTransport
        transport = ProviderTransport()
    purpose, profile = packet['purpose'], packet['profile']
    red, blue = (220, 20, 30), (20, 40, 220)
    pending = {k: [] for k in _CAPS}
    runtime = ModelRuntime()
    start = time.monotonic()
    interrupted = None
    last_error = None

    def mark(cap, ok):
        pending[cap].append(ok)
        expected = 2 if cap in ('single_image', 'multi_image') else 1
        status = 'failed' if not all(pending[cap]) else ('passed' if len(pending[cap]) == expected else 'not_checked')
        result['capabilities'][cap] = {'status': status, 'checked_at': None if status == 'not_checked' else int(time.time() * 1000)}

    def probe(caps, prompt, images, predicate):
        nonlocal interrupted, last_error
        if interrupted:
            return
        if cancel is not None and cancel.is_set():
            interrupted = 'model_cancelled'
            return
        remaining = deadline - (time.monotonic() - start)
        if remaining <= 0:
            interrupted = 'model_timeout'
            return
        try:
            blocks, _ = image_blocks(images, profile['protocol'])
            async def operation():
                return await transport.generate(profile=dict(profile), api_key=packet['api_key'],
                    system_prompt='Return only one complete JSON object. Do not call tools.',
                    user_payload=prompt, images=images, encoded_images=blocks)
            response = runtime.run(purpose, operation, cancel=cancel, deadline=remaining, heavy=bool(images))
            if isinstance(response, str):
                response = parse_json(response)
            if isinstance(response, dict) and 'output' in response:
                response = response['output']
            ok = isinstance(response, dict) and bool(predicate(response))
            for cap in caps:
                mark(cap, ok)
            if not ok:
                last_error = 'model_invalid_response'
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if code == 'model_cleanup_timeout':
                code = 'model_timeout'
            code = code if code in _CODES else 'model_unavailable'
            if code in ('model_cancelled', 'model_timeout'):
                interrupted = code
            else:
                last_error = code
                for cap in caps:
                    mark(cap, False)

    try:
        probe(('text',), 'Return JSON with answer equal to the result of 2 + 3.', (), lambda x: x.get('answer') == 5 and type(x.get('answer')) is int)
        if purpose == 'translation':
            probe(('translation',), 'Translate Hello into Chinese. Return {"items":[{"id":"t1","text":"..."}]}.', (),
                  lambda x: isinstance(x.get('items'), list) and len(x['items']) == 1 and isinstance(x['items'][0], dict) and x['items'][0].get('id') == 't1' and x['items'][0].get('text') in ('你好', '您好'))
        else:
            probe(('structure',), 'Return JSON with items as a list containing exactly one object with id x and count 2.', (),
                  lambda x: x.get('items') == [{'id': 'x', 'count': 2}] and type(x['items'][0]['count']) is int)
            prompt = 'Identify the dominant color in each image. Return {"observations":[{"alias":"p1","color":"red or blue"}]}, ordered as provided.'
            for color, rgb in (('red', red), ('blue', blue)):
                probe(('single_image',), prompt, [_image('p1', rgb)], lambda x, c=color: x.get('observations') == [{'alias': 'p1', 'color': c}])
            for first, second, colors in ((red, blue, ('red', 'blue')), (blue, red, ('blue', 'red'))):
                probe(('multi_image',), prompt, [_image('p1', first), _image('p2', second)],
                      lambda x, c=colors: x.get('observations') == [{'alias': 'p1', 'color': c[0]}, {'alias': 'p2', 'color': c[1]}])
            combined = ('The buyer message m1 says: Please identify the image. Identify its dominant color. '
                        'Return one reception JSON schema object: action must be "ask", "wait_quote", "human" or "skip"; '
                        'reply_messages is a list of one to three objects, each with a nonempty English body and '
                        'reply_sources as an array of source IDs (use "m1"); requirement_changes is an array '
                        '(use [] when no purchase requirement is established); conversation_summary is a concise '
                        'Chinese string; observations is an array containing {"alias":"p1","color":"red or blue"}, '
                        'where color is the single actual color you see, not the literal alternatives. '
                        'Do not invent product details or commitments.')
            probe(('combined',), combined, [_image('p1', red)],
                  lambda x: x.get('observations') == [{'alias': 'p1', 'color': 'red'}] and _business(x))
    finally:
        close = getattr(transport, 'aclose', None)
        async def cleanup():
            if close:
                await close()
        try:
            runtime.close(cleanup)
        except Exception as exc:
            if not interrupted:
                interrupted = 'model_timeout' if getattr(exc, 'code', None) == 'model_cleanup_timeout' else None
                if interrupted is None:
                    last_error = 'model_unavailable'
    if interrupted:
        result.update(status='cancelled' if interrupted == 'model_cancelled' else 'failed', code=interrupted)
    elif last_error:
        result.update(status='failed', code=last_error)
    else:
        result.update(status='passed', code='ok')
    return result


def run_check_from_stdin():
    """Read at most one MiB and write only the final bounded JSON result."""
    try:
        raw = sys.stdin.buffer.readline(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError()
        packet = parse_json(raw.decode('utf-8'))
        result = run_check(packet)
    except Exception:
        result = _result({})
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')) + '\n')
    sys.stdout.flush()
