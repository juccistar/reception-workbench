"""Host-only, ownership-checked access to locally cached original images."""
from __future__ import annotations

import base64
import hashlib
import io
import re
import secrets
import threading
import time
import warnings

from PIL import Image, ImageOps

from .errors import ServiceError
from .image_assets import MAX_BYTES, MAX_DIMENSION, MAX_PIXELS
from .image_transfer import ImageTransferMixin
from .product_identity import product_kind, product_link
from .repository import encoded

_CHUNK = 256 * 1024
_FORMATS = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}


def _fail():
    raise ServiceError('image_unavailable', '原图不可用或来源已变化，请刷新后重试')


class CachedMediaMixin(ImageTransferMixin):
    def _cached_image_snapshot(self, ref, *, metadata=False):
        def asset_record(kind, key):
            if metadata and hasattr(self.repo, 'image_transfer_metadata'):
                return self.repo.image_transfer_metadata(kind, key) or {}
            return self.repo.get(kind, key) or {}
        if not isinstance(ref, dict) or not self.account_verified or self._closing:
            _fail()
        kind = ref.get('kind')
        keys = {'message': {'kind', 'conversation_id', 'message_id', 'source_version', 'sha256'},
                'requirement': {'kind', 'case_id', 'media_id', 'source_version', 'sha256'},
                'product': {'kind', 'local_product_id', 'version', 'sha256'},
                'supplement': {'kind', 'conversation_id', 'attachment_id', 'sha256'}}
        if kind not in keys or set(ref) != keys[kind] or any(not isinstance(v, str) or not v or len(v) > 500 for k, v in ref.items() if k != 'version'):
            _fail()
        sha = ref['sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            _fail()
        if kind == 'product':
            if type(ref['version']) is not int:
                _fail()
            product = self.repo.get('library_product', ref['local_product_id']) or {}
            record = self.repo.get('product_main_image', ref['local_product_id']) or {}
            images = record.get('images')
            if not isinstance(images, list):
                images = [{'status': 'ready', 'asset_sha256': record.get('asset_sha256')}]
            if (product.get('seller_id') != self.seller_id or record.get('seller_id') != self.seller_id
                    or product.get('product_id') != record.get('product_id') or record.get('updated_at', 0) != ref['version']
                    or not any(isinstance(i, dict) and i.get('status') == 'ready' and i.get('asset_sha256') == sha for i in images[:5])):
                _fail()
            asset = asset_record('image_asset', sha)
        elif kind == 'supplement':
            conversation = self.repo.get('conversation', ref['conversation_id']) or {}
            asset = asset_record('supplement_attachment', ref['attachment_id'])
            if (conversation.get('seller_id') != self.seller_id or asset.get('seller_id') != self.seller_id
                    or asset.get('conversation_id') != ref['conversation_id'] or asset.get('buyer_id') != conversation.get('buyer_id')
                    or asset.get('id') != ref['attachment_id'] or asset.get('status') != 'ready'
                    or asset.get('origin') != 'business_supplement' or asset.get('sha256') != sha):
                _fail()
        else:
            if kind == 'message':
                message = self._photo_target(ref['conversation_id'], ref['message_id'], ref['source_version'])
            else:
                case = self._requirement_media_case(ref['case_id'])
                projection = self.get_requirement_media(ref['case_id'])
                item = next((i for i in projection['items'] if i.get('media_id') == ref['media_id']), None)
                if not item or item.get('image_ref') != ref:
                    _fail()
                message = self._photo_target(case['conversation_id'], item['source_message_id'], ref['source_version'])
            state = self._photo_state(message)
            if state.get('status') != 'ready' or state.get('sha256') != sha:
                _fail()
            asset = asset_record('image_asset', sha)
        if asset.get('sha256') != sha:
            _fail()
        if metadata and 'version' in asset:
            length = asset.get('content_length')
            if asset.get('content_type') != 'text' or type(length) is not int or not 0 < length <= ((MAX_BYTES + 2) // 3) * 4:
                _fail()
            kind = 'supplement_attachment' if ref['kind'] == 'supplement' else 'image_asset'
            key = ref['attachment_id'] if kind == 'supplement_attachment' else sha
            return (kind, key, asset['version'], length, asset.get('size'), asset.get('mime') or asset.get('media_type'), sha)
        content = asset.get('original_b64')
        if not isinstance(content, str) or not content or len(content) > ((MAX_BYTES + 2) // 3) * 4:
            _fail()
        return (content, asset.get('size'), asset.get('mime') or asset.get('media_type'), sha)

    def _cached_image_metadata(self, ref):
        return self._cached_image_snapshot(ref, metadata=True)

    def _cached_image_load_transfer(self, metadata):
        if len(metadata) == 4:  # lightweight test doubles and legacy repository implementations
            return metadata
        kind, key, _version, length, size, mime, sha = metadata
        asset = self.repo.get(kind, key) or {}
        content = asset.get('original_b64')
        if not isinstance(content, str) or len(content) != length:
            _fail()
        return content, size, mime, sha

    def _cached_image(self, ref, *, validate_pixels=True):
        content, size, asset_mime, sha = self._cached_image_snapshot(ref)
        try:
            data = base64.b64decode(content, validate=True)
            if not 0 < len(data) <= MAX_BYTES or len(data) != size or hashlib.sha256(data).hexdigest() != sha:
                _fail()
            if not validate_pixels:
                return data, None, None, None
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=tuple(_FORMATS)) as image:
                    mime = _FORMATS.get(image.format)
                    width, height = image.size
                    if (mime != asset_mime or not 0 < width <= MAX_DIMENSION
                            or not 0 < height <= MAX_DIMENSION or width * height > MAX_PIXELS or getattr(image, 'n_frames', 1) != 1):
                        _fail()
                    image.verify()
                with Image.open(io.BytesIO(data), formats=tuple(_FORMATS)) as image:
                    image.load()
            return data, mime, width, height
        except ServiceError:
            raise
        except Exception:
            _fail()

    def _image_original_describe(self, reference):
        with self._lock:
            data, mime, width, height = self._cached_image(reference)
            return {'size': len(data), 'media_type': mime, 'width': width, 'height': height,
                    'sha256': hashlib.sha256(data).hexdigest(), 'chunk_size': _CHUNK}

    def _image_original_chunk(self, reference, offset, sha256):
        if type(offset) is not int or offset < 0 or offset % _CHUNK or sha256 != reference.get('sha256'):
            _fail()
        with self._lock:
            data, _, _, _ = self._cached_image(reference, validate_pixels=False)
            if offset >= len(data):
                _fail()
            return {'offset': offset, 'sha256': sha256, 'data_b64': base64.b64encode(data[offset:offset + _CHUNK]).decode('ascii')}

    def _image_copy_describe(self, reference):
        # At most two converted images, retained only for a short bounded transfer.
        with self._lock:
            sessions = getattr(self, '_cached_copy_sessions', None)
            if sessions is None:
                sessions = self._cached_copy_sessions = {}
            now = time.monotonic()
            for token in list(sessions):
                if sessions[token]['expires'] <= now:
                    del sessions[token]
            if len(sessions) >= 2:
                raise ServiceError('image_busy', '图片复制繁忙，请稍后重试')
            data, _, _, _ = self._cached_image(reference)
            try:
                class BoundedOutput(io.BytesIO):
                    def write(self, value):
                        if self.tell() + len(value) > 80 * 1024 * 1024:
                            raise ValueError('PNG output limit')
                        return super().write(value)
                with Image.open(io.BytesIO(data), formats=tuple(_FORMATS)) as image:
                    image.load()
                    oriented = ImageOps.exif_transpose(image)
                    if oriented.mode not in ('RGB', 'RGBA'):
                        oriented = oriented.convert('RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB')
                    output = BoundedOutput()
                    oriented.save(output, format='PNG')
                    png = output.getvalue()
                    width, height = oriented.size
            except Exception:
                _fail()
            token = secrets.token_urlsafe(24)
            sha = hashlib.sha256(png).hexdigest()
            sessions[token] = {'reference': dict(reference), 'data': png, 'sha256': sha, 'expires': now + 30}
            def expire():
                with self._lock:
                    sessions.pop(token, None)
            timer = threading.Timer(30, expire)
            timer.daemon = True
            timer.start()
            return {'token': token, 'size': len(png), 'media_type': 'image/png', 'width': width,
                    'height': height, 'sha256': sha, 'chunk_size': _CHUNK}

    def _image_copy_chunk(self, reference, token, offset, sha256):
        if not isinstance(token, str) or type(offset) is not int or offset < 0 or offset % _CHUNK:
            _fail()
        with self._lock:
            sessions = getattr(self, '_cached_copy_sessions', {})
            item = sessions.get(token)
            if (not item or item['expires'] <= time.monotonic() or item['reference'] != reference
                    or item['sha256'] != sha256 or offset >= len(item['data'])):
                sessions.pop(token, None)
                _fail()
            try:
                self._cached_image(reference, validate_pixels=False)
            except Exception:
                sessions.pop(token, None)
                raise
            data = item['data'][offset:offset + _CHUNK]
            if offset + len(data) == len(item['data']):
                sessions.pop(token, None)
            return {'offset': offset, 'sha256': sha256, 'data_b64': base64.b64encode(data).decode('ascii')}

    def _copy_text_for_reference(self, reference):
        def unavailable():
            raise ServiceError('copy_unavailable', '复制内容不可用或归属已变化，请刷新后重试')
        if not isinstance(reference, dict) or len(reference) != 2:
            unavailable()
        field = reference.get('field')
        target = set(reference) - {'field'}
        if field not in ('product_id', 'product_url') or target not in ({'case_id'}, {'local_product_id'}):
            unavailable()
        key = next(iter(target))
        value = reference[key]
        if not isinstance(value, str) or not value or len(value) > 500 or any(ord(char) < 32 for char in value):
            unavailable()
        with self._lock:
            if not self.account_verified or self._closing:
                unavailable()
            if key == 'case_id':
                case = self._requirement_media_case(value)
                identifier = ((case.get('requirements') or {}).get('product_id') or {}).get('value')
            else:
                product = self.repo.get('library_product', value) or {}
                if product.get('seller_id') != self.seller_id:
                    unavailable()
                identifier = product.get('product_id')
            if not product_kind(identifier):
                unavailable()
            text = product_link(identifier) if field == 'product_url' else identifier
            if not text:
                unavailable()
            return {'field': field, 'text': text}

    def _product_link_for_reference(self, reference):
        if not isinstance(reference, dict) or len(reference) != 1 or set(reference) not in ({'case_id'}, {'local_product_id'}):
            _fail()
        value = next(iter(reference.values()))
        if not isinstance(value, str) or not value or len(value) > 500:
            _fail()
        with self._lock:
            if not self.account_verified or self._closing:
                _fail()
            if 'case_id' in reference:
                case = self._requirement_media_case(value)
                identifier = ((case.get('requirements') or {}).get('product_id') or {}).get('value')
            else:
                product = self.repo.get('library_product', value) or {}
                if product.get('seller_id') != self.seller_id:
                    _fail()
                identifier = product.get('product_id')
            url = product_link(identifier)
            if not url:
                _fail()
            return {'url': url}
