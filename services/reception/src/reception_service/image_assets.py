"""Bounded, credential-free retrieval and decoding of message images."""
from __future__ import annotations

import base64
from collections import OrderedDict
from contextlib import ExitStack
import hashlib
import http.client
import io
import ipaddress
import queue
import socket
import ssl
import threading
import time
import warnings
import zlib
from urllib.parse import quote, urljoin, urlsplit

from PIL import Image, ImageOps

from .media_resources import PROCESS_MEDIA_BUDGET, ResourceLimitError
from .errors import ServiceError

from .image_limits import (MAX_ORIGINAL_BYTES, MAX_PIXELS, MAX_DIMENSION,
                           DOWNLOAD_DEADLINE_SECONDS, DOWNLOAD_CONNECT_SECONDS,
                           DOWNLOAD_IDLE_SECONDS, MAX_REDIRECTS)

MAX_BYTES = MAX_ORIGINAL_BYTES
MAX_PREVIEW_BYTES = 1024 * 1024
DEADLINE_SECONDS = DOWNLOAD_DEADLINE_SECONDS
_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
_BINARY_MIME = "application/octet-stream"
_ERROR_TYPES = {
    "image size limit exceeded": ("validation", "size_limit"),
    "unsupported image type": ("validation", "unsupported_type"),
    "image type mismatch": ("validation", "type_mismatch"),
    "invalid image dimensions": ("validation", "dimensions"),
    "animated images are unsupported": ("validation", "animation"),
    "image preview failed": ("decode", "preview"),
    "invalid image": ("decode", "invalid_image"),
    "image network timeout": ("network", "timeout"),
    "image host resolution failed": ("network", "resolution"),
    "image redirect rejected": ("source", "redirect"),
    "image redirect limit exceeded": ("source", "redirect"),
    "image download rejected": ("http", "rejected"),
    "image download failed": ("network", "download"),
    "invalid image URL": ("source", "invalid_url"),
    "image host not allowed": ("source", "host_not_allowed"),
    "unsafe image host address": ("source", "unsafe_address"),
    "image retrieval failed": ("network", "retrieval"),
}
# getaddrinfo is not cancellable on all supported platforms. Bound outstanding
# resolver workers globally so repeated timeouts cannot accumulate threads.
_DNS_SLOTS = threading.BoundedSemaphore(4)


class ImageAssetError(ValueError):
    """An error with fixed, safe diagnostic attributes."""

    def __init__(self, message):
        super().__init__(message)
        self.stage, self.category = _ERROR_TYPES.get(message, ("unknown", "unknown"))


def _magic_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


class _ImageRedirect(Exception):
    def __init__(self, location):
        super().__init__('image redirect')
        self.location = location


def _lease(amount, *, pixels=False, deadline=None):
    try:
        return PROCESS_MEDIA_BUDGET.acquire(amount, pixels=pixels, deadline=deadline, wait=deadline is not None)
    except ResourceLimitError as error:
        raise ServiceError('model_resource_over_limit' if error.code == 'resource_too_large' else 'model_resource_busy',
                           '图片处理资源暂不可用', stage='context') from None


# Pillow parses PNG metadata during open and again on load. Bound both compressed
# expansion and the number of Python objects before allowing either operation.
_METADATA_LIMIT = 2 * 1024 * 1024
_METADATA_CHUNKS = 128
_METADATA_RESERVE = 24 * 1024 * 1024


