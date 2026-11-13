"""Import one verified local image into the current Accio workspace and observe it.

Workspace identity comes only from this facade's successful synthetic AI check.
No approved-root settings, credentials, links, or tool approvals are changed.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import stat
import uuid

from ..errors import ServiceError
from ..image_assets import decode_image, MAX_BYTES, ImageAssetError
from .accio_native.visual_query import VisualQueryRunner, request_workspace
from .accio_native.vendor.adapters.tool_signals import output_has_tools
from .reception_prompts import LEGACY_OBSERVATION

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')


def _fail():
    raise ServiceError('photo_vision_unavailable', '本次图片识别未取得可核实结果', stage='analysis')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def parse_observation(answer):
    """Strict, bounded observation schema; model output never supplies provenance."""
    if not isinstance(answer, str) or len(answer) > 10_000:
        _fail()
    answer = answer.strip()
    if answer.startswith('```json') and answer.endswith('```'):
        answer = answer[7:-3].strip()
    try:
        obj = json.loads(answer, object_pairs_hook=_unique)
    except (ValueError, TypeError, RecursionError):
        _fail()
    if (not isinstance(obj, dict) or set(obj) != {'status', 'description', 'visible_text', 'uncertainties'}
            or obj.get('status') != 'ok' or output_has_tools(obj)):
        _fail()
    description = obj['description']
    if not isinstance(description, str) or not description.strip() or len(description) > 2000:
        _fail()
    for key, limit in (('visible_text', 20), ('uncertainties', 10)):
        values = obj[key]
        if (not isinstance(values, list) or len(values) > limit
                or any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in values)):
            _fail()
    texts = [description, *obj['visible_text'], *obj['uncertainties']]
    if any(any(ord(char) < 32 and char not in '\n\r\t' for char in text) for text in texts):
        _fail()
    return {key: obj[key] for key in ('description', 'visible_text', 'uncertainties')}


def _directory(path):
    if not path.is_absolute() or not path.is_dir() or path.resolve(strict=True) != path:
        _fail()
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail()
    return path


def observe_photo(native, agent_id, workspace_identity, message, asset, request_id, *, on_start):
    """One AI call; no retry. Callback authorizes/charges the exact start boundary."""
    if (not isinstance(workspace_identity, dict) or workspace_identity.get('agent_id') != agent_id
            or not isinstance(request_id, str) or not _ID.fullmatch(request_id) or not callable(on_start)
            or not isinstance(asset, dict) or not isinstance(asset.get('sha256'), str)
            or not _HASH.fullmatch(asset['sha256'])):
        _fail()
    from ..photos import is_photo_placeholder
    if (not isinstance(message, dict) or message.get('role') != 'buyer' or message.get('own') is not False
            or not is_photo_placeholder(message.get('text'))):
        _fail()
    encoded = asset.get('original_b64')
    if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES + 2) // 3 * 4:
        _fail()
    try:
        content = base64.b64decode(encoded, validate=True)
        checked = decode_image(content, asset.get('mime'))
    except (ValueError, TypeError, ImageAssetError):
        _fail()
    if checked['sha256'] != asset['sha256']:
        _fail()
    binding = request_workspace(native.cli, agent_id, workspace_identity.get('conversation_name'))
    root = _directory(Path(binding['rootPath']))
    imports = root / '.bgcheck_inputs'
    imports.mkdir(exist_ok=True)
    _directory(imports)
    folder = imports / ('reception-image-' + uuid.uuid4().hex)
    folder.mkdir()
    image = None
    try:
        _directory(folder)
        ext = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}[checked['mime']]
        image = folder / (uuid.uuid4().hex + ext)
        with image.open('xb') as stream:
            stream.write(content)
        # Files are complete and closed before Accio is invoked. The runner
        # rechecks hash before/after and verifies its new task's workspace.
        prompt = '接待分析草稿-图片观察-' + uuid.uuid4().hex + '\n' + LEGACY_OBSERVATION + '\nfile_path = ' + str(image)
        runner = VisualQueryRunner(native.data_dir)
        on_start()
        result = runner.run(native.cli, agent_id, image, checked['sha256'], prompt, timeout=100)
        parsed = parse_observation(result.get('answer'))
        if (result.get('image_sha256') != checked['sha256'] or not isinstance(result.get('request_id'), str)
                or not _ID.fullmatch(result['request_id'])):
            _fail()
        return {**parsed, 'request_id': result['request_id'], 'image_sha256': checked['sha256']}
    finally:
        # Delete only our unchanged temporary input, never unrelated workspace
        # material. Durable original bytes remain in the seller database.
        try:
            if (image is not None and image.is_file() and not image.is_symlink()
                    and image.resolve() == image and image.stat().st_size == len(content)
                    and hashlib.sha256(image.read_bytes()).hexdigest() == checked['sha256']):
                image.unlink()
            folder.rmdir()
        except OSError:
            pass
