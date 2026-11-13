"""Local requirement media and strictly source-bound product image selection.

Display CAS revision is a stable 52-bit digest of the current local projection and
cover/source settings; it is independent of the case's business revision. No
network or model calls are made by the read operation.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .errors import ServiceError
from .photos import has_photo, photo_version
from .repository import encoded, new_id, now_ms
from .image_assets import fetch_image, _image_url

_OWNER = ('seller_id', 'buyer_id', 'conversation_id')
_SELECT = re.compile(r'\b(?:this|these|those|that)\s+(?:one|ones|item|items|product|products|model|models|style|styles)|\bi\s+(?:want|need|choose|prefer)\s+(?:this|these|those|that)\b|这(?:个|些|款|几款|种)|要这|选这', re.I)
_URL = re.compile(r'https://[^\s<>"\']+', re.I)


def _owned(message, case):
    return all(message.get(k) == case.get(k) for k in _OWNER)


def _quote_ids(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('reply_to_message_id', 'referenced_message_id', 'source_message_id', 'messageId', 'message_id') and isinstance(child, str):
                yield child
            elif key in ('referenced_message_ids', 'source_message_ids', 'evidence_ids') and isinstance(child, list):
                yield from (x for x in child if isinstance(x, str))
            elif isinstance(child, (dict, list)):
                yield from _quote_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from _quote_ids(child)


def _selected_ids(messages):
    result = set()
    for message in messages:
        if message.get('role') != 'buyer' or not _SELECT.search(message.get('text') or ''):
            continue
        result.update(_quote_ids({k: message[k] for k in ('reply_to_message_id', 'referenced_message_id', 'referenced_message_ids') if k in message}))
        for attachment in message.get('attachments') or []:
            if isinstance(attachment, dict) and attachment.get('kind') == 'quote':
                result.update(_quote_ids(attachment.get('content')))
    return result


def _case_selected_ids(repo, case, messages):
    cases = [c for c in repo.list('case', conversation_id=case['conversation_id']) if _owned(c, case)]
    if len(cases) > 1:
        referenced = set(_quote_ids(case))
        messages = [m for m in messages if m.get('message_id') in referenced]
    return _selected_ids(messages)


def get_product_source_images(repo, case, messages):
    """Return owned raw image messages; caller validates image asset/hash as usual.

    Explicit bindings are version-bound and local to the case. A seller image is
    eligible only when a buyer selects it in a structured quote. The sole buyer
    image candidate is used only in a single-case conversation without another
    product source. No downloaded image is needed to make this selection.
    """
    if not isinstance(case, dict) or not case.get('case_id'):
        return []
    owned = [m for m in messages if isinstance(m, dict) and _owned(m, case)]
    images = [m for m in owned if m.get('role') in ('buyer', 'seller') and has_photo(m)]
    selected = _case_selected_ids(repo, case, owned)
    settings = repo.get('requirement_media', case['case_id'], {})
    bindings = settings.get('sources', {}) if all(settings.get(k) == case.get(k) for k in _OWNER) else {}
    product = (case.get('requirements') or {}).get('product_images') or {}
    evidence = set(product.get('evidence_ids') or []) if isinstance(product, dict) else set()
    result = []
    for message in images:
        mid = message['message_id']
        binding = bindings.get(mid, {}) if isinstance(bindings, dict) else {}
        if binding.get('enabled') is False and binding.get('source_version') == photo_version(message):
            continue
        explicit = binding.get('enabled') is True and binding.get('source_version') == photo_version(message)
        # Product evidence must identify an actual buyer image, not a seller's
        # unselected demonstration. Buyer quote supplies the latter's selection.
        eligible = explicit or mid in evidence or mid in selected
        if message.get('role') == 'seller' and mid not in selected:
            eligible = False
        if eligible:
            result.append(message)
    if result:
        return result
    if any(isinstance(v, dict) and v.get('enabled') is False for v in bindings.values()):
        return []
    cases = [c for c in repo.list('case', conversation_id=case['conversation_id']) if _owned(c, case)]
    competing = bool(isinstance(product, dict) and (product.get('value') or product.get('evidence_ids')))
    return images if len(images) == 1 and images[0].get('role') == 'buyer' and len(cases) == 1 and not competing else []


def _valid_product_url(url):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower().rstrip('.')
        return (parts.scheme.lower() == 'https' and not parts.username and not parts.password
                and parts.port in (None, 443) and (host == 'alibaba.com' or host.endswith('.alibaba.com')
                or host == 'aliexpress.com' or host.endswith('.aliexpress.com'))
                and parts.path.lower().startswith(('/product-detail/', '/product/', '/item/')))
    except (ValueError, TypeError):
        return False


def _media_id(kind, message_id, discriminator=''):
    return 'media_' + hashlib.sha256(encoded([kind, message_id, discriminator]).encode()).hexdigest()


class RequirementMediaMixin:
    def _init_requirement_media(self):
        self._requirement_media_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='requirement-media')
        self._requirement_media_slots = threading.BoundedSemaphore(4)
        self._requirement_media_stopped = False
        self._requirement_media_active = set()

    def _stop_requirement_media(self):
        self._requirement_media_stopped = True
        # Queued tasks must run their cancellation cleanup before the repository closes.
        self._requirement_media_pool.shutdown(wait=True, cancel_futures=False)

    def _requirement_media_case(self, case_id):
        case = self._require('case', case_id)
        conversation = self._require('conversation', case['conversation_id'])
        if (not self.account_verified or case.get('seller_id') != self.seller_id
                or any(case.get(k) != conversation.get(k) for k in _OWNER)):
            raise ServiceError('message_identity_mismatch', '采购事项不属于当前已核验会话')
        return case

    def get_requirement_media(self, case_id):
        with self._lock:
            case = self._requirement_media_case(case_id)
            messages = [m for m in self.repo.list('message', conversation_id=case['conversation_id']) if _owned(m, case)]
            sources = {m['message_id'] for m in get_product_source_images(self.repo, case, messages)}
            cases = [c for c in self.repo.list('case', conversation_id=case['conversation_id']) if _owned(c, case)]
            if len(cases) > 1:
                settings = self.repo.get('requirement_media', case_id, {})
                bindings = settings.get('sources', {}) if all(settings.get(k) == case.get(k) for k in _OWNER) else {}
                relevant = set(_quote_ids(case)) | sources | set(bindings)
                messages = [m for m in messages if m['message_id'] in relevant]
            items = []
            for message in messages:
                mid = message['message_id']
                if has_photo(message) and message.get('role') in ('buyer', 'seller'):
                    version = photo_version(message)
                    state = self._photo_state(message)
                    item = {'media_id': _media_id('customer_image', mid), 'kind': 'customer_image',
                            'source_message_id': mid, 'role': message['role'], 'source_version': version,
                            'status': state.get('status', 'not_loaded'), 'is_product_source': mid in sources}
                    if state.get('reason'):
                        item['reason'] = state['reason']
                    if item['status'] == 'ready':
                        asset = self.repo.get('image_asset', state.get('sha256'), {})
                        preview = asset.get('preview_data_url')
                        if asset.get('sha256') == state.get('sha256') and isinstance(preview, str) and preview.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,')) and len(preview) <= 1_400_000:
                            item['data_url'] = preview
                        else:
                            item['status'] = 'failed'
                    items.append(item)
            settings = self.repo.get('requirement_media', case_id, {})
            if any(settings.get(k) != case.get(k) for k in _OWNER):
                settings = {}
            cover = settings.get('cover_id')
            if cover not in {item['media_id'] for item in items}:
                cover = None
            # Cache bytes can be large; hash their stable content, not include them in CAS material.
            material = [{k: (hashlib.sha256(v.encode()).hexdigest() if k == 'data_url' else v)
                         for k, v in item.items()} for item in items]
            revision = int(hashlib.sha256(encoded([material, cover, settings.get('sources', {})]).encode()).hexdigest()[:13], 16)
            return {'case_id': case_id, 'revision': revision, 'cover_id': cover, 'items': items}

    def _mutate_requirement_media(self, operation, case_id, expected_revision, client_mutation_id, **changes):
        if (not isinstance(case_id, str) or not case_id or type(expected_revision) is not int
                or not isinstance(client_mutation_id, str) or not 1 <= len(client_mutation_id.strip()) <= 200):
            raise ServiceError('invalid_params', '媒体操作参数无效')
        request = {'operation': operation, 'case_id': case_id, 'expected_revision': expected_revision, **changes}
        with self._lock, self.repo.transaction():
            case = self._requirement_media_case(case_id)
            old = self.repo.get('requirement_media_mutation', client_mutation_id)
            if old:
                if old.get('request') != request:
                    raise ServiceError('mutation_conflict', '操作编号已被其他请求使用')
                return old['result']
            current = self.get_requirement_media(case_id)
            if current['revision'] != expected_revision:
                raise ServiceError('revision_conflict', '媒体资料已变化，请刷新后重试')
            settings = self.repo.get('requirement_media', case_id, {})
            if any(settings.get(k) != case.get(k) for k in _OWNER):
                settings = {k: case[k] for k in _OWNER}
            if operation == 'cover':
                media_id = changes['media_id']
                if media_id is not None and not any(row['media_id'] == media_id and row['kind'] != 'product_link' for row in current['items']):
                    raise ServiceError('image_not_found', '封面图片不存在')
                settings['cover_id'] = media_id
            else:
                message_id, version, enabled = changes['message_id'], changes['source_version'], changes['enabled']
                message = self.repo.get('message', encoded([case['conversation_id'], message_id]))
                if (not message or not _owned(message, case) or not has_photo(message)
                        or message.get('role') not in ('buyer', 'seller') or photo_version(message) != version):
                    raise ServiceError('source_changed', '图片来源已变化，请刷新后重试')
                messages = [m for m in self.repo.list('message', conversation_id=case['conversation_id']) if _owned(m, case)]
                if enabled and message['role'] == 'seller' and message_id not in _case_selected_ids(self.repo, case, messages):
                    raise ServiceError('invalid_params', '商家图片须由客户明确引用选择')
                settings.setdefault('sources', {})[message_id] = {'source_version': version, 'enabled': enabled}
            self.repo.put('requirement_media', case_id, settings)
            if operation == 'source':
                self._invalidate(case['conversation_id'])
            result = self.get_requirement_media(case_id)
            self.repo.put('requirement_media_mutation', client_mutation_id, {'request': request, 'result': result})
        self._changed(case['conversation_id'], case_id)
        return result

    def load_requirement_media(self, case_id, media_id, client_request_id):
        raise ServiceError("image_unavailable", "历史商品卡图片不再作为图片来源")

    def _finish_requirement_media_cancel(self, repo, case, item, job_id, status):
        """Finish only this job and its own cache association, never a replacement."""
        if self.repo is not repo:
            return None
        with repo.transaction():
            job = repo.get('job', job_id)
            if not job or job.get('status') not in ('queued', 'running'):
                return None
            key = encoded([case['case_id'], item['media_id']])
            cache = repo.get('requirement_media_cache', key, {})
            if cache.get('job_id') == job_id and all(cache.get(k) == case.get(k) for k in _OWNER):
                cache.pop('sha256', None)
                cache.update(status='failed', reason='cancelled' if status == 'cancelled' else 'source_changed')
                repo.put('requirement_media_cache', key, cache)
            job.update(status=status, stage=status, completed_at=now_ms(),
                       message='商品图片加载已取消' if status == 'cancelled' else '商品图片来源已变化')
            repo.put('job', job_id, job, history=True)
            return job

    def _load_requirement_media_worker(self, repo, epoch, case, item, url, job_id):
        job = None
        try:
            with self._lock:
                cancelled = (self._requirement_media_stopped or self._closing or self.repo is not repo
                             or self._epoch != epoch or not self.account_verified or self.seller_id != case['seller_id'])
                if cancelled:
                    job = self._finish_requirement_media_cancel(repo, case, item, job_id, 'cancelled')
            if not cancelled:
                try:
                    asset = fetch_image(url)
                except Exception:
                    asset = None
                with self._lock:
                    cancelled = (self._requirement_media_stopped or self._closing or self.repo is not repo
                                 or self._epoch != epoch or not self.account_verified or self.seller_id != case['seller_id'])
                    if cancelled:
                        job = self._finish_requirement_media_cancel(repo, case, item, job_id, 'cancelled')
                    else:
                        current_case = repo.get('case', case['case_id'], {})
                        message = repo.get('message', encoded([case['conversation_id'], item['source_message_id']]), {})
                        cache_key = encoded([case['case_id'], item['media_id']])
                        cache = repo.get('requirement_media_cache', cache_key, {})
                        if (any(current_case.get(k) != case.get(k) for k in _OWNER) or not _owned(message, case)
                                or photo_version(message) != item['source_version'] or cache.get('job_id') != job_id):
                            job = self._finish_requirement_media_cancel(repo, case, item, job_id, 'stale')
                        else:
                            with repo.transaction():
                                job = repo.get('job', job_id)
                                if job and job.get('status') in ('queued', 'running'):
                                    if asset:
                                        repo.put('image_asset', asset['sha256'], asset)
                                        cache.update(status='ready', sha256=asset['sha256'])
                                    else:
                                        cache.update(status='failed')
                                    repo.put('requirement_media_cache', cache_key, cache)
                                    job.update(status='succeeded' if asset else 'failed', stage='complete', completed_at=now_ms(),
                                               message='商品图片已加载' if asset else '商品图片加载失败')
                                    repo.put('job', job_id, job, history=True)
                                else:
                                    job = None
            if job:
                self._emit('job.updated', job)
                self._changed(case['conversation_id'], case['case_id'])
        finally:
            with self._lock:
                self._requirement_media_active.discard(job_id)
            self._requirement_media_slots.release()

    def set_requirement_media_cover(self, case_id, media_id, expected_revision, client_mutation_id):
        if media_id is not None and (not isinstance(media_id, str) or not media_id):
            raise ServiceError('invalid_params', '封面图片无效')
        return self._mutate_requirement_media('cover', case_id, expected_revision, client_mutation_id, media_id=media_id)

    def set_product_image_source(self, case_id, message_id, source_version, enabled, expected_revision, client_mutation_id):
        if (not isinstance(message_id, str) or not message_id or not isinstance(source_version, str)
                or not re.fullmatch(r'[0-9a-f]{64}', source_version) or type(enabled) is not bool):
            raise ServiceError('invalid_params', '图片来源参数无效')
        return self._mutate_requirement_media('source', case_id, expected_revision, client_mutation_id,
                                              message_id=message_id, source_version=source_version, enabled=enabled)
