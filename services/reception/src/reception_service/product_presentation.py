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
            image = source.convert('RGB')
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
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
            downloaded = []
            for entry in rows[:5]:
                source_url = entry.get('url') if isinstance(entry, dict) else None
                try:
                    _image_url(source_url)
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
                if self.repo is not repo or record.get('job_id') != job_id:
                    return
                cancelled = self._closing or self._stop.is_set() or self._requirement_media_stopped
                if cancelled:
                    record.update(status='unknown', message='主图读取未完成；不会重复查询', updated_at=now_ms())
                    job_state = 'cancelled'
                else:
                    images = []
                    for entry, url, asset in downloaded:
                        if asset:
                            repo.put('image_asset', asset['sha256'], asset)
                        images.append({'status': 'ready' if asset else 'failed',
                                       'image_index': entry.get('image_index') if isinstance(entry, dict) else None,
                                       'asset_sha256': asset['sha256'] if asset else None,
                                       'source_url': url if asset else None})
                    first = next((image for image in images if image['status'] == 'ready'), None)
                    # A failed backfill must not discard a previously cached image.
                    if not first and record.get('asset_sha256') and repo.get('image_asset', record['asset_sha256']):
                        first = {'status': 'ready', 'asset_sha256': record['asset_sha256'],
                                 'source_url': record.get('source_url')}
                        images.insert(0, first)
                    reference_message = ('平台图组首图，仅供展示和找货参考'
                                         if result.get('selection_rule') == 'lowest_image_index' else '')
                    record.update(status='ready' if first else 'unavailable',
                        message=reference_message if first else '商品查询未提供可用主图',
                        asset_sha256=first['asset_sha256'] if first else None,
                        source_url=first.get('source_url') if first else None,
                        images=images[:5], source=result.get('source'), result_sha256=result.get('result_sha256'),
                        selection_rule=result.get('selection_rule'), primary_verified=result.get('primary_verified'),
                        product_binding=result.get('product_binding'), updated_at=now_ms())
                    job_state = 'succeeded'
                repo.put('product_main_image', identifier, record, history=True)
                job = repo.get('job', job_id)
                job.update(status=job_state, message='商品主图已缓存' if record.get('status') == 'ready' and not cancelled else record['message'], completed_at=now_ms())
                repo.put('job', job_id, job, history=True)
            self._emit('job.updated', job)
            self._changed()
        except Exception as exc:
            with self._lock, repo.transaction():
                record = repo.get('product_main_image', identifier) or {}
                if self.repo is repo and record.get('job_id') == job_id:
                    record.update(status=('unknown' if self._stop.is_set() else
                                          'ready' if record.get('asset_sha256') and repo.get('image_asset', record['asset_sha256']) else 'failed'),
                                  message='商品主图读取失败，客户图片及接待不受影响',
                                  error_code=getattr(exc, 'code', 'product_image_read_failed'), updated_at=now_ms())
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
