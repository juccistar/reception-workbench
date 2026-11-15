"""Validate owned image inputs before constructing a model request."""
from __future__ import annotations

import base64
import sys
import time

from .errors import ServiceError
from .image_assets import decode_image, MAX_BYTES, ImageAssetError
from .photos import direct_image_id, photo_version
from .media_resources import PROCESS_MEDIA_BUDGET, ResourceLimitError


class OwnedInput:
    """Explicit ownership of input memory until the actual model task finishes."""
    def __init__(self, amount):
        self.lease = PROCESS_MEDIA_BUDGET.acquire(amount)
        self._transferred = False

    def reserve_more(self, amount):
        self.lease.resize(self.lease.amount + amount)

    def transfer(self):
        if self._transferred:
            raise RuntimeError('input already transferred')
        self._transferred = True
        return self.close

    def close_if_owned(self):
        if not self._transferred:
            self.close()

    def close(self):
        self.lease.close()


class PhotoDeadline(float):
    """Photo deadline retaining the original shared model-chain start."""
    def __new__(cls, deadline, started):
        instance = super().__new__(cls, deadline)
        instance.started = started
        return instance


def start_photo_deadline(settings):
    started = time.monotonic()
    seconds = settings.get('image_prepare_seconds', 60) if isinstance(settings, dict) else 60
    seconds = min(seconds, 60) if type(seconds) is int and seconds >= 1 else 60
    return PhotoDeadline(started + seconds, started)


class OwnedPhotoInputs(list):
    """Private list-compatible snapshot carrying its process budget owner."""
    def __init__(self, owner):
        super().__init__()
        self.owner = owner


def estimate_input_reservation(context, photo_inputs, instructions=''):
    """Bound the existing snapshots and the copies made during projection/decode."""
    stack = [context, photo_inputs, instructions]
    seen = set()
    size = 0
    while stack:
        value = stack.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        size += sys.getsizeof(value) * 4
        if size > PROCESS_MEDIA_BUDGET.limit:
            raise ResourceLimitError('resource_too_large')
        if isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
    # A resolver returns base64 plus decoded bytes only after it is called.
    if isinstance(context, dict):
        refs = context.get('model_image_refs', [])
        if isinstance(refs, list):
            size += len(refs) * (MAX_BYTES * 3)
    return size + 2 * 1024 * 1024


def prepare_model_images(context, photo_inputs, *, cancel=None, deadline=None, batch_mib=64):
    refs = list((context.get('photo_context') or {}).get('direct_images', [])) + list(context.get('supplement_image_refs', []))
    items = photo_inputs or []
    if not isinstance(items, (list, tuple)) or len(refs) != len(items):
        raise ServiceError('photo_input_unavailable', '图片原图未绑定当前请求，请重新生成', stage='context')
    manifest = context.get('image_manifest') or []
    images = []
    total = 0
    try:
        for ref, item in zip(refs, items):
            if cancel is not None and cancel.is_set():
                raise ServiceError('cancelled', '请求已取消', stage='analysis')
            if deadline is not None and time.monotonic() >= deadline:
                raise ServiceError('photo_preparation_timeout', '图片准备超过时间预算', stage='analysis')
            asset = item['asset']
            if ref.get('origin') == 'business_supplement':
                attachment = item['attachment']
                if (attachment.get('origin') != 'business_supplement' or attachment.get('id') != ref['attachment_id']
                    or ref['image_id'] != attachment['id'] or attachment.get('sha256') != ref['image_sha256']
                    or any(attachment.get(k) != ref.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id'))):
                    raise ValueError()
                source_id, version, role = ref['attachment_id'], str(ref['supplement_revision']), 'seller'
            else:
                message = item['message']
                if (any(ref.get(k) != message.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id', 'message_id'))
                    or ref.get('source_version') != photo_version(message) or message.get('role') not in ('buyer', 'seller')
                    or message.get('own') is not (message.get('role') == 'seller') or ref.get('role') != message.get('role')
                    or ref.get('image_id') != direct_image_id(message, ref.get('image_sha256'))):
                    raise ValueError()
                source_id, version, role = ref['message_id'], ref['source_version'], ref['role']
            encoded = asset['original_b64']
            if not isinstance(encoded, str) or len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
                raise ValueError()
            data = base64.b64decode(encoded, validate=True)
            total += len(data)
            if total > batch_mib * 1024 * 1024:
                raise ServiceError('photo_preparation_budget', '图片总大小超过预算', stage='analysis')
            checked = decode_image(data, asset['mime'])
            if checked['sha256'] != ref['image_sha256'] or asset.get('sha256') != checked['sha256']:
                raise ValueError()
            matches = [row for row in manifest if row.get('image_id') == ref['image_id']]
            if len(matches) != 1 or not isinstance(matches[0].get('alias'), str):
                raise ValueError()
            images.append(dict(alias=matches[0]['alias'], image_id=ref['image_id'], source_id=source_id,
                               source_version=version, role=role, mime=checked['mime'], data=data,
                               sha256=checked['sha256'], width=checked['width'], height=checked['height']))
        if len({row['alias'] for row in images}) != len(images):
            raise ValueError()
        if deadline is not None and time.monotonic() >= deadline:
            raise ServiceError('photo_preparation_timeout', '图片准备超过时间预算', stage='analysis')
        return images, refs
    except ServiceError:
        raise
    except (ValueError, TypeError, KeyError, ImageAssetError):
        raise ServiceError('photo_input_unavailable', '图片来源或内容已变化，请重新生成', stage='context') from None
