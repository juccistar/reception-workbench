"""Independent product-main-image display/cache; never a reception photo source."""
from __future__ import annotations

import copy
import time

from .errors import ServiceError
from .image_assets import fetch_image, _image_url
from .product_identity import product_kind
from .repository import new_id, now_ms


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
    asset = repo.get('image_asset', row.get('asset_sha256')) if row.get('asset_sha256') else None
    if state == 'ready' and asset:
        result.update(width=asset.get('width'), height=asset.get('height'))
        if include_preview and isinstance(asset.get('preview_data_url'), str):
            result['data_url'] = asset['preview_data_url']
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
                if record['status'] != 'not_loaded':
                    continue
                if len(self._product_main_image_active) >= 2 or not self._requirement_media_slots.acquire(blocking=False):
                    break
                identifier, stamp = product['local_product_id'], now_ms()
                job_id = new_id('main_image')
                record.update(status='loading', job_id=job_id, updated_at=stamp, message='正在读取商品主图')
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
            source_url = result.get('main_image_url')
            asset = None
            if source_url:
                _image_url(source_url)
                if self._closing or self._stop.is_set():
                    raise ServiceError('cancelled', '商品主图读取已停止')
                # Demo stays local even if a fixture supplies a URL.
                asset = fetch_image(source_url) if self.mode == 'live' else None
            with self._lock, repo.transaction():
                record = repo.get('product_main_image', identifier) or {}
                if self.repo is not repo or record.get('job_id') != job_id:
                    return
                cancelled = self._closing or self._stop.is_set() or self._requirement_media_stopped
                if cancelled:
                    record.update(status='unknown', message='主图读取未完成；不会重复查询', updated_at=now_ms())
                    job_state = 'cancelled'
                else:
                    if asset:
                        repo.put('image_asset', asset['sha256'], asset)
                    reference_message = ('平台图组首图，仅供展示和找货参考'
                                         if result.get('selection_rule') == 'lowest_image_index' else '')
                    record.update(status='ready' if asset else 'unavailable',
                        message=reference_message if asset else '商品查询未提供可用主图',
                        asset_sha256=asset['sha256'] if asset else None,
                        source_url=source_url if asset else None,
                        source=result.get('source'), result_sha256=result.get('result_sha256'),
                        selection_rule=result.get('selection_rule'), primary_verified=result.get('primary_verified'),
                        product_binding=result.get('product_binding'), updated_at=now_ms())
                    job_state = 'succeeded'
                repo.put('product_main_image', identifier, record, history=True)
                job = repo.get('job', job_id)
                job.update(status=job_state, message='商品主图已缓存' if asset and not cancelled else record['message'], completed_at=now_ms())
                repo.put('job', job_id, job, history=True)
            self._emit('job.updated', job)
            self._changed()
        except Exception as exc:
            with self._lock, repo.transaction():
                record = repo.get('product_main_image', identifier) or {}
                if self.repo is repo and record.get('job_id') == job_id:
                    record.update(status='unknown' if self._stop.is_set() else 'failed',
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
        if record.get('seller_id') == self.seller_id and record.get('status') == 'ready':
            return record.get('source_url')
        return None
