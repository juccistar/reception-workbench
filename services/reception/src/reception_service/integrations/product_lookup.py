"""One bounded, read-only product lookup; persistence and grants belong to service.

The verified workctl tool returns data as a JSON string containing prod_info text.
No URL is manufactured from an ID, and catalogue text is never verified business
knowledge. This module performs no search, fallback, retry, image read or send.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from ..errors import ServiceError

_ID = re.compile(r'[1-9][0-9]{0,24}')
_URL = re.compile(r'https://[^\s<>"\']+', re.I)
_MAX_RESULT = 256_000
_MAX_REFERENCE = 8_000


def _cancelled(event):
    if event is not None and event.is_set():
        raise ServiceError('cancelled', '商品查询已取消，原查询记录保留', stage='read')


def _reference_url(value, product_id):
    """Accept a returned public product URL bound to this exact product ID."""
    if (not isinstance(value, str) or len(value) > 4096 or not value.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value) or '\\' in value):
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').lower()
        if (parts.scheme.lower() != 'https' or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)
                or not (host == 'alibaba.com' or host.endswith('.alibaba.com'))
                or not parts.path.startswith(('/product-detail/', '/product/'))):
            return None
        match = re.search(r'(?:_|/)([1-9][0-9]{0,24})(?:\.html?)?/?$', parts.path, re.I)
        if match is None or match[1] != product_id:
            return None
        # IDs are in the path. Tracking/signature query values are not stored.
        return urlunsplit(('https', host, parts.path, '', ''))
    except ValueError:
        return None


def _payload(reply):
    current = reply
    for _ in range(6):
        if isinstance(current, str):
            try:
                decoded = json.loads(current)
            except (ValueError, TypeError):
                return current
            if decoded == current:
                return current
            current = decoded
        elif isinstance(current, dict):
            if current.get('success') is False or current.get('businessSuccess') is False or current.get('error'):
                raise ServiceError('product_lookup_failed', '商品查询未确认成功', stage='read')
            if isinstance(current.get('data'), (dict, str, list)):
                current = current['data']
            else:
                return current
        else:
            return current
    raise ServiceError('product_lookup_invalid_response', '商品资料封装层次无效', stage='read')


def normalize_product_result(reply, product_id):
    try:
        raw = json.dumps(reply, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ServiceError('product_lookup_invalid_response', '商品资料结构无效', stage='read') from None
    if len(raw) > _MAX_RESULT:
        raise ServiceError('product_lookup_invalid_response', '商品资料超出单次读取预算', stage='read')
    payload = _payload(reply)
    if isinstance(payload, dict) and isinstance(payload.get('prod_info'), str):
        text = payload['prod_info']
    elif isinstance(payload, str):
        text = payload
    elif isinstance(payload, dict) and payload:
        # Future structured values remain reference text. No speculative field
        # mapping turns catalogue properties into confirmed customer requirements.
        text = json.dumps(payload, ensure_ascii=False)
    else:
        raise ServiceError('product_lookup_invalid_response', '商品查询缺少可用结果', stage='read')
    if not text.strip():
        raise ServiceError('product_lookup_invalid_response', '商品查询缺少可用结果', stage='read')
    urls = {url for value in _URL.findall(text) if (url := _reference_url(value.rstrip('.,);]}>'), product_id))}
    titles = list(dict.fromkeys(re.findall(r'^\s*(?:prod_title|product title)\s*:\s*(.+?)\s*$', text, re.I | re.M)))
    return {
        'product_id': product_id,
        'product_url': next(iter(urls)) if len(urls) == 1 else None,
        'original_title': titles[0][:1000] if len(titles) == 1 else None,
        'summary': text[:_MAX_REFERENCE],
        'summary_truncated': len(text) > _MAX_REFERENCE,
        'product_images': [],
        'sku': None,
        'source': 'native_product_lookup',
        'tool': 'icbu.tm.get-product',
        'reference_only': True,
        'result_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
    }


def read_product(native, seller_id, product_id, request_id, *, cancel_event=None):
    if (not isinstance(seller_id, str) or not _ID.fullmatch(seller_id)
            or not isinstance(product_id, str) or not _ID.fullmatch(product_id)
            or not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200
            or any(ord(char) < 32 for char in request_id)):
        raise ServiceError('invalid_request', '商品查询身份或编号无效', stage='read')
    _cancelled(cancel_event)
    native._identity(seller_id)
    _cancelled(cancel_event)
    # Exact documented read-only command; no caller-controlled executable, flag,
    # URL, tool name or gateway token can be inserted into this request.
    reply = native.process.run([native.cli, 'icbu', 'tm', 'get-product', '--prod-id', product_id,
                                '--self-ali-id', seller_id, '--buyer-country-code', 'US', '--format', 'json'], timeout=35)
    _cancelled(cancel_event)
    if not isinstance(reply, dict) or reply.get('success') is not True:
        raise ServiceError('product_lookup_failed', '商品查询未确认成功', stage='read')
    result = normalize_product_result(reply, product_id)
    result['request_id'] = request_id
    return result
