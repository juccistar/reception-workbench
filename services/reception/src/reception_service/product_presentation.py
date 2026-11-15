"""Independent product-main-image display/cache; never a reception photo source."""
from __future__ import annotations

import base64
import copy
import io
import time

from PIL import Image

from .errors import ServiceError
from .image_assets import fetch_image, _image_url
from .product_identity import product_kind
from .repository import new_id, now_ms


def _gallery_preview(data_url):
    """Keep five previews plus the compatibility cover below the JSON-line budget."""
    if not isinstance(data_url, str):
        return None
    if len(data_url) <= 175_000:
        return data_url
    try:
        prefix, encoded_image = data_url.split(',', 1)
        if prefix not in ('data:image/png;base64', 'data:image/jpeg;base64') or len(encoded_image) > 1_400_000:
            return None
        with Image.open(io.BytesIO(base64.b64decode(encoded_image, validate=True))) as source:
            source.load()
            image = source.convert('RGBA') if 'A' in source.getbands() else source.convert('RGB')
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        if image.mode == 'RGBA' and image.getchannel('A').getextrema()[0] < 255:
            while True:
                output = io.BytesIO()
                image.save(output, format='PNG', optimize=True)
                if output.tell() <= 128 * 1024:
                    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
                if max(image.size) <= 1:
                    return None
                image.thumbnail((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)), Image.Resampling.LANCZOS)
        image = image.convert('RGB')
        for quality in (80, 60, 40):
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=quality, optimize=True)
            if output.tell() <= 128 * 1024:
                return 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
    except Exception:
        return None
    return None


def project_main_image(repo, product, *, include_preview=False):
    if not product:
        return None
    identifier = product['local_product_id']
    row = repo.get('product_main_image', identifier) or {}
    if row and (row.get('seller_id') != product.get('seller_id') or row.get('product_id') != product.get('product_id')):
        row = {}
    state = row.get('status', 'unavailable' if product_kind(product.get('product_id')) == 'custom' else 'not_loaded')
    result = {'local_product_id': identifier, 'status': state, 'updated_at': row.get('updated_at', 0),
              'message': row.get('message', '本地定制编号没有国际站商品主图' if state == 'unavailable' else ''),
              'display_only': True}
    for key in ('job_id', 'error_code', 'stage', 'available_count'):
        if row.get(key) is not None:
            result[key] = row[key]
    if isinstance(row.get('primary_verified'), bool):
        result['primary_verified'] = row['primary_verified']
    if row.get('selection_rule') == 'lowest_image_index':
        result['selection_rule'] = row['selection_rule']
    images = row.get('images')
    if not isinstance(images, list):
        images = [{'status': 'ready', 'asset_sha256': row['asset_sha256']} ] if row.get('asset_sha256') else []
    projected = []
    for image in images[:5]:
        asset = repo.get('image_asset', image.get('asset_sha256')) if image.get('asset_sha256') else None
        image_state = image.get('status', 'unavailable')
        if image_state == 'ready' and not asset:
            image_state = 'unavailable'
        item = {'status': image_state}
        if asset:
            if image_state == 'ready' and asset.get('sha256') == image.get('asset_sha256'):
                item['image_ref'] = {'kind': 'product', 'local_product_id': identifier,
                                     'sha256': asset['sha256'], 'version': row.get('updated_at', 0)}
            for key in ('width', 'height'):
                if isinstance(asset.get(key), int):
                    item[key] = asset[key]
            if include_preview:
                preview = _gallery_preview(asset.get('preview_data_url'))
                if preview:
                    item['data_url'] = preview
                elif image_state == 'ready':
                    item['status'] = 'unavailable'
        projected.append(item)
    result['images'] = projected
    first = next((image for image in projected if image['status'] == 'ready'), None)
    if first:
        for key in ('width', 'height', 'image_ref'):
            if key in first:
                result[key] = first[key]
        if include_preview and first.get('data_url'):
            result['data_url'] = first['data_url']
    elif state == 'ready':
        result.update(status='unavailable', message='本地主图缓存暂不可用')
    return result


