"""Separate product display lookup; no customer images or AI context.

The image component is a single read. The observed response has no product ID;
its association is based on the verified identity and request parameters.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from ..errors import ServiceError
from ..image_assets import ImageAssetError, _image_url

_ID = re.compile(r'[1-9][0-9]{0,24}\Z')


def _cancelled(event):
    if event is not None and event.is_set():
        raise ServiceError('cancelled', '商品主图读取已取消', stage='read')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _check_product_ids(value, product_id):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('productId', 'product_id') and (type(item) not in (str, int) or str(item) != product_id):
                raise ServiceError('product_image_identity_mismatch', '商品图片返回的编号不符', stage='read')
            _check_product_ids(item, product_id)
    elif isinstance(value, list):
        for item in value:
            _check_product_ids(item, product_id)


def _component_image(reply, product_id, diagnostics=None):
    _check_product_ids(reply, product_id)
    diagnostics = diagnostics if diagnostics is not None else {}
    data = reply.get('data')
    diagnostics['product_data_type'] = ('object' if isinstance(data, dict) else 'string' if isinstance(data, str)
                                        else 'null' if data is None else 'other')
    try:
        if isinstance(data, str):
            if data.strip() == 'Record does not exist.':
                diagnostics['product_response_reason'] = 'not_found'
                return 'unavailable', None
            if not data.strip():
                diagnostics['product_response_reason'] = 'empty_data'
                return 'unavailable', None
            try:
                data = json.loads(data, object_pairs_hook=_unique_object)
            except (ValueError, TypeError, RecursionError):
                diagnostics['product_response_reason'] = 'invalid_json'
                return 'unavailable', None
        if data is None:
            diagnostics['product_response_reason'] = 'empty_data'
            return 'unavailable', None
        _check_product_ids(data, product_id)
        if not isinstance(data, dict) or not isinstance(data.get('basicInfo'), dict):
            diagnostics['product_response_reason'] = 'missing_basic_info'
            return 'unavailable', None
        images = data['basicInfo'].get('images')
        if not isinstance(images, list):
            diagnostics['product_response_reason'] = 'missing_images'
            return 'unavailable', None
        if not images:
            return 'no_image', None
        by_index = {}
        invalid = []
        for row in images:
            if not isinstance(row, dict):
                diagnostics['product_response_reason'] = 'invalid_image_item'
                return 'unavailable', None
            index, url = row.get('imageIndex'), row.get('originalImageUrl')
            if type(index) is not int or index < 0 or index in by_index:
                diagnostics['product_response_reason'] = 'invalid_image_index'
                return 'unavailable', None
            by_index[index] = None
            try:
                host, _ = _image_url(url)
                if not host.endswith('.alicdn.com'):
                    raise ValueError('invalid image host')
                by_index[index] = url
            except (ValueError, TypeError, ImageAssetError):
                invalid.append(index)
        selected = []
        seen = set()
        for index, url in sorted(by_index.items()):
            if url and url not in seen:
                selected.append({'image_index': index, 'url': url})
                seen.add(url)
                if len(selected) == 5:
                    break
        return ('product_images' if selected else 'invalid_images'), (selected, invalid)
    except ServiceError:
        raise
    except (ValueError, TypeError, ImageAssetError, RecursionError):
        return 'unavailable', None


def read_product_display(native, seller_id, product_id, request_id, *, cancel_event=None):
    """Perform the specified read; never infer fields from an unknown schema."""
    if (not isinstance(seller_id, str) or not _ID.fullmatch(seller_id)
            or not isinstance(product_id, str) or not _ID.fullmatch(product_id)
            or not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200
            or any(ord(char) < 32 for char in request_id)):
        raise ServiceError('invalid_request', '商品主图读取身份或编号无效', stage='read')
    _cancelled(cancel_event)
    identity = native._identity(seller_id)
    if not isinstance(identity, (tuple, list)) or not identity or identity[0] != seller_id:
        raise ServiceError('identity_invalid', '商品主图读取身份未核实', stage='identity')
    _cancelled(cancel_event)
    with tempfile.TemporaryDirectory(prefix='reception-product-display-') as folder:
        params = Path(folder).resolve() / 'params.json'
        params.write_text(json.dumps({'productId': int(product_id), 'queryType': 'trunk', 'componentList': ['images']}, separators=(',', ':')), encoding='utf-8')
        reply = native.process.run([native.cli, 'icbu', 'product', 'list-information', '--json-file', str(params),
                                    '--format', 'json'], timeout=35)
    _cancelled(cancel_event)
    if not isinstance(reply, dict) or reply.get('success') is not True:
        raise ServiceError('product_display_failed', '商品主图查询未确认成功', stage='read')
    try:
        raw = json.dumps(reply, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ServiceError('product_display_invalid_response', '商品主图查询结果结构无效', stage='read') from None
    if len(raw) > 256_000:
        raise ServiceError('product_display_invalid_response', '商品主图查询结果超出读取预算', stage='read')
    response_diagnostics = {}
    source, images = _component_image(reply, product_id, response_diagnostics)
    invalid_indices = []
    if isinstance(images, tuple):
        images, invalid_indices = images
    images = images or []
    result = {'product_id': product_id, 'main_image_url': images[0]['url'] if images else None,
              'images': images, 'source': source, 'result_sha256': hashlib.sha256(raw).hexdigest(),
              'product_binding': 'request_parameters', 'primary_verified': False,
              'selection_rule': 'lowest_image_index' if images else None}
    if source == 'unavailable':
        result['response_diagnostics'] = response_diagnostics
    if invalid_indices:
        result['invalid_count'] = len(invalid_indices)
        result['invalid_indices'] = invalid_indices[:5]
    return result
