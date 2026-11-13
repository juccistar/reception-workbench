"""Private, conversation-scoped merchant attachment storage and extraction."""
from __future__ import annotations

import base64
import hashlib
import io
from pathlib import PurePath

from PIL import Image

from .errors import ServiceError
from .image_assets import decode_image
from .repository import new_id, now_ms

MAX_FILE = 6 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
MAX_COUNT = 5
MAX_TEXT = 16000
MAX_PAGES = 10
MAX_PREVIEW = 48 * 1024
_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
          '.webp': 'image/webp', '.pdf': 'application/pdf', '.txt': 'text/plain',
          '.md': 'text/markdown', '.csv': 'text/csv'}


def _small_preview(data_url):
    """Bound each display thumbnail independently of the original AI input."""
    prefix = 'data:image/jpeg;base64,'
    if not data_url.startswith(prefix):
        raise ValueError('invalid preview')
    data = base64.b64decode(data_url[len(prefix):], validate=True)
    with Image.open(io.BytesIO(data), formats=('JPEG',)) as source:
        image = source.convert('RGB')
    image.thumbnail((256, 256), Image.Resampling.LANCZOS)
    while True:
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=65, optimize=True)
        data = output.getvalue()
        if len(data) <= MAX_PREVIEW:
            return prefix + base64.b64encode(data).decode('ascii')
        if max(image.size) <= 1:
            raise ValueError('preview limit')
        image.thumbnail((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)), Image.Resampling.LANCZOS)


def parse_attachment(name, content_b64):
    if (not isinstance(name, str) or not name or len(name) > 200 or '/' in name or '\\' in name
            or any(ord(ch) < 32 for ch in name) or not isinstance(content_b64, str)
            or len(content_b64) > ((MAX_FILE + 2) // 3) * 4):
        raise ServiceError('invalid_attachment', '附件名称或大小无效')
    mime = _TYPES.get(PurePath(name).suffix.lower())
    if not mime:
        raise ServiceError('invalid_attachment', '不支持此附件类型')
    try:
        data = base64.b64decode(content_b64, validate=True)
    except (ValueError, TypeError):
        raise ServiceError('invalid_attachment', '附件内容无法读取') from None
    if not data or len(data) > MAX_FILE:
        raise ServiceError('invalid_attachment', '单个附件须大于 0 且不超过 6 MiB')
    sha = hashlib.sha256(data).hexdigest()
    result = {'id': new_id('supplement_attachment'), 'name': name, 'media_type': mime,
              'size': len(data), 'sha256': sha, 'status': 'ready', 'origin': 'business_supplement',
              'created_at': now_ms()}
    try:
        if mime.startswith('image/'):
            image = decode_image(data, mime)
            result['preview_data_url'] = _small_preview(image['preview_data_url'])
        elif mime == 'application/pdf':
            from pypdf import PdfReader, apply_configuration
            with apply_configuration(jbig2dec_binary=None, maximum_declared_stream_length=8_000_000,
                    array_based_stream_maximum_output_length=8_000_000, zlib_maximum_output_length=8_000_000,
                    lzw_maximum_output_length=8_000_000, run_length_maximum_output_length=8_000_000,
                    jbig2_maximum_output_length=8_000_000, brotli_maximum_output_length=8_000_000,
                    stream_decoding_work_maximum_length=16_000_000, stream_parsing_work_maximum_count=100_000,
                    page_tree_maximum_entries=1000, page_tree_maximum_depth=20,
                    page_tree_maximum_page_entries=10000, xform_maximum_invocations_per_extraction=100):
                reader = PdfReader(io.BytesIO(data), strict=True)
                if reader.is_encrypted or len(reader.pages) > MAX_PAGES:
                    raise ValueError('pdf limit')
                texts = []
                total = 0
                for page in reader.pages:
                    text = page.extract_text() or ''
                    total += len(text)
                    if total > MAX_TEXT:
                        raise ValueError('text limit')
                    texts.append(text)
                result['text'] = '\n'.join(texts)
                result['pages'] = len(reader.pages)
                if len(result['text']) > MAX_TEXT or not result['text'].strip():
                    raise ValueError('no text')
        else:
            text = data.decode('utf-8-sig')
            if '\x00' in text or len(text) > MAX_TEXT:
                raise ValueError('text limit')
            result['text'] = text
    except Exception:
        raise ServiceError('attachment_parse_failed', '附件解析失败：图片须有效；文本须为 UTF-8 且不超过 16000 字符；PDF 须未加密、最多 10 页并含可提取文字') from None
    if 'text' in result:
        result['text_sha256'] = hashlib.sha256(result['text'].encode('utf-8')).hexdigest()
    return result, content_b64


def attachment_rows(repo, conversation_id):
    rows = [row for row in repo.list('supplement_attachment', conversation_id=conversation_id)
            if row.get('status') != 'removed']
    for row in rows:
        try:
            content = base64.b64decode(row['original_b64'], validate=True)
            if (not 0 < len(content) <= MAX_FILE or len(content) != row['size']
                    or hashlib.sha256(content).hexdigest() != row['sha256']
                    or ('text' in row and hashlib.sha256(row['text'].encode('utf-8')).hexdigest() != row.get('text_sha256'))):
                raise ValueError('integrity')
        except (ValueError, TypeError, KeyError, AttributeError):
            row['status'] = 'failed'
            row.pop('preview_data_url', None)
            row.pop('text', None)
            row['error'] = '附件读取或完整性校验失败，请移除或从备份恢复'
    return rows


def public_attachment(row, *, preview=True):
    fields = ('id', 'name', 'media_type', 'size', 'sha256', 'status', 'origin', 'created_at', 'pages', 'error')
    result = {key: row[key] for key in fields if key in row}
    if (row.get('status') == 'ready' and row.get('media_type', '').startswith('image/')
            and row.get('conversation_id') and row.get('id') and row.get('sha256')):
        result['image_ref'] = {'kind': 'supplement', 'conversation_id': row['conversation_id'],
                               'attachment_id': row['id'], 'sha256': row['sha256']}
    if preview and row.get('preview_data_url'):
        result['preview_data_url'] = row['preview_data_url']
    if preview and isinstance(row.get('text'), str):
        result['text_preview'] = row['text'][:500]
    return result