class ProductPresentationMixin:
    def _init_product_display(self):
        self._product_main_image_active = set()
        self._product_main_image_next_schedule = 0.0

    def get_product_main_image(self, local_product_id):
        """Local preview only. Neither listing lookup nor image download starts here."""
        with self._lock:
            product = self._library_entity('library_product', local_product_id)
            return project_main_image(self.repo, product, include_preview=True)

    def request_product_images(self, client_request_id, case_id=None, local_product_id=None):
        if (not isinstance(client_request_id, str) or not client_request_id.strip()
                or len(client_request_id) > 200 or any(ord(c) < 32 for c in client_request_id)
                or bool(case_id) == bool(local_product_id)):
            raise ServiceError('invalid_params', '商品图片请求参数无效')
        with self._lock, self.repo.transaction():
            self._library_ready()
            if self._closing or self._stop.is_set() or self._requirement_media_stopped:
                raise ServiceError('closing', '商品图片读取暂不可用')
            if case_id:
                case = self.repo.get('case', case_id)
                if not case or case.get('seller_id') != self.seller_id:
                    raise ServiceError('not_found', '当前商家没有这份需求')
                binding = self.repo.get('requirement_product_binding', case_id)
                if not binding:
                    pid = ((case.get('requirements') or {}).get('product_id') or {}).get('value')
                    if case.get('requirements_format') != 'ai-v1' or product_kind(pid) != 'alibaba':
                        raise ServiceError('product_unbound', '商品图片尚未准备')
                    self._sync_requirement_product(case)
                    binding = self.repo.get('requirement_product_binding', case_id)
                if (not binding or binding.get('seller_id') != self.seller_id
                        or binding.get('case_id') != case_id or binding.get('conversation_id') != case.get('conversation_id')):
                    raise ServiceError('product_unbound', '商品图片关联无效')
                local_product_id = binding.get('local_product_id')
            product = self._library_entity('library_product', local_product_id)
            identifier = product['local_product_id']
            if product_kind(product.get('product_id')) != 'alibaba':
                raise ServiceError('product_image_unavailable', '本地定制编号没有平台商品图片')
            prior = self.repo.get('product_image_request', client_request_id)
            if prior:
                if prior.get('seller_id') != self.seller_id or prior.get('local_product_id') != identifier:
                    raise ServiceError('mutation_conflict', '同一请求编号不能用于不同商品')
                return {'job_id': prior['job_id']}
            record = self.repo.get('product_main_image', identifier) or {}
            if identifier in self._product_main_image_active:
                job_id = record.get('job_id')
                if not job_id:
                    raise ServiceError('product_image_busy', '商品图片正在读取')
                if not record.get('manual_refresh'):
                    old_assets = {record.get('asset_sha256')}
                    old_assets.update(item.get('asset_sha256') for item in record.get('images', []) if isinstance(item, dict))
                    record.update(manual_refresh=True, asset_sha256=None, source_url=None, images=[], available_count=0,
                                  updated_at=max(now_ms(), (record.get('updated_at') or 0) + 1))
                    self.repo.put('product_main_image', identifier, record, history=True)
                    self._discard_unreferenced_product_assets(old_assets)
                    self._changed()
                self.repo.put('product_image_request', client_request_id, {'seller_id': self.seller_id,
                              'local_product_id': identifier, 'job_id': job_id})
                return {'job_id': job_id}
            if not self._requirement_media_slots.acquire(blocking=False):
                raise ServiceError('product_image_busy', '图片读取队列已满，请稍后重试')
            stamp = max(now_ms(), (record.get('updated_at') or 0) + 1)
            job_id = new_id('main_image')
            old_assets = {record.get('asset_sha256')}
            old_assets.update(item.get('asset_sha256') for item in record.get('images', []) if isinstance(item, dict))
            record = {'seller_id': self.seller_id, 'local_product_id': identifier, 'product_id': product['product_id'],
                      'status': 'loading', 'display_only': True, 'job_id': job_id, 'updated_at': stamp,
                      'message': '正在读取商品图片', 'stage': 'product_metadata', 'available_count': 0,
                      'images': [], 'image_group_version': 1, 'manual_refresh': True}
            job = {'job_id': job_id, 'kind': 'load_product_main_image', 'conversation_id': None, 'case_id': case_id,
                   'status': 'queued', 'stage': 'product_metadata', 'message': record['message'],
                   'automatic': False, 'created_at': stamp, 'started_at': None, 'completed_at': None}
            self.repo.put('product_main_image', identifier, record, history=True)
            self.repo.put('job', job_id, job, history=True)
            self.repo.put('product_image_request', client_request_id, {'seller_id': self.seller_id,
                          'local_product_id': identifier, 'job_id': job_id})
            self._discard_unreferenced_product_assets(old_assets)
            self._product_main_image_active.add(identifier)
            try:
                facade = self.integrations
                from .integrations.live import LiveIntegration
                if type(facade) is LiveIntegration:
                    facade = LiveIntegration(self.data_root / self.mode, copy.deepcopy(self.settings))
                self._requirement_media_pool.submit(self._load_product_main_image, self.repo, copy.deepcopy(product), job_id, facade)
            except Exception:
                self._product_main_image_active.discard(identifier)
                self._requirement_media_slots.release()
                record.update(status='failed', stage='queue', error_code='product_image_start_failed', message='商品图片读取未能启动')
                job.update(status='failed', message=record['message'], completed_at=now_ms())
                self.repo.put('product_main_image', identifier, record, history=True)
                self.repo.put('job', job_id, job, history=True)
            self._emit('job.updated', job)
        self._changed()
        return {'job_id': job_id}

    def _discard_unreferenced_product_assets(self, candidates):
        # Only inspect candidates formerly referenced by this product. Other
        # entities and business history may contain references in nested fields.
        for sha in candidates:
            if not isinstance(sha, str) or len(sha) != 64:
                continue
            needle = '%' + sha + '%'
            entity_ref = self.repo.db.execute(
                "SELECT 1 FROM entities WHERE kind != 'image_asset' AND data LIKE ? LIMIT 1", (needle,)).fetchone()
            history_ref = self.repo.db.execute(
                "SELECT 1 FROM revisions WHERE kind NOT IN ('image_asset','product_main_image') AND data LIKE ? LIMIT 1", (needle,)).fetchone()
            if not entity_ref and not history_ref:
                self.repo.db.execute("DELETE FROM entities WHERE kind='image_asset' AND id=?", (sha,))

    def _main_image_record(self, product):
        identifier = product['local_product_id']
        if self.repo.get('product_main_image', identifier):
            return
        custom = product_kind(product['product_id']) == 'custom'
        self.repo.put('product_main_image', identifier, {
            'seller_id': self.seller_id, 'local_product_id': identifier, 'product_id': product['product_id'],
            'status': 'unavailable' if custom else 'not_loaded', 'display_only': True,
            'message': '本地定制编号没有国际站商品主图' if custom else '',
            'job_id': None, 'updated_at': now_ms()})

    def _schedule_product_main_images(self):
        if (not self.account_verified or self._closing or self._stop.is_set()
                or getattr(self, '_requirement_media_stopped', True)):
            return
        if time.monotonic() < self._product_main_image_next_schedule:
            return
        self._product_main_image_next_schedule = time.monotonic() + .5
        with self.repo.transaction():
            for product in self._library_rows('library_product'):
                self._main_image_record(product)
                record = self.repo.get('product_main_image', product['local_product_id'])
                legacy_ready = (record.get('status') == 'ready' and record.get('asset_sha256')
                                and not record.get('image_group_version')
                                and record.get('seller_id') == product.get('seller_id')
                                and record.get('product_id') == product.get('product_id'))
                if record['status'] != 'not_loaded' and not legacy_ready:
                    continue
                if len(self._product_main_image_active) >= 2 or not self._requirement_media_slots.acquire(blocking=False):
                    break
                identifier, stamp = product['local_product_id'], now_ms()
                job_id = new_id('main_image')
                record.update(status='loading', job_id=job_id, updated_at=stamp, message='正在读取商品主图',
                              image_group_version=1)
                job = {'job_id': job_id, 'kind': 'load_product_main_image', 'conversation_id': None, 'case_id': None,
                       'status': 'queued', 'stage': 'product_metadata', 'message': '正在读取商品主图',
                       'automatic': False, 'created_at': stamp, 'started_at': None, 'completed_at': None}
                self.repo.put('product_main_image', identifier, record, history=True)
                self.repo.put('job', job_id, job, history=True)
                self._product_main_image_active.add(identifier)
                try:
                    facade = self.integrations
                    from .integrations.live import LiveIntegration
                    if type(facade) is LiveIntegration:
                        facade = LiveIntegration(self.data_root / self.mode, copy.deepcopy(self.settings))
                    self._requirement_media_pool.submit(self._load_product_main_image,
                                                       self.repo, copy.deepcopy(product), job_id, facade)
                except Exception:
                    self._product_main_image_active.discard(identifier)
                    self._requirement_media_slots.release()
                    record.update(status='failed', message='主图后台读取未能启动', updated_at=now_ms())
                    job.update(status='failed', message=record['message'], completed_at=now_ms())
                    self.repo.put('product_main_image', identifier, record, history=True)
                    self.repo.put('job', job_id, job, history=True)
                self._emit('job.updated', job)

    def _load_product_main_image(self, repo, product, job_id, facade):
        identifier = product['local_product_id']
        try:
            with self._lock:
                if self._closing or self._stop.is_set() or self.repo is not repo:
                    return
                self._job_update(job_id, status='running', started_at=now_ms())
            self._ensure_identity(facade)
            result = facade.read_product_display(self.seller_id, product['product_id'], job_id, cancel_event=self._stop)
            if not isinstance(result, dict) or result.get('product_id') != product['product_id']:
                raise ServiceError('product_image_identity_mismatch', '商品主图返回的商品编号不符')
            rows = result.get('images')
            if not isinstance(rows, list):
                rows = [{'url': result['main_image_url'], 'image_index': 0}] if result.get('main_image_url') else []
            if not rows and result.get('source') not in ('no_image', 'invalid_images'):
                raise ServiceError('product_display_invalid_response', '商品图片响应结构无法识别', stage='product_metadata')
            if result.get('source') == 'unavailable':
                raise ServiceError('product_display_invalid_response', '商品图片响应结构无法识别', stage='product_metadata')
            with self._lock, repo.transaction():
                record = repo.get('product_main_image', identifier) or {}
                current = repo.get('library_product', identifier) or {}
                if (self.repo is not repo or record.get('job_id') != job_id
                        or current.get('seller_id') != product.get('seller_id')
                        or current.get('product_id') != product.get('product_id')):
                    return
                if rows:
                    record['stage'] = 'download'
                    repo.put('product_main_image', identifier, record)
            downloaded = []
            for entry in rows[:5]:
                source_url = entry.get('url') if isinstance(entry, dict) else None
                try:
                    host, _ = _image_url(source_url)
                    if not host.endswith('.alicdn.com'):
                        raise ValueError('invalid product image host')
                    if self._closing or self._stop.is_set():
                        raise ServiceError('cancelled', '商品主图读取已停止')
                    # Demo stays local even if a fixture supplies a URL.
                    asset = fetch_image(source_url) if self.mode == 'live' else None
                except Exception:
                    if self._closing or self._stop.is_set():
                        raise
                    asset = None
                downloaded.append((entry, source_url, asset))
            with self._lock, repo.transaction():
                record = repo.get('product_main_image', identifier) or {}
                current = repo.get('library_product', identifier) or {}
                if (self.repo is not repo or record.get('job_id') != job_id
                        or current.get('seller_id') != product.get('seller_id')
                        or current.get('product_id') != product.get('product_id')):
                    return
                cancelled = self._closing or self._stop.is_set() or self._requirement_media_stopped
                if cancelled:
                    record.update(status='unknown', stage='interrupted', message='主图读取未完成；不会重复查询',
                                  updated_at=max(now_ms(), record.get('updated_at', 0) + 1))
                    job_state = 'cancelled'
                else:
                    images = []
                    for entry, url, asset in downloaded:
                        if asset:
                            repo.put('image_asset', asset['sha256'], asset)
                        images.append({'status': 'ready' if asset else 'failed',
                                       'image_index': entry.get('image_index') if isinstance(entry, dict) else None,
                                       'asset_sha256': asset['sha256'] if asset else None,
                                       'error_code': None if asset else 'product_image_download_failed',
                                       'source_url': url if isinstance(url, str) and url.startswith('https://') else None})
                    first = next((image for image in images if image['status'] == 'ready'), None)
                    # A failed backfill must not discard a previously cached image.
                    if not first and not record.get('manual_refresh') and record.get('asset_sha256') and repo.get('image_asset', record['asset_sha256']):
                        first = {'status': 'ready', 'asset_sha256': record['asset_sha256'],
                                 'source_url': record.get('source_url')}
                        images.insert(0, first)
                    reference_message = ('平台图组首图，仅供展示和找货参考'
                                         if result.get('selection_rule') == 'lowest_image_index' else '')
                    failed_count = sum(1 for image in images if image['status'] == 'failed') + result.get('invalid_count', 0)
                    status = 'ready' if first else 'failed' if failed_count else 'unavailable'
                    message = ('部分商品图片读取失败' if first and failed_count else reference_message if first
                               else '商品图片下载失败' if failed_count else '当前商品未提供图片')
                    record.update(status=status, stage='complete', available_count=sum(1 for image in images if image['status'] == 'ready'),
                        error_code='product_image_download_failed' if failed_count else None,
                        message=message,
                        asset_sha256=first['asset_sha256'] if first else None,
                        source_url=first.get('source_url') if first else None,
                        images=images[:5], invalid_items=[{'image_index': index, 'error_code': 'product_image_invalid_url'}
                            for index in result.get('invalid_indices', [])[:5]],
                        source=result.get('source'), result_sha256=result.get('result_sha256'),
                        selection_rule=result.get('selection_rule'), primary_verified=result.get('primary_verified'),
                        product_binding=result.get('product_binding'), updated_at=max(now_ms(), record.get('updated_at', 0) + 1))
                    job_state = 'failed' if status == 'failed' else 'succeeded'
                repo.put('product_main_image', identifier, record, history=True)
                job = repo.get('job', job_id)
                job.update(status=job_state, message='商品主图已缓存' if record.get('status') == 'ready' and not cancelled else record['message'], completed_at=now_ms())
                repo.put('job', job_id, job, history=True)
            self._emit('job.updated', job)
            self._changed()
        except Exception as exc:
            with self._lock, repo.transaction():
                record = repo.get('product_main_image', identifier) or {}
                current = repo.get('library_product', identifier) or {}
                if (self.repo is repo and record.get('job_id') == job_id
                        and current.get('seller_id') == product.get('seller_id')
                        and current.get('product_id') == product.get('product_id')):
                    record.update(status=('unknown' if self._stop.is_set() else
                                          'ready' if record.get('asset_sha256') and repo.get('image_asset', record['asset_sha256']) else 'failed'),
                                  message='商品主图读取失败，客户图片及接待不受影响',
                                  stage=getattr(exc, 'stage', None) or 'product_metadata',
                                  error_code=getattr(exc, 'code', 'product_image_read_failed'),
                                  updated_at=max(now_ms(), record.get('updated_at', 0) + 1))
                    repo.put('product_main_image', identifier, record, history=True)
                    job = repo.get('job', job_id)
                    job.update(status='failed', message=record['message'], completed_at=now_ms(),
                               error={'code': record['error_code'], 'message': record['message']})
                    repo.put('job', job_id, job, history=True)
                    self._emit('job.updated', job)
            self._changed()
        finally:
            with self._lock:
                self._product_main_image_active.discard(identifier)
                self._requirement_media_slots.release()

    def _source_product_ready(self, params):
        if params.get('reconcile'):
            return True
        task = self.repo.get('procurement_task', params.get('task_id')) or {}
        image = self.repo.get('product_main_image', task.get('local_product_id')) or {}
        return image.get('status') not in ('not_loaded', 'loading')

    def _source_main_image_url(self, local_product_id):
        record = self.repo.get('product_main_image', local_product_id) or {}
        product = self.repo.get('library_product', local_product_id) or {}
        if (record.get('seller_id') == self.seller_id and product.get('seller_id') == self.seller_id
                and record.get('product_id') == product.get('product_id') and record.get('status') == 'ready'
                and record.get('asset_sha256') and self.repo.get('image_asset', record['asset_sha256'])):
            return record.get('source_url')
        return None
