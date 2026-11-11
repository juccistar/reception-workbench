"""Bounded, cancellable read-only translation lane."""
import hashlib
import queue
import threading

from .context import FIELDS
from .errors import ServiceError
from .integrations.accio_native.vendor.diagnostics import CODES, STAGES, process_diagnostics
from .photos import is_photo_placeholder
from .repository import encoded, new_id, now_ms


class TranslationMixin:
    def _init_translation(self):
        self._translation_queue = queue.Queue()
        self._translation_gate = threading.Condition()
        self._translation_cancel = None
        self._business_active = False
        self._translation_worker = threading.Thread(target=self._translation_loop, name='translation-worker', daemon=True)
        self._translation_worker.start()

    def _business_enter(self):
        with self._translation_gate:
            self._business_active = True
            if self._translation_cancel:
                self._translation_cancel.set()
            while self._translation_cancel is not None:
                self._translation_gate.wait()

    def _business_leave(self):
        with self._translation_gate:
            self._business_active = False
            self._translation_gate.notify_all()

    def _stop_translation(self):
        with self._translation_gate:
            if self._translation_cancel:
                self._translation_cancel.set()
            self._translation_gate.notify_all()
        if threading.current_thread() != self._translation_worker:
            self._translation_worker.join()

    def _translation_sources(self, cid, items):
        conversation = self._chat_owner(cid)
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise ServiceError('invalid_params', '每批翻译需包含 1 至 50 条来源')
        resolved = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {'kind', 'source_id', 'field', 'source_hash'}:
                raise ServiceError('invalid_params', '翻译来源无效')
            kind, source_id, field = item.get('kind'), item.get('source_id'), item.get('field')
            if not isinstance(source_id, str) or not source_id:
                raise ServiceError('invalid_params', '翻译来源编号无效')
            if kind == 'message' and field is None:
                source = self.repo.get('message', encoded([cid, source_id]))
                text = source.get('text') if source else None
            elif kind == 'case_text' and isinstance(field, str) and (field == 'summary' or field.startswith('facts.') and field[6:] in FIELDS):
                source = self.repo.get('case', source_id)
                text = (source.get('summary') if field == 'summary' else source.get('facts', {}).get(field[6:], {}).get('value')) if source else None
            else:
                raise ServiceError('invalid_params', '翻译字段无效')
            if not source or any(source.get(k) != conversation.get(k) for k in ('seller_id', 'buyer_id', 'conversation_id')):
                raise ServiceError('source_mismatch', '翻译来源不属于当前会话')
            if (not isinstance(text, str) or not text.strip() or len(text) > 50000
                    or kind == 'message' and is_photo_placeholder(text)):
                raise ServiceError('invalid_params', '翻译来源正文不可用')
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            supplied = item.get('source_hash')
            if supplied is not None and (not isinstance(supplied, str) or supplied != digest):
                raise ServiceError('source_changed', '翻译来源已变化，请重新读取')
            identity = {'kind': kind, 'source_id': source_id, 'field': field, 'source_hash': digest}
            key = hashlib.sha256(encoded([self.seller_id, cid, identity, 'zh-CN', 'v1']).encode('utf-8')).hexdigest()
            resolved.append((identity, key, text))
        if sum(len(text) for _, _, text in resolved) > 100000:
            raise ServiceError('invalid_params', '翻译内容过长，请拆分批次')
        return resolved

    def get_translations(self, conversation_id, items):
        with self._lock:
            sources = self._translation_sources(conversation_id, items)
            result = []
            for identity, key, _ in sources:
                record = self.repo.get('translation', key)
                result.append({**identity, 'status': record['status'] if record else 'pending',
                               'text': record.get('text') if record else None,
                               'error': record.get('error') if record else None})
            return {'items': result}

    def request_translations(self, conversation_id, items, client_request_id):
        if not isinstance(client_request_id, str) or not 1 <= len(client_request_id.strip()) <= 200:
            raise ServiceError('invalid_params', '请求编号无效')
        with self._lock, self.repo.transaction():
            sources = self._translation_sources(conversation_id, items)
            if self.mode == 'live' and self.checks['ai']['status'] != 'passed':
                raise ServiceError('ai_capability', '请先检查 AI 接待能力')
            request = {'conversation_id': conversation_id, 'items': items}
            existing = self.repo.get('translation_request', client_request_id)
            if existing:
                if existing['request'] != request:
                    raise ServiceError('request_conflict', '同一请求编号不能用于不同参数')
                return {'job_id': existing['job_id']}
            job = {'job_id': new_id('job'), 'kind': 'translation', 'conversation_id': conversation_id,
                   'case_id': None, 'status': 'queued', 'stage': 'queued', 'message': '等待翻译',
                   'created_at': now_ms(), 'started_at': None, 'completed_at': None, 'automatic': False}
            work = []
            seen = set()
            for identity, key, text in sources:
                if key in seen:
                    continue
                seen.add(key)
                record = self.repo.get('translation', key)
                if record and record['status'] == 'pending':
                    previous = self.repo.get('job', record.get('job_id'))
                    if not previous or previous.get('status') not in ('queued', 'running'):
                        record = None
                if not record or record['status'] == 'failed':
                    self.repo.put('translation', key, {'status': 'pending', 'text': None, 'error': None, 'job_id': job['job_id']})
                    work.append((identity, key, text))
            self.repo.put('job', job['job_id'], job, history=True)
            self.repo.put('translation_request', client_request_id, {'request': request, 'job_id': job['job_id']})
            self._translation_queue.put((job['job_id'], conversation_id, work))
        self._emit('job.updated', job)
        return {'job_id': job['job_id']}

    def _translation_loop(self):
        while not self._stop.is_set():
            try:
                job_id, cid, work = self._translation_queue.get(timeout=.15)
            except queue.Empty:
                continue
            cancel = threading.Event()
            with self._translation_gate:
                while (self._business_active or not self._queue.empty()) and not self._stop.is_set():
                    self._translation_gate.wait(timeout=.15)
                if self._stop.is_set():
                    cancel.set()
                self._translation_cancel = cancel
            ai_diagnostics = {}
            try:
                self._job_update(job_id, status='running', stage='translation', started_at=now_ms(), message='正在翻译')
                if cancel.is_set():
                    raise ServiceError('cancelled', '翻译已取消')
                result = {'items': []}
                if work:
                    def started():
                        with self._lock, self.repo.transaction():
                            self._charge_usage(ai=True)
                    self._ensure_identity(self._translation_facade)
                    self._translation_facade.last_ai_diagnostics = {}
                    try:
                        result = self._translation_facade.translate([{'key': key, 'text': text} for _, key, text in work],
                                                                     job_id, on_call_start=started, cancel_event=cancel)
                    finally:
                        ai_diagnostics = process_diagnostics(getattr(self._translation_facade, 'last_ai_diagnostics', None))
                translated = result.get('items') if isinstance(result, dict) else None
                if (cancel.is_set() or not isinstance(translated, list) or len(translated) != len(work)
                        or any(not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text'].strip() for item in translated)
                        or {item.get('key') for item in translated} != {key for _, key, _ in work}):
                    raise ServiceError('cancelled' if cancel.is_set() else 'invalid_ai_output', '翻译未完成，请重试')
                with self._lock, self.repo.transaction():
                    for item in translated:
                        self.repo.put('translation', item['key'], {'status': 'ready', 'text': item['text'], 'error': None})
                self._job_update(job_id, status='succeeded', stage='complete', completed_at=now_ms(), message='翻译完成',
                                 diagnostics=ai_diagnostics)
            except Exception as error:
                with self._lock, self.repo.transaction():
                    for _, key, _ in work:
                        record = self.repo.get('translation', key)
                        if record and record.get('job_id') == job_id:
                            self.repo.put('translation', key, {'status': 'failed', 'text': None, 'error': '翻译未完成，请重试'})
                identity_error = isinstance(error, ServiceError) and error.code in ('seller_mismatch', 'identity_required')
                code = error.code if isinstance(error, ServiceError) else 'native_failed'
                if not isinstance(code, str) or code not in CODES | {'seller_mismatch', 'identity_required', 'cancelled',
                                                                      'native_failed', 'invalid_ai_output', 'invalid_request',
                                                                      'invalid_context'}:
                    code = 'native_failed'
                stage = error.stage if isinstance(error, ServiceError) else 'translation'
                stage = stage if isinstance(stage, str) and stage in STAGES | {'service', 'translation'} else 'translation'
                diagnostics = dict(ai_diagnostics)
                if isinstance(error, ServiceError):
                    diagnostics.update(process_diagnostics(error.diagnostics))
                    if type(error.diagnostics.get('exit_code')) is int:
                        diagnostics['exit_code'] = error.diagnostics['exit_code']
                else:
                    diagnostics.update(process_diagnostics({'exception_class': type(error).__name__}))
                diagnostics.update(code=code, stage=stage)
                cancelled = code == 'cancelled' or (not isinstance(error, ServiceError) and cancel.is_set())
                self._job_update(job_id, status='cancelled' if cancelled else 'failed', stage='translation',
                                 completed_at=now_ms(), message=str(error) if identity_error else '翻译未完成，请重试',
                                 error_code=code, diagnostics=diagnostics)
            finally:
                self._emit('state.changed', {'reason': 'translations_updated', 'conversation_id': cid})
                with self._translation_gate:
                    self._translation_cancel = None
                    self._translation_gate.notify_all()
                self._translation_queue.task_done()
        while True:
            try:
                job_id, cid, work = self._translation_queue.get_nowait()
            except queue.Empty:
                break
            with self._lock, self.repo.transaction():
                for _, key, _ in work:
                    record = self.repo.get('translation', key)
                    if record and record.get('job_id') == job_id:
                        self.repo.put('translation', key, {'status': 'failed', 'text': None, 'error': '翻译已取消'})
            self._job_update(job_id, status='cancelled', stage='cancelled', completed_at=now_ms(), message='翻译已取消')
            self._translation_queue.task_done()
