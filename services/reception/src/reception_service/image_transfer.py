"""Private, bounded asynchronous cached-image transfers."""
from __future__ import annotations

import base64
import hashlib
import io
import secrets
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageOps

from .errors import ServiceError
from .image_assets import MAX_BYTES, MAX_DIMENSION, MAX_PIXELS, inspect_image, _METADATA_RESERVE
from .media_resources import PROCESS_MEDIA_BUDGET, ResourceLimitError

CHUNK = 256 * 1024
TTL = 60.0
PAYLOAD_LIMIT = 256 * 1024 * 1024
PNG_LIMIT = 128 * 1024 * 1024
FORMATS = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp', 'GIF': 'image/gif'}


def _unavailable():
    raise ServiceError('image_unavailable', '原图不可用或来源已变化，请刷新后重试')


def _busy():
    raise ServiceError('image_busy', '图片处理繁忙，请稍后重试')


class _BoundedOutput(io.BytesIO):
    def __init__(self, limit=PNG_LIMIT):
        super().__init__()
        self.limit = limit

    def write(self, value):
        if self.tell() + len(value) > self.limit:
            raise ValueError('PNG output limit')
        return super().write(value)


def _convert(snapshot, copy):
    content, size, declared_mime, sha = snapshot
    data = content if isinstance(content, bytes) else base64.b64decode(content, validate=True)
    if (not 0 < len(data) <= MAX_BYTES or len(data) != size
            or (not isinstance(content, bytes) and hashlib.sha256(data).hexdigest() != sha)):
        raise ValueError('invalid image content')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data), formats=tuple(FORMATS)) as image:
            mime = FORMATS.get(image.format)
            width, height = image.size
            if (mime != declared_mime or not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION
                    or width * height > MAX_PIXELS or getattr(image, 'n_frames', 1) != 1):
                raise ValueError('invalid image metadata')
            image.verify()
        if copy:
            pixels = width * height
            output_bound = min(PNG_LIMIT, pixels * 4 + height + pixels // 250 + 1024 * 1024)
            with Image.open(io.BytesIO(data), formats=tuple(FORMATS)) as image:
                image.load()
                with ImageOps.exif_transpose(image) as oriented:
                    if oriented.mode not in ('RGB', 'RGBA'):
                        with oriented.convert('RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB') as converted:
                            with _BoundedOutput(output_bound) as output:
                                converted.save(output, format='PNG')
                                data = output.getvalue()
                            width, height = converted.size
                    else:
                        with _BoundedOutput(output_bound) as output:
                            oriented.save(output, format='PNG')
                            data = output.getvalue()
                        width, height = oriented.size
                    mime = 'image/png'
                    sha = hashlib.sha256(data).hexdigest()
    return data, {'size': len(data), 'media_type': mime, 'width': width, 'height': height,
                  'sha256': sha, 'chunk_size': CHUNK}


class _Transfers:
    def __init__(self):
        self.lock = threading.RLock()
        self.entries = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='image-transfer')
        self.closed = False

    def _discard(self, token):
        entry = self.entries.get(token) if isinstance(token, str) else None
        if not entry:
            return
        entry['revoked'] = True
        entry['cancel'].set()
        entry['timer'].cancel()
        future = entry.get('future')
        if future is None or future.done() or future.cancel():
            self.entries.pop(token, None)
            entry['data'] = entry['snapshot'] = None
            if entry.get('lease'):
                entry['lease'].close()

    def expire(self, token):
        with self.lock:
            self._discard(token)

    def prepare(self, reference, metadata, copy, loader):
        with self.lock:
            if self.closed:
                _unavailable()
            if len(self.entries) >= 2:
                _busy()
            token = secrets.token_urlsafe(24)
            timer = threading.Timer(TTL, self.expire, args=(token,))
            timer.daemon = True
            entry = {'reference': dict(reference), 'metadata': metadata, 'snapshot': None, 'loader': loader,
                     'expires': time.monotonic() + TTL, 'cancel': threading.Event(), 'lease': None,
                     'timer': timer, 'revoked': False, 'status': 'pending', 'data': None}
            self.entries[token] = entry
            try:
                entry['future'] = self.pool.submit(self._work, token, copy)
                entry['future'].add_done_callback(lambda _future: self._finished(token))
                timer.start()
            except Exception:
                self._discard(token)
                raise
            return {'token': token}

    def _finished(self, token):
        with self.lock:
            entry = self.entries.get(token)
            if entry and entry['revoked']:
                self._discard(token)

    def _work(self, token, copy):
        with self.lock:
            entry = self.entries.get(token)
            if not entry or entry['revoked']:
                return
            deadline, cancel, loader, metadata = entry['expires'], entry['cancel'], entry['loader'], entry['metadata']
        # No service or manager lock is held while waiting for resources or decoding.
        lease = None
        try:
            # Snapshot, decoded original and parser overhead are reserved before loading.
            lease = PROCESS_MEDIA_BUDGET.acquire(3 * MAX_BYTES + ((MAX_BYTES + 2) // 3) * 4 + _METADATA_RESERVE,
                                                 cancel=cancel, deadline=deadline, wait=True)
            snapshot = loader(metadata)
            content, size, mime, sha = snapshot
            if not isinstance(content, str) or len(content) > ((MAX_BYTES + 2) // 3) * 4:
                raise ValueError('invalid image content')
            raw = base64.b64decode(content, validate=True)
            if len(raw) != size:
                raise ValueError('invalid image content')
            info = inspect_image(raw, mime)
            if info['sha256'] != sha:
                raise ValueError('invalid image content')
            output_bound = PNG_LIMIT
            if copy:
                pixels = info['width'] * info['height']
                # Worst-case uncompressed RGBA scanlines and zlib/chunk overhead.
                output_bound = min(PNG_LIMIT, pixels * 4 + info['height'] + pixels // 250 + 1024 * 1024)
                reserve = 12 * pixels + 2 * output_bound + 3 * size + len(content) + _METADATA_RESERVE
                lease.resize(reserve, pixels=True)
            data, description = _convert((raw, size, mime, sha), copy)
            del raw
            with self.lock:
                if entry['revoked'] or self.closed or time.monotonic() >= deadline:
                    return
                retained = sum(len(e['data']) for e in self.entries.values() if e['data'] is not None)
                if len(data) > PAYLOAD_LIMIT or retained + len(data) > PAYLOAD_LIMIT:
                    entry.update(status='failed', error='image_too_large')
                    return
                lease.resize(len(snapshot[0]) + len(data), pixels=False)
                entry.update(status='ready', data=data, snapshot=snapshot, description=description, lease=lease)
                lease = None
        except ResourceLimitError as exc:
            with self.lock:
                if not entry['revoked']:
                    entry.update(status='failed', error='image_too_large' if exc.code == 'resource_too_large' else 'image_busy')
        except Exception:
            with self.lock:
                if not entry['revoked']:
                    entry.update(status='failed', error='image_unavailable')
        finally:
            if lease:
                lease.close()

    def get(self, token):
        entry = self.entries.get(token) if isinstance(token, str) else None
        if not entry or entry['revoked'] or time.monotonic() >= entry['expires']:
            if entry:
                self._discard(token)
            _unavailable()
        return entry

    def close(self):
        with self.lock:
            self.closed = True
            for token in list(self.entries):
                self._discard(token)
        self.pool.shutdown(wait=False, cancel_futures=True)


class ImageTransferMixin:
    def _image_manager(self):
        # Call under the service lock, including during close.
        manager = getattr(self, '_image_transfers', None)
        if manager is None:
            manager = self._image_transfers = _Transfers()
        return manager

    def _image_prepare(self, reference, copy):
        if type(copy) is not bool:
            _unavailable()
        with self._lock:
            metadata = self._cached_image_metadata(reference)
            return self._image_manager().prepare(reference, metadata, copy, self._cached_image_load_transfer)

    def _image_checked(self, manager, token):
        # Both locks are held in service -> manager order; workers never take service lock.
        entry = manager.get(token)
        try:
            if self._cached_image_metadata(entry['reference']) != entry['metadata']:
                _unavailable()
        except Exception:
            manager._discard(token)
            raise
        return entry

    def _image_status(self, token):
        with self._lock:
            manager = self._image_manager()
            with manager.lock:
                entry = self._image_checked(manager, token)
                if entry['status'] == 'ready':
                    return {'status': 'ready', **entry['description']}
                if entry['status'] == 'failed':
                    return {'status': 'failed', 'error': entry['error']}
                return {'status': 'pending'}

    def _image_transfer_chunk(self, token, offset, sha256):
        if type(offset) is not int or offset < 0 or offset % CHUNK:
            _unavailable()
        with self._lock:
            manager = self._image_manager()
            with manager.lock:
                entry = self._image_checked(manager, token)
                if entry['status'] != 'ready' or sha256 != entry['description']['sha256'] or offset >= len(entry['data']):
                    _unavailable()
                data = entry['data'][offset:offset + CHUNK]
                return {'offset': offset, 'sha256': sha256, 'data_b64': base64.b64encode(data).decode('ascii')}

    def _image_cancel(self, token):
        with self._lock:
            manager = self._image_manager()
            with manager.lock:
                manager._discard(token)
        return {'cancelled': True}

    def _image_transfer_close(self):
        with self._lock:
            manager = getattr(self, '_image_transfers', None)
            if manager is not None:
                manager.close()