def _check_png_metadata(data: bytes) -> None:
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return
    offset, count, total = 8, 0, 0
    view = memoryview(data)
    while offset + 12 <= len(data):
        size = int.from_bytes(view[offset:offset + 4], 'big')
        end = offset + 12 + size
        if end > len(data):
            raise ImageAssetError('invalid image')
        kind = bytes(view[offset + 4:offset + 8])
        if kind in (b'tEXt', b'zTXt', b'iTXt', b'iCCP', b'eXIf'):
            count += 1
            total += size
            if count > _METADATA_CHUNKS or total > _METADATA_LIMIT:
                raise ImageAssetError('invalid image')
            payload = view[offset + 8:offset + 8 + size]
            if kind in (b'zTXt', b'iCCP', b'iTXt'):
                # Bounded encoded input makes these small temporary copies safe.
                raw = bytes(payload)
                try:
                    keyword, rest = raw.split(b'\0', 1)
                    if not 1 <= len(keyword) <= 79:
                        raise ValueError()
                    if kind == b'iTXt':
                        flag, method = rest[0], rest[1]
                        if flag not in (0, 1) or method != 0:
                            raise ValueError()
                        _, rest = rest[2:].split(b'\0', 1)
                        _, compressed = rest.split(b'\0', 1)
                        if flag == 0:
                            compressed = None
                    else:
                        if rest[0] != 0:
                            raise ValueError()
                        compressed = rest[1:]
                    if compressed is not None:
                        decoder = zlib.decompressobj()
                        expanded = decoder.decompress(compressed, _METADATA_LIMIT - total + 1)
                        total += len(expanded)
                        if total > _METADATA_LIMIT or decoder.unconsumed_tail or not decoder.eof:
                            raise ValueError()
                except (ValueError, IndexError, zlib.error):
                    raise ImageAssetError('invalid image') from None
        offset = end
        if kind == b'IEND':
            break


