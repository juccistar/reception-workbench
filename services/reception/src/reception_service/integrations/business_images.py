"""One owned business query reads its image inputs, without a separate vision job."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
import uuid

from ..errors import ServiceError
from ..image_assets import decode_image, MAX_BYTES, ImageAssetError
from ..photos import direct_image_id, photo_version
from ..model_payload import model_request
from .photo_vision import _directory
from .accio_native.visual_query import VisualQueryRunner, request_workspace
from .accio_native.vendor.adapters.common import AdapterError
from .reception_prompts import IMAGE_COMPAT


def query_business_images(native, agent_id, workspace_identity, context, photo_inputs, instructions,
                          *, on_call_start=None, cancel_event=None, diagnostics=None, photo_deadline=None):
    refs = (context.get('photo_context') or {}).get('direct_images', [])
    if (not isinstance(photo_inputs, (list, tuple)) or not photo_inputs
            or len(refs) != len(photo_inputs) or not isinstance(workspace_identity, dict)
            or workspace_identity.get('agent_id') != agent_id):
        raise ServiceError('photo_input_unavailable', '本轮图片工作区未就绪，请重新检查 AI 能力', stage='analysis')
    binding = request_workspace(native.cli, agent_id, workspace_identity.get('conversation_name'))
    root = _directory(Path(binding['rootPath']))
    imports = root / '.bgcheck_inputs'
    imports.mkdir(exist_ok=True)
    _directory(imports)
    folder = imports / ('reception-business-' + uuid.uuid4().hex)
    folder.mkdir()
    staged = []
    manifest = []
    settings = getattr(native, 'settings', {})
    settings = settings if isinstance(settings, dict) else {}
    batch_mib = settings.get('image_batch_mib', 64)
    prepare_seconds = settings.get('image_prepare_seconds', 60)
    if type(batch_mib) is not int or not 1 <= batch_mib <= 1024:
        batch_mib = 64
    if type(prepare_seconds) is not int or not 1 <= prepare_seconds <= 600:
        prepare_seconds = 60
    preparation_deadline = (photo_deadline if type(photo_deadline) in (int, float)
                            else time.monotonic() + prepare_seconds)
    total_bytes = 0
    runner = VisualQueryRunner(native.data_dir)
    try:
        _directory(folder)
        for ref, item in zip(refs, photo_inputs):
            message, asset = item.get('message'), item.get('asset')
            if (not isinstance(message, dict) or not isinstance(asset, dict)
                    or any(ref.get(k) != message.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id', 'message_id'))
                    or ref.get('source_version') != photo_version(message)
                    or message.get('role') not in ('buyer', 'seller')
                    or message.get('own') is not (message.get('role') == 'seller')
                    or ref.get('role') != message.get('role')
                    or ref.get('image_id') != direct_image_id(message, ref.get('image_sha256'))
                    or asset.get('sha256') != ref.get('image_sha256')):
                raise ValueError('Image source mismatch')
            encoded = asset.get('original_b64')
            if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES + 2) // 3 * 4:
                raise ValueError('Invalid image bytes')
            if cancel_event is not None and cancel_event.is_set():
                raise ServiceError('cancelled', '图片接待请求已取消', stage='analysis')
            if time.monotonic() >= preparation_deadline:
                raise ServiceError('photo_preparation_timeout', '图片准备超过时间预算，本轮图片尚未全部读取', stage='analysis')
            content = base64.b64decode(encoded, validate=True)
            total_bytes += len(content)
            if total_bytes > batch_mib * 1024 * 1024:
                raise ServiceError('photo_preparation_budget', '图片总大小超过预算，本轮图片尚未全部读取', stage='analysis')
            checked = decode_image(content, asset.get('mime'))
            if time.monotonic() >= preparation_deadline:
                raise ServiceError('photo_preparation_timeout', '图片准备超过时间预算，本轮图片尚未全部读取', stage='analysis')
            if checked['sha256'] != ref['image_sha256']:
                raise ValueError('Image changed')
            suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}[checked['mime']]
            image = folder / (uuid.uuid4().hex + suffix)
            with image.open('xb') as stream:
                stream.write(content)
            staged.append((image, checked['sha256'], len(content)))
            alias = next((row['alias'] for row in context.get('image_manifest', []) if row.get('image_id') == ref['image_id']), 'p' + str(len(manifest) + 1))
            manifest.append({'alias': alias, 'image_id': ref['image_id'], 'message_id': ref['message_id'], 'role': message['role'],
                             'sender_id': message['seller_id'] if message['role'] == 'seller' else message['buyer_id'],
                             'file_path': str(image), 'image_sha256': checked['sha256']})
        image_rules = '\n' + IMAGE_COMPAT + '\nimage_inputs = '
        prompt = model_request(instructions, context, image_rules + json.dumps(manifest, ensure_ascii=False))
        if cancel_event is not None and cancel_event.is_set():
            raise ServiceError('cancelled', '图片接待请求已取消', stage='analysis')
        if time.monotonic() >= preparation_deadline:
            raise ServiceError('photo_preparation_timeout', '图片准备超过时间预算，本轮图片尚未全部读取', stage='analysis')
        if on_call_start is not None:
            on_call_start()
        result = runner.run_many(native.cli, agent_id, [(path, sha) for path, sha, _ in staged],
                                 prompt, timeout=190, cancel_event=cancel_event, expected_manifest=manifest)
        answer = result.get('answer', '')
        if (not isinstance(answer, str) or any(str(path) in answer or json.dumps(str(path))[1:-1] in answer
                                              for path, _, _ in staged)):
            raise ValueError('Invalid business answer')
        return {'answer': answer, 'request_id': result['request_id'], 'verified_image_reads': refs}
    except (AdapterError, ServiceError):
        raise
    except (ValueError, TypeError, KeyError, OSError, ImageAssetError):
        raise ServiceError('photo_input_unavailable', '图片未取得本轮可核实读取结果，请检查当前 AI 连接', stage='analysis') from None
    finally:
        if diagnostics is not None:
            diagnostics(runner.last_diagnostics)
        for path, sha, size in staged:
            try:
                if (path.is_file() and not path.is_symlink() and path.resolve() == path
                        and path.stat().st_size == size and hashlib.sha256(path.read_bytes()).hexdigest() == sha):
                    path.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass
