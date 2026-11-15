"""Version-bound photo previews on the existing business worker.

Original messages stay unchanged. Both image bytes and the association are inside
seller SQLite, so the existing full database backup includes them. Pixel loading
alone grants no AI capability and never changes a send or reception permission.
"""
from __future__ import annotations

import copy
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
import re
import time

from .errors import ServiceError, read_diagnostics
from .photos import (has_photo, is_photo_placeholder, photo_version, with_photo_notice,
                     photo_observation_id, valid_photo_observation, pending_photo_message_ids)
from .repository import encoded, new_id, now_ms

_ACTIVE = frozenset({'queued', 'running'})
_MAX_ACTIVE = 4
_MAX_ATTEMPTS = 3
_RETRY_MS = 60_000


class MessagePhotosMixin:
    def _photo_target(self, conversation_id, message_id, source_version):
        if (not isinstance(conversation_id, str) or not isinstance(message_id, str)
                or not isinstance(source_version, str) or not re.fullmatch(r'[0-9a-f]{64}', source_version)):
            raise ServiceError('invalid_params', '图片请求缺少有效消息版本')
        conversation = self._require('conversation', conversation_id)
        message = self._require('message', encoded([conversation_id, message_id]))
        if (not self.account_verified or conversation.get('seller_id') != self.seller_id
                or message.get('seller_id') != self.seller_id
                or message.get('buyer_id') != conversation.get('buyer_id')
                or message.get('conversation_id') != conversation_id):
            raise ServiceError('message_identity_mismatch', '图片不属于当前已核验会话')
        if not has_photo(message):
            raise ServiceError('image_not_found', '该消息没有图片记录')
        if photo_version(message) != source_version:
            raise ServiceError('source_changed', '图片消息已变化，请刷新后查看')
        return message

    def _photo_state(self, message):
        version = photo_version(message)
        if message.get('role') not in ('buyer', 'seller') or not is_photo_placeholder(message.get('text')):
            return {'source_version': version, 'status': 'unavailable', 'reason': 'format_not_supported',
                    'message': '此类图片记录尚不能取得原图，请在国际站查看'}
        record = self.repo.get('message_image', encoded([message['conversation_id'], message['message_id']]), {})
        if (record.get('source_version') != version or any(record.get(k) != message.get(k) for k in
                ('seller_id', 'buyer_id', 'conversation_id', 'message_id'))):
            return {'source_version': version, 'status': 'not_loaded', 'message': '照片待加载'}
        if record.get('status') == 'loading':
            job = self.repo.get('job', record.get('job_id'))
            if not job or job.get('status') not in _ACTIVE:
                return {**record, 'status': 'failed', 'message': '上次图片读取未完成，可重试'}
        return record

    def _photo_projection(self, message):
        return with_photo_notice(message, self._photo_state(message) if has_photo(message) else None)

    def get_message_image(self, conversation_id, message_id, source_version):
        with self._lock:
            message = self._photo_target(conversation_id, message_id, source_version)
            state = self._photo_state(message)
            result = {key: state[key] for key in ('status', 'source_version', 'message') if key in state}
            result['input_mode'] = 'direct'
            result['vision_status'] = 'unavailable' if state.get('status') in ('failed', 'unavailable') else 'not_started'
            if state.get('status') == 'ready':
                asset = self.repo.get('image_asset', state.get('sha256'))
                if not asset or asset.get('sha256') != state.get('sha256'):
                    return {**result, 'status': 'failed', 'vision_status': 'unavailable', 'message': '本地图片内容缺失，可重试'}
                # Original bytes and resource URLs never cross the renderer boundary.
                result.update(data_url=asset['preview_data_url'], width=asset['width'],
                              height=asset['height'], sha256=asset['sha256'],
                              image_ref={'kind': 'message', 'conversation_id': conversation_id, 'message_id': message_id,
                                         'source_version': source_version, 'sha256': asset['sha256']})
                record = self.repo.get('photo_observation', encoded([conversation_id, message_id]), {})
                observation = valid_photo_observation(message, record, asset['sha256'])
                if observation:
                    result.update(vision_status='ready', vision_description=observation['description'])
                elif self._vision_record_matches(message, record, asset['sha256']):
                    if record.get('status') == 'processing':
                        job = self.repo.get('job', record.get('job_id'), {})
                        result['vision_status'] = 'processing' if job.get('status') in _ACTIVE else 'failed'
                    elif record.get('status') == 'failed':
                        result['vision_status'] = 'failed'
            return result

    def _enqueue_photo(self, params):
        required = {'conversation_id', 'message_id', 'source_version'}
        if required - set(params) or set(params) - (required | {'retry'}) or type(params.get('retry', False)) is not bool:
            raise ServiceError('invalid_params', '图片任务参数不完整或含未知字段')
        with self._lock, self.repo.transaction():
            if self._closing:
                raise ServiceError('cancelled', '服务正在退出')
            message = self._photo_target(**{key: params[key] for key in required})
            state = self._photo_state(message)
            if state['status'] == 'unavailable':
                raise ServiceError('image_unavailable', state['message'])
            jobs = self.repo.list('job')
            active = [job for job in jobs if job['kind'] == 'load_message_image' and job['status'] in _ACTIVE]
            existing = next((job for job in active if job.get('conversation_id') == params['conversation_id']
                             and job.get('source_message_id') == params['message_id']
                             and job.get('image_source_version') == params['source_version']
                             and job['job_id'] == state.get('job_id')), None)
            if existing:
                return {'job_id': existing['job_id']}
            cached_job = self.repo.get('job', state.get('job_id')) if state.get('job_id') else None
            asset_exists = state.get('status') == 'ready' and self.repo.get('image_asset', state.get('sha256')) is not None
            if cached_job and (asset_exists or state['status'] == 'failed' and not params.get('retry')):
                return {'job_id': cached_job['job_id']}
            attempts = state.get('attempts', 0)
            if attempts >= _MAX_ATTEMPTS:
                raise ServiceError('image_retry_limit', '本条图片已达到读取次数上限，请在国际站查看')
            if attempts and now_ms() - state.get('attempted_at', 0) < _RETRY_MS:
                raise ServiceError('image_retry_wait', '图片读取刚结束，请稍后再试')
            if len(active) >= _MAX_ACTIVE:
                raise ServiceError('image_queue_full', '图片读取队列已满，请稍后加载')
            job = {'job_id': new_id('job'), 'kind': 'load_message_image',
                   'conversation_id': params['conversation_id'], 'case_id': None,
                   'source_message_id': params['message_id'], 'image_source_version': params['source_version'],
                   'photo_generation': self._monitor_generation,
                   'status': 'queued', 'stage': 'queued', 'message': '等待读取照片', 'automatic': False,
                   'created_at': now_ms(), 'started_at': None, 'completed_at': None}
            self.repo.put('job', job['job_id'], job, history=True)
            record = {'conversation_id': params['conversation_id'], 'message_id': params['message_id'],
                      'seller_id': self.seller_id, 'buyer_id': message['buyer_id'],
                      'source_version': params['source_version'], 'status': 'loading',
                      'message': '正在读取照片', 'job_id': job['job_id'],
                      'attempts': attempts + 1, 'attempted_at': now_ms()}
            self.repo.put('message_image', encoded([params['conversation_id'], params['message_id']]), record, history=True)
            self._queue.put((job['job_id'], 'load_message_image', copy.deepcopy(params), self._epoch, False))
        self._emit('job.updated', job)
        self._changed(params['conversation_id'])
        return {'job_id': job['job_id']}

    def _queue_recent_photos(self, conversation_id):
        # Preview queue capacity is backpressure, not a business image limit.
        with self._lock:
            candidates = self._vision_candidates(conversation_id, include_seller=True)
            for message in candidates:
                if self._photo_state(message)['status'] != 'not_loaded':
                    continue
                try:
                    self._enqueue_photo({'conversation_id': conversation_id, 'message_id': message['message_id'],
                                         'source_version': photo_version(message)})
                except ServiceError:
                    # Optional photo retrieval never turns a successful text sync into failure.
                    pass

    def _vision_candidates(self, conversation_id, *, include_seller=False):
        with self._lock:
            return self._vision_candidates_locked(conversation_id, include_seller=include_seller)

    def _vision_candidates_locked(self, conversation_id, *, include_seller=False):
        # Count text messages separately: image bursts do not shrink the text window.
        roles = ('buyer', 'seller') if include_seller else ('buyer',)
        messages = sorted(self.repo.list('message', conversation_id=conversation_id),
                          key=lambda row: (row.get('timestamp_ms') or -1, row['message_id']))
        from .reception_context import dialogue_window
        conversation = self.repo.get('conversation', conversation_id, {})
        case = self.repo.get('case', conversation.get('case_id'), {}) if conversation.get('case_id') else {}
        pending = pending_photo_message_ids(self.repo, conversation_id, messages)
        recent, historical = dialogue_window(messages, case, pending_message_ids=pending)
        selected = {row['message_id'] for row in recent + historical}
        return [m for m in messages if m['message_id'] in selected and m.get('role') in roles
                and is_photo_placeholder(m.get('text'))]

    @staticmethod
    def _vision_record_matches(message, record, sha):
        return (isinstance(record, dict) and isinstance(sha, str)
                and all(record.get(k) == message.get(k) for k in
                        ('seller_id', 'buyer_id', 'conversation_id', 'message_id'))
                and record.get('source_version') == photo_version(message)
                and record.get('image_sha256') == sha
                and record.get('observation_id') == photo_observation_id(message, sha))

    def _vision_cached(self, message, sha, *, retry_failed=False):
        record = self.repo.get('photo_observation', encoded([message['conversation_id'], message['message_id']]), {})
        if not self._vision_record_matches(message, record, sha):
            return False
        # Only an explicit manual generation may retry a known failure, with the
        # same bounds as previews. An abandoned in-flight request is not replayed.
        if record.get('status') == 'failed' and retry_failed:
            return (record.get('attempts', _MAX_ATTEMPTS) >= _MAX_ATTEMPTS
                    or now_ms() - record.get('attempted_at', now_ms()) < _RETRY_MS)
        return bool(valid_photo_observation(message, record, sha) or record.get('status') in ('failed', 'processing'))

    def _vision_needed(self, conversation_id):
        count = 0
        for message in self._vision_candidates(conversation_id):
            image = self._photo_state(message)
            if image.get('status') in ('failed', 'unavailable'):
                continue
            if image.get('status') == 'ready' and self._vision_cached(message, image.get('sha256')):
                continue
            count += 1
        return count

    def _prepare_direct_photos(self, conversation_id, job_id, epoch, facade):
        """Load originals on this worker; the main AI request reads them itself."""
        started = time.monotonic()
        cid = conversation_id
        with self._lock:
            token = self._read_token(facade)
            original_messages = copy.deepcopy(self.repo.list('message', conversation_id=cid))
            original_conversation = copy.deepcopy(self._require('conversation', cid))
            original_chat = self.repo.get('chat_draft', cid)
            original_note = self._supplement(cid)
            from .requirement_media import get_product_source_images
            job_case_id = self._require('job', job_id).get('case_id')
            case = self.repo.get('case', job_case_id, {}) if job_case_id else {}
            if any(case.get(key) != original_conversation.get(key) for key in ('seller_id', 'buyer_id', 'conversation_id')):
                case = {}
            cases = [c for c in self.repo.list('case', conversation_id=cid)
                     if all(c.get(key) == original_conversation.get(key) for key in ('seller_id', 'buyer_id', 'conversation_id'))]
            def selected_images(rows):
                selected = {}
                for candidate_case in cases:
                    for candidate in get_product_source_images(self.repo, candidate_case, rows):
                        selected[candidate['message_id']] = candidate
                return list(selected.values())
            candidates = copy.deepcopy(selected_images(original_messages))
            seconds = self.settings.get('image_prepare_seconds', 60)
            seconds = min(seconds, 60) if type(seconds) is int and seconds >= 1 else 60
            batch_mib = self.settings.get('image_batch_mib', 64)
            batch_mib = batch_mib if type(batch_mib) is int and 1 <= batch_mib <= 1024 else 64
            parallelism = self.settings.get('image_parallelism', 2)
            parallelism = parallelism if type(parallelism) is int and 1 <= parallelism <= 4 else 2
            bindings = {}
            candidate_order = sorted(reversed(candidates),
                                     key=lambda message: self._photo_state(message).get('status') != 'loading')
            from .model_payload import _safe_text
            def message_basis(rows):
                selected = selected_images(rows)
                return ([(m.get('message_id'), m.get('role'), _safe_text(m.get('text') or ''), m.get('timestamp_ms'))
                         for m in rows], [(m['message_id'], photo_version(m)) for m in selected])
            original_basis = message_basis(original_messages)

        def guard():
            self._require_current_read(token)
            conversation = self._require('conversation', cid)
            if (epoch != self._epoch or self._closing
                    or any(conversation.get(key) != original_conversation.get(key) for key in
                           ('seller_id', 'buyer_id', 'case_id', 'revision', 'human_owned'))
                    or [c for c in self.repo.list('case', conversation_id=cid)
                        if all(c.get(key) == original_conversation.get(key) for key in ('seller_id', 'buyer_id', 'conversation_id'))] != cases
                    or message_basis(self.repo.list('message', conversation_id=cid)) != original_basis
                    or self.repo.get('chat_draft', cid) != original_chat
                    or self._supplement(cid) != original_note):
                raise ServiceError('cancelled', '图片准备期间来源或权限已变化')
            for message, sha in bindings.values():
                current = self._photo_target(cid, message['message_id'], photo_version(message))
                state = self._photo_state(current)
                asset = self.repo.image_transfer_metadata('image_asset', sha)
                if state.get('status') != 'ready' or state.get('sha256') != sha or not asset or asset.get('sha256') != sha:
                    raise ServiceError('cancelled', '图片内容已变化，旧回复未应用')

        deadline = started + seconds
        cancel_event = self._generation_cancel_event(job_id)
        coverage = {message['message_id']: {'message_id': message['message_id'], 'role': message['role'],
                                           'status': 'pending', 'reason': 'not_prepared'} for message in candidates}
        from .model_media import PhotoDeadline
        guard.photo_deadline = PhotoDeadline(deadline, started)
        guard.photo_coverage = coverage
        total_bytes = 0
        pending = []
        split = callable(getattr(facade, 'read_photo_source', None)) and callable(getattr(facade, 'fetch_photo_source', None))
        pool = ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix='photo-download') if split else None

        def check_budget():
            if cancel_event is not None and cancel_event.is_set():
                raise ServiceError('cancelled', '图片准备已取消')
            if time.monotonic() >= deadline:
                raise ServiceError('photo_preparation_timeout', '图片准备超过时间预算，本轮图片尚未全部读取')

        def publish_coverage():
            items = copy.deepcopy(list(coverage.values()))
            ready_count = sum(item['status'] == 'ready' for item in items)
            failed_count = sum(item['status'] == 'failed' for item in items)
            pending_count = len(items) - ready_count - failed_count
            self._job_update(job_id, photo_coverage={'total_count': len(items), 'ready_count': ready_count,
                             'failed_count': failed_count, 'pending_count': pending_count, 'items': items},
                             diagnostics={'photo_candidates': len(items), 'photo_ready': ready_count,
                                          'photo_failed': failed_count, 'photo_pending': pending_count,
                                          'photo_bytes': total_bytes,
                                          'photo_prepare_ms': int((time.monotonic() - started) * 1000)})

        def bind(message):
            nonlocal total_bytes
            with self._lock:
                guard()
                state = self._photo_state(message)
                sha = state.get('sha256')
                asset = self.repo.image_transfer_metadata('image_asset', sha) if sha else None
                if state.get('status') != 'ready' or not asset or asset.get('sha256') != sha:
                    coverage[message['message_id']].update(status='failed', reason='load_failed')
                    return
                size = asset.get('size')
                if type(size) is not int or size <= 0:
                    coverage[message['message_id']].update(status='failed', reason='invalid_asset')
                    return
                if total_bytes + size > batch_mib * 1024 * 1024:
                    coverage[message['message_id']].update(status='pending', reason='byte_budget')
                    raise ServiceError('photo_preparation_budget', '图片总大小超过预算，本轮图片尚未全部读取')
                total_bytes += size
                bindings[message['message_id']] = (message, sha)
                coverage[message['message_id']].update(status='ready', reason='awaiting_request_read')

        def finish_one():
            message, preview_id, future = pending.pop(0)
            try:
                while True:
                    check_budget()
                    try:
                        asset = future.result(timeout=min(.1, max(.001, deadline - time.monotonic())))
                        loaded = (asset, None)
                        break
                    except FutureTimeout as failure:
                        if future.done():
                            loaded = (None, failure)
                            break
                        continue
                    except Exception as failure:
                        loaded = (None, failure)
                        break
                params = {'conversation_id': cid, 'message_id': message['message_id'],
                          'source_version': photo_version(message)}
                result = self._task_load_message_image(params, preview_id, epoch, facade, loaded=loaded)
                status = result.pop('_job_status', 'succeeded')
                self._job_update(preview_id, status=status, stage='complete', completed_at=now_ms(), message=result['message'])
            except BaseException:
                self._job_update(preview_id, status='cancelled', stage='cancelled', completed_at=now_ms(), message='图片读取已取消')
                raise
            bind(message)
            publish_coverage()

        try:
            publish_coverage()
            for message in candidate_order:
                check_budget()
                self._yield_to_ready_work()
                with self._lock:
                    guard()
                    state = self._photo_state(message)
                if state.get('status') == 'not_loaded':
                    try:
                        self._enqueue_photo({'conversation_id': cid, 'message_id': message['message_id'],
                                             'source_version': photo_version(message)})
                    except ServiceError as error:
                        coverage[message['message_id']].update(reason='queue_unavailable')
                        raise ServiceError('photo_preparation_pending', '图片尚未准备完成，请稍后重试') from error
                    with self._lock:
                        state = self._photo_state(message)
                if state.get('status') != 'loading':
                    bind(message)
                    publish_coverage()
                    continue
                preview_id = state.get('job_id')
                with self._lock:
                    queued = self.repo.get('job', preview_id, {}).get('status') == 'queued'
                    if queued:
                        self._job_update(preview_id, status='running', stage='load_message_image', started_at=now_ms())
                if not queued:
                    coverage[message['message_id']].update(reason='loading_elsewhere')
                    publish_coverage()
                    while True:
                        check_budget()
                        with self._lock:
                            guard()
                            state = self._photo_state(message)
                        if state.get('status') != 'loading':
                            bind(message)
                            publish_coverage()
                            break
                        if cancel_event is not None:
                            cancel_event.wait(min(.05, max(.001, deadline - time.monotonic())))
                        else:
                            time.sleep(min(.05, max(.001, deadline - time.monotonic())))
                    continue
                if split:
                    try:
                        source = facade.read_photo_source(copy.deepcopy(message))
                        future = pool.submit(facade.fetch_photo_source, source)
                    except Exception as failure:
                        future = Future()
                        future.set_exception(failure)
                else:
                    future = Future()
                    try:
                        future.set_result(facade.load_photo(copy.deepcopy(message)))
                    except Exception as failure:
                        future.set_exception(failure)
                pending.append((message, preview_id, future))
                if len(pending) >= (parallelism if split else 1):
                    finish_one()
            while pending:
                finish_one()
            check_budget()
            return guard
        except ServiceError as error:
            for item in coverage.values():
                if item['status'] == 'pending' and item['reason'] == 'not_prepared':
                    item['reason'] = error.code
            raise
        finally:
            for message, preview_id, future in pending:
                future.cancel()
                self._job_update(preview_id, status='cancelled', stage='cancelled', completed_at=now_ms(), message='图片读取已取消')
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            publish_coverage()

    def _direct_photo_inputs(self, context):
        """Private arguments for an owned integration, never an RPC/image DTO."""
        photo = context.get('photo_context') or {}
        from .model_media import OwnedInput
        from .media_resources import ResourceLimitError
        refs = [('image_asset', ref['image_sha256']) for ref in photo.get('direct_images', [])]
        refs += [('supplement_attachment', ref['attachment_id']) for ref in context.get('supplement_image_refs', [])]
        with self._lock:
            metadata = [(kind, key, self.repo.image_transfer_metadata(kind, key)) for kind, key in refs]
            if any(not item or item.get('content_type') != 'text'
                   or type(item.get('content_length')) is not int or item['content_length'] <= 0
                   for _, _, item in metadata):
                raise ServiceError('source_changed', '图片内容已变化，请重新生成')
            try:
                owner = OwnedInput(2 * 1024 * 1024 + sum(item['content_length'] * 4 for _, _, item in metadata))
            except ResourceLimitError as error:
                raise ServiceError('model_resource_over_limit' if error.code == 'resource_too_large' else 'model_resource_busy',
                                   '模型输入超出资源预算', stage='model') from None
            try:
                return self._snapshot_direct_photo_inputs(context, photo, metadata, owner)
            except BaseException:
                owner.close_if_owned()
                raise

    def _snapshot_direct_photo_inputs(self, context, photo, metadata, owner):
        from .model_media import OwnedPhotoInputs
        result = OwnedPhotoInputs(owner)
        with self._lock:
            for ref in photo.get('direct_images', []):
                message = self._photo_target(ref['conversation_id'], ref['message_id'], ref['source_version'])
                asset = self.repo.get('image_asset', ref['image_sha256'])
                state = self._photo_state(message)
                if (ref.get('role') != message.get('role') or not asset
                        or state.get('sha256') != ref['image_sha256'] or asset.get('sha256') != ref['image_sha256']):
                    raise ServiceError('source_changed', '图片内容已变化，请重新生成')
                result.append({'message': copy.deepcopy(message), 'asset': copy.deepcopy(asset)})
            for ref in context.get('supplement_image_refs', []):
                item = self.repo.get('supplement_attachment', ref['attachment_id'])
                note = self._supplement(ref['conversation_id'])
                if (not item or item.get('origin') != 'business_supplement'
                        or any(item.get(k) != ref.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id'))
                        or item.get('sha256') != ref.get('image_sha256')
                        or note.get('revision') != ref.get('supplement_revision')):
                    raise ServiceError('source_changed', '业务补充图片已变化，请重新生成')
                result.append({'attachment': copy.deepcopy(item), 'asset': {
                    'sha256': item['sha256'], 'mime': item['media_type'], 'original_b64': item['original_b64']}})
            if any((self.repo.image_transfer_metadata(kind, key) or {}).get('version') != item['version']
                   for kind, key, item in metadata):
                raise ServiceError('source_changed', '图片内容已变化，请重新生成')
        return result

    def _observe_draft_photos(self, case, job_id, epoch, facade, prior_drafts, automatic):
        """Prepare at most four images on this worker, then return a stale guard.

        Preview queue entries may be completed inline; their later queue turn is
        a no-op. No waiting on this same worker and no second external worker.
        """
        cid = case['conversation_id']
        with self._lock:
            token = self._read_token(facade)
            original_messages = copy.deepcopy(self.repo.list('message', conversation_id=cid))
            original_conversation = copy.deepcopy(self._require('conversation', cid))
            original_chat = self.repo.get('chat_draft', cid)
            candidates = copy.deepcopy(self._vision_candidates(cid))
            image_bindings = {}

        def guard(message=None, sha=None):
            # Called with the service lock held, before and after external work.
            self._require_current_read(token)
            current = self._require('case', case['case_id'])
            conversation = self._require('conversation', cid)
            drafts = {d['draft_id']: d['revision'] for d in self.repo.list('draft', case_id=case['case_id'])}
            if (epoch != self._epoch or self._closing or self._manual_only or current != case
                    or any(conversation.get(k) != original_conversation.get(k) for k in
                           ('seller_id', 'buyer_id', 'case_id', 'revision', 'human_owned'))
                    or self.repo.list('message', conversation_id=cid) != original_messages
                    or self.repo.get('chat_draft', cid) != original_chat
                    or drafts != prior_drafts or conversation.get('human_owned')
                    or current.get('state') in ('closed', 'human_owned')
                    or self._conversation_unresolved(cid)
                    or any(d.get('origin') == 'manual' and d.get('status') in ('pending', 'stale')
                           for d in self.repo.list('draft', case_id=case['case_id']))
                    or automatic and (not self._running or not self._in_scope(cid)
                                      or not self._automatic_source(cid) or self._message_sync_pending(cid))):
                raise ServiceError('cancelled', '图片识别期间来源或接待权限已变化')
            bindings = dict(image_bindings)
            if message is not None:
                bindings[message['message_id']] = (message, sha)
            for original, expected_sha in bindings.values():
                current_message = self._photo_target(cid, original['message_id'], photo_version(original))
                image = self._photo_state(current_message)
                asset = self.repo.get('image_asset', expected_sha) if expected_sha else None
                if (image.get('status') != 'ready' or image.get('sha256') != expected_sha
                        or not asset or asset.get('sha256') != expected_sha):
                    raise ServiceError('cancelled', '图片内容已变化，旧识别结果未应用')

        # Spend a limited visual allowance on the buyer's newest reference first.
        # The stored message history and text context remain chronological.
        for message in reversed(candidates):
            self._yield_to_ready_work()
            with self._lock:
                guard()
                state = self._photo_state(message)
            if state.get('status') == 'not_loaded':
                try:
                    self._enqueue_photo({'conversation_id': cid, 'message_id': message['message_id'],
                                         'source_version': photo_version(message)})
                except ServiceError:
                    # Queue/full-download failures remain explicit unseen events.
                    continue
                with self._lock:
                    state = self._photo_state(message)
            if state.get('status') == 'loading':
                preview_id = state.get('job_id')
                with self._lock:
                    preview_job = self.repo.get('job', preview_id, {})
                if preview_job.get('status') == 'queued':
                    params = {'conversation_id': cid, 'message_id': message['message_id'],
                              'source_version': photo_version(message)}
                    self._job_update(preview_id, status='running', stage='load_message_image', started_at=now_ms())
                    try:
                        result = self._task_load_message_image(params, preview_id, epoch, facade)
                        status = result.pop('_job_status', 'succeeded')
                        self._job_update(preview_id, status=status, stage='complete', completed_at=now_ms(), message=result['message'])
                    except ServiceError:
                        self._job_update(preview_id, status='cancelled', stage='cancelled', completed_at=now_ms(), message='图片读取已取消')
                        raise
            with self._lock:
                guard()
                state = self._photo_state(message)
                sha = state.get('sha256') if state.get('status') == 'ready' else None
                asset = self.repo.get('image_asset', sha) if sha else None
                if not asset or asset.get('sha256') != sha:
                    continue
                image_bindings[message['message_id']] = (message, sha)
                if self._vision_cached(message, sha, retry_failed=not automatic):
                    continue
                job = self._require('job', job_id)
                key = encoded([cid, message['message_id']])
                attempt_id = new_id('vision')
                started = [False]
                previous = self.repo.get('photo_observation', key, {})
                attempts = previous.get('attempts', 0) if self._vision_record_matches(message, previous, sha) else 0
                record = {'seller_id': message['seller_id'], 'buyer_id': message['buyer_id'],
                          'conversation_id': cid, 'message_id': message['message_id'],
                          'source_version': photo_version(message), 'image_sha256': sha,
                          'observation_id': photo_observation_id(message, sha), 'attempt_id': attempt_id,
                          'request_id': attempt_id, 'job_id': job_id, 'attempt_started': False,
                          'attempts': attempts + 1, 'attempted_at': now_ms()}

            def on_start():
                with self._lock, self.repo.transaction():
                    guard(message, sha)
                    if started[0]:
                        raise ServiceError('invalid_ai_output', '图片识别请求重复启动')
                    current_job = self._require('job', job_id)
                    current_job['external_started'] = True
                    current_job['vision_calls_started'] = current_job.get('vision_calls_started', 0) + 1
                    current_job.update(stage='photo_observation', message='正在识别客户照片')
                    record.update(status='processing', attempt_started=True, updated_at=now_ms())
                    self.repo.put('photo_observation', key, record, history=True)
                    self.repo.put('job', job_id, current_job, history=True)
                    self._charge_usage(ai=True)
                    started[0] = True
                self._emit('job.updated', current_job)
                self._changed(cid, case['case_id'])

            observation = None
            try:
                observe = getattr(facade, 'observe_photo', None)
                if callable(observe):
                    observation = observe(copy.deepcopy(message), copy.deepcopy(asset), attempt_id, on_start=on_start)
            except Exception:
                # No provider exception/body/path is persisted. Guards below
                # distinguish an unavailable observation from revoked authority.
                pass
            with self._lock, self.repo.transaction():
                guard(message, sha)
                if (started[0] and isinstance(observation, dict) and observation.get('image_sha256') == sha):
                    record.update(status='ready', request_id=observation.get('request_id'),
                                  description=observation.get('description'), visible_text=observation.get('visible_text'),
                                  uncertainties=observation.get('uncertainties'))
                if not valid_photo_observation(message, record, sha):
                    record = {k: v for k, v in record.items() if k not in ('description', 'visible_text', 'uncertainties')}
                    record.update(status='failed', reason='observation_unavailable', request_id=attempt_id)
                record['updated_at'] = now_ms()
                self.repo.put('photo_observation', key, record, history=True)
                self._invalidate(cid)
            self._changed(cid, case['case_id'])
        with self._lock:
            guard()
        return guard

    def _task_load_message_image(self, params, job_id, epoch, facade, *, loaded=None):
        with self._lock:
            job = self._require('job', job_id)
            if job.get('photo_generation') != self._monitor_generation:
                raise ServiceError('cancelled', '图片排队期间设置或连接已变化')
            message = copy.deepcopy(self._photo_target(params['conversation_id'], params['message_id'], params['source_version']))
            token = self._read_token(facade)
            self._require_current_read(token)
        error = None
        asset = None
        try:
            if loaded is None:
                asset = facade.load_photo(message)
            elif loaded[1] is not None:
                raise loaded[1]
            else:
                asset = loaded[0]
        except Exception as failure:
            error = '图片读取失败，暂未取得可显示的原图'
            self._job_update(job_id, diagnostics=read_diagnostics(failure))
        with self._lock, self.repo.transaction():
            self._require_current_read(token)
            current = self.repo.get('message', encoded([params['conversation_id'], params['message_id']]))
            if current is None or photo_version(current) != params['source_version']:
                return {'_job_status': 'stale', 'message': '照片所属消息已变化，旧结果未应用'}
            record = self.repo.get('message_image', encoded([params['conversation_id'], params['message_id']]), {})
            if record.get('job_id') != job_id or record.get('source_version') != params['source_version']:
                return {'_job_status': 'stale', 'message': '照片读取任务已被替代'}
            if not error:
                # Integration supplies fully decoded images, not arbitrary stored URLs.
                if (not isinstance(asset, dict) or not isinstance(asset.get('sha256'), str)
                        or not re.fullmatch(r'[0-9a-f]{64}', asset['sha256'])
                        or not isinstance(asset.get('preview_data_url'), str)
                        or not asset['preview_data_url'].startswith(('data:image/jpeg;base64,', 'data:image/png;base64,'))
                        or len(asset['preview_data_url']) > 1_400_000):
                    error = '图片内容校验未通过'
            if error:
                record.update(status='failed', message=error, updated_at=now_ms())
            else:
                self.repo.put('image_asset', asset['sha256'], {key: asset[key] for key in (
                    'sha256', 'mime', 'width', 'height', 'size', 'original_b64', 'preview_data_url')})
                record.update(status='ready', message='图片已加载', sha256=asset['sha256'],
                              association=asset.get('association', 'synthetic'), updated_at=now_ms())
            key = encoded([params['conversation_id'], params['message_id']])
            self.repo.put('message_image', key, record, history=True)
            observed = self.repo.get('photo_observation', key, {})
            if observed.get('status') == 'ready' and (record['status'] != 'ready'
                    or not valid_photo_observation(current, observed, record.get('sha256'))):
                self._invalidate(params['conversation_id'])
        self._changed(params['conversation_id'])
        return {'_job_status': 'failed' if error else 'succeeded', 'message': record['message']}