def inspect_image(data: bytes, mime: str, *, deadline=None) -> dict:
    """Verify a bounded image container and metadata without decoding pixels."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ImageAssetError('image size limit exceeded')
    if not isinstance(mime, str) or mime.lower() not in (*_FORMATS.values(), _BINARY_MIME):
        raise ImageAssetError('unsupported image type')
    mime = mime.lower()
    if mime == _BINARY_MIME:
        mime = _magic_mime(data)
        if mime is None:
            raise ImageAssetError('unsupported image type')
    try:
        with _lease(len(data) * 2 + _METADATA_RESERVE, deadline=deadline):
            _check_png_metadata(data)
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=tuple(_FORMATS)) as image:
                    width, height = image.size
                    if _FORMATS.get(image.format) != mime:
                        raise ImageAssetError('image type mismatch')
                    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION and width * height <= MAX_PIXELS):
                        raise ImageAssetError('invalid image dimensions')
                    if getattr(image, 'n_frames', 1) != 1:
                        raise ImageAssetError('animated images are unsupported')
                    image.verify()
        return {'mime': mime, 'sha256': hashlib.sha256(data).hexdigest(), 'width': width, 'height': height, 'size': len(data)}
    except (ImageAssetError, ServiceError):
        raise
    except Exception:
        raise ImageAssetError('invalid image') from None


def validate_original(data: bytes, mime: str) -> dict:
    """Verify and decode the original without creating a preview or replacement."""
    info = inspect_image(data, mime)
    with _lease(info['width'] * info['height'] * 4 + len(data) * 2 + _METADATA_RESERVE, pixels=True):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=tuple(_FORMATS)) as image:
                    image.load()
        except Exception:
            raise ImageAssetError('invalid image') from None
    return info


def decode_image(data: bytes, mime: str, *, deadline=None, receive_lease=None) -> dict:
    info = inspect_image(data, mime, deadline=deadline)
    reserve = info['width'] * info['height'] * 12 + len(data) * 4 + MAX_PREVIEW_BYTES * 4 + _METADATA_RESERVE
    with _lease(reserve, pixels=True, deadline=deadline) as decoding:
        asset = _decode_image_leased(data, info['mime'])
        if receive_lease is not None:
            # Pixel intermediates have closed. Hand off encoded-result storage
            # before releasing the decode lease or copying the owned string.
            retained = len(data) + 2 * len(asset['original_b64']) + 2 * len(asset['preview_data_url']) + 4096
            try:
                decoding.resize(retained, pixels=False)
                receive_lease.resize(retained)
            except ResourceLimitError as error:
                raise ServiceError('model_resource_over_limit' if error.code == 'resource_too_large' else 'model_resource_busy',
                                   '图片处理资源暂不可用', stage='context') from None
        return asset


def _decode_image_leased(data: bytes, mime: str) -> dict:
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ImageAssetError("image size limit exceeded")
    if not isinstance(mime, str) or mime.lower() not in (*_FORMATS.values(), _BINARY_MIME):
        raise ImageAssetError("unsupported image type")
    mime = mime.lower()
    if mime == _BINARY_MIME:
        mime = _magic_mime(data)
        if mime is None:
            raise ImageAssetError("unsupported image type")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=tuple(_FORMATS)) as checked:
                checked.verify()
            with Image.open(io.BytesIO(data), formats=tuple(_FORMATS)) as image:
                if _FORMATS.get(image.format) != mime:
                    raise ImageAssetError("image type mismatch")
                width, height = image.size
                if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION and width * height <= MAX_PIXELS):
                    raise ImageAssetError("invalid image dimensions")
                if getattr(image, "n_frames", 1) != 1:
                    raise ImageAssetError("animated images are unsupported")
                image.load()
                with ExitStack() as stack:
                    oriented = stack.enter_context(ImageOps.exif_transpose(image))
                    oriented.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                    rgba = stack.enter_context(oriented.convert("RGBA"))
                    alpha = stack.enter_context(rgba.getchannel("A"))
                    transparent = alpha.getextrema()[0] < 255
                    clean = stack.enter_context(Image.new("RGBA" if transparent else "RGB", oriented.size))
                    source = rgba if transparent else stack.enter_context(rgba.convert("RGB"))
                    clean.paste(source)
                    while True:
                        for quality in ((None,) if transparent else (85, 70, 55, 40)):
                            with io.BytesIO() as output:
                                if transparent:
                                    clean.save(output, format="PNG", optimize=True)
                                else:
                                    clean.save(output, format="JPEG", quality=quality, optimize=True)
                                preview = output.getvalue()
                            if len(preview) <= MAX_PREVIEW_BYTES:
                                preview_mime = "image/png" if transparent else "image/jpeg"
                                return {"sha256": hashlib.sha256(data).hexdigest(), "mime": mime,
                                        "width": width, "height": height, "size": len(data),
                                        "original_b64": base64.b64encode(data).decode("ascii"),
                                        "preview_data_url": f"data:{preview_mime};base64," + base64.b64encode(preview).decode("ascii")}
                        if max(clean.size) <= 1:
                            raise ImageAssetError("image preview failed")
                        clean.thumbnail((max(1, clean.width * 3 // 4), max(1, clean.height * 3 // 4)), Image.Resampling.LANCZOS)
    except ImageAssetError:
        raise
    except Exception:
        raise ImageAssetError("invalid image") from None


# Only derived previews are cached; original bytes and account data remain in SQLite.
_PREVIEW_CACHE = OrderedDict()
_PREVIEW_CACHE_LOCK = threading.Lock()
_PREVIEW_CACHE_LIMIT = 8 * 1024 * 1024


def derived_preview(asset: dict) -> str:
    """Upgrade old flattened previews after the caller verifies source ownership."""
    preview = asset.get('preview_data_url')
    mime = asset.get('mime')
    if mime not in ('image/png', 'image/webp') or not isinstance(preview, str) or not preview.startswith('data:image/jpeg;base64,'):
        return preview
    original = asset.get('original_b64')
    sha = asset.get('sha256')
    if not isinstance(original, str) or len(original) > ((MAX_BYTES + 2) // 3) * 4:
        raise ImageAssetError('invalid image')
    try:
        data = base64.b64decode(original, validate=True)
    except Exception:
        raise ImageAssetError('invalid image') from None
    if hashlib.sha256(data).hexdigest() != sha:
        raise ImageAssetError('invalid image')
    key = (sha, mime, 1)
    # Serializing regeneration also deduplicates concurrent requests for the same image.
    with _PREVIEW_CACHE_LOCK:
        cached = _PREVIEW_CACHE.get(key)
        if cached is not None:
            _PREVIEW_CACHE.move_to_end(key)
            return cached
        decoded = decode_image(data, mime)
        result = decoded['preview_data_url']
        if len(result) <= _PREVIEW_CACHE_LIMIT:
            while _PREVIEW_CACHE and sum(map(len, _PREVIEW_CACHE.values())) + len(result) > _PREVIEW_CACHE_LIMIT:
                _PREVIEW_CACHE.popitem(last=False)
            _PREVIEW_CACHE[key] = result
        return result


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ImageAssetError("image network timeout")
    return remaining


def _resolve(host: str, deadline: float) -> list[str]:
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise ImageAssetError("image network timeout")
    result = queue.Queue(maxsize=1)

    def work():
        try:
            records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            result.put([record[4][0] for record in records])
        except Exception:
            result.put(None)
        finally:
            _DNS_SLOTS.release()

    try:
        threading.Thread(target=work, daemon=True).start()
    except Exception:
        _DNS_SLOTS.release()
        raise ImageAssetError("image host resolution failed") from None
    try:
        addresses = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise ImageAssetError("image network timeout") from None
    if not addresses:
        raise ImageAssetError("image host resolution failed")
    return addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, deadline: float):
        super().__init__(host, 443, timeout=_remaining(deadline), context=ssl.create_default_context())
        self.address = address
        self.addresses = [address]
        self.deadline = deadline

    def connect(self):
        # DNS can return an unreachable IPv6/edge address before a working one.
        # Retry only connection establishment, against the already validated IPs,
        # with the same hostname/SNI and the original total deadline.
        for index, address in enumerate(self.addresses):
            _remaining(self.deadline)
            ip = ipaddress.ip_address(address)
            family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            raw = socket.socket(family, socket.SOCK_STREAM)
            self.sock = raw
            try:
                raw.settimeout(min(DOWNLOAD_CONNECT_SECONDS, _remaining(self.deadline)))
                raw.connect((address, 443))
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
                self.sock.settimeout(min(DOWNLOAD_CONNECT_SECONDS, _remaining(self.deadline)))
                self.sock.do_handshake()
                self.sock.settimeout(min(DOWNLOAD_IDLE_SECONDS, _remaining(self.deadline)))
                return
            except ssl.SSLError:
                self.close()
                raise
            except OSError:
                self.close()
                if index + 1 == len(self.addresses):
                    raise
                _remaining(self.deadline)


def _transport(host: str, path: str, addresses: list[str], deadline: float) -> tuple[str, bytes]:
    connection = _PinnedHTTPS(host, addresses[0], deadline)
    connection.addresses = list(dict.fromkeys(addresses))[:8]

    def abort():
        sock = connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    timer = threading.Timer(_remaining(deadline), abort)
    timer.daemon = True
    timer.start()
    try:
        connection.request("GET", path, headers={"Accept": "image/png,image/jpeg,image/webp,image/gif", "Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            location = response.getheader('Location')
            if not isinstance(location, str) or not location:
                raise ImageAssetError('image redirect rejected')
            raise _ImageRedirect(location)
        if response.status != 200:
            raise ImageAssetError("image download rejected")
        mime = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if mime not in (*_FORMATS.values(), _BINARY_MIME):
            raise ImageAssetError("unsupported image type")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ImageAssetError("image download rejected")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > MAX_BYTES):
            raise ImageAssetError("image size limit exceeded")
        parts, total = [], 0
        while True:
            _remaining(deadline)
            block = response.read(min(65536, MAX_BYTES + 1 - total))
            if not block:
                break
            total += len(block)
            if total > MAX_BYTES:
                raise ImageAssetError("image size limit exceeded")
            parts.append(block)
        _remaining(deadline)
        return mime, b"".join(parts)
    except (ImageAssetError, _ImageRedirect):
        raise
    except Exception:
        if time.monotonic() >= deadline:
            raise ImageAssetError("image network timeout") from None
        raise ImageAssetError("image download failed") from None
    finally:
        timer.cancel()
        connection.close()


def _image_url(resource_url):
    if not isinstance(resource_url, str) or len(resource_url) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in resource_url):
        raise ImageAssetError('invalid image URL')
    parsed = urlsplit(resource_url)
    host = parsed.hostname
    if parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment or '\\' in resource_url:
        raise ImageAssetError('invalid image URL')
    # This exact OSS bucket was observed as the platform resource's redirect.
    # Arbitrary aliyuncs buckets and HTML/login hosts remain excluded.
    if host not in ('clouddisk.alibaba.com', 'hz-icbuim-file.oss-cn-beijing.aliyuncs.com', 'alicdn.com') and not host.endswith('.alicdn.com'):
        raise ImageAssetError('image host not allowed')
    if any(not label or len(label) > 63 or label[0] == '-' or label[-1] == '-' or not all(c.isascii() and (c.isalnum() or c == '-') for c in label) for label in host.split('.')):
        raise ImageAssetError('image host not allowed')
    if not parsed.query.isascii():
        raise ImageAssetError('invalid image URL')
    path = quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")
    return host, path + ('?' + parsed.query if parsed.query else '')


class _AssetReservation:
    def __init__(self, lease):
        self.lease = lease

    def __del__(self):
        self.lease.close()


class _OwnedString(str):
    """Retain the reservation when callers copy the value into another dict."""
    def __new__(cls, value, reservation):
        result = super().__new__(cls, value)
        result.reservation = reservation
        return result

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def fetch_image(resource_url: str, *, resolver=None, transport=None) -> dict:
    # Cover receive chunks and their joined bytes before the size is known.
    # Decode and encoded-result storage are admitted in their own stages.
    lease = _lease(2 * MAX_BYTES + 65536)
    try:
        asset = _fetch_image_leased(resource_url, resolver=resolver, transport=transport, receive_lease=lease)
        # Constructing the owned string temporarily copies the encoded original.
        reservation = _AssetReservation(lease)
        lease = None
        asset['original_b64'] = _OwnedString(asset['original_b64'], reservation)
        reservation.lease.resize(len(asset['original_b64']) + len(asset['preview_data_url']) + 4096)
        return asset
    finally:
        if lease is not None:
            lease.close()


def _fetch_image_leased(resource_url: str, *, resolver=None, transport=None, receive_lease=None) -> dict:
    try:
        deadline = time.monotonic() + DEADLINE_SECONDS
        for hop in range(MAX_REDIRECTS + 1):
            host, path = _image_url(resource_url)
            addresses = (resolver or _resolve)(host, deadline)
            _remaining(deadline)
            if not addresses:
                raise ImageAssetError('image host resolution failed')
            validated = []
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if not ip.is_global or getattr(ip, 'ipv4_mapped', None) is not None:
                    raise ImageAssetError('unsafe image host address')
                validated.append(str(ip))
            try:
                mime, data = (transport or _transport)(host, path, validated, deadline)
            except _ImageRedirect as redirect:
                if hop == MAX_REDIRECTS:
                    raise ImageAssetError('image redirect limit exceeded') from None
                # Validate the Location before urljoin can normalize its controls.
                if (not isinstance(redirect.location, str) or len(redirect.location) > 8192
                        or any(ord(c) <= 32 or ord(c) == 127 for c in redirect.location)):
                    raise ImageAssetError('image redirect rejected') from None
                resource_url = urljoin(resource_url, redirect.location)
                continue
            _remaining(deadline)
            if receive_lease is not None:
                # Release the now-discarded chunk list before waiting for pixels;
                # several receives must not reserve later encoding work at once.
                receive_lease.resize(len(data) + 65536)
            return decode_image(data, mime, deadline=deadline, receive_lease=receive_lease)
    except (ImageAssetError, ServiceError):
        raise
    except Exception:
        raise ImageAssetError('image retrieval failed') from None
