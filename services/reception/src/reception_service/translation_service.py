"""Independent chat-only translation: persistent cache and bounded HTTP workers."""
import copy
import hashlib
import itertools
import ntpath
import os
import queue
import threading
import time

from .errors import ServiceError
from .photos import is_photo_placeholder
from .repository import encoded, new_id, now_ms
from .translation_provider import RouterTranslator, TranslationProviderError, normalize_base_url

_DEFAULT = {'enabled': False, 'base_url': 'https://api.deepseek.com/v1', 'model': '',
            'revision': 0, 'credential_ref': None, 'enabled_data_root': None}
_ACTIVE = {'queued', 'running', 'deferred'}


def _hash(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def _split(text, size=2200):
    """Conservative byte budget; never silently drop a long message's tail."""
    result, current, count = [], [], 0
    for char in text:
        length = len(char.encode('utf-8'))
        if current and count + length > size:
            result.append(''.join(current))
            current, count = [], 0
        current.append(char)
        count += length
    if current:
        result.append(''.join(current))
    return result


class _DemoTranslator:
    def __init__(self, integration):
        self.integration = integration

    def translate(self, items, request_id, *, cancel_event=None):
        result = self.integration.translate(items, request_id, cancel_event=cancel_event)
        return {**result, 'usage': {}, 'model': 'demo', 'request_id': request_id}


class TranslationMixin:
    def _init_translation(self):
        self._translation_settings = {**_DEFAULT, **self._control.get('meta', 'translation_settings', {})}
        if self.mode == 'demo' and self._translation_settings['revision'] == 0:
            self._translation_settings['enabled'] = True
        self._translation_key = ''
        self._translation_health = ''
        self._translation_message = ''
        self._translation_generation = 0
        self._translation_queue = queue.PriorityQueue(maxsize=200)
        self._translation_sequence = itertools.count()
        self._translation_active = {}
        self._translation_worker = threading.Thread(target=self._translation_loop, name='translation-worker', daemon=True)
        self._translation_worker.start()

    def _business_enter(self):
        # HTTP translation has no Native resource to yield to the business worker.
        pass

    def _business_leave(self):
        pass

    def _stop_translation(self):
        with self._lock:
            for _, cancel in self._translation_active.values():
                cancel.set()
        if threading.current_thread() is not self._translation_worker:
            self._translation_worker.join(timeout=10)
            if self._translation_worker.is_alive() or any(thread.is_alive() for thread, _ in list(self._translation_active.values())):
                self._executor_health.fail('stalled', 'translation_shutdown_timeout')
                raise ServiceError('executor_unavailable', '翻译任务尚未退出，无法安全关闭。', stage='shutdown')
        self._translation_key = ''

    def get_translation_config(self):
        """Host-only configuration. The credential reference is never a secret."""
        with self._lock:
            return copy.deepcopy(self._translation_settings)

    def _translation_enabled_for_current_root(self):
        settings = self._translation_settings
        if self.mode == 'demo' and settings.get('revision') == 0:
            return bool(settings.get('enabled'))
        root = settings.get('enabled_data_root')
        return bool(settings.get('enabled') and self.account_verified and self.seller_id
                    and settings.get('enabled_seller_id') == self.seller_id
                    and isinstance(root, str) and root
                    and ntpath.normcase(os.path.realpath(root)) == ntpath.normcase(os.path.realpath(self.data_root)))

    def _set_translation_enabled(self, enabled, seller_id, data_root, revision):
        if (type(enabled) is not bool or type(revision) is not int or not isinstance(seller_id, str)
                or not isinstance(data_root, str) or not data_root or len(data_root) > 32768):
            raise ServiceError('invalid_params', '翻译授权参数无效。')
        with self._lock:
            if not self.account_verified or not self.seller_id or seller_id != self.seller_id:
                raise ServiceError('identity_blocked', '当前商家身份已变化。')
            if ntpath.normcase(os.path.realpath(data_root)) != ntpath.normcase(os.path.realpath(self.data_root)):
                raise ServiceError('source_changed', '业务数据目录已变化。')
            if revision != self._translation_settings['revision']:
                raise ServiceError('revision_conflict', '翻译设置已变化，请重新读取。')
            if enabled and self.mode != 'demo':
                self._translation_snapshot()
            self._translation_generation += 1
            for _, cancel in self._translation_active.values():
                cancel.set()
            self._translation_settings.update(enabled=enabled, enabled_seller_id=seller_id if enabled else None,
                                              enabled_data_root=str(self.data_root.resolve()) if enabled else None,
                                              revision=revision + 1)
            self._translation_health = self._translation_message = ''
            self._control.put('meta', 'translation_settings', self._translation_settings)
            info = self.get_translation_info()
        self._emit('translation.settings_changed', info)
        return info

    def get_translation_info(self):
        with self._lock:
            settings = self._translation_settings
            if self.mode == 'demo':
                configured = True
            else:
                try:
                    self._translation_snapshot()
                    configured = True
                except ServiceError:
                    configured = False
            enabled = self._translation_enabled_for_current_root()
            status = ('demo' if self.mode == 'demo' else 'not_configured' if not configured else
                      'disabled' if not enabled else self._translation_health or 'ready')
            messages = {'demo': '演示翻译仅使用本地合成数据，不调用第三方。',
                        'not_configured': '请配置模型和 API Key；已保存译文仍可查看。',
                        'disabled': '第三方聊天翻译已关闭；已保存译文仍可查看。',
                        'ready': '聊天翻译已配置，首次调用或合成检查将核验服务。'}
            usage = self._control.get('meta', 'translation_usage', {})
            return {**{key: settings[key] for key in ('base_url', 'model', 'revision')}, 'enabled': enabled,
                    'has_key': bool(self._translation_key) or (self.mode != 'demo' and configured), 'status': status,
                    'message': self._translation_message or messages.get(status, '正在检查翻译服务。'),
                    'usage': {key: int(usage.get(key, 0)) for key in
                              ('requests', 'prompt_tokens', 'completion_tokens', 'unknown_requests', 'cache_hits')}}

    def configure_translation_credential(self, credential_ref, api_key, revision):
        with self._lock:
            if revision != self._translation_settings['revision'] or credential_ref != self._translation_settings['credential_ref']:
                raise ServiceError('revision_conflict', '翻译配置已变化，请重新读取。')
            self._validate_translation_key(api_key)
            self._translation_key = api_key
            return self.get_translation_info()

    @staticmethod
    def _validate_translation_key(api_key):
        if (not isinstance(api_key, str) or len(api_key.encode('utf-8')) > 8192
                or any(char in api_key for char in ('\x00', '\r', '\n'))):
            raise ServiceError('invalid_params', '翻译密钥格式无效。')

    def validate_translation_settings(self, base_url, model):
        """Host-only validation before publishing a shared connection; no writes."""
        if (not isinstance(model, str) or len(model) > 200
                or any(ord(char) < 32 for char in model)):
            raise ServiceError('invalid_params', '翻译设置无效。')
        try:
            base_url = normalize_base_url(base_url)
        except TranslationProviderError:
            raise ServiceError('invalid_params', '服务地址须为有效的 HTTPS API 地址；本机网关可使用 HTTP（127.0.0.1、localhost 或 [::1]）。') from None
        return {'base_url': base_url, 'model': model.strip()}

    def save_translation_settings(self, enabled, base_url, model, revision, credential_ref=None, api_key='',
                                  enabled_data_root=None):
        if (type(enabled) is not bool or type(revision) is not int or not isinstance(model, str)
                or len(model) > 200 or any(ord(char) < 32 for char in model)
                or credential_ref is not None and (not isinstance(credential_ref, str) or len(credential_ref) != 64
                                                   or any(char not in '0123456789abcdef' for char in credential_ref))):
            raise ServiceError('invalid_params', '翻译设置无效。')
        self._validate_translation_key(api_key)
        if (enabled_data_root is not None and (not isinstance(enabled_data_root, str)
                or len(enabled_data_root) > 32768 or any(ord(char) < 32 for char in enabled_data_root))):
            raise ServiceError('invalid_params', '翻译设置无效。')
        validated = self.validate_translation_settings(base_url, model)
        base_url, model = validated['base_url'], validated['model']
        if enabled and self.mode != 'demo' and (not model.strip() or not api_key or not credential_ref):
            raise ServiceError('translation_not_configured', '启用前请填写模型并配置 API Key。')
        with self._lock:
            if revision != self._translation_settings['revision']:
                raise ServiceError('revision_conflict', '翻译设置已变化，请保留输入并重新读取。')
            self._translation_generation += 1
            for _, cancel in self._translation_active.values():
                cancel.set()
            self._translation_settings = {'enabled': enabled, 'base_url': base_url, 'model': model.strip(),
                                          'revision': revision + 1, 'credential_ref': credential_ref,
                                          'enabled_data_root': enabled_data_root if enabled else None}
            self._translation_key = api_key
            self._translation_health = self._translation_message = ''
            self._control.put('meta', 'translation_settings', self._translation_settings)
        self._emit('translation.settings_changed', self.get_translation_info())
        return self.get_translation_info()

    def configure_model_router(self, router):
        """Inject the shared runtime; its owner retains responsibility for closing it."""
        with self._lock:
            self._translation_router = router
            self._translation_generation += 1
            for _, cancel in self._translation_active.values():
                cancel.set()

    def _translation_snapshot(self):
        if not getattr(self, '_model_config_valid', True):
            raise ServiceError('model_configuration_invalid', '模型配置不可用', stage='model_configuration')
        router = getattr(self, '_translation_router', None) or getattr(self, '_model_router', None)
        if router is None:
            raise ServiceError('translation_not_configured', '请先配置第三方翻译服务。')
        return router, router.resolve('translation', ('text', 'translation'))

    def _translation_cache_model(self):
        if self.mode == 'demo':
            return ['demo', self._translation_settings['base_url'], self._translation_settings['model']]
        from dataclasses import asdict
        try:
            _, snapshot = self._translation_snapshot()
            return asdict(snapshot)
        except ServiceError:
            return ['unconfigured', self._translation_settings['base_url'], self._translation_settings['model']]

    def _new_translation_provider(self):
        if self.mode == 'demo':
            return _DemoTranslator(self.integrations)
        router, _ = self._translation_snapshot()
        return RouterTranslator(router)

    def _translation_sources(self, cid, items):
        conversation = self._chat_owner(cid)
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise ServiceError('invalid_params', '每批翻译需包含 1 至 50 条聊天消息。')
        messages = sorted(self.repo.list('message', conversation_id=cid),
                          key=lambda row: (row.get('timestamp_ms') or -1, row['message_id']))
        positions = {row['message_id']: index for index, row in enumerate(messages)}
        resolved = []
        for item in items:
            if (not isinstance(item, dict) or set(item) - {'kind', 'source_id', 'source_hash', 'field'}
                    or item.get('kind') not in ('message', 'manual_suggestion') or item.get('field') is not None
                    or not isinstance(item.get('source_id'), str)):
                raise ServiceError('invalid_params', '第三方仅支持聊天文字和人工回复建议，不翻译采购事项。')
            kind = item['kind']
            if kind == 'message':
                source = self.repo.get('message', encoded([cid, item['source_id']]))
                if (not source or source.get('role') not in ('buyer', 'seller')
                        or any(source.get(key) != conversation.get(key) for key in ('seller_id', 'buyer_id', 'conversation_id'))):
                    raise ServiceError('source_mismatch', '翻译消息不属于当前会话。')
                text = source.get('text')
            else:
                source = self.repo.get('manual_suggestion', cid)
                if (not source or source.get('suggestion_id') != item['source_id']
                        or source.get('conversation_id') != cid):
                    raise ServiceError('source_mismatch', '人工建议不属于当前会话或已被替换。')
                text = source.get('body')
            if not isinstance(text, str) or not text.strip() or len(text) > 50000 or is_photo_placeholder(text):
                raise ServiceError('invalid_params', '翻译来源正文不可用。')
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            if (kind == 'manual_suggestion' and item.get('source_hash') != digest
                    or kind == 'message' and item.get('source_hash') is not None and item['source_hash'] != digest):
                raise ServiceError('source_changed', '翻译正文已变化，请重新读取。')
            context = []
            remaining = self.settings.get('translation_context_bytes', 300)
            context_limit = self.settings.get('translation_context_messages', 2)
            preceding = messages[:positions[source['message_id']]] if kind == 'message' else messages
            for row in reversed(preceding):
                if (len(context) >= context_limit or remaining <= 0):
                    break
                value = row.get('text')
                if (row.get('role') not in ('buyer', 'seller') or not isinstance(value, str) or not value.strip()
                        or is_photo_placeholder(value)
                        or any(row.get(key) != conversation.get(key) for key in ('seller_id', 'buyer_id', 'conversation_id'))):
                    continue
                # Keep a bounded tail as linguistic context, never as source evidence.
                tail = value.encode('utf-8')[-remaining:].decode('utf-8', errors='ignore')
                context.append({'speaker': row['role'], 'text': tail})
                remaining -= len(tail.encode('utf-8'))
            context.reverse()
            identity = {'kind': kind, 'source_id': item['source_id'], 'field': None, 'source_hash': digest}
            key = _hash([2, self.seller_id, cid, identity, 'zh-CN', self._translation_cache_model(),
                         'chat-v3-reviewed-039', context])
            payload = {'text': text, 'speaker': source['role'] if kind == 'message' else 'seller', 'context': context}
            resolved.append((identity, key, payload))
        if sum(len(payload['text']) for _, _, payload in resolved) > 100000:
            raise ServiceError('invalid_params', '翻译内容过长，请拆分批次。')
        return resolved

    def get_translations(self, conversation_id, items):
        with self._lock:
            result = []
            for identity, key, payload in self._translation_sources(conversation_id, items):
                record = self.repo.get('translation_v2', key)
                legacy = False
                old_provider = False
                if not record and not self._control.get('meta', 'translation_legacy_hidden', False):
                    old_v2_key = _hash([2, self.seller_id, conversation_id, identity, 'zh-CN', self._translation_settings['base_url'],
                                        self._translation_settings['model'], 'chat-v2', payload['context']])
                    previous = self.repo.get('translation_v2', old_v2_key)
                    if previous and previous.get('status') == 'ready':
                        record, legacy, old_provider = previous, True, True
                if not record and not self._control.get('meta', 'translation_legacy_hidden', False):
                    old_key = _hash([self.seller_id, conversation_id, identity, 'zh-CN', 'v1'])
                    previous = self.repo.get('translation', old_key)
                    if previous and previous.get('status') == 'ready':
                        record, legacy = previous, True
                result.append({**identity, 'status': record['status'] if record else 'pending',
                               'text': record.get('text') if record else None, 'error': record.get('error') if record else None,
                               'legacy': legacy, 'provider': '第三方历史缓存' if old_provider else 'Accio 历史缓存' if legacy else
                               '本地演示' if self.mode == 'demo' else self._translation_settings['model'], 'cache_key': key})
            hits = sum(row['status'] == 'ready' for row in result)
            if hits:
                usage = self._control.get('meta', 'translation_usage', {})
                usage['cache_hits'] = usage.get('cache_hits', 0) + hits
                self._control.put('meta', 'translation_usage', usage)
            return {'items': result}

    def _translation_job(self, kind, cid=None):
        job = {'job_id': new_id('job'), 'kind': kind, 'conversation_id': cid, 'case_id': None,
               'status': 'queued', 'stage': 'queued', 'message': '等待聊天翻译' if cid else '等待翻译服务检查',
               'created_at': now_ms(), 'started_at': None, 'completed_at': None, 'automatic': False}
        self.repo.put('job', job['job_id'], job, history=True)
        return job

    def request_translations(self, conversation_id, items, client_request_id):
        if not isinstance(client_request_id, str) or not 1 <= len(client_request_id.strip()) <= 200:
            raise ServiceError('invalid_params', '翻译请求编号无效。')
        with self._lock, self.repo.transaction():
            request = {'conversation_id': conversation_id, 'items': items}
            previous = self.repo.get('translation_request_v2', client_request_id)
            if previous:
                if previous['request'] != request:
                    raise ServiceError('request_conflict', '同一请求编号不能用于不同参数。')
                self._chat_owner(conversation_id)
                return {'job_id': previous['job_id']}
            sources = self._translation_sources(conversation_id, items)
            if any(identity['kind'] == 'manual_suggestion' for identity, _, _ in sources):
                suggestion = self._manual_suggestion(conversation_id)
                if not suggestion or suggestion['status'] != 'ready':
                    raise ServiceError('source_changed', '人工建议依据已变化，请重新生成后再翻译。')
            if not self._translation_enabled_for_current_root():
                raise ServiceError('translation_disabled', '请先在设置中启用第三方聊天翻译。')
            if self._translation_queue.qsize() >= 190:
                raise ServiceError('translation_busy', '翻译队列已满，请稍后重试。')
            provider = self._new_translation_provider()
            work, seen, dependencies = [], set(), set()
            for source in sources:
                identity, key, payload = source
                if key in seen:
                    continue
                seen.add(key)
                record = self.repo.get('translation_v2', key)
                if record and record.get('status') == 'ready':
                    continue
                if record and record.get('status') == 'pending':
                    existing = self.repo.get('job', record.get('job_id'))
                    if existing and existing.get('status') in _ACTIVE:
                        dependencies.add(existing['job_id'])
                        continue
                work.append(source)
            pending_count = self.repo.db.execute(
                "SELECT COUNT(*) FROM entities t JOIN entities j ON j.kind='job' AND j.id=json_extract(t.data,'$.job_id') "
                "WHERE t.kind='translation_v2' AND json_extract(t.data,'$.status')='pending' "
                "AND json_extract(j.data,'$.status') IN ('queued','running','deferred')").fetchone()[0]
            if pending_count + len(work) > 200:
                raise ServiceError('translation_busy', '待翻译消息已达上限，请稍后重试。')
            if not work and len(dependencies) == 1:
                owner_job = next(iter(dependencies))
                self.repo.put('translation_request_v2', client_request_id, {'request': request, 'job_id': owner_job})
                return {'job_id': owner_job}
            job = self._translation_job('translation', conversation_id)
            for identity, key, _ in work:
                self.repo.put('translation_v2', key, {**identity, 'conversation_id': conversation_id,
                              'status': 'pending', 'text': None, 'error': None, 'job_id': job['job_id']})
            self.repo.put('translation_request_v2', client_request_id, {'request': request, 'job_id': job['job_id']})
            task = {'job_id': job['job_id'], 'cid': conversation_id, 'work': work, 'provider': provider,
                    'generation': self._translation_generation, 'attempt': 0, 'check': False,
                    'dependencies': list(dependencies), 'sources': sources}
            self._translation_queue.put_nowait((time.monotonic(), next(self._translation_sequence), task))
        self._emit('job.updated', job)
        return {'job_id': job['job_id']}

    def check_translation_provider(self):
        with self._lock, self.repo.transaction():
            if self._translation_queue.qsize() >= 190:
                raise ServiceError('translation_busy', '翻译队列已满。')
            provider = self._new_translation_provider()
            job = self._translation_job('translation_check')
            self._translation_health, self._translation_message = 'checking', '正在检查翻译服务。'
            task = {'job_id': job['job_id'], 'cid': None, 'work': [], 'provider': provider,
                    'generation': self._translation_generation, 'attempt': 0, 'check': True}
            self._translation_queue.put_nowait((time.monotonic(), next(self._translation_sequence), task))
        self._emit('job.updated', job)
        return {'job_id': job['job_id']}

    def clear_translation_cache(self):
        with self._lock, self.repo.transaction():
            self._translation_generation += 1
            for _, cancel in self._translation_active.values():
                cancel.set()
            removed = self.repo.db.execute("SELECT COUNT(*) FROM entities WHERE kind='translation_v2'").fetchone()[0]
            self.repo.db.execute("DELETE FROM entities WHERE kind IN ('translation_v2','translation_part_v2','translation_request_v2')")
            # Legacy entries have no embedded source kind. Hide their fallback
            # instead of deleting unrelated historical requirement translations.
            self._control.put('meta', 'translation_legacy_hidden', True)
        self._emit('translation.cache_cleared', {})
        return {'removed': removed}

    def _translation_loop(self):
        while not self._stop.is_set():
            with self._lock:
                for key, (thread, _) in list(self._translation_active.items()):
                    if not thread.is_alive():
                        self._translation_active.pop(key, None)
                available = len(self._translation_active) < 1
            if not available:
                self._stop.wait(.05)
                continue
            try:
                due, sequence, task = self._translation_queue.get(timeout=.1)
            except queue.Empty:
                continue
            if due > time.monotonic():
                self._translation_queue.put_nowait((due, sequence, task))
                self._translation_queue.task_done()
                self._stop.wait(min(.1, due - time.monotonic()))
                continue
            cancel = threading.Event()
            thread = threading.Thread(target=self._run_translation, args=(task, cancel), name='chat-translation', daemon=True)
            with self._lock:
                self._translation_active[task['job_id']] = (thread, cancel)
                thread.start()
            self._translation_queue.task_done()
        with self._lock:
            active = list(self._translation_active.values())
            for _, cancel in active:
                cancel.set()
        deadline = time.monotonic() + 9
        for thread, _ in active:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        while True:
            try:
                _, _, task = self._translation_queue.get_nowait()
            except queue.Empty:
                break
            self._translation_fail(task, 'cancelled', '翻译已取消。')
            self._translation_queue.task_done()

    def _translation_current(self, task, cancel):
        if self._stop.is_set() or cancel.is_set() or task['generation'] != self._translation_generation:
            raise TranslationProviderError('cancelled', '')
        provider = task['provider']
        if isinstance(provider, RouterTranslator):
            provider.router.assert_current(provider.snapshot)
        if not task['check']:
            if not self._translation_enabled_for_current_root():
                raise TranslationProviderError('cancelled', '')
            sources = task.get('sources', task['work'])
            current = self._translation_sources(task['cid'], [identity for identity, _, _ in sources]) if sources else []
            if [key for _, key, _ in current] != [key for _, key, _ in sources]:
                raise TranslationProviderError('source_changed', '')
            if any(identity['kind'] == 'manual_suggestion' for identity, _, _ in current):
                suggestion = self._manual_suggestion(task['cid'])
                if not suggestion or suggestion['status'] != 'ready':
                    raise TranslationProviderError('source_changed', '')

    def _record_translation_usage(self, result=None, *, started=False):
        usage = self._control.get('meta', 'translation_usage', {})
        if started:
            usage['requests'] = usage.get('requests', 0) + 1
            usage['unknown_requests'] = usage.get('unknown_requests', 0) + 1
        elif result is not None:
            counts = result.get('usage') or {}
            if all(type(counts.get(key)) is int for key in ('prompt_tokens', 'completion_tokens')):
                usage['unknown_requests'] = max(0, usage.get('unknown_requests', 0) - 1)
                for key in ('prompt_tokens', 'completion_tokens'):
                    usage[key] = usage.get(key, 0) + counts[key]
        self._control.put('meta', 'translation_usage', usage)

    def _run_translation(self, task, cancel):
        job_id = task['job_id']
        from .model_attempts import AttemptLifecycle, claim_many
        attempt_tokens = {}
        attempt_lifecycle = None
        try:
            with self._lock:
                self._translation_current(task, cancel)
                dependencies = [self.repo.get('job', value) for value in task.get('dependencies', [])]
                if any(job and job.get('status') in _ACTIVE for job in dependencies):
                    self._job_update(job_id, status='deferred', stage='translation', message='等待同一消息已排队的翻译。')
                    self._translation_queue.put_nowait((time.monotonic() + .1, next(self._translation_sequence), task))
                    return
                if any(not job or job.get('status') != 'succeeded' for job in dependencies):
                    raise TranslationProviderError('translation_dependency_failed', '')
            self._job_update(job_id, status='running', stage='translation', started_at=now_ms(),
                             message='正在检查第三方翻译' if task['check'] else '正在翻译聊天文字')
            pending = []
            if task['check']:
                pending = [{'key': 'synthetic-check', 'text': 'Hello. I need 12 pieces.', 'speaker': 'buyer', 'context': []}]
            else:
                with self._lock:
                    for identity, key, payload in task['work']:
                        for index, part in enumerate(_split(payload['text'])):
                            part_key = key + ':' + str(index)
                            if not self.repo.get('translation_part_v2', part_key):
                                pending.append({**payload, 'key': part_key, 'text': part,
                                                '_attempt_source': {'conversation_id': task['cid'], 'identity': identity,
                                                    'text': payload['text'], 'speaker': payload['speaker'],
                                                    'context': payload['context'], 'target': 'zh-CN',
                                                    'part_index': index, 'part': part}})
            while pending:
                batch, cost = [], 800  # Reserve fixed prompt/envelope space before conservative byte estimates.
                while pending and len(batch) < 6:
                    estimate = len(encoded(pending[0]).encode('utf-8'))
                    if batch and cost + estimate > 4000:
                        break
                    batch.append(pending.pop(0))
                    cost += estimate
                attempt_tokens = {}
                def started():
                    with self._lock, self.repo.transaction():
                        self._translation_current(task, cancel)
                        if not task['check']:
                            attempt_tokens.update(claim_many(self.repo, seller_id=self.seller_id, purpose='translation',
                                channel='chat', sources=[row['_attempt_source'] for row in batch]))
                        self._record_translation_usage(started=True)
                attempt_lifecycle = AttemptLifecycle(self.repo, started, attempt_tokens)
                if isinstance(task['provider'], RouterTranslator):
                    result = task['provider'].translate(batch, job_id, cancel_event=cancel, on_call_start=attempt_lifecycle)
                else:
                    result = task['provider'].translate(batch, job_id, cancel_event=cancel)
                rows = result.get('items') if isinstance(result, dict) else None
                if (not isinstance(rows, list) or len(rows) != len(batch)
                        or any(not isinstance(row, dict) or not isinstance(row.get('text'), str) or not row['text'].strip() for row in rows)
                        or {row.get('key') for row in rows} != {row['key'] for row in batch}):
                    raise TranslationProviderError('invalid_response', '')
                with self._lock, self.repo.transaction():
                    self._translation_current(task, cancel)
                    if self.mode != 'demo':
                        self._record_translation_usage(result)
                    for row in rows:
                        if not task['check']:
                            self.repo.put('translation_part_v2', row['key'], {'text': row['text']})
                    attempt_lifecycle.fallback('completed')
                    attempt_tokens = {}
            with self._lock, self.repo.transaction():
                self._translation_current(task, cancel)
                for identity, key, payload in task['work']:
                    chunks = [self.repo.get('translation_part_v2', key + ':' + str(index))['text']
                              for index, _ in enumerate(_split(payload['text']))]
                    self.repo.put('translation_v2', key, {**identity, 'conversation_id': task['cid'], 'format': 2,
                                  'status': 'ready', 'text': '\n'.join(chunks), 'error': None, 'created_at': now_ms(),
                                  'model': self._translation_settings['model'], 'context_hash': _hash(payload['context'])})
                self._translation_health = ''
                self._translation_message = '演示翻译检查通过，未访问第三方。' if self.mode == 'demo' else '第三方聊天翻译请求成功。'
            self._job_update(job_id, status='succeeded', stage='complete', completed_at=now_ms(), message='翻译检查完成' if task['check'] else '聊天译文已保存')
        except Exception as error:
            had_attempt = bool(attempt_tokens) or bool(attempt_lifecycle and attempt_lifecycle.had_attempt)
            if attempt_lifecycle is not None:
                attempt_lifecycle.fallback('unknown')
            code = error.code if isinstance(error, (TranslationProviderError, ServiceError)) else 'translation_failed'
            if code in ('model_cleanup_timeout', 'model_cleanup_failed'):
                self._executor_health.fail('failed', code)
            if (not had_attempt and isinstance(error, TranslationProviderError) and error.retryable and task['attempt'] < 2
                    and not task['check'] and not cancel.is_set() and not self._stop.is_set()
                    and task['generation'] == self._translation_generation):
                delay = error.retry_after if error.retry_after is not None else (1, 3)[task['attempt']]
                task['attempt'] += 1
                self._job_update(job_id, status='deferred', stage='translation', retry_at=now_ms() + int(delay * 1000),
                                 retry_attempt=task['attempt'], message='翻译服务暂不可用，等待重试。')
                self._translation_queue.put((time.monotonic() + max(.1, delay), next(self._translation_sequence), task))
            else:
                self._translation_fail(task, code, '翻译配置或权限无效，请核对模型和密钥。' if code == 'invalid_config' else
                                       '翻译已取消。' if code == 'cancelled' else '翻译未完成，原文已保留，可稍后重试。')
        finally:
            self._emit('state.changed', {'reason': 'translations_updated', 'conversation_id': task['cid']})
            if task['check']:
                self._emit('translation.settings_changed', self.get_translation_info())

    def _translation_fail(self, task, code, message):
        with self._lock, self.repo.transaction():
            for _, key, _ in task['work']:
                record = self.repo.get('translation_v2', key)
                if record and record.get('job_id') == task['job_id']:
                    self.repo.put('translation_v2', key, {**record, 'status': 'failed', 'error': message})
            if task['generation'] == self._translation_generation and code != 'cancelled':
                self._translation_health, self._translation_message = 'failed', message
        self._job_update(task['job_id'], status='cancelled' if code == 'cancelled' else 'stale' if code in ('source_changed', 'source_mismatch') else 'failed',
                         stage='translation', completed_at=now_ms(), message=message, error_code=code)
