"""Private WhatsApp image resolver boundary for a frozen model context."""
from __future__ import annotations

import base64
import time
from .errors import ServiceError
from .image_assets import decode_image, MAX_BYTES, ImageAssetError


def resolve_whatsapp_images(context, resolver, *, cancel=None, deadline=None):
    """Resolve explicit source-bound refs; resolver(ref) returns an owned asset dict."""
    refs = context.get('model_image_refs', [])
    if not isinstance(refs, list):
        raise ServiceError('photo_input_unavailable', '图片来源无效', stage='context')
    if refs and not callable(resolver):
        raise ServiceError('photo_input_unavailable', '图片来源不可用', stage='context')
    messages = context.get('whatsapp_messages', [])
    images, manifest = [], []
    total = 0
    try:
        for ref in refs:
            if cancel is not None and cancel.is_set():
                raise ServiceError('cancelled', '请求已取消', stage='analysis')
            if deadline is not None and time.monotonic() >= deadline:
                raise ServiceError('photo_preparation_timeout', '图片准备超过时间预算', stage='analysis')
            matches = [m for m in messages if m.get('source_id') == ref['source_id']]
            if (len(matches) != 1 or matches[0].get('role') != ref['role'] or ref['role'] not in ('buyer', 'seller')
                    or not isinstance(matches[0].get('media'), dict)
                    or matches[0]['media'].get('source_version') != ref['source_version']
                    or not all(isinstance(ref.get(k), str) and ref[k] for k in ('source_id', 'source_version', 'image_id', 'sha256'))):
                raise ValueError()
            asset = resolver(dict(ref))
            encoded = asset['original_b64']
            if not isinstance(encoded, str) or len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
                raise ValueError()
            data = base64.b64decode(encoded, validate=True)
            total += len(data)
            if total > 64 * 1024 * 1024:
                raise ServiceError('photo_preparation_budget', '图片总大小超过预算', stage='analysis')
            checked = decode_image(data, asset['mime'])
            if checked['sha256'] != ref['sha256'] or asset.get('sha256') != ref['sha256']:
                raise ValueError()
            alias = 'p' + str(len(images) + 1)
            images.append(dict(alias=alias, image_id=ref['image_id'], source_id=ref['source_id'],
                               source_version=ref['source_version'], role=ref['role'], mime=checked['mime'],
                               data=data, sha256=checked['sha256'], width=checked['width'], height=checked['height']))
            manifest.append({k: images[-1][k] for k in ('alias', 'image_id', 'source_id', 'role')})
        if deadline is not None and time.monotonic() >= deadline:
            raise ServiceError('photo_preparation_timeout', '图片准备超过时间预算', stage='analysis')
        if len({image['source_id'] for image in images}) != len(images) or len({image['image_id'] for image in images}) != len(images):
            raise ValueError()
        return images, manifest
    except ServiceError:
        raise
    except (ValueError, TypeError, KeyError, ImageAssetError):
        raise ServiceError('photo_input_unavailable', '图片来源或内容已变化，请重新生成', stage='context') from None
