"""Reception business service: durable state, one external worker, revocable sends."""
from __future__ import annotations

import copy
import hashlib
import inspect
from pathlib import Path
import queue
import re
import threading
import time

from . import __version__
from .context import FIELDS, build_context, missing_fields_for_case, validate_reply, validated_facts, reply_validation_reason
from .procurement import ProcurementMixin, merge_fact, invalidate_case_sources, evidence_snapshot, fact_change
from .product_library import ProductLibraryMixin
from .errors import ServiceError, safe_diagnostics, read_diagnostics
from .execution import ExecutionMixin, BusinessQueue, AI_JOBS
from .executor_health import ExecutorHealth
from .integrations import build_integrations
from .integrations.live import LiveIntegration, SNAPSHOT_ERROR_MESSAGES
from .continuity import ContinuityMixin, text_hash
from .manual_chat import ManualChatMixin
from .conversation_assistance import ConversationAssistanceMixin
from .requirement_changes import RequirementChangesMixin, requirement_revision
from .reply_batches import ReplyBatchMixin
from .send_gate import SendAuthorization, SendGateDecision
from .reply_messages import normalize_reply_output
from .cached_media import CachedMediaMixin
from .whatsapp import WhatsAppMixin
from .whatsapp_merchant_context import WhatsAppMerchantContextMixin
from .business_supplement import supplement_fingerprint
from . import policy_library
from .integrations.generation_metadata import applied_policy_summaries, checked_metadata
from .translation_service import TranslationMixin
from .feishu_settings import FeishuSettingsMixin
from .message_monitor import MessageMonitorMixin
from .conversation_style import social_intent
from .repository import Repository, encoded, new_id, now_ms
from .synthetic_check import synthetic_context
from .sync_activity import visible_jobs
from . import unread
from .reception_state import ReceptionProjection
from .business_progress import apply_progress, project_progress
from .requirement_media import RequirementMediaMixin, get_reception_source_images
from .photos import is_photo_placeholder, photo_fingerprint, with_direct_photo_reads
from .message_photos import MessagePhotosMixin
from .storage_guard import preflight_workspace
from .workspace_lock import WorkspaceLock, native_children_stopped
from .seller_guard import SellerGuard, SellerBusyError
from .send_retry_policy import (decision as send_retry_decision, retry_message as send_retry_message,
                                settle_waiting_jobs, restore_retry_job_states)

SLOW = frozenset({'sync_messages', 'fill_history', 'generate_draft', 'check_connection', 'check_ai', 'refresh_company_profile',
                  'preview_send', 'confirm_send', 'send_chat', 'run_outbox', 'check_feishu', 'reconcile_operation', 'load_message_image'})
FAST = frozenset({'get_snapshot', 'get_executor_health', 'get_conversation', 'get_messages', 'get_message_image', 'get_requirement_media',
                  'set_requirement_media_cover', 'set_product_image_source', 'load_requirement_media', 'get_context', 'list_jobs',
                  'list_outbox', 'save_draft', 'save_policy', 'list_builtin_policies', 'adopt_builtin_policy', 'save_settings', 'set_scope',
                  'set_reception', 'takeover', 'begin_manual_edit', 'save_chat_draft', 'prepare_send', 'inject_demo', 'mark_messages_read',
                  'save_conversation_supplement', 'upload_supplement_attachment', 'remove_supplement_attachment',
                  'generate_manual_suggestion', 'cancel_manual_suggestion', 'get_translations', 'request_translations',
                  'get_translation_info', '_save_feishu_configuration', '_configure_feishu_credential', '_feishu_credential_requests', '_grant_feishu_write', 'set_case_category', 'create_procurement_case',
                  'list_products', 'get_product', 'get_product_main_image', 'request_product_images', 'list_suppliers', 'get_supplier', 'save_supplier', 'save_product_offer',
                  'select_supplier_offer', 'bind_requirement_product', 'set_product_default_offer', 'get_procurement_settings',
                  'save_procurement_settings', 'set_procurement_enabled', 'list_procurement_tasks', 'request_procurement',
                  'reconcile_procurement', '_image_prepare', '_image_status', '_image_transfer_chunk', '_image_cancel',
                  '_product_link_for_reference', '_copy_text_for_reference',
                  'get_whatsapp_state', 'save_whatsapp_binding', 'get_whatsapp_context', 'set_whatsapp_reception',
                  'takeover_whatsapp', 'reconcile_whatsapp_operation', '_wa_set_account', '_wa_ingest',
                  '_wa_prepare_send', '_wa_transport_result', '_wa_ack', '_wa_media_begin', '_wa_media_chunk',
                  '_wa_media_finish', '_wa_media_fail', '_wa_export_merchant_context', '_wa_export_customer_candidates', '_wa_writeback_capabilities',
                  '_wa_writeback_preview', '_wa_writeback_submit', '_configure_models', 'shutdown'})
DEFAULT_SETTINGS = {'workctl_path': '', 'poll_seconds': 30, 'probe_seconds': 2, 'merge_seconds': 1,
                    'required_fields': ['product', 'quantity', 'specifications', 'destination'],
                    'image_batch_mib': 64, 'image_prepare_seconds': 60, 'image_parallelism': 2,
                    'reception_images_enabled': True, 'context_message_count': 50,
                    'generation_retry_limit': 3, 'translation_parallelism': 2,
                    'translation_context_messages': 2, 'translation_context_bytes': 300,
                    'max_questions_per_reply': 2, 'max_questions_per_field': 2,
                    'required_fields_by_category': {},
                    'feishu': {'enabled': False, 'write_enabled': False}}
CHECK_NAMES = ('runtime', 'identity', 'messages', 'ai', 'send_preview', 'feishu')
SYNC_PAGE_LIMIT = 10
HISTORY_PAGE_LIMIT = 1000
ADVANCED_INTEGER_SETTINGS = {
    'image_batch_mib': (8, 256), 'image_prepare_seconds': (10, 300), 'image_parallelism': (1, 4),
    'context_message_count': (1, 200),
    'generation_retry_limit': (1, 10), 'translation_parallelism': (1, 4),
    'translation_context_messages': (0, 10), 'translation_context_bytes': (0, 6000),
    'max_questions_per_reply': (1, 2), 'max_questions_per_field': (1, 10),
}


def blank_check():
    return {'status': 'not_checked', 'check_id': None, 'checked_at': None,
            'message': '尚未检查', 'simulated': False}


def coverage():
    return {'status': 'not_synced', 'earliest_ms': None, 'latest_ms': None, 'updated_at': None,
            'next_cursor': None, 'message': '尚未读取历史消息'}


class ReceptionService(FeishuSettingsMixin, ReplyBatchMixin, WhatsAppMerchantContextMixin, WhatsAppMixin, RequirementChangesMixin, CachedMediaMixin, ProductLibraryMixin, RequirementMediaMixin, ExecutionMixin, ProcurementMixin, ConversationAssistanceMixin, TranslationMixin, ContinuityMixin, MessagePhotosMixin, MessageMonitorMixin, ManualChatMixin):
    def __init__(self, data_root: Path, mode='live', seller_id='', emit=None, *, _integration_factory=None, _fixture_initializer=None):
        if mode != 'live' and not (mode == 'demo' and _integration_factory and _fixture_initializer):
            raise ServiceError('invalid_mode', '运行模式无效')
        self._seller_guard = None
        # Synthetic historical regression factories explicitly retain the old mixin.
        self._whatsapp_retired = _integration_factory is None
        self._workspace_lock = WorkspaceLock(data_root).acquire(invalidate_clean=False)
        try:
            preflight_workspace(data_root)
            self._workspace_lock.invalidate_clean_shutdown()
            self._initialize(data_root, mode, seller_id, emit, _integration_factory, _fixture_initializer)
        except BaseException:
            # No clean marker on partial startup. Stop any successfully started
            # threads before releasing the data-root lock and storage handles.
            if hasattr(self, '_stop'):
                self._stop.set()
            if hasattr(self, '_generation_active'):
                self._join_generation_tasks()
            deadline = time.monotonic() + 10
            threads = [getattr(self, name, None) for name in
                       ('_worker', '_monitor_thread', '_translation_worker', '_health_watchdog')]
            for thread in threads:
                if thread and thread.ident is not None and thread is not threading.current_thread():
                    thread.join(timeout=max(0, deadline - time.monotonic()))
            active_translation = getattr(self, '_translation_active', {})
            if (any(thread and thread.is_alive() for thread in threads)
                    or any(thread.is_alive() for thread, _ in list(active_translation.values()))):
                # The live threads retain this service and its locks and repositories.
                raise ServiceError('executor_unavailable', '启动线程尚未退出，无法安全释放数据锁。', stage='shutdown')
            for name in ('repo', '_control'):
                repository = getattr(self, name, None)
                if repository is not None:
                    repository.close()
            self._release_seller_after_drain()
            self._workspace_lock.release()
            raise

    def _initialize(self, data_root, mode, seller_id, emit, integration_factory, fixture_initializer):
        self.data_root = Path(data_root)
        self.mode = mode
        self.expected_seller_id = seller_id
        self.seller_id = 'demo-seller' if mode == 'demo' else ''
        self.account_verified = mode == 'demo'
        self._emit_callback = emit
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closing = False
        self._closed = False
        self._epoch = 0
        self._queue = BusinessQueue()
        self._init_execution()
        from .model_router import ModelRouter
        self._model_router = ModelRouter()
        self._model_config_valid = False
        self._executor_health = ExecutorHealth(self)
        self._queue.executor_guard = self._executor_health.require
        self._chat_facades = {}
        self._running = False
        self._auto_send = False
        self._manual_only = False
        self._scope = {'mode': 'selected', 'conversation_ids': [], 'max_auto_replies': None}
        self._frozen_scope = None
        # Session boundaries survive a pause, but never a process restart.
        self._authorized_scope = None
        self._started_at_ms = None
        self._source_baseline = {}
        self._budget_used = 0
        self._ai_budget_used = 0
        self._next_poll = 0.0
        self._next_outbox = 0.0
        live_root = self.data_root / 'live'
        new_live = mode == 'live' and (not live_root.exists() or live_root.is_dir() and not any(live_root.iterdir()))
        self._control = Repository(self.data_root / mode / 'control.sqlite3')
        if new_live:
            # Even an account with no conversations must be reopenable/backable.
            # Never recreate a missing ledger inside an existing workspace.
            from .integrations.accio_native.vendor.adapters.common import Ledger
            from .workspace_backup import RECEIPTS
            Ledger(live_root / 'adapters-live.sqlite3').initialize()
            (self.data_root / RECEIPTS).mkdir(parents=True, exist_ok=True)
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.settings.update(self._control.get('meta', 'settings', {}))
        self._init_feishu_settings()
        self._integration_factory = integration_factory or build_integrations
        self.integrations = self._integration_factory(mode, self.data_root / mode, copy.deepcopy(self.settings))
        configure_router = getattr(self.integrations, 'configure_model_router', None)
        if configure_router is not None:
            configure_router(self._model_router)
        self.repo = Repository(self.data_root / 'demo' / 'workspace.sqlite3', self.seller_id) if mode == 'demo' else Repository(None)
        self.checks = {name: blank_check() for name in CHECK_NAMES}
        self._init_requirement_media()
        self._init_message_monitor()
        self._recover()
        self._load_scope()
        self._init_product_library()
        self._init_whatsapp()
        self._initialize_unread()
        self._fixture_initializer = fixture_initializer
        if self._fixture_initializer is not None:
            self._fixture_initializer(self)
        self._init_translation()
        self._worker = threading.Thread(target=self._executor_health.run, args=(self._work_loop, 'worker'), name='reception-external-worker', daemon=True)
        self._worker.start()
        self._health_watchdog = threading.Thread(target=self._executor_health.run, args=(self._executor_health.watch, 'health'), name='reception-health', daemon=True)
        self._health_watchdog.start()
        self._start_message_monitor()

    def _emit(self, event, data):
        if self._emit_callback:
            try:
                self._emit_callback({'event': event, 'data': copy.deepcopy(data)})
            except Exception:
                # A disconnected renderer cannot roll back a committed transaction.
                return

    def _changed(self, conversation_id=None, case_id=None):
        self._emit('state.changed', {k: v for k, v in {'conversation_id': conversation_id, 'case_id': case_id}.items() if v})

    def _require(self, kind, key):
        value = self.repo.get(kind, key)
        if value is None:
            raise ServiceError('not_found', '业务记录不存在')
        return value

    def _recover(self):
        with self.repo.transaction():
            self.repo.compact_supplements()
            for product in self.repo.list('product_catalog'):
                if (product.get('lookup') or {}).get('status') == 'querying':
                    product['lookup'] = {'status': 'unknown', 'reason': '进程中断', 'queried_at': None}
                    self.repo.put('product_catalog', product['catalog_product_id'], product, history=True)
            for case in self.repo.list('case'):
                if case.get('state') not in ('needs_human', 'human_owned'):
                    continue
                conversation = self.repo.get('conversation', case['conversation_id'])
                if not conversation:
                    continue
                if case['state'] == 'human_owned':
                    # This legacy state was set by an actual takeover, unlike a
                    # model's needs_human signal. Preserve ownership separately.
                    conversation.update(human_owned=True, manual_chat=True, revision=conversation['revision'] + 1)
                    self.repo.put('conversation', conversation['conversation_id'], conversation, history=True)
                case.update(state='waiting_quote' if not case.get('missing') else 'clarifying',
                            revision=case['revision'] + 1, updated_at=now_ms())
                self.repo.put('case', case['case_id'], case, history=True)
            for job in self.repo.list('job'):
                if job['status'] in ('queued', 'running'):
                    job.update(status='unknown' if job['kind'] in ('confirm_send', 'automatic_send', 'send_chat') else 'cancelled',
                               completed_at=now_ms(), message='上次进程结束；未决发送保持未知', stage='recovery')
                    self.repo.put('job', job['job_id'], job, history=True)
            for operation in self.repo.list('operation'):
                job = self.repo.get('job', operation.get('job_id')) if operation.get('purpose') == 'chat' else None
                if (operation.get('purpose') == 'chat' and operation['status'] == 'prepared'
                        and operation.get('submission_started') is False and job
                        and job.get('operation_id') == operation['operation_id']
                        and job.get('kind') == 'send_chat' and job.get('status') == 'cancelled'
                        and job.get('started_at') is None):
                    # Compatibility with the old clean-shutdown drain: its job proves
                    # this prepared intent never ran. A crash with no such proof stays unknown.
                    operation.update(status='rejected', submission_started=False, updated_at=now_ms(),
                                     message='上次退出前尚未执行，本条消息未提交，正文已保留')
                    self.repo.put('operation', operation['operation_id'], operation, history=True)
                    self._sync_chat_draft_status(operation)
                    continue
                if operation['status'] in ('prepared', 'sending'):
                    operation.update(status='unknown', message='进程中断，缺少权威回执', updated_at=now_ms())
                    self.repo.put('operation', operation['operation_id'], operation, history=True)
                    if operation.get('purpose') == 'chat':
                        self._sync_chat_draft_status(operation)
                        job = self.repo.get('job', operation.get('job_id'))
                        if job:
                            job.update(status='unknown', stage='recovery', completed_at=now_ms(), message=operation['message'])
                            self.repo.put('job', job['job_id'], job, history=True)
                    draft = self.repo.get('draft', operation['draft_id']) if operation.get('purpose') != 'chat' else None
                    if draft:
                        draft.update(status='unknown', updated_at=now_ms())
                        self.repo.put('draft', draft['draft_id'], draft, history=True)
            for item in self.repo.list('outbox'):
                if item['status'] == 'syncing':
                    item.update(status='unknown', message='上次同步结果未知；保留操作编号，等待核对', updated_at=now_ms())
                    self.repo.put('outbox', item['operation_id'], item, history=True)
            self._restore_continuity()
            self._recover_reply_batches()
            self._recover_manual_reply_batches()
            for work in self.repo.list('supplement_followup'):
                if work.get('status') == 'generating':
                    work.update(status='pending', due_at=now_ms() + 1500)
                    self.repo.put('supplement_followup', work['conversation_id'], work)
            restore_retry_job_states(self.repo, self._job_update, self.seller_id)

    def _initialize_unread(self):
        with self.repo.transaction():
            for conversation in self.repo.list('conversation'):
                unread.initialize(self.repo, conversation)

    def _activate_seller(self, seller):
        if not isinstance(seller, str) or not seller.strip():
            raise ServiceError('identity_blocked', '尚未取得真实卖家身份', stage='identity')
        if self.expected_seller_id and self.expected_seller_id != seller:
            raise ServiceError('seller_mismatch', '当前账号与预期卖家不匹配', stage='identity')
        with self._lock:
            if self.seller_id and self.seller_id != seller:
                raise ServiceError('seller_mismatch', '服务生命周期内不能切换卖家', stage='identity')
            if not self.seller_id:
                if self._seller_guard is not None and self._seller_guard.seller != seller:
                    raise ServiceError('seller_mismatch', '服务生命周期内不能切换卖家', stage='identity')
                if self._seller_guard is None:
                    try:
                        self._seller_guard = SellerGuard(seller, self.data_root / 'live').acquire()
                    except SellerBusyError:
                        raise ServiceError('seller_busy', '该卖家已有运行中的会话，请先退出原工作台', stage='identity') from None
                    except (OSError, ValueError):
                        raise ServiceError('identity_blocked', '无法安全占用卖家会话', stage='identity') from None
                old = self.repo
                seller_dir = hashlib.sha256(seller.encode('utf-8')).hexdigest()
                self.repo = Repository(self.data_root / 'live' / 'sellers' / seller_dir / 'workspace.sqlite3', seller)
                self.seller_id = seller
                self.account_verified = True
                self._retire_translation_settings()
                self._recover()
                self._load_scope()
                self._init_whatsapp()
                self._initialize_unread()
                # Preserve jobs created in the unverified empty workspace only after recovery.
                with self.repo.transaction():
                    for job in old.list('job'):
                        self.repo.put('job', job['job_id'], job, history=True)
                old.close()
            self.account_verified = True

    def call(self, method, params=None):
        if self._whatsapp_retired and method in {
                'get_whatsapp_state', 'save_whatsapp_binding', 'get_whatsapp_context',
                'set_whatsapp_reception', 'takeover_whatsapp', 'reconcile_whatsapp_operation',
                '_wa_set_account', '_wa_ingest', '_wa_prepare_send', '_wa_transport_result', '_wa_ack',
                '_wa_media_begin', '_wa_media_chunk', '_wa_media_finish', '_wa_media_fail'}:
            raise ServiceError('whatsapp_moved', '请从启动台使用独立 WhatsApp 账号环境')
        if method not in FAST | SLOW:
            raise ServiceError('unknown_method', '不支持的业务方法')
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ServiceError('invalid_params', '参数必须是对象')
        if method == 'shutdown':
            self._pause(closing=True)
            return {'status': 'closing'}
        if method == 'send_chat' and not self._closed:
            return self._enqueue(method, params)  # Recover an existing ack even while closing.
        if self._closing:
            raise ServiceError('closing', '服务正在关闭')
        if method in SLOW:
            return self._enqueue(method, params)
        try:
            return getattr(self, method)(**params)
        except TypeError as error:
            raise ServiceError('invalid_params', '业务参数不完整或含未知字段') from error

    def _configure_models(self, **packet):
        with self._lock:
            self._model_config_valid = False
            try:
                affected = self._model_router.configure(packet)
            except ServiceError:
                self._translation_generation += 1
                for _, cancel in self._translation_active.values():
                    cancel.set()
                self.checks['ai'] = {**blank_check(), 'status': 'blocked', 'message': '模型配置不可用，请重新检查'}
                self._wa_enabled = False
                with self.repo.transaction():
                    self._wa_cancel()
                    self._revoke_model_batches()
                self._pause()
                raise
            self._model_config_valid = True
            if 'translation' in affected:
                self._translation_generation += 1
                for _, cancel in self._translation_active.values():
                    cancel.set()
            if 'reception' in affected:
                self.checks['ai'] = {**blank_check(), 'status': 'not_checked', 'message': '接待模型已变化，请重新检查'}
                self._wa_enabled = False
                with self.repo.transaction():
                    self._wa_cancel()
                    self._revoke_model_batches()
                self._pause()
        self._changed()
        return {'affected': sorted(affected)}

    def _revoke_model_batches(self):
        for batch in self.repo.list('reply_batch'):
            if batch.get('origin') == 'automatic' and batch.get('status') in ('prepared', 'sending', 'waiting_retry', 'waiting_receipt', 'waiting_sync'):
                self._stop_reply_batch(batch, '接待模型配置已变化，未提交条目已停止')

    def _freeze_reception_model(self, required_capabilities=('text', 'structure'), *, allow_unchecked=False):
        if not self._model_config_valid:
            raise ServiceError('model_configuration_invalid', '模型配置不可用', stage='model_configuration')
        from dataclasses import asdict
        return asdict(self._model_router.resolve('reception', required_capabilities, allow_unchecked=allow_unchecked))

    def _assert_reception_model(self, frozen):
        if not self._model_config_valid:
            raise ServiceError('model_configuration_invalid', '模型配置不可用', stage='model_configuration')
        from .model_router import ModelSnapshot
        try:
            if not isinstance(frozen, dict) or frozen.get('purpose') != 'reception':
                raise ValueError('invalid reception snapshot')
            snapshot = ModelSnapshot(**{**frozen, 'required_capabilities': tuple(frozen['required_capabilities'])})
        except (KeyError, TypeError, ValueError):
            raise ServiceError('model_configuration_stale', '模型配置不可用或已变化', stage='model_configuration') from None
        self._model_router.assert_current(snapshot)

    def _verify_model_result(self, frozen, result):
        if self.mode != 'live':
            return None
        actual = result.get('model_snapshot') if isinstance(result, dict) else None
        self._assert_reception_model(frozen)
        self._assert_reception_model(actual)
        keys = ('purpose', 'profile_id', 'route_revision', 'config_revision', 'credential_revision', 'capability_revision')
        if (any(frozen.get(key) != actual.get(key) for key in keys)
                or actual.get('allow_unchecked', False) and not frozen.get('allow_unchecked', False)):
            raise ServiceError('model_configuration_stale', '模型配置已变化，请重新生成', stage='model_configuration')
        return copy.deepcopy(actual)

    def _budget(self):
        return {'limit': None, 'used': self._budget_used, 'remaining': None,
                'ai_used': self._ai_budget_used, 'ai_remaining': None, 'unlimited': True}

    def _company_name(self):
        if self.mode == 'demo':
            return '演示商家公司'
        if not self.account_verified:
            return None
        profile = self.repo.get('meta', 'company_profile', {})
        return profile.get('company_name') if profile.get('seller_id') == self.seller_id else None

    def _task_refresh_company_profile(self, params, job_id, epoch, facade):
        self._ensure_identity(facade)
        seller = self.seller_id
        try:
            profile = facade.company_profile(seller)
            if (not isinstance(profile, dict) or profile.get('seller_id') != seller
                    or not isinstance(profile.get('company_name'), str) or not profile['company_name'].strip()
                    or len(profile['company_name']) > 400 or any(ord(c) < 32 for c in profile['company_name'])):
                raise ServiceError('company_profile_unavailable', '公司名称读取失败')
        except Exception as error:
            self._job_update(job_id, diagnostics=read_diagnostics(error))
            with self._lock, self.repo.transaction():
                if epoch != self._epoch or self._closing or self.seller_id != seller:
                    raise ServiceError('cancelled', '公司名称读取期间来源已变化')
                previous = self.repo.get('meta', 'company_profile', {})
                if previous.get('seller_id') != seller or not previous.get('company_name'):
                    self.repo.put('meta', 'company_profile', {'seller_id': seller, 'company_name': '', 'updated_at': now_ms()})
            self._changed()
            return {'_job_status': 'failed', 'message': '公司名称读取失败，可重试'}
        with self._lock, self.repo.transaction():
            if epoch != self._epoch or self._closing or self.seller_id != seller:
                raise ServiceError('cancelled', '公司名称读取期间来源已变化')
            self.repo.put('meta', 'company_profile', {'seller_id': seller,
                'company_name': profile['company_name'].strip(), 'updated_at': now_ms()})
        self._changed()
        return {'message': '公司名称已更新'}

    def get_executor_health(self):
        return self._executor_health.snapshot()

    def get_snapshot(self):
        from .product_catalog import project_case
        with self._lock:
            projection = ReceptionProjection(self)
            checks = copy.deepcopy(self.checks)
            if self.mode == 'live':
                try:
                    self._freeze_reception_model()
                except ServiceError:
                    checks['ai'] = {**blank_check(), 'status': 'blocked', 'message': '接待模型配置不可用或已变化'}
            source_conversations = self.repo.list('conversation')
            owners = {c['conversation_id']: c for c in source_conversations if c.get('seller_id') == self.seller_id}
            latest = {}
            for case in projection.cases.values():
                owner = owners.get(case.get('conversation_id'))
                if (not owner or case.get('seller_id') != self.seller_id
                        or case.get('buyer_id') != owner.get('buyer_id') or not case.get('case_id')):
                    continue
                key = (case.get('updated_at') or 0, case['case_id'])
                if case['conversation_id'] not in latest or key > latest[case['conversation_id']][0]:
                    requirements = case.get('requirements') or {}
                    def field(name):
                        item = requirements.get(name)
                        value = item.get('value') if isinstance(item, dict) else None
                        return value if isinstance(value, str) and value.strip() else None
                    latest[case['conversation_id']] = (key, {'case_id': case['case_id'],
                        'product': field('product'), 'quantity': field('quantity'), 'updated_at': case.get('updated_at')})
            conversations = [dict(self._summary(c, projection),
                                  latest_requirement=latest.get(c['conversation_id'], (None, None))[1])
                             for c in source_conversations]
            conversations.sort(key=lambda c: c['last_timestamp'] or 0, reverse=True)
            return {'app': {'version': __version__, 'mode': self.mode, 'seller_id': self.seller_id,
                            'seller_suffix': self.seller_id[-6:], 'account_verified': self.account_verified,
                            'company_name': self._company_name()},
                    'reception': {'running': self._running, 'auto_send': self._auto_send,
                                  'manual_only': self._manual_only, 'started_at_ms': self._started_at_ms,
                                  'scope': copy.deepcopy(self._scope), 'budget': self._budget()},
                    'checks': checks, 'conversations': conversations,
                    'handoff_cases': sorted([project_case(self.repo, case) for case in projection.cases.values() if case['state'] == 'waiting_quote'],
                                            key=lambda case: case['updated_at'], reverse=True),
                    'policies': [policy_library.normalize_policy(p) for p in self.repo.list('policy')], 'jobs': self.list_jobs(), 'outbox': self.list_outbox(),
                    'executor_health': self._executor_health.snapshot(),
                    'settings': self._feishu_settings_snapshot(), 'message_sync': self._message_sync_snapshot(),
                    'translation': self.get_translation_info()}

    def list_assistance_tasks(self, conversation_id=None, status=None):
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise ServiceError('invalid_params', '客户标识无效')
        if status is not None and status not in ('open', 'resolved', 'dismissed'):
            raise ServiceError('invalid_params', '协助状态无效')
        with self._lock:
            if conversation_id is not None:
                self._require('conversation', conversation_id)
            items = self.repo.list('assistance_task', **({'conversation_id': conversation_id} if conversation_id is not None else {}))
            return {'items': [t for t in items if t.get('seller_id') == self.seller_id and (status is None or t['status'] == status)]}

    def save_assistance_result(self, task_id, expected_revision, result, result_public, schedule_followup, client_mutation_id):
        return self._save_assistance_result(task_id, expected_revision, result, result_public,
                                            schedule_followup, client_mutation_id)

    def start_assistance_task(self, task_id, expected_revision, client_mutation_id):
        if (not isinstance(task_id, str) or not task_id or type(expected_revision) is not int or expected_revision < 1
                or not isinstance(client_mutation_id, str) or not 1 <= len(client_mutation_id.strip()) <= 200):
            raise ServiceError('invalid_params', '协助处理参数无效')
        request_hash = text_hash(encoded([task_id, expected_revision]))
        with self._lock, self.repo.transaction():
            task = self._require('assistance_task', task_id)
            conversation = self._chat_owner(task['conversation_id'])
            if any(task.get(key) != conversation.get(key) for key in ('seller_id', 'buyer_id', 'conversation_id')):
                raise ServiceError('operation_owner_mismatch', '协助任务不属于当前客户')
            prior = self.repo.get('assistance_start_mutation', client_mutation_id)
            if prior:
                if prior.get('request_hash') != request_hash:
                    raise ServiceError('mutation_conflict', '操作编号已用于其他协助处理')
                return task
            if task['revision'] != expected_revision:
                raise ServiceError('revision_conflict', '协助任务已变化，请刷新后再操作')
            if task['status'] != 'open':
                raise ServiceError('assistance_closed', '该协助任务已结束')
            if not task.get('processing_started_at'):
                task.update(processing_started_at=now_ms(), updated_at=now_ms(), revision=task['revision'] + 1)
                self.repo.put('assistance_task', task_id, task, history=True)
                conversation['revision'] += 1
                self.repo.put('conversation', task['conversation_id'], conversation)
                self._invalidate(task['conversation_id'])
            self.repo.put('assistance_start_mutation', client_mutation_id, {'request_hash': request_hash, 'task_id': task_id})
        self._changed(task['conversation_id'], task['case_id'])
        return task

    def _create_assistance(self, case, source, job_id, kind, fact_keys, detail=None):
        fact_keys = sorted(set(key for key in fact_keys if key in FIELDS))
        existing = next((t for t in self.repo.list('assistance_task', case_id=case['case_id'])
                         if t.get('seller_id') == self.seller_id and t['status'] == 'open'
                         and t['kind'] == kind and sorted(t.get('fact_keys', [])) == fact_keys
                         and (not detail or t.get('detail') == detail[:2000])), None)
        if existing:
            return existing
        stamp = now_ms()
        task = {'task_id': new_id('assist'), 'seller_id': self.seller_id, 'buyer_id': case['buyer_id'],
                'conversation_id': case['conversation_id'], 'case_id': case['case_id'], 'kind': kind,
                'status': 'open', 'title': {'fact_conflict': '需要核实客户需求', 'verify_followup': '需要核实并跟进',
                                          'generation_failure': '需要检查接待生成'}.get(kind, '需要人工协助'),
                'detail': detail[:2000] if isinstance(detail, str) and detail.strip() else '请核实相关信息，接待可继续进行。',
                'source_message_id': source['message_id'] if source else None,
                'source_job_id': job_id, 'fact_keys': fact_keys, 'revision': 1, 'created_at': stamp,
                'updated_at': stamp, 'resolution': '', 'result_public': False, 'followup_status': 'not_scheduled',
                'processing_started_at': None}
        self.repo.put('assistance_task', task['task_id'], task, history=True)
        return task

    def _summary(self, conversation, projection=None):
        projection = projection or ReceptionProjection(self)
        case = projection.cases.get(conversation['case_id'])
        pending = sum(1 for draft in projection.drafts[conversation['conversation_id']]
                      if draft['status'] in ('pending', 'stale', 'unknown'))
        return {**{key: conversation[key] for key in ('conversation_id', 'buyer_id', 'customer_name', 'country',
                                                       'last_message', 'last_timestamp', 'case_id', 'unread_count')},
                'last_message': '照片' if is_photo_placeholder(conversation['last_message']) else conversation['last_message'],
                'state': case['state'] if case else 'clarifying', 'pending_count': pending,
                'human_owned': conversation.get('human_owned') is True,
                'reply_pending': projection.reply_pending(conversation),
                'reception_status': projection.status(conversation)}

    def get_conversation(self, conversation_id, case_id=None, refresh_empty=False):
        from .product_catalog import project_case
        if type(refresh_empty) is not bool:
            raise ServiceError('invalid_params', '读取选项必须为布尔值')
        with self._lock:
            conversation = self._require('conversation', conversation_id)
            case = (self._require('case', case_id) if case_id is not None
                    else self.repo.get('case', conversation.get('case_id')))
            if case and (case['conversation_id'] != conversation_id or case['seller_id'] != self.seller_id
                         or case['buyer_id'] != conversation['buyer_id']):
                raise ServiceError('case_conversation_mismatch', '采购事项不属于当前客户会话')
            if refresh_empty:
                self._request_empty_history(conversation_id)
            drafts = [row for row in self.repo.list('draft', case_id=case['case_id']) if not row.get('parent_draft_id')] if case else []
            drafts.sort(key=lambda d: (d['updated_at'], d['created_at']))
            for draft in drafts:
                draft.pop('quality_warnings', None)
                if isinstance(draft.get('validation'), dict):
                    draft['validation'].pop('warnings', None)
            summary = self.repo.get('conversation_summary', conversation_id)
            if summary and (summary.get('seller_id') != conversation.get('seller_id')
                            or summary.get('buyer_id') != conversation.get('buyer_id')):
                summary = None
            summary = ({key: summary[key] for key in ('conversation_id', 'body', 'revision', 'updated_at')}
                       if summary else None)
            current_source = (self._automatic_source(conversation_id)
                              if self._running and self._in_scope(conversation_id) and not conversation.get('human_owned') else None)
            return {'conversation': {**self._summary(conversation), 'revision': conversation['revision']}, 'case': project_case(self.repo, case),
                    'automatic_source': ({'message_id': current_source['message_id'], 'text_sha256': text_hash(current_source['text'])}
                                         if current_source else None),
                    'cases': [project_case(self.repo, c) for c in self.repo.list('case', conversation_id=conversation_id)],
                    'messages': self.get_messages(conversation_id, limit=100)['items'],
                    'reading': unread.reading(self.repo, conversation_id),
                    'history': self.repo.get('coverage', conversation_id, coverage()),
                    'draft': drafts[-1] if drafts else None,
                    'chat_draft': self.repo.get('chat_draft', conversation_id),
                    'supplement': self._supplement(conversation_id),
                    'conversation_summary': summary,
                    'requirement_media': self.get_requirement_media(case['case_id']) if case else None,
                    'manual_suggestion': self._manual_suggestion(conversation_id),
                    'reply_batches': [self._reply_batch_progress(row) for row in self.repo.list('reply_batch', conversation_id=conversation_id)],
                    'recoverable_reply_batches': self._recoverable_reply_batches(conversation_id)}

    def get_messages(self, conversation_id, before_id=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ServiceError('invalid_params', '分页条数应在 1 至 200 之间')
        with self._lock:
            self._require('conversation', conversation_id)
            before = None
            if before_id is not None:
                message = self.repo.get('message', encoded([conversation_id, before_id]))
                if not message or message.get('conversation_id') != conversation_id or message.get('message_id') != before_id:
                    # Older databases may have used a different entity key.
                    message = next((row for row in self.repo.list('message', conversation_id=conversation_id)
                                    if row.get('message_id') == before_id), None)
                if not message:
                    raise ServiceError('invalid_cursor', '分页游标不属于当前客户')
                stamp = message.get('timestamp_ms')
                before = (stamp if stamp is not None else -1, before_id)
            messages = self.repo.message_page(conversation_id, limit + 1, before=before)
            return {'items': [self._photo_projection(message) for message in messages[-limit:]],
                    'has_more': len(messages) > limit,
                    'reading': unread.reading(self.repo, conversation_id)}

    def mark_messages_read(self, conversation_id, through_message_id, observed_sequence):
        with self._lock, self.repo.transaction():
            conversation = self._require('conversation', conversation_id)
            result = unread.acknowledge(self.repo, conversation, through_message_id, observed_sequence)
        self._changed(conversation_id)
        return result

    def _messages(self, conversation_id):
        return sorted((self._photo_projection(message) for message in self.repo.list('message', conversation_id=conversation_id)),
                      key=lambda m: (m['timestamp_ms'] if m['timestamp_ms'] is not None else -1, m['message_id']))

    def get_context(self, case_id):
        with self._lock:
            case = self._require('case', case_id)
            context = build_context(self.repo, case, self.settings, self._budget(),
                                    continuation=self._generation_continuation(case))
            context.update(self._product_context_for_case(case))
            self._with_case_catalog_context(context)
            from .model_payload import model_json
            from .reception_context import semantic_fingerprint
            model_json(context)
            context['fingerprint'] = semantic_fingerprint(context)
            return context

    def list_jobs(self, include_background=False):
        if type(include_background) is not bool:
            raise ServiceError('invalid_params', '读取选项必须为布尔值')
        with self._lock:
            if include_background:
                return sorted(self.repo.list('job'), key=lambda j: j['created_at'], reverse=True)
            return visible_jobs(self.repo)

    @staticmethod
    def _send_operation_dto(operation):
        # Only receipt metadata crosses the UI boundary; never expose intents or raw output.
        keys = ('operation_id', 'case_id', 'case_revision', 'draft_id', 'draft_revision',
                'conversation_id', 'source_message_id', 'status', 'message', 'message_id',
                'submission_started', 'updated_at', 'purpose', 'client_request_id')
        return {'kind': 'send', **{key: operation.get(key) for key in keys}}

    def list_outbox(self):
        with self._lock:
            items = self.repo.list('outbox')
            items += [self._send_operation_dto(o) for o in self.repo.list('operation')]
            return sorted(items, key=lambda o: o.get('updated_at') or 0, reverse=True)

    def _invalidate(self, conversation_id=None, policy_id=None):
        self._cancel_generation_tasks(conversation_id, reason='source_changed')
        for draft in self.repo.list('draft'):
            if draft['status'] not in ('pending', 'stale'):
                continue
            if conversation_id and draft['conversation_id'] != conversation_id:
                continue
            if policy_id and policy_id not in draft['validation']['sources']:
                continue
            draft.update(status='stale', updated_at=now_ms())
            draft['validation'] = {**draft['validation'], 'ready': False,
                                   'issues': list(dict.fromkeys(draft['validation']['issues'] + ['依据或授权状态已变化，请重新复核']))}
            self.repo.put('draft', draft['draft_id'], draft, history=True)
        for confirmation in self.repo.list('confirmation'):
            if not conversation_id or confirmation['conversation_id'] == conversation_id:
                confirmation['invalidated'] = True
                self.repo.put('confirmation', confirmation['confirmation_id'], confirmation)

    def _new_case(self, conversation, procurement_key='', source_message_id=None):
        case = {'case_id': new_id('case'), 'seller_id': self.seller_id, 'buyer_id': conversation['buyer_id'],
                'conversation_id': conversation['conversation_id'], 'customer_name': conversation['customer_name'],
                'procurement_key': procurement_key, 'revision': 1, 'state': 'clarifying', 'facts': {},
                'missing': [], 'questions': {}, 'updated_at': now_ms()}
        case['missing'] = missing_fields_for_case(case['facts'], self.settings, case)
        self.repo.put('case', case['case_id'], case, history=True)
        self.repo.put('case_origin', case['case_id'], {'seller_id': self.seller_id, 'buyer_id': case['buyer_id'],
                      'conversation_id': case['conversation_id'], 'message_id': source_message_id,
                      'initial_history': source_message_id is None}, history=True)
        return case

    def _registration_snapshot(self, case):
        with self._lock, self.repo.transaction():
            return self._registration_snapshot_locked(case)

    def _registration_snapshot_locked(self, case):
        snapshot = copy.deepcopy(case)
        origin = self.repo.get('case_origin', case['case_id'])
        if not origin:
            return snapshot  # Legacy/unknown origin must not be guessed from mutable facts.
        if (not origin['message_id'] and origin.get('initial_history')
                and self.repo.get('coverage', case['conversation_id'], coverage())['status'] == 'complete'):
            first = next((m for m in self._messages(case['conversation_id']) if m['role'] == 'buyer'), None)
            if first:
                origin = {**origin, 'message_id': first['message_id']}
                self.repo.put('case_origin', case['case_id'], origin, history=True)
        if origin['message_id']:
            message = self.repo.get('message', encoded([case['conversation_id'], origin['message_id']]))
            if (message and message['role'] == 'buyer' and message['seller_id'] == case['seller_id']
                    and message['buyer_id'] == case['buyer_id'] and origin['seller_id'] == case['seller_id']
                    and origin['buyer_id'] == case['buyer_id'] and origin['conversation_id'] == case['conversation_id']):
                snapshot['_inquiry_source_id'] = origin['message_id']
        return snapshot

    def _record_new_buyer_activity(self, conversation):
        cid = conversation['conversation_id']
        conversation['last_buyer_at'] = now_ms()
        case = self._require('case', conversation['case_id'])
        case.update(revision=case['revision'] + 1, updated_at=now_ms())
        case['missing'] = missing_fields_for_case(case['facts'], self.settings, case)
        latest_buyer = self._latest_buyer(cid)
        if latest_buyer and self._stop_requested(latest_buyer['text']):
            case['state'] = 'closed'
        elif not conversation['human_owned'] and case['state'] not in ('closed', 'waiting_quote'):
            case['state'] = 'clarifying'
        self.repo.put('case', case['case_id'], case, history=True)

    def _weak_history_head(self, cid, messages, version):
        """Bind a confirmed live read to its head, never to an older backfill row."""
        if not self._running or self._started_at_ms is None or not version:
            return None
        activity = self.repo.get('message_activity', cid, {})
        if (activity.get('version') != version or activity.get('activity_clock') != 'conversation'
                or activity.get('observed_at', -1) < self._started_at_ms):
            return None
        ordered = self._messages(cid)
        head = ordered[-1] if ordered else None
        if (not head or head.get('role') != 'buyer' or type(head.get('timestamp_ms')) is not int
                or head['timestamp_ms'] <= self._started_at_ms):
            return None
        baseline = self._source_baseline.get(cid)
        if baseline and (head['message_id'] == baseline['message_id'] or
                         baseline.get('timestamp_ms') is not None and head['timestamp_ms'] <= baseline['timestamp_ms']):
            return None
        marker = self.repo.get('history_only_message', encoded([cid, head['message_id']]))
        if (not marker or type(marker.get('activity_version')) is not int
                or version <= marker['activity_version']):
            return None
        # The real head read must contain this exact ID, role, body and time.
        # Completion additionally requires two identical native body reads for a
        # weak activity; historical pages and the synthetic follow-up cannot opt in.
        return next((m for m in messages if m.get('message_id') == head['message_id']
                     and m.get('role') == 'buyer' and m.get('text') == head['text']
                     and m.get('timestamp_ms') == head['timestamp_ms']), None)

    def _promote_history_activity(self, cid, messages, version, message_id, digest, *, confirmed_live_head=False):
        if not version:
            return False
        if message_id and not digest:
            identified = next((m for m in messages if m.get('message_id') == message_id
                               and m.get('role') == 'buyer' and isinstance(m.get('text'), str)), None)
            if not identified:
                return False
            digest = text_hash(identified['text'])
        elif not message_id:
            exact_head = self._weak_history_head(cid, messages, version) if confirmed_live_head else None
            if not exact_head or digest and text_hash(exact_head['text']) != digest:
                return False
            message_id, digest = exact_head['message_id'], text_hash(exact_head['text'])
        key = encoded([cid, message_id])
        marker = self.repo.get('history_only_message', key)
        exact = next((m for m in messages if m.get('message_id') == message_id), None)
        if (marker and type(marker.get('activity_version')) is int
                and version > marker['activity_version'] and exact
                and exact.get('role') == 'buyer' and isinstance(exact.get('text'), str)
                and text_hash(exact['text']) == digest):
            self.repo.db.execute("DELETE FROM entities WHERE kind='history_only_message' AND id=?", (key,))
            conversation = self._require('conversation', cid)
            self._record_new_buyer_activity(conversation)
            conversation['revision'] += 1
            self.repo.put('conversation', cid, conversation)
            self._invalidate(cid)
            return True
        return False

    def _ingest(self, info, messages, *, has_more=True, next_cursor=None, full=False, history=False, history_activity_version=None):
        with self._lock, self.repo.transaction():
            cid, buyer = info.get('conversation_id'), info.get('buyer_id')
            if (not isinstance(cid, str) or not cid or not isinstance(buyer, str) or not buyer
                    or not self.account_verified):
                raise ServiceError('message_identity_mismatch', '会话缺少已核验身份', stage='messages')
            conversation = self.repo.get('conversation', cid)
            if conversation and conversation['buyer_id'] != buyer:
                raise ServiceError('identity_mismatch', '会话买家身份不匹配')
            if not conversation:
                conversation = {'conversation_id': cid, 'seller_id': self.seller_id,
                                'buyer_id': buyer, 'customer_name': str(info.get('customer_name') or buyer),
                                'country': '', 'last_message': '', 'last_timestamp': None, 'case_id': '',
                                'unread_count': 0, 'revision': 0, 'human_owned': False, 'last_buyer_at': None}
                case = self._new_case(conversation)
                conversation['case_id'] = case['case_id']
                self.repo.put('conversation', cid, conversation)
            unread.initialize(self.repo, conversation)
            new_messages = []
            message_changes = []
            changed = False
            new_buyer = False
            edited_ids, image_edited_ids = set(), set()
            def image_source(message):
                from .photos import has_photo
                if not has_photo(message):
                    return None
                return {'text': message['text'], 'role': message['role'],
                        'images': [row for row in message.get('attachments', [])
                                   if isinstance(row, dict) and row.get('kind') == 'image']}
            for raw in messages:
                if (raw.get('seller_id') != self.seller_id or raw.get('buyer_id') != buyer
                        or raw.get('conversation_id') != cid or not isinstance(raw.get('message_id'), str) or not raw['message_id']
                        or raw.get('role') not in ('buyer', 'seller') or not isinstance(raw.get('text'), str)):
                    raise ServiceError('message_identity_mismatch', '消息缺少身份或不属于当前会话', stage='messages')
                stamp = raw.get('timestamp_ms')
                if stamp is not None and (type(stamp) is not int or stamp < 0):
                    raise ServiceError('invalid_message', '消息时间格式无效')
                msg = {k: raw.get(k) for k in ('message_id', 'seller_id', 'buyer_id', 'conversation_id', 'role', 'text', 'timestamp_ms')}
                msg['attachments'] = raw.get('attachments') if isinstance(raw.get('attachments'), list) else []
                msg['own'] = raw['role'] == 'seller'
                key = encoded([cid, msg['message_id']])
                old = self.repo.get('message', key)
                if old != msg:
                    message_changes.append(msg)
                    if old is None:
                        new_messages.append(msg)
                        if history:
                            version = history_activity_version
                            if version is None:
                                version = self.repo.get('message_activity', cid, {}).get('version', 0)
                            self.repo.put('history_only_message', key, {'conversation_id': cid, 'message_id': msg['message_id'],
                                                                         'activity_version': version})
                    semantic_edit = False
                    if old is not None:
                        if old.get('text') != msg['text'] or old.get('role') != msg['role']:
                            edited_ids.add(msg['message_id'])
                            semantic_edit = True
                        if image_source(old) != image_source(msg):
                            image_edited_ids.add(msg['message_id'])
                            semantic_edit = True
                        elif image_source(msg) is not None:
                            from .photos import photo_version
                            cached = self.repo.get('message_image', key)
                            if cached and cached.get('status') == 'ready' and cached.get('source_version') == photo_version(old):
                                cached['source_version'] = photo_version(msg)
                                self.repo.put('message_image', key, cached)
                    self.repo.put('message', key, msg, history=True)
                    changed = True
                    if msg['role'] == 'buyer' and (semantic_edit or not history and old is None and (conversation['last_timestamp'] is None
                                                  or stamp is None or stamp >= conversation['last_timestamp'])):
                        new_buyer = True
            # Every raw version above is validated and retained in history, but
            # reading eligibility follows the final version of each ID in a batch.
            new_ids = {message['message_id'] for message in new_messages}
            message_changes = list({message['message_id']: message for message in message_changes}.values())
            new_messages = [message for message in message_changes if message['message_id'] in new_ids]
            unread.ingest(self.repo, conversation, new_messages, history=history, changes=message_changes)
            if changed:
                task = self.repo.get('message_sync_task', cid, {})
                if self.mode == 'live' and task.get('wait_reason') == 'waiting_activity':
                    task.update(pending=True, activity_required=True, wait_reason=None,
                                no_body_confirmed=False, empty_reads=0, read_priority='activity', retry_at=0)
                    self.repo.put('message_sync_task', cid, task)
                conversation['revision'] += 1
                ordered = self._messages(cid)
                if ordered:
                    conversation['last_message'] = ordered[-1]['text']
                    conversation['last_timestamp'] = ordered[-1]['timestamp_ms']
                if new_buyer:
                    self._record_new_buyer_activity(conversation)
                if edited_ids or image_edited_ids:
                    for affected in self.repo.list('case', conversation_id=cid):
                        requirements = affected.get('requirements') or {}
                        invalid = [key for key, row in requirements.items() if isinstance(row, dict)
                                   and (edited_ids | (image_edited_ids if key == 'product_images' else set())).intersection(row.get('evidence_ids') or [])]
                        if invalid:
                            affected['requirements_revision'] = requirement_revision(affected) + 1
                            for key in invalid:
                                requirements[key] = {'value': [] if key == 'product_images' else None, 'evidence_ids': []}
                            if 'product_id' in invalid:
                                from .product_identity import derive_product_link
                                derive_product_link(affected)
                                self.repo.db.execute("DELETE FROM entities WHERE kind='requirement_product_binding' AND id=?", (affected['case_id'],))
                            affected.update(revision=affected['revision'] + 1, updated_at=now_ms())
                            self.repo.put('case', affected['case_id'], affected, history=True)
                self.repo.put('conversation', cid, conversation)
                self._invalidate(cid)
            existing = self.repo.get('coverage', cid, coverage())
            stamps = [m['timestamp_ms'] for m in self._messages(cid) if m['timestamp_ms'] is not None]
            existing.update(earliest_ms=min(stamps) if stamps else None, latest_ms=max(stamps) if stamps else None,
                            updated_at=now_ms(), message='历史读取完成' if full and not has_more else '已读取部分消息')
            if full:
                existing.update(status='partial' if has_more else 'complete', next_cursor=next_cursor)
            elif existing['status'] != 'complete':
                existing.update(status='partial', next_cursor=existing['next_cursor'] if existing['next_cursor'] is not None else next_cursor)
            self.repo.put('coverage', cid, existing)
            return changed

    @staticmethod
    def _stop_requested(text):
        return bool(re.search(r'(?:stop (?:contacting|messaging)|do not contact|don.t contact|unsubscribe|不要联系|停止联系|退订)', text, re.I))

    def _latest_buyer(self, conversation_id):
        return next((m for m in reversed(self._messages(conversation_id)) if m['role'] == 'buyer'), None)

    def _post_quote_analysis_allowed(self, case, source, *, excluding=None, jobs=None, drafts=None):
        if case['state'] != 'waiting_quote':
            return True
        if (not source or source['role'] != 'buyer'
                or self._generation_attempted(case['conversation_id'], case, source, excluding=excluding, jobs=jobs, drafts=drafts)):
            return False
        # A completed quotation must not reopen because its old evidence or draft
        # is still the latest buyer message, including legacy records without jobs.
        source_id = source['message_id']
        case_drafts = self.repo.list('draft', case_id=case['case_id']) if drafts is None else drafts
        if any(d['case_id'] == case['case_id'] and d['source_message_id'] == source_id
               and d['origin'] != 'manual' and d['status'] in ('stale', 'rejected') for d in case_drafts):
            return True  # Unsatisfied reply obligations survive policy/source invalidation.
        if (any(source_id in fact['evidence_ids'] for fact in case['facts'].values())
                or any(d['source_message_id'] == source_id
                       for d in (self.repo.list('draft', case_id=case['case_id']) if drafts is None else drafts)
                       if d['case_id'] == case['case_id'])):
            return False
        if source.get('attachments'):
            return True
        text = source['text'].strip()
        intent = social_intent(text)
        if intent:
            return intent not in ('thanks', 'ack', 'farewell')
        acknowledgment = (
            r'(?:thank(?:s| you)(?: so much| very much| a lot)?'
            r'(?: for (?:the |your )?(?:help|information|details|update|assistance))?'
            r'|ok(?:ay)?|got it|understood|noted|confirmed|received|sounds good|all right|sure'
            r'|谢谢(?:您|你)?|感谢(?:您|你)?|收到|好的?|明白(?:了)?|了解(?:了)?|确认)')
        # Match only an entire acknowledgment. A thank-you followed by a new
        # request or changed specification still needs analysis.
        separator = r'[\s,.!?;，。！？；、]*'
        return bool(text) and not re.fullmatch(
            acknowledgment + r'(?:' + separator + r'(?:and\s+)?' + acknowledgment + r')*' + separator,
            text, re.I)

    def save_draft(self, case_id, body, expected_revision, client_mutation_id):
        if not isinstance(body, str) or len(body) > 50_000 or not isinstance(client_mutation_id, str) or not client_mutation_id:
            raise ServiceError('invalid_params', '草稿正文或幂等编号无效')
        with self._lock, self.repo.transaction():
            request = {'case_id': case_id, 'body': body, 'expected_revision': expected_revision}
            mutation = self.repo.get('mutation', client_mutation_id)
            if mutation:
                if mutation['request'] != request:
                    raise ServiceError('mutation_conflict', '同一幂等编号不能保存不同内容')
                return mutation['result']
            case = self._require('case', case_id)
            drafts = [d for d in self.repo.list('draft') if d['case_id'] == case_id]
            drafts.sort(key=lambda d: (d['updated_at'], d['created_at']))
            previous = drafts[-1] if drafts else None
            if expected_revision != (previous['revision'] if previous else None):
                raise ServiceError('revision_conflict', '草稿版本已变化，请保留输入并重新读取')
            if (any(d['status'] in ('sending', 'unknown') for d in drafts)
                    or any(o['status'] in ('prepared', 'sending', 'unknown')
                           for o in self.repo.list('operation', case_id=case_id))):
                raise ServiceError('send_unresolved', '草稿仍有发送结果待核对')
            context = self.get_context(case_id)
            source = self._latest_buyer(case['conversation_id'])
            policies = {**{p['policy_id']: p['revision'] for p in context['knowledge']}, **context.get('policy_versions', {})}
            if (previous and previous['status'] == 'pending' and previous['origin'] == 'manual'
                    and previous['body'] == body and previous['case_id'] == case_id
                    and previous['conversation_id'] == case['conversation_id']
                    and previous['source_message_id'] == (source['message_id'] if source else None)
                    and previous['source_case_revision'] == case['revision']
                    and previous['source_conversation_revision'] == context['conversation_revision']
                    and previous.get('policy_versions', {}) == policies
                    and previous.get('photo_fingerprint') == photo_fingerprint(context)):
                draft = previous
            else:
                sources = [source['message_id']] if source else []
                # Manual text is checked against all applicable evidence, never given blanket approval.
                sources += [p['policy_id'] for p in context['knowledge']]
                sources += [o['observation_id'] for o in context['photo_context']['observations']]
                draft = self._draft(case, body, sources, context, 'manual', previous=previous)
                self.repo.put('draft', draft['draft_id'], draft, history=True)
                self._invalidate_confirmations(case['conversation_id'])
            self.repo.put('mutation', client_mutation_id, {'request': request, 'result': draft})
        self._changed(case['conversation_id'], case_id)
        return draft

    def _invalidate_confirmations(self, conversation_id):
        for confirmation in self.repo.list('confirmation'):
            if confirmation['conversation_id'] == conversation_id:
                confirmation['invalidated'] = True
                self.repo.put('confirmation', confirmation['confirmation_id'], confirmation)

    def _draft(self, case, body, sources, context, origin, previous=None, *, messages=None):
        source = self._latest_buyer(case['conversation_id'])
        stamp = now_ms()
        return {'draft_id': previous['draft_id'] if previous and previous['status'] not in ('accepted', 'rejected') else new_id('draft'),
                'case_id': case['case_id'], 'conversation_id': case['conversation_id'],
                'revision': previous['revision'] + 1 if previous else 1,
                'body': body, 'status': 'pending', 'source_message_id': source['message_id'] if source else None,
                'source_case_revision': case['revision'], 'source_conversation_revision': context['conversation_revision'],
                'origin': origin, 'created_at': previous['created_at'] if previous else stamp, 'updated_at': stamp,
                'reply_messages': copy.deepcopy(messages if messages is not None else [{'body': body, 'reply_sources': sources}]),
                'validation': validate_reply(body, sources, context, max_chars=1200 + 2 * max(0, len(messages or []) - 1)),
                'context_fingerprint': context['fingerprint'],
                'photo_fingerprint': photo_fingerprint(context),
                'photo_read_images': copy.deepcopy(context.get('photo_context', {}).get('read_images', [])),
                'supplement_fingerprint': supplement_fingerprint(context),
                'policy_versions': {**{p['policy_id']: p['revision'] for p in context['knowledge']}, **context.get('policy_versions', {})},
                'applied_policies': applied_policy_summaries(context)}

    def save_policy(self, kind, title, content, product_scope, confirmed, public, policy_id=None,
                    expected_revision=None, valid_until=None, *, category=None, enabled=None, usage=None,
                    customer_wording=None, internal_notes=None, priority=None):
        return policy_library.save_policy(self, kind, title, content, product_scope, confirmed, public,
                    policy_id, expected_revision, valid_until, category=category, enabled=enabled, usage=usage,
                    customer_wording=customer_wording, internal_notes=internal_notes, priority=priority)

    def list_builtin_policies(self):
        return policy_library.list_builtin_policies(self)

    def adopt_builtin_policy(self, template_id, content=None, product_scope=None, enabled=None,
                             confirmed=None, expected_revision=None, update=False):
        return policy_library.adopt_builtin_policy(self, template_id, content, product_scope, enabled,
                                                   confirmed, expected_revision, update)

    def save_settings(self, **changes):
        if any(k not in DEFAULT_SETTINGS for k in changes):
            raise ServiceError('invalid_settings', '仅允许已知设置字段')
        updated = copy.deepcopy(self.settings)
        for key, value in changes.items():
            if key in ADVANCED_INTEGER_SETTINGS:
                lower, upper = ADVANCED_INTEGER_SETTINGS[key]
                if type(value) is not int or not lower <= value <= upper:
                    raise ServiceError('invalid_settings', '高级处理设置超出允许范围')
            if key == 'reception_images_enabled' and type(value) is not bool:
                raise ServiceError('invalid_settings', '相关原图开关无效')
            if key == 'workctl_path' and (not isinstance(value, str) or '\x00' in value or len(value) > 4096):
                raise ServiceError('invalid_settings', 'workctl 路径无效')
            if key == 'poll_seconds' and (type(value) not in (int, float) or not 5 <= value <= 3600):
                raise ServiceError('invalid_settings', '轮询间隔应在 5 至 3600 秒')
            if key == 'probe_seconds' and (type(value) not in (int, float) or not 2 <= value <= 60):
                raise ServiceError('invalid_settings', '新消息检测间隔应在 2 至 60 秒')
            if key == 'merge_seconds' and (type(value) not in (int, float) or not 0 <= value <= 120):
                raise ServiceError('invalid_settings', '合并间隔应在 0 至 120 秒')
            if key == 'required_fields':
                if not isinstance(value, list) or any(f not in FIELDS for f in value):
                    raise ServiceError('invalid_settings', '必填字段无效')
                value = list(dict.fromkeys(['product'] + value))
            if key == 'required_fields_by_category':
                if (not isinstance(value, dict) or len(value) > 100 or any(
                        not isinstance(label, str) or not label.strip() or label != label.strip()
                        or len(label) > 8 or any(ord(char) < 32 for char in label)
                        or not isinstance(fields, list) or len(fields) > len(FIELDS)
                        or any(not isinstance(field, str) or field not in FIELDS for field in fields)
                        for label, fields in value.items())):
                    raise ServiceError('invalid_settings', '品类条件需要简短品类及有效需求字段')
                value = {label: list(dict.fromkeys(['product'] + fields)) for label, fields in value.items()}
            if key == 'feishu':
                allowed = {'enabled', 'write_enabled', 'cli_path', 'base_token', 'table', 'table_id',
                           'form_share_token', 'account_option', 'initial_status', 'fields', 'app_id', 'table_url'}
                if (not isinstance(value, dict) or set(value) - allowed
                        or any(type(v) is not bool for k, v in value.items() if k in ('enabled', 'write_enabled'))
                        or any(not isinstance(v, str) or len(v) > 4096 or '\x00' in v for k, v in value.items()
                               if k not in ('enabled', 'write_enabled', 'fields'))):
                    raise ServiceError('invalid_settings', '飞书仅支持已知开关、原生路径和表单标识，不接受凭据')
                if 'fields' in value:
                    fields = value['fields']
                    if (not isinstance(fields, dict) or len(fields) > 32 or any(
                            not isinstance(role, str) or len(role) > 64 or not isinstance(entry, dict)
                            or set(entry) != {'name', 'type'} or not isinstance(entry['name'], str)
                            or not entry['name'].strip() or len(entry['name']) > 200
                            or entry['type'] not in ('text', 'select', 'number', 'attachment', 'datetime', 'auto_number')
                            for role, entry in fields.items())):
                        raise ServiceError('invalid_settings', '飞书字段映射格式无效')
                if value.get('write_enabled') is True:
                    raise ServiceError('authorization_required', '请单独授权本次飞书写入。')
                if 'app_id' in value:
                    # New application settings replace the retired CLI form configuration.
                    value = {k: v for k, v in value.items() if k in ('enabled', 'app_id', 'table_url', 'fields', 'account_option', 'initial_status')}
                    legacy = updated['feishu'].get('legacy_cli_config')
                    if not legacy and updated['feishu'].get('cli_path'):
                        legacy = {**updated['feishu'], 'write_enabled': False}
                    if isinstance(legacy, dict):
                        value['legacy_cli_config'] = copy.deepcopy(legacy)
                else:
                    value = {**updated['feishu'], **value}
                value['revision'] = self._feishu_revision() + 1
                value['write_enabled'] = False
            updated[key] = value
        updated['feishu']['write_enabled'] = False
        with self._lock, self.repo.transaction():
            self._wa_enabled = False
            self._wa_cancel()
        self._pause()
        with self._lock:
            self._reset_authorization_start()
            self._monitor_generation += 1
            self._next_probe = 0.0
            self._probe_failures = 0
            self._message_sync.update(status='waiting_connection' if self.mode == 'live' else 'demo',
                                      checked_at=None, error_code=None)
            self.settings = updated
            self._control.put('meta', 'settings', updated)
            self.checks = {name: blank_check() for name in CHECK_NAMES}
            with self.repo.transaction():
                for case in self.repo.list('case'):
                    missing = missing_fields_for_case(case['facts'], updated, case)
                    if missing != case['missing']:
                        case.update(missing=missing, revision=case['revision'] + 1, updated_at=now_ms())
                        if case['state'] == 'waiting_quote' and missing:
                            case['state'] = 'clarifying'
                        self.repo.put('case', case['case_id'], case, history=True)
                self._invalidate()
        # Factory is local; credentials/runtime discovery belongs to explicit checks.
        replacement = self._integration_factory(self.mode, self.data_root / self.mode, copy.deepcopy(updated))
        configure_router = getattr(replacement, 'configure_model_router', None)
        if configure_router is not None:
            configure_router(self._model_router)
        self._configure_feishu_integration(replacement)
        with self._lock:
            self.integrations = replacement
            if self._fixture_initializer is not None:
                self._fixture_initializer(self)
            self._monitor_wake.set()
        self._changed()
        return self.get_snapshot()

    def _load_scope(self):
        saved = self.repo.get('reception_scope', 'international')
        if not isinstance(saved, dict):
            return
        mode = saved.get('mode')
        ids = saved.get('conversation_ids')
        # Legacy incoming covered every conversation, with the start boundary
        # applied separately. Keep that scope while requiring fresh authorization.
        if mode == 'incoming' and isinstance(ids, list) and all(isinstance(cid, str) for cid in ids):
            self._scope = {'mode': 'all', 'conversation_ids': [], 'max_auto_replies': None}
            self.repo.put('reception_scope', 'international', copy.deepcopy(self._scope))
            return
        if mode not in ('all', 'selected') or not isinstance(ids, list) or any(not isinstance(cid, str) for cid in ids):
            self._scope = {'mode': 'selected', 'conversation_ids': [], 'max_auto_replies': None}
        else:
            self._scope = {'mode': mode, 'conversation_ids': list(dict.fromkeys(ids)), 'max_auto_replies': None}

    def set_scope(self, mode, conversation_ids, max_auto_replies=None):
        if (mode not in ('selected', 'all') or not isinstance(conversation_ids, list)
                or any(not isinstance(v, str) for v in conversation_ids)
                or (max_auto_replies is not None and (type(max_auto_replies) is not int or max_auto_replies < 1))):
            raise ServiceError('invalid_scope', '接待范围无效')
        with self._lock:
            if not self.account_verified:
                raise ServiceError('identity_blocked', '请先连接当前商家')
            if any(not self.repo.get('conversation', cid) for cid in conversation_ids):
                raise ServiceError('invalid_scope', '范围包含未知客户')
        self._pause()
        with self._lock:
            self._scope = {'mode': mode, 'conversation_ids': list(dict.fromkeys(conversation_ids)),
                           'max_auto_replies': None}
            self.repo.put('reception_scope', 'international', copy.deepcopy(self._scope))
            self._reset_authorization_start()
        return self.get_snapshot()

    def _reset_authorization_start(self):
        self._authorized_scope = None
        self._started_at_ms = None
        self._source_baseline = {}

    def _pause(self, closing=False):
        with self._lock:
            self._running = False
            self._auto_send = False
            self._frozen_scope = None
            self._epoch += 1
            self._cancel_generation_tasks(reason='shutdown' if closing else 'paused')
            if closing:
                self._wa_enabled = False
                self._wa_cancel()
                self._closing = True
                self._stop.set()
                self._monitor_wake.set()
            facade = self.integrations
            if not self._closed:
                with self.repo.transaction():
                    self._invalidate_confirmations_all()
        # revoke() is contractually a local, immediate action, outside the state lock.
        facade.revoke()
        self._changed()

    def _invalidate_confirmations_all(self):
        for confirmation in self.repo.list('confirmation'):
            confirmation['invalidated'] = True
            self.repo.put('confirmation', confirmation['confirmation_id'], confirmation)

    def set_reception(self, enabled, auto_send=False, manual_only=False):
        if any(type(value) is not bool for value in (enabled, auto_send, manual_only)):
            raise ServiceError('invalid_params', '接待开关无效')
        if manual_only and auto_send:
            raise ServiceError('invalid_params', '人工接待不能同时开启自动发送')
        if not enabled:
            # A pause revokes work without selecting a different reception mode.
            # Renderers may send only enabled=False, whose other flags default False.
            self._pause()
            return self.get_snapshot()
        self._executor_health.require()
        with self._lock:
            self._executor_health.require()
            if self.mode == 'live':
                if not self.account_verified or self.checks['messages']['status'] != 'passed':
                    raise ServiceError('connection_required', '请先完成只读连接检查')
            scope = None if manual_only else self._authorized_scope
            if not manual_only and scope is None:
                ids = (self._scope['conversation_ids'] if self._scope['mode'] == 'selected' else
                       [c['conversation_id'] for c in self.repo.list('conversation')] if self._scope['mode'] == 'all' else [])
                scope = {**copy.deepcopy(self._scope), 'conversation_ids': list(ids), 'seller_id': self.seller_id,
                         'started_at_ms': now_ms()}
            if not manual_only and scope['mode'] == 'selected' and not scope['conversation_ids']:
                raise ServiceError('scope_required', '请选择需要自动接待的客户')
            # Starting or changing a mode revokes queued work and native confirmations.
            # revoke() is local and immediate; no network is held under this lock.
            if (not self._running or self._manual_only != manual_only or self._auto_send != auto_send):
                self._epoch += 1
                with self.repo.transaction():
                    self._invalidate_confirmations_all()
                self.integrations.revoke()
            self._manual_only = manual_only
            if manual_only:
                self._reset_authorization_start()
            elif self._authorized_scope is None:
                self._started_at_ms = scope['started_at_ms']
                self._source_baseline = {c['conversation_id']: self._latest_buyer(c['conversation_id'])
                                         for c in self.repo.list('conversation')}
                self._authorized_scope = copy.deepcopy(scope)
            self._frozen_scope = copy.deepcopy(scope)
            self._running = True
            self._auto_send = auto_send
            self._next_poll = time.monotonic()
        self._changed()
        return self.get_snapshot()

    def takeover(self, conversation_id, enabled):
        if type(enabled) is not bool:
            raise ServiceError('invalid_params', '接管开关无效')
        with self._lock, self.repo.transaction():
            conversation = self._chat_owner(conversation_id)
            case = self._require('case', conversation['case_id'])
            legacy_owned = case['state'] == 'human_owned'
            if conversation.get('human_owned') != enabled or legacy_owned:
                conversation.update(human_owned=enabled, manual_chat=True, revision=conversation['revision'] + 1)
                self.repo.put('conversation', conversation_id, conversation, history=True)
                if legacy_owned:
                    latest = self._latest_buyer(conversation_id)
                    case.update(state='closed' if latest and self._stop_requested(latest['text']) else
                                'waiting_quote' if not case['missing'] else 'clarifying',
                                revision=case['revision'] + 1, updated_at=now_ms())
                    self.repo.put('case', case['case_id'], case, history=True)
                self._invalidate(conversation_id)
            if enabled:
                self._begin_manual_edit_locked(conversation)
        self._changed(conversation_id)
        return self.get_snapshot()

    def _validate_send(self, draft_id, expected_revision, *, manual=False):
        if not self.account_verified:
            raise ServiceError('identity_required', '请先核验当前卖家身份')
        if not manual and self._manual_only:
            raise ServiceError('cancelled', '人工接待已关闭自动发送权限')
        draft = self._require('draft', draft_id)
        if self.mode == 'live' and not manual:
            self._assert_reception_model(draft.get('model_snapshot'))
        case = self._require('case', draft['case_id'])
        conversation = self._require('conversation', draft['conversation_id'])
        if draft.get('batch_id'):
            return self._validate_reply_batch_child(draft, expected_revision, manual=manual)
        self._assert_conversation_resolved(draft['conversation_id'])
        if draft['revision'] != expected_revision:
            raise ServiceError('revision_conflict', '发送正文版本已变化')
        if draft['status'] != 'pending':
            raise ServiceError('draft_not_sendable', '草稿已失效或有未决发送')
        source = self._intent_source(draft, case)
        if case['state'] == 'closed' or self._stop_requested(source['text']):
            raise ServiceError('contact_stopped', '当前客户已停止联系')
        if not manual and (conversation['human_owned'] or case['state'] == 'human_owned'):
            raise ServiceError('human_owned', '当前客户由人工接管，不能自动发送')
        if (draft['source_case_revision'] != case['revision'] or draft['source_conversation_revision'] != conversation['revision']
                or not self._related_cases_current(draft)):
            raise ServiceError('stale_draft', '需求、相关供货条件或新消息已变化，请重新生成或编辑')
        context = with_direct_photo_reads(self.get_context(case['case_id']), draft.get('photo_read_images', []))
        if (draft.get('supplement_fingerprint') != supplement_fingerprint(context)
                and (draft.get('supplement_fingerprint') is not None or context['supplement'].get('body'))):
            raise ServiceError('stale_supplement', '业务补充已变化，请重新生成或复核正文')
        expected_photo = draft.get('photo_fingerprint')
        if ((expected_photo is not None and expected_photo != photo_fingerprint(context))
                or expected_photo is None and context['photo_context']['events']):
            raise ServiceError('stale_photo', '图片依据已变化，请重新生成或复核正文')
        policy_versions = {**{p['policy_id']: p['revision'] for p in context['knowledge']}, **context.get('policy_versions', {})}
        if draft.get('policy_versions', {}) != policy_versions:
            raise ServiceError('stale_policy', '适用资料或有效期已变化')
        validation = validate_reply(draft['body'], draft['validation']['sources'], context,
                                    max_chars=1200 + 2 * max(0, len(draft.get('reply_messages', [])) - 1))
        if not validation['ready']:
            raise ServiceError('validation_blocked', '正文存在未核实依据，请先修订', stage='validation')
        supplement = draft.get('continuation', {}).get('phase') == 'supplement'
        if supplement and (draft.get('continuation') != self._supplement_continuation(case)
                           or not draft.get('supplement_issue_ids')
                           or not set(draft['supplement_issue_ids']) <= set(draft['continuation']['issue_ids'])):
            raise ServiceError('stale_supplement', '业务补充续答来源或关联问题已变化')
        operations = [o for o in self.repo.list('operation') if o.get('purpose') != 'chat'
                      and not (supplement and o.get('status') == 'accepted'
                               and (o.get('intent', {}).get('continuity_phase') != 'supplement'
                                    or not set((self.repo.get('continuity_plan', o.get('plan_id')) or {}).get('supplement_issue_ids', []))
                                    & set(draft.get('supplement_issue_ids', []))))
                      and not (o.get('purpose') == 'continuity' and o.get('intent', {}).get('continuity_phase') == 'ack')
                      and o['case_id'] == case['case_id'] and o['source_message_id'] == draft['source_message_id']
                      and o['status'] in ('sending', 'accepted', 'unknown')]
        if operations:
            raise ServiceError('duplicate_send', '当前客户消息已有成功或未决发送，禁止重复提交')
        return draft, case, conversation

    def prepare_send(self, draft_id, expected_revision):
        with self._lock, self.repo.transaction():
            if not self._running:
                raise ServiceError('reception_paused', '接待已暂停，请先开启接待')
            draft, case, conversation = self._validate_send(draft_id, expected_revision, manual=True)
            confirmation = {'confirmation_id': new_id('confirm'), 'expires_at': now_ms() + 120_000,
                            'draft_id': draft_id, 'revision': draft['revision'], 'conversation_id': draft['conversation_id'],
                            'customer_name': conversation['customer_name'], 'seller_suffix': self.seller_id[-6:],
                            'body': draft['body'], 'source_message_id': draft['source_message_id'],
                            'case_revision': case['revision'], 'conversation_revision': conversation['revision'],
                            'epoch': self._epoch, 'invalidated': False, 'used': False}
            self.repo.put('confirmation', confirmation['confirmation_id'], confirmation)
            return {k: confirmation[k] for k in ('confirmation_id', 'expires_at', 'draft_id', 'revision', 'conversation_id',
                                                 'customer_name', 'seller_suffix', 'body', 'source_message_id')}

    def _receipt_owner(self, operation, *, registration=False):
        if not registration and operation.get('purpose') == 'chat':
            conversation = self._chat_owner(operation.get('conversation_id'))
            if operation.get('seller_id') != self.seller_id or operation.get('buyer_id') != conversation['buyer_id']:
                raise ServiceError('operation_owner_mismatch', '聊天操作不属于当前已核验卖家及客户')
            return
        case = self.repo.get('case', operation.get('case_id'))
        cid = operation.get('case_snapshot', {}).get('conversation_id') if registration else operation.get('conversation_id')
        conversation = self.repo.get('conversation', cid)
        owner = operation.get('case_snapshot', {}) if registration else operation
        if (not self.account_verified or not case or not conversation
                or case['seller_id'] != self.seller_id or case['conversation_id'] != cid
                or case['buyer_id'] != conversation['buyer_id']
                or owner.get('seller_id', None if registration else self.seller_id) != self.seller_id
                or owner.get('buyer_id', None if registration else case['buyer_id']) != case['buyer_id']):
            raise ServiceError('operation_owner_mismatch', '操作不属于当前已核验卖家及客户')
        if registration:
            if owner.get('case_id') != case['case_id'] or owner.get('revision') != operation.get('case_revision'):
                raise ServiceError('operation_owner_mismatch', '登记操作与事项版本不匹配')
        else:
            source = self.repo.get('message', encoded([cid, operation.get('source_message_id')]))
            if (not source or source['role'] != 'buyer' or source['seller_id'] != self.seller_id
                    or source['buyer_id'] != case['buyer_id'] or source['conversation_id'] != cid):
                raise ServiceError('operation_owner_mismatch', '发送操作缺少本客户来源依据')

    @staticmethod
    def _registration_status(result):
        status = result.get('status')
        if status in ('succeeded', 'accepted', 'created', 'updated', 'synced', 'confirmed'):
            return 'succeeded' if isinstance(result.get('record_id'), str) and result['record_id'].strip() else 'unknown'
        return 'failed' if status in ('failed', 'rejected', 'blocked', 'disabled') else 'unknown'

    def _store_registration(self, item, result):
        current = self.repo.get('registration', item['case_id'], {})
        if item['case_revision'] >= current.get('case_revision', 0):
            self.repo.put('registration', item['case_id'], {'record_id': result['record_id'], 'number': result.get('number'),
                          'operation_id': item['operation_id'], 'case_revision': item['case_revision']})

    def reconcile_operation(self, operation_id):
        with self._lock:
            operation = self.repo.get('operation', operation_id)
            registration = operation is None
            if registration:
                operation = self._require('outbox', operation_id)
            self._receipt_owner(operation, registration=registration)
            # In-flight writes own their completion; terminal receipts cannot be downgraded.
            unresolved = ('unknown', 'failed') if registration else ('unknown',)
            if operation['status'] not in unresolved:
                return ({k: operation.get(k) for k in ('operation_id', 'kind', 'case_id', 'case_revision', 'status', 'message')}
                        if registration else self._send_operation_dto(operation))
            facade = self.integrations
        try:
            result = facade.reconcile_operation(operation_id)
            if not isinstance(result, dict):
                result = {}
        except Exception:
            result = {}  # A missing or unavailable receipt never authorizes a replay.
        with self._lock, self.repo.transaction():
            current = self._require('outbox' if registration else 'operation', operation_id)
            self._receipt_owner(current, registration=registration)
            if current != operation:
                return ({k: current.get(k) for k in ('operation_id', 'kind', 'case_id', 'case_revision', 'status', 'message')}
                        if registration else self._send_operation_dto(current))
            if registration:
                status = self._registration_status(result)
                # A failed lookup/configuration check is not evidence that an
                # unresolved write was rejected. Only an explicit rejection is retryable.
                if status == 'failed' and (result.get('status') != 'rejected' or result.get('submission_started') is not False):
                    status = 'unknown'
                current.update(status=status, updated_at=now_ms(), message={
                    'succeeded': '已核对登记回执', 'failed': '登记已明确拒绝，可复核后重试',
                    'unknown': '登记结果未知；保留原操作编号，禁止重建远端记录'}[status])
                self.repo.put('outbox', operation_id, current, history=True)
                if status == 'succeeded':
                    self._store_registration(current, result)
                receipt = {k: current.get(k) for k in ('operation_id', 'kind', 'case_id', 'case_revision', 'status', 'message')}
            else:
                batch = (self._require('reply_batch', current['batch_id'])
                         if current.get('batch_id') and current.get('purpose') != 'chat' else None)
                batch_basis_current = bool(batch and self._reply_batch_receipt_basis_current(
                    batch, current, result, self._reply_batch_operation_snapshot(batch, current), check_sync=False))
                status = result.get('status')
                message_id = result.get('message_id')
                if (status not in ('accepted', 'rejected') or status == 'accepted' and
                        (not isinstance(message_id, str) or not message_id.strip() or result.get('submission_started') is not True)
                        or status == 'rejected' and result.get('submission_started') is not False):
                    status = 'unknown'
                submitted = (False if status == 'rejected' else
                             current.get('submission_started') is True or result.get('submission_started') is True)
                current.update(status=status, updated_at=now_ms(), message={
                    'accepted': '平台已受理发送', 'rejected': '发送已明确拒绝',
                    'unknown': '发送结果未知，禁止自动重试'}[status], submission_started=submitted)
                if status == 'accepted':
                    current['message_id'] = message_id
                self.repo.put('operation', operation_id, current, history=True)
                if current.get('purpose') == 'chat':
                    self._sync_chat_draft_status(current)
                    if status == 'accepted':
                        self._chat_history(current, result)
                    self._manual_batch_receipt(current, result)
                elif current.get('purpose') == 'continuity':
                    self._continuity_receipt(current, result)
                draft = self.repo.get('draft', current.get('draft_id')) if current.get('purpose') not in ('chat', 'continuity') and not current.get('batch_id') else None
                matching_draft = (draft and draft['revision'] == current.get('draft_revision')
                                  and draft['case_id'] == current['case_id']
                                  and draft['conversation_id'] == current['conversation_id']
                                  and draft['source_message_id'] == current['source_message_id']
                                  and draft['source_case_revision'] == current.get('case_revision'))
                if matching_draft and draft['status'] in ('unknown', 'sending'):
                    draft.update(status=status, updated_at=now_ms())
                    self.repo.put('draft', draft['draft_id'], draft, history=True)
                # Recover only the original body's dated platform message. Do not
                # replay procurement transitions, question counts or registrations.
                stamp = result.get('timestamp_ms')
                if status == 'accepted' and matching_draft and type(stamp) is int and stamp > 0:
                    cid = current['conversation_id']
                    key = encoded([cid, message_id])
                    if self.repo.get('message', key) is None:
                        case = self._require('case', current['case_id'])
                        message = {'message_id': message_id, 'seller_id': case['seller_id'], 'buyer_id': case['buyer_id'],
                                   'conversation_id': cid, 'role': 'seller', 'text': draft['body'],
                                   'timestamp_ms': stamp, 'attachments': [], 'own': True}
                        self.repo.put('message', key, message, history=True)
                        conversation = self._require('conversation', cid)
                        latest = self._messages(cid)[-1]
                        conversation.update(last_message=latest['text'], last_timestamp=latest['timestamp_ms'],
                                            revision=conversation['revision'] + 1)
                        self.repo.put('conversation', cid, conversation)
                if current.get('batch_id') and current.get('purpose') != 'chat':
                    child = self.repo.get('draft', current.get('draft_id'))
                    if child and child.get('batch_id') == current['batch_id']:
                        child.update(status=status, updated_at=now_ms())
                        self.repo.put('draft', child['draft_id'], child, history=True)
                    if (status == 'accepted' and type(stamp) is int and stamp > 0
                            and self.repo.get('message', encoded([current['conversation_id'], message_id])) is None):
                        conversation = self._require('conversation', current['conversation_id'])
                        self._ingest(conversation, [{'message_id': message_id, 'seller_id': self.seller_id,
                            'buyer_id': current['buyer_id'], 'conversation_id': current['conversation_id'],
                            'role': 'seller', 'text': current['intent']['reply'], 'timestamp_ms': stamp,
                            'attachments': [], 'own': True}])
                    self._reply_batch_receipt(current, result, basis_current=batch_basis_current)
                if status in ('accepted', 'rejected'):
                    kinds = (('send_chat',) if current.get('purpose') == 'chat' else
                             ('continuity_send', 'automatic_send') if current.get('purpose') == 'continuity'
                             else ('automatic_send', 'confirm_send'))
                    for job in self.repo.list('job', conversation_id=current['conversation_id']):
                        job_result = job.get('result') if isinstance(job.get('result'), dict) else {}
                        linked = (current.get('job_id') == job['job_id'] or job.get('operation_id') == operation_id
                                  or job_result.get('operation_id') == operation_id)
                        if (job['kind'] in kinds and job['status'] == 'unknown' and linked
                                and job.get('case_id') == current.get('case_id')
                                and (not current.get('job_id') or current['job_id'] == job['job_id'])
                                and job.get('operation_id', operation_id) == operation_id
                                and job_result.get('operation_id', operation_id) == operation_id):
                            self._job_update(job['job_id'], status='succeeded' if status == 'accepted' else 'failed',
                                             stage='complete', completed_at=job.get('completed_at') or now_ms(),
                                             message=current['message'], result={**job_result, 'operation_id': operation_id,
                                                 'status': status, 'message': current['message'],
                                                 'message_id': current.get('message_id'), 'submission_started': submitted})
                receipt = self._send_operation_dto(current)
        self._changed(operation.get('conversation_id'), operation['case_id'])
        return receipt

    def inject_demo(self, conversation_id, text):
        if self.mode != 'demo':
            raise ServiceError('demo_only', '真实工作区不能注入演示消息')
        if not isinstance(text, str) or not text.strip() or len(text) > 50_000:
            raise ServiceError('invalid_params', '示例消息不能为空或超出长度限制')
        with self._lock:
            conversation = self._require('conversation', conversation_id)
            message = {'message_id': new_id('demo_msg'), 'seller_id': self.seller_id,
                       'buyer_id': conversation['buyer_id'], 'conversation_id': conversation_id, 'role': 'buyer',
                       'text': text, 'timestamp_ms': now_ms(), 'attachments': [], 'own': False}
            self._ingest(conversation, [message])
            messages = getattr(self.integrations, 'messages', None)
            if isinstance(messages, list):
                messages.append(copy.deepcopy(message))
        self._changed(conversation_id)
        return self.get_snapshot()

    def _enqueue(self, kind, params, *, automatic=False, read_reason=None):
        if kind == 'send_chat':
            return self._enqueue_chat(params)  # Existing exact requests remain readable.
        self._executor_health.require()
        if read_reason is not None and (kind != 'sync_messages' or read_reason not in ('activity', 'history', 'audit', 'selection', 'backfill')):
            raise ServiceError('invalid_params', '读取任务来源无效')
        if kind == 'continuity_send' and automatic:
            return self._queue_continuity_plan(params)
        if kind == 'load_message_image':
            return self._enqueue_photo(params)
        allowed = {'sync_messages': {'conversation_id'}, 'fill_history': {'conversation_id'},
                   'generate_draft': {'case_id', 'expected_case_revision', 'supplement_followup'}, 'check_connection': set(), 'check_ai': set(),
                   'refresh_company_profile': set(),
                   'preview_send': {'draft_id', 'expected_revision'}, 'confirm_send': {'confirmation_id'},
                   'run_outbox': set(), 'check_feishu': set(), 'reconcile_operation': {'operation_id'}}
        if automatic and kind == 'automatic_send':
            allowed[kind] = {'draft_id', 'expected_revision'}
        required = {'fill_history': {'conversation_id'}, 'generate_draft': {'case_id', 'expected_case_revision'},
                    'preview_send': {'draft_id', 'expected_revision'}, 'confirm_send': {'confirmation_id'},
                    'reconcile_operation': {'operation_id'}}
        if set(params) - allowed[kind] or required.get(kind, set()) - set(params):
            raise ServiceError('invalid_params', '任务参数不完整或含未知字段')
        if 'supplement_followup' in params and (not automatic or params['supplement_followup'] is not True):
            raise ServiceError('invalid_params', '业务补充续答只能由自动调度发起')
        with self._lock, self.repo.transaction():
            if self._manual_only and kind in ('generate_draft', 'automatic_send', 'run_outbox'):
                raise ServiceError('manual_only', '人工接待仅同步消息及逐条确认发送，请切换模式后使用自动处理')
            cid = params.get('conversation_id')
            case_id = params.get('case_id')
            if kind == 'automatic_send':
                target_draft = self._require('draft', params['draft_id'])
                cid, case_id = target_draft['conversation_id'], target_draft['case_id']
            if kind == 'reconcile_operation':
                operation = self.repo.get('operation', params['operation_id'])
                registration = operation is None
                if registration:
                    operation = self._require('outbox', params['operation_id'])
                self._receipt_owner(operation, registration=registration)
                cid = operation.get('conversation_id')
                case_id = operation.get('case_id') if operation.get('purpose') != 'chat' else None
                existing = next((j for j in self.repo.list('job')
                                 if j.get('kind') == 'reconcile_operation'
                                 and j.get('operation_id') == params['operation_id']
                                 and j.get('status') in ('queued', 'running')), None)
                if existing:
                    return {'job_id': existing['job_id']}
            if case_id:
                case = self._require('case', case_id)
                cid = case['conversation_id']
            if cid:
                self._require('conversation', cid)
            if cid and (kind == 'fill_history' or self.mode == 'live' and kind == 'sync_messages'):
                existing = next((j for j in self.repo.list('job', conversation_id=cid)
                                 if j['kind'] == kind and j['status'] in ('queued', 'running')), None)
                if existing:
                    return {'job_id': existing['job_id']}
            source = None
            if kind == 'generate_draft':
                self._assert_conversation_resolved(cid)
                case = self._require('case', case_id)
                if case['revision'] != params['expected_case_revision']:
                    raise ServiceError('revision_conflict', '需求版本已变化')
                source = self._latest_buyer(cid)
                existing = next((j for j in self.repo.list('job', conversation_id=cid)
                                 if j['kind'] == 'generate_draft' and j.get('case_id') == case_id
                                 and j.get('epoch') == self._epoch and j.get('source_case_revision') == case['revision']
                                 and j.get('source_message_id') == (source['message_id'] if source else None)
                                 and (j['status'] in ('queued', 'running') or j['status'] == 'deferred'
                                      and j.get('retry_status') in ('waiting', 'replan'))), None)
                if existing:
                    return {'job_id': existing['job_id']}
                if automatic:
                    supplement = self._supplement_continuation(case) if params.get('supplement_followup') else None
                    work = self.repo.get('supplement_followup', cid) or {}
                    available_source = self._supplement_source(case) if supplement else self._automatic_source(cid)
                    if (not self._running or not self._in_scope(cid) or not available_source
                            or params.get('supplement_followup') and (not supplement or work.get('status') != 'pending'
                                or work.get('due_at', 0) > now_ms())
                            or not params.get('supplement_followup') and self._generation_attempted(cid, case, source)):
                        raise ServiceError('cancelled', '自动生成来源、范围或额度已失效')
            if kind == 'confirm_send':
                confirmation = self.repo.get('confirmation', params['confirmation_id'])
                draft = self.repo.get('draft', confirmation.get('draft_id')) if confirmation else None
                if (confirmation and draft and confirmation.get('conversation_id') == draft.get('conversation_id')
                        and self.repo.get('conversation', confirmation['conversation_id'])):
                    cid, case_id = confirmation['conversation_id'], draft.get('case_id')
            job = {'job_id': new_id('job'), 'kind': kind, 'conversation_id': cid, 'case_id': case_id,
                   'status': 'queued', 'stage': 'queued', 'message': '等待后台执行', 'created_at': now_ms(),
                   'started_at': None, 'completed_at': None, 'automatic': automatic}
            if kind == 'reconcile_operation':
                job['operation_id'] = params['operation_id']
            if kind == 'automatic_send':
                job['draft_id'] = params['draft_id']
                job['source_message_id'] = target_draft['source_message_id']
                if target_draft.get('batch_id'):
                    job.update(batch_id=target_draft['batch_id'], part_id=target_draft['part_id'])
            if kind in ('generate_draft', 'check_ai'):
                job['external_started'] = False
            if kind == 'generate_draft':
                reserved = 1 if automatic else 0
                previous = [j for j in self.repo.list('job', conversation_id=cid) if j['kind'] == 'generate_draft'
                            and j.get('source_message_id') == (source['message_id'] if source else None)]
                job.update(source_case_revision=params['expected_case_revision'], epoch=self._epoch,
                           supplement_followup=params.get('supplement_followup') is True,
                           source_message_id=source['message_id'] if source else None,
                           source_text_sha256=text_hash(source['text']) if source else None,
                           retry_attempt=max((j.get('retry_attempt', 0) for j in previous), default=0),
                           ai_reserved_remaining=reserved)
            if self.mode == 'live' and kind == 'sync_messages':
                job.update(background_sync=read_reason is not None, sync_reason=read_reason)
                if cid:
                    self._bind_message_sync(job)
            self.repo.put('job', job['job_id'], job, history=not job.get('background_sync'))
            if kind == 'generate_draft' and params.get('supplement_followup'):
                work.update(status='generating', job_id=job['job_id'])
                self.repo.put('supplement_followup', cid, work)
            self._queue.put((job['job_id'], kind, copy.deepcopy(params), self._epoch, automatic))
        if not job.get('background_sync'):
            self._emit('job.updated', job)
        return {'job_id': job['job_id']}

    def _job_update(self, job_id, **changes):
        with self._lock, self.repo.transaction():
            job = self._require('job', job_id)
            if 'diagnostics' in changes:
                changes['diagnostics'] = {**safe_diagnostics(job.get('diagnostics')),
                                          **safe_diagnostics(changes['diagnostics'])}
            job.update(changes)
            if 'sync_target' in job and changes.get('status') not in (None, 'queued', 'running'):
                # Persist retry state before publishing a terminal job, so the
                # monitor cannot slip a retry between failure and its backoff.
                self._message_sync_job_finished(job_id, status=changes['status'])
            self.repo.put('job', job_id, job, history=not job.get('background_sync') or job['status'] in ('failed', 'unknown'))
        if not job.get('background_sync'):
            self._emit('job.updated', job)
        return job

    def _cancel_queued(self, item):
        job_id, kind, params, _, _ = item
        if kind == 'load_message_image' and self._require('job', job_id).get('status') not in ('queued', 'running'):
            return  # A generation prephase may already have completed this item.
        if kind == 'product_lookup':
            from .product_catalog import key_for
            with self.repo.transaction():
                key = key_for(self.seller_id, 'alibaba', params['expected_product_id'])
                row = self.repo.get('product_catalog', key)
                if row and row.get('request_id') == job_id and row['lookup']['status'] == 'querying':
                    row['lookup'] = {'status': 'unknown', 'reason': 'cancelled', 'queried_at': None}
                    self.repo.put('product_catalog', key, row, history=True)
        if kind == 'send_chat':
            with self._lock, self.repo.transaction():
                operation = self.repo.get('operation', params.get('operation_id'))
                if operation and operation.get('purpose') == 'chat' and operation['status'] == 'prepared':
                    operation.update(status='rejected', submission_started=False, updated_at=now_ms(),
                                     message='退出前尚未执行，本条消息未提交，正文已保留')
                    self.repo.put('operation', operation['operation_id'], operation, history=True)
                    self._sync_chat_draft_status(operation)
                    self._chat_facades.pop(operation['operation_id'], None)
        self._job_update(job_id, status='cancelled', completed_at=now_ms(), stage='cancelled', message='服务已关闭；排队任务未执行')

    def _preserve_dispatch_failure(self, item):
        job_id, kind, params = item[:3]
        if job_id in self._generation_active:
            return  # The cancelled AI thread still owns its eventual completion.
        with self._lock, self.repo.transaction():
            job = self.repo.get('job', job_id)
            if not job or job.get('status') not in ('queued', 'running'):
                return
            sending = kind in ('send_chat', 'automatic_send', 'continuity_send', 'confirm_send')
            for operation in self.repo.list('operation') if sending else ():
                if ((operation.get('job_id') == job_id or operation.get('operation_id') == params.get('operation_id'))
                        and operation.get('status') in ('prepared', 'sending')):
                    operation.update(status='unknown', submission_started=True, updated_at=now_ms(),
                                     message='执行状态异常，保留原发送核查')
                    self.repo.put('operation', operation['operation_id'], operation, history=True)
                    if operation.get('purpose') == 'chat':
                        self._sync_chat_draft_status(operation)
            self._job_update(job_id, status='unknown' if sending else 'failed', stage='executor',
                             completed_at=now_ms(), message='执行状态异常，未自动重新提交' if sending else '后台执行异常，任务已停止')

    def _run_queued(self, item, *, facade=None):
        job_id, kind, params, epoch, automatic = item
        previous_job = getattr(self._execution_local, 'job_id', None)
        self._execution_local.job_id = job_id
        measured_at = None
        try:
            if self._stop.is_set():
                self._cancel_queued(item)
                return
            with self._lock:
                current_job = self._require('job', job_id)
                # Preview preparation and dispatch share this atomic claim. A
                # completed inline preview must never execute a second time.
                if current_job.get('status') != 'queued':
                    return
                if kind in AI_JOBS and self._generation_cancel_event(job_id).is_set():
                    self._job_update(job_id, status='cancelled', stage='cancelled', completed_at=now_ms(),
                                     message='来源或任务已更新，旧生成未执行')
                    return
                stamp = now_ms()
                measured_at = time.monotonic()
                self._job_update(job_id, status='running', stage=kind, started_at=stamp, message='后台处理中',
                                 diagnostics={'queue_wait_ms': max(0, stamp - current_job['created_at'])})
            if (automatic and (epoch != self._epoch or not self._running)
                    or kind in ('generate_draft', 'run_outbox')
                    and (epoch != self._epoch or self._manual_only)):
                raise ServiceError('cancelled', '接待权限已撤销')
            result = getattr(self, '_task_' + kind)(params, job_id, epoch,
                                                   self.integrations if facade is None else facade)
            status = result.pop('_job_status', 'succeeded') if isinstance(result, dict) else 'succeeded'
            message = result.get('message', '任务完成') if isinstance(result, dict) else '任务完成'
            failure = result.get('error') if isinstance(result, dict) and status in ('failed', 'deferred', 'stale') else None
            if kind == 'generate_draft' and status == 'stale' and self._can_replan_generation(job_id):
                self._defer_generation_retry(job_id, 'source_changed', replan=True)
                return
            self._job_update(job_id, status=status, completed_at=now_ms(),
                             stage=result.get('stage', 'complete') if isinstance(failure, dict) else 'complete', message=message,
                             **({'result': result} if result is not None else {}),
                             **({'error': failure, 'diagnostics': result.get('diagnostics', {})} if isinstance(failure, dict) else {}))
        except ServiceError as error:
            if error.code in ('model_cleanup_timeout', 'model_cleanup_failed'):
                self._executor_health.fail('failed', error.code)
            if self.mode == 'live' and error.code != 'cancelled' and (error.stage == 'identity' or error.code == 'seller_mismatch'):
                self._identity_failed(epoch)
            if kind == 'product_lookup':
                self._finish_product_claim(job_id, error.code)
            if kind == 'generate_draft' and (error.code.startswith('schema_') or error.code in ('invalid_ai_output', 'unverified_reply', 'answer_non_json', 'answer_missing', 'answer_schema', 'native_error', 'native_failed', 'timeout', 'native_timeout', 'task_failed', 'connection_error', 'channel_unavailable')):
                self._defer_generation_retry(job_id, error.code)
                return
            if kind == 'generate_draft' and error.code in ('source_changed', 'revision_conflict') and self._can_replan_generation(job_id):
                self._defer_generation_retry(job_id, error.code, replan=True)
                return
            if kind == 'automatic_send' and error.code in ('stale_draft', 'stale_policy', 'stale_photo', 'source_mismatch', 'revision_conflict'):
                with self._lock, self.repo.transaction():
                    pending = self.repo.get('draft', params.get('draft_id'))
                    if (pending and pending['status'] == 'pending' and pending.get('origin') != 'manual'
                            and pending['revision'] == params.get('expected_revision') and not self._draft_basis_current(pending)):
                        pending.update(status='stale', updated_at=now_ms())
                        self.repo.put('draft', pending['draft_id'], pending, history=True)
                        self._job_update(job_id, status='stale', stage=error.stage, completed_at=now_ms(),
                                         message='发送前依据已变化，旧建议保留，等待最新消息规划',
                                         error={'code': error.code, 'message': str(error)}, diagnostics=error.diagnostics)
                        return
            if automatic and kind == 'automatic_send':
                with self._lock, self.repo.transaction():
                    pending = self.repo.get('draft', params.get('draft_id'))
                    if pending and pending.get('status') == 'pending':
                        pending.update(status='blocked', updated_at=now_ms())
                        self.repo.put('draft', pending['draft_id'], pending, history=True)
            if automatic and kind == 'continuity_send':
                with self._lock, self.repo.transaction():
                    plan = self.repo.get('continuity_plan', params.get('plan_id'))
                    if plan and plan.get('status') == 'pending':
                        plan.update(status='blocked', updated_at=now_ms())
                        self.repo.put('continuity_plan', plan['plan_id'], plan, history=True)
            self._record_check_error(kind, job_id, error)
            self._job_update(job_id, status='cancelled' if error.code == 'cancelled' else 'failed', stage=error.stage,
                             completed_at=now_ms(), message=str(error), error={'code': error.code, 'message': str(error)},
                             diagnostics=error.diagnostics)
        except Exception as unexpected:
            self._executor_health.log('task', job_id, unexpected)
            if kind in ('send_chat', 'automatic_send', 'continuity_send', 'confirm_send'):
                # An unexpected failure cannot establish whether the native
                # submission crossed its boundary. Keep the original intent.
                self._executor_health.fail('failed', 'send_state_unknown')
                with self._lock, self.repo.transaction():
                    for operation in self.repo.list('operation'):
                        if (operation.get('job_id') == job_id or operation.get('operation_id') == params.get('operation_id')) and operation.get('status') in ('prepared', 'sending'):
                            operation.update(status='unknown', submission_started=True, updated_at=now_ms(),
                                             message='发送结果未知，保留原操作核查')
                            self.repo.put('operation', operation['operation_id'], operation, history=True)
                            if operation.get('purpose') == 'chat':
                                self._sync_chat_draft_status(operation)
                    self._job_update(job_id, status='unknown', completed_at=now_ms(), stage='executor',
                                     message='发送结果未知，保留原操作核查')
                return
            if kind == 'product_lookup':
                self._finish_product_claim(job_id, 'task_failed')
            if kind == 'generate_draft':
                self._defer_generation_retry(job_id, 'task_failed')
                return
            error = ServiceError('task_failed', '后台任务失败，请查看安全检查记录', stage=kind)
            if automatic and kind in ('automatic_send', 'continuity_send'):
                with self._lock, self.repo.transaction():
                    entity = 'draft' if kind == 'automatic_send' else 'continuity_plan'
                    key = params.get('draft_id' if kind == 'automatic_send' else 'plan_id')
                    pending = self.repo.get(entity, key)
                    if pending and pending.get('status') == 'pending':
                        pending.update(status='blocked', updated_at=now_ms())
                        self.repo.put(entity, key, pending, history=True)
            self._record_check_error(kind, job_id, error)
            self._job_update(job_id, status='failed', completed_at=now_ms(), stage=kind, message=str(error),
                             error={'code': error.code, 'message': str(error)})
        finally:
            try:
                if kind == 'generate_draft' and params.get('supplement_followup'):
                    with self._lock, self.repo.transaction():
                        job = self.repo.get('job', job_id) or {}
                        work = self.repo.get('supplement_followup', job.get('conversation_id')) or {}
                        if work.get('job_id') == job_id and work.get('status') == 'generating' and job.get('status') in ('cancelled', 'stale', 'failed'):
                            work.update(status='blocked' if job['status'] == 'failed' else 'pending', due_at=now_ms() + 1500)
                            self.repo.put('supplement_followup', job['conversation_id'], work)
                if measured_at is not None:
                    self._job_update(job_id, diagnostics={'task_elapsed_ms': int((time.monotonic() - measured_at) * 1000)})
            finally:
                if kind in AI_JOBS:
                    with self._lock:
                        if job_id not in self._generation_active:
                            self._generation_events.pop(job_id, None)
                self._queue.complete(item)
                if job_id not in self._generation_active:
                    self._executor_health.finish_job(job_id)
                self._execution_local.job_id = previous_job
                self._changed()

    def _defer_generation_retry(self, job_id, code, *, replan=False):
        with self._lock, self.repo.transaction():
            job = self._require('job', job_id)
            previous = [j for j in self.repo.list('job', conversation_id=job['conversation_id'])
                        if j.get('kind') == 'generate_draft' and j.get('source_message_id') == job.get('source_message_id')]
            if self._generation_cancel_event(job_id).is_set():
                self._job_update(job_id, status='cancelled', stage='cancelled', completed_at=now_ms(),
                                 message='旧生成已取消，等待当前来源重新规划', ai_reserved_remaining=0,
                                 retry_at=None, retry_status='cancelled')
                return
            from .model_attempts import job_blocks_replay
            if self.mode == 'live' and job_blocks_replay(self.repo, job):
                self._job_update(job_id, status='failed', stage='retry', completed_at=now_ms(),
                                 message='模型请求已开始但未得到可用结果，不自动重放',
                                 retry_at=None, ai_reserved_remaining=0, retry_status='exhausted',
                                 last_error_code=code, error={'code': code, 'message': '模型请求未完成'})
                return
            attempt = max((j.get('retry_attempt', 0) for j in previous), default=0) + 1
            same_version = [j for j in previous if not job.get('source_text_sha256')
                            or j.get('source_text_sha256') == job['source_text_sha256']]
            failures = max((j.get('generation_failure_count',
                                 j.get('retry_attempt', 0) if j.get('retry_status') != 'replan' else 0)
                            for j in same_version), default=0) + (0 if replan else 1)
            if not replan and failures >= self.settings.get('generation_retry_limit', 3):
                self._job_update(job_id, status='failed', stage='retry', completed_at=now_ms(),
                                 message='本条消息生成已达到连续失败预算，请处理待办后明确重试',
                                 retry_at=None, retry_attempt=attempt, generation_failure_count=failures,
                                 ai_reserved_remaining=0, last_error_code=code, retry_status='exhausted',
                                 error={'code': 'generation_retry_limit', 'message': '本条消息生成连续失败'})
                return
            retry_at = now_ms() + (0 if replan else min(60000, 2000 * 2 ** min(max(0, failures - 1), 5)))
            job = self._job_update(job_id, status='deferred', stage='retry', completed_at=now_ms(),
                                  message='正在合并最新消息重新规划' if replan else '生成暂时不可用，等待自动重试',
                                  retry_at=retry_at, retry_attempt=attempt, generation_failure_count=failures,
                                  ai_reserved_remaining=0, last_error_code=code, retry_status='replan' if replan else 'waiting')
            if not replan:
                self._maybe_acknowledge(job, code)

    def _yield_to_ready_work(self, *, replies=False):
        # Only the coordinator dispatches business work. Image preparation in
        # the AI slot must never recursively execute unrelated native sends.
        if threading.current_thread() is not self._worker:
            return
        with self._lock:
            def eligible(item):
                if item[1] in AI_JOBS and self._generation_active:
                    return False
                return item[1] == 'send_chat' or replies and item[1] in (
                    'automatic_send', 'continuity_send', 'reconcile_operation', 'check_ai', 'preview_send', 'generate_draft')
            item = self._queue.take_ready(eligible, self._work_priority)
        if item is not None:
            self._dispatch_owned(item)

    def _work_loop(self):
        health = self._executor_health
        next_schedule = 0.0
        schedule_failures = 0
        stage, item = 'idle', None
        try:
            while not self._stop.is_set() and not health.failed.is_set():
                if time.monotonic() >= next_schedule:
                    stage = 'schedule'
                    health.update('running', stage)
                    try:
                        self._schedule()
                    except Exception as error:
                        health.log(stage, None, error)
                        schedule_failures += 1
                        if schedule_failures >= 3:
                            health.fail('failed', 'schedule_failed')
                            break
                        health.update('recovering', stage, code='schedule_failed')
                        health.revoke()
                        self._stop.wait((1, 5)[schedule_failures - 1])
                        continue
                    schedule_failures = 0
                    next_schedule = time.monotonic() + .15
                stage = 'queue'
                health.update('running', stage)
                item = self._take_ready_work()
                if item is None:
                    if self._generation_active:
                        active_job = next(iter(self._generation_active.copy()), None)
                        health.update('waiting_external', 'generation', active_job)
                    else:
                        health.update('idle', 'idle')
                    self._stop.wait(.03)
                    continue
                stage = 'dispatch'
                health.update('running', stage, item[0])
                self._business_enter()
                try:
                    self._dispatch_owned(item)
                    if item[1] == 'load_message_image' and not self._stop.is_set():
                        self._yield_to_ready_work(replies=True)
                    if self.mode == 'live' and item[1] == 'sync_messages' and not self._stop.is_set():
                        self._yield_to_ready_work(replies=True)
                finally:
                    self._business_leave()
                item = None
        except BaseException as error:
            # Dispatch or queue ownership may already have crossed a native or
            # task_done boundary. Leave durable state for the existing recovery.
            health.log(stage, item[0] if item else None, error)
            health.fail('failed', 'executor_fault')
            if item is not None:
                try:
                    self._preserve_dispatch_failure(item)
                except Exception as cleanup_error:
                    health.log('cleanup', item[0], cleanup_error)
        finally:
            if item is not None and item[0] not in self._generation_active:
                self._queue.complete(item)
                health.finish_job(item[0])
            # A failed consumer still owns cleanup of entries that remain in the
            # queue: these exact dequeues have never crossed a native boundary.
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                except BaseException as error:
                    health.log('cleanup', None, error)
                    health.fail('failed', 'cleanup_failed')
                    break
                try:
                    self._cancel_queued(pending)
                except BaseException as error:
                    health.log('cleanup', pending[0], error)
                    health.fail('failed', 'cleanup_failed')
                finally:
                    if pending[1] in AI_JOBS:
                        with self._lock:
                            self._generation_events.pop(pending[0], None)
                    self._queue.complete(pending)
                    health.finish_job(pending[0])
            if not health.failed.is_set():
                health.update('stopped', 'stopped')

    def _schedule(self):
        requests = []
        with self._lock:
            if self._closing:
                return
            self._schedule_procurement()
            self._schedule_whatsapp()
            self._schedule_manual_reply_batches()
            self._schedule_reply_batches()
            self._schedule_continuity()
            self._schedule_generation_retries()
            with self.repo.lock:
                active = set(self.repo.db.execute(
                    "SELECT json_extract(data,'$.kind'),json_extract(data,'$.case_id') FROM entities "
                    "WHERE kind='job' AND json_extract(data,'$.status') IN ('queued','running')"))
            reading_ready = self.account_verified and (self.mode == 'demo' or self.checks['messages']['status'] == 'passed')
            if self.mode == 'demo' and (self._running or reading_ready) and time.monotonic() >= self._next_poll:
                self._next_poll = time.monotonic() + self.settings['poll_seconds']
                # Pausing automatic replies must not stop normal chat receiving.
                requests.append(('sync_messages', {}, self._running))
            if self._running and not self._manual_only:
                for conversation in self.repo.list('conversation'):
                    cid = conversation['conversation_id']
                    case = self.repo.get('case', conversation['case_id'])
                    source = self._automatic_source(cid)
                    supplement = self._supplement_continuation(case)
                    work = self.repo.get('supplement_followup', cid) or {}
                    if supplement and work.get('status') == 'pending':
                        source = self._supplement_source(case)
                        retry_state, _, retry_at = send_retry_decision(self.repo, self.seller_id, cid, supplement['source_message_id'], 'supplement', supplement['issue_ids'])
                        existing_drafts = self.repo.list('draft', case_id=case['case_id'])
                        ready = (retry_state == 'ready' or retry_state == 'waiting' and retry_at <= now_ms())
                        if (source and self.account_verified
                                and not conversation.get('human_owned') and case['state'] not in ('human_owned', 'closed')
                                and self._in_scope(cid) and not self._conversation_unresolved(cid)
                                and work.get('due_at', 0) <= now_ms() and ready
                                and not any(d.get('status') in ('pending', 'sending', 'unknown') for d in existing_drafts)
                                and ('generate_draft', case['case_id']) not in active):
                            requests.append(('generate_draft', {'case_id': case['case_id'],
                                             'expected_case_revision': case['revision'], 'supplement_followup': True}, True))
                        continue
                    if (conversation['human_owned'] or self._conversation_unresolved(cid) or case['state'] in ('human_owned', 'closed')
                            or not self._in_scope(cid) or not source or not conversation['last_buyer_at']
                            or not self._post_quote_analysis_allowed(case, source)
                            or max(0, now_ms() - conversation['last_buyer_at']) < self.settings['merge_seconds'] * 1000):
                        continue
                    drafts = self.repo.list('draft', case_id=case['case_id'])
                    pending = next((d for d in reversed(drafts) if d['status'] == 'pending'
                                    and d['origin'] != 'manual' and d['source_message_id'] == source['message_id']
                                    and d.get('validation', {}).get('ready')), None)
                    continuation_pending = pending and any(p.get('draft_id') == pending['draft_id']
                            and p['status'] in ('pending', 'sending', 'unknown')
                            for p in self.repo.list('continuity_plan', conversation_id=cid))
                    retry_state, _, retry_at = send_retry_decision(self.repo, self.seller_id, cid, source['message_id'])
                    retry_ready = retry_state == 'ready' or retry_state == 'waiting' and retry_at <= now_ms()
                    if (pending and retry_state in ('blocked', 'exhausted')):
                        pending.update(status=retry_state, updated_at=now_ms())
                        self.repo.put('draft', pending['draft_id'], pending, history=True)
                    if (pending and retry_ready and not continuation_pending and pending.get('retry_after', 0) <= now_ms()
                            and (pending.get('continuation', {}).get('phase') != 'supplement' or pending.get('supplement_issue_ids'))
                            and self._auto_send and ('automatic_send', case['case_id']) not in active):
                        requests.append(('automatic_send', {'draft_id': pending['draft_id'],
                                                          'expected_revision': pending['revision']}, True))
                    if any(d['status'] in ('pending', 'sending', 'unknown')
                           or (d['origin'] == 'manual' and d['status'] == 'stale') for d in drafts):
                        continue
                    if self._generation_attempted(cid, case, source) or ('generate_draft', case['case_id']) in active:
                        continue
                    requests.append(('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']}, True))
            # Synchronization is explicitly configured and gets idle worker time only.
            if (not self._manual_only and not requests and not active and self.mode == 'live' and self.account_verified
                    and self.settings['feishu'].get('enabled') and self.settings['feishu'].get('write_enabled')
                    and time.monotonic() >= self._next_outbox
                    and any(o['status'] == 'pending' for o in self.repo.list('outbox'))):
                self._next_outbox = time.monotonic() + max(30, self.settings['poll_seconds'])
                requests.append(('run_outbox', {}, False))
        # A newly synchronized source is ready now. Do not put another complete
        # account poll ahead of its reply; the native send still rechecks freshness.
        for kind, params, automatic in sorted(requests, key=lambda request: request[0] == 'sync_messages'):
            if self._closing:
                break
            try:
                self._enqueue(kind, params, automatic=automatic)
            except ServiceError:
                # The customer/version can change between scheduling and enqueue.
                continue

    def _automatic_source(self, conversation_id, *, excluding=None, operations=None, allow_pending_sync=False):
        conversation = self.repo.get('conversation', conversation_id)
        case = self.repo.get('case', conversation['case_id']) if conversation else None
        if (not allow_pending_sync and self._message_sync_pending(conversation_id)
                or (self._conversation_unresolved(conversation_id, excluding=excluding) if operations is None else
                    any(o.get('operation_id') != excluding and o.get('status') in ('prepared', 'sending', 'unknown')
                        for o in operations)) or not self.account_verified or not case or case['seller_id'] != self.seller_id
                or case['buyer_id'] != conversation['buyer_id'] or case['conversation_id'] != conversation_id):
            return None
        source, undated_buyer = self.repo.automatic_source_facts(conversation_id)
        if source:
            source = self._photo_projection(source)
        if (not source or self.repo.get('history_only_message', encoded([conversation_id, source['message_id']]))
                or source['seller_id'] != self.seller_id or source['buyer_id'] != case['buyer_id']
                or source['conversation_id'] != conversation_id or type(source.get('timestamp_ms')) is not int):
            return None
        # An undated buyer message cannot be ordered safely, including relative to
        # a dated message. Keep that conversation available for explicit review.
        if undated_buyer:
            return None
        if self.mode == 'live' or self._scope['mode'] == 'incoming':
            if self._started_at_ms is None or source['timestamp_ms'] <= self._started_at_ms:
                return None
            baseline = self._source_baseline.get(conversation_id)
            if baseline and (source['message_id'] == baseline['message_id'] or
                             (baseline['timestamp_ms'] is not None and source['timestamp_ms'] <= baseline['timestamp_ms'])):
                return None
        return source

    def _generation_attempted(self, conversation_id, case, source, excluding=None, *, jobs=None, drafts=None):
        if not source:
            return False
        drafts = self.repo.list('draft', conversation_id=conversation_id) if drafts is None else drafts
        if any(d['status'] == 'accepted' and d['source_message_id'] == source['message_id'] for d in drafts):
            return True
        jobs = self.repo.list('job', conversation_id=conversation_id) if jobs is None else jobs
        for job in sorted(jobs, key=lambda j: j['created_at'], reverse=True):
            if job['kind'] != 'generate_draft' or job['job_id'] == excluding or job['status'] in ('stale', 'cancelled'):
                continue
            same_source = job.get('source_message_id') == source['message_id']
            legacy_source = ('source_message_id' not in job and job['case_id'] == case['case_id']
                             and (job.get('source_case_revision') == case['revision']
                                  or job['created_at'] >= self.repo.get('conversation', conversation_id)['last_buyer_at']))
            if not (same_source or legacy_source):
                continue
            if job.get('source_text_sha256') and job['source_text_sha256'] != text_hash(source['text']):
                continue
            if job['status'] == 'deferred' and job.get('retry_at') is not None:
                return job.get('source_case_revision') == case['revision'] and job['retry_at'] > now_ms()
            if job['status'] == 'failed' and job.get('external_started'):
                from .model_attempts import job_blocks_replay
                if not job_blocks_replay(self.repo, job):
                    continue
            if job['status'] == 'succeeded':
                draft_id = (job.get('result') or {}).get('draft_id')
                previous = next((d for d in drafts if d['draft_id'] == draft_id), None) if draft_id else None
                if previous and previous['status'] in ('stale', 'rejected'):
                    continue
            return True
        return False

    def _in_scope(self, conversation_id, *, excluding=None, allow_pending_sync=False):
        scope = self._frozen_scope
        if not scope or scope['seller_id'] != self.seller_id:
            return False
        if scope['mode'] == 'incoming':
            return self._automatic_source(conversation_id, excluding=excluding, allow_pending_sync=allow_pending_sync) is not None
        return scope['mode'] == 'all' or conversation_id in scope['conversation_ids']

    def _set_check(self, name, job_id, status, message, simulated=False):
        with self._lock:
            self.checks[name] = {'status': status, 'check_id': job_id, 'checked_at': now_ms(),
                                 'message': message, 'simulated': simulated}
        self._changed()

    def _record_check_error(self, kind, job_id, error):
        if error.code == 'cancelled':
            return
        names = {'check_connection': 'messages', 'check_ai': 'ai', 'preview_send': 'send_preview', 'check_feishu': 'feishu'}
        if kind in names:
            status = 'blocked' if any(word in error.code for word in ('blocked', 'required', 'missing', 'identity')) else 'failed'
            self._set_check(names[kind], job_id, status, str(error), self.mode == 'demo')
        if kind in ('sync_messages', 'fill_history'):
            with self._lock:
                job = self.repo.get('job', job_id)
                if job and job.get('conversation_id'):
                    current = self.repo.get('coverage', job['conversation_id'], coverage())
                    current.update(status='partial' if error.code == 'history_limit' else 'failed',
                                   updated_at=now_ms(), message=str(error))
                    self.repo.put('coverage', job['conversation_id'], current)
    def _identity_failed(self, epoch):
        # A late failure from a cancelled check must not revoke a newer grant.
        with self._lock:
            if epoch != self._epoch or self._closing:
                return False
            self.account_verified = False
            self._wa_enabled = False
            self._procurement_authorized = False
            self._procurement_epoch += 1
            with self.repo.transaction():
                self._wa_cancel()
            self._pause()
            self.checks['identity'] = {'status': 'blocked', 'check_id': None,
                                       'checked_at': now_ms(), 'message': '卖家身份未核验',
                                       'simulated': False}
        self._changed()
        return True

    def _ensure_identity(self, facade):
        with self._lock:
            if not self.account_verified:
                raise ServiceError('identity_required', '请先运行只读连接检查核验卖家身份', stage='identity')
            epoch, seller = self._epoch, self.seller_id
        if self.mode == 'live':
            try:
                with self._executor_health.phase(getattr(self._execution_local, 'job_id', None), 'identity_ms'):
                    actual = facade.resolve_identity(seller)
                if actual != seller:
                    raise ServiceError('seller_mismatch', '当前平台卖家身份已变化', stage='identity')
            except Exception as error:
                if getattr(error, 'code', None) == 'cancelled':
                    raise
                if not self._identity_failed(epoch):
                    raise ServiceError('cancelled', '身份检查期间授权或设置已变化', stage='identity') from error
                raise
            with self._lock:
                if epoch != self._epoch or self._closing or not self.account_verified:
                    raise ServiceError('cancelled', '身份检查期间授权或设置已变化', stage='identity')

    def _task_check_connection(self, params, job_id, epoch, facade):
        self._set_check('runtime', job_id, 'running', '检查运行环境')
        status = facade.runtime_status()
        if epoch != self._epoch or self._closing:
            raise ServiceError('cancelled', '连接检查期间授权或设置已变化')
        if not status.get('ok'):
            self._set_check('runtime', job_id, 'blocked', status.get('message', '缺少运行授权'))
            self._set_check('identity', job_id, 'blocked', '运行环境未就绪')
            raise ServiceError('runtime_blocked', '运行环境或授权尚未就绪', stage='runtime')
        simulated = self.mode == 'demo' or bool(status.get('simulated'))
        self._set_check('runtime', job_id, 'demo' if simulated else 'passed',
                        '本地演示运行环境' if simulated else '运行环境就绪', simulated)
        try:
            with self._executor_health.phase(job_id, 'identity_ms'):
                seller = facade.resolve_identity(self.expected_seller_id or self.seller_id)
            if epoch != self._epoch or self._closing:
                raise ServiceError('cancelled', '身份检查期间授权或设置已变化')
            if self.mode == 'live':
                self._activate_seller(seller)
            elif seller != self.seller_id:
                raise ServiceError('seller_mismatch', '演示卖家标识不匹配')
        except Exception as error:
            if getattr(error, 'code', None) == 'cancelled':
                raise
            if self.mode == 'live' and not self._identity_failed(epoch):
                raise ServiceError('cancelled', '身份检查期间授权或设置已变化', stage='identity') from error
            self._set_check('identity', job_id, 'blocked', '卖家身份未核验', simulated)
            raise
        self._set_check('identity', job_id, 'demo' if simulated else 'passed',
                        '演示卖家（合成数据）' if simulated else '已核验当前卖家', simulated)
        self._task_refresh_company_profile({}, job_id, epoch, facade)
        result = self._sync({'_bootstrap': self.mode == 'live'}, job_id, facade)
        if epoch != self._epoch or self._closing:
            raise ServiceError('cancelled', '读取期间授权或设置已变化')
        self._set_check('messages', job_id, 'demo' if simulated else 'passed',
                        '合成消息读取完成' if simulated else '会话摘要已读取，正文按变化会话后台同步', simulated)
        if self.mode == 'live':
            # Capability is verified by the bounded native read. Any unfinished
            # activity remains pending and blocks automatic use until caught up.
            result.pop('_job_status', None)
        return {**result, 'message': '演示连接检查完成' if simulated else '连接检查完成，消息按会话后台同步'}

    def _sync(self, params, job_id, facade):
        with self._lock:
            token = self._read_token(facade)
            self._require_current_read(token)
        self._ensure_identity(facade)
        cid = params.get('conversation_id')
        if cid:
            with self._lock:
                self._require_current_read(token)
                current = self._require('conversation', cid)
            infos = [current]
        else:
            bootstrap = params.get('_bootstrap') is True and self.mode == 'live'
            infos, seen, cursor = [], set(), None
            for _ in range(HISTORY_PAGE_LIMIT):
                with self._executor_health.phase(job_id, 'message_read_ms'):
                    page = facade.read_recent_conversations(self.seller_id, before_ms=cursor, count=20 if bootstrap else 100)
                with self._lock:
                    self._require_current_read(token)
                more, next_cursor = self._history_cursor(page, cursor)
                for info in page.get('items', []):
                    if info['conversation_id'] not in seen:
                        infos.append(info)
                        seen.add(info['conversation_id'])
                if bootstrap:
                    # Verify one real body read, then let the monitor fill the
                    # remaining customers. Startup must not traverse an account's
                    # entire archive before high-frequency detection can begin.
                    with self._lock, self.repo.transaction():
                        self._require_current_read(token)
                        for info in infos:
                            self._observe_message_activity(info)
                        sweep = self.repo.get('message_monitor', 'summary', {})
                        if sweep.get('next_cursor') is None:
                            self.repo.put('message_monitor', 'summary', {
                                'next_cursor': next_cursor if more else None,
                                'next_scan_at': sweep.get('next_scan_at', 0) if more else 0})
                    infos = infos[:1]
                    break
                if not more:
                    break
                cursor = next_cursor
                self._yield_to_ready_work()
            else:
                raise ServiceError('history_limit', '会话列表达到本次读取上限，请稍后重试', stage='messages')
        changed_count = 0
        unfinished = 0
        for info in infos:
            cid = info['conversation_id']
            with self._lock:
                self._require_current_read(token)
                job = self.repo.get('job', job_id, {})
                if self.mode == 'live' and 'latest_timestamp' in info:
                    self._observe_message_activity(info)
                target = job.get('sync_target') if params.get('conversation_id') else info.get('latest_timestamp')
                activity = self.repo.get('message_activity', cid, {})
                matching_activity = (activity.get('latest_timestamp') == info.get('latest_timestamp') and
                                     (not info.get('activity_signature') or activity.get('signature') == info['activity_signature']))
                target_version = (job.get('sync_version', 0) if params.get('conversation_id') else
                                  activity.get('version', 0) if matching_activity else 0)
                target_clock = (job.get('sync_clock', 'message') if params.get('conversation_id') else
                                info.get('activity_clock', 'message'))
                target_message_id = (job.get('sync_message_id') if params.get('conversation_id') else info.get('latest_message_id'))
                target_text_hash = (job.get('sync_text_sha256') if params.get('conversation_id') else info.get('latest_text_sha256'))
                current = self.repo.get('coverage', cid, coverage())
                gap = self.repo.get('sync_gap', cid, {})
                original = gap['coverage'] if gap else current
                # Freeze the old boundary: pages saved during this attempt must not
                # count as overlap on a later attempt after the page budget runs out.
                boundary_ids = set(gap['boundary_ids'] if gap else
                                   [m['message_id'] for m in self._messages(cid)[-100:]])
                cursor = current['next_cursor'] if gap else None
                latest = gap.get('latest_ms') if gap else None
            changed = False
            completed = False
            native_messages = {}
            for _ in range(SYNC_PAGE_LIMIT):
                if self._stop.is_set():
                    raise ServiceError('cancelled', '服务已关闭')
                with self._executor_health.phase(job_id, 'message_read_ms'):
                    page = facade.read_message_page(self.seller_id, info['buyer_id'], cid, before_ms=cursor, count=100)
                more, next_cursor = self._history_cursor(page, cursor)
                messages = page.get('messages', [])
                stamps = [m.get('timestamp_ms') for m in messages if type(m.get('timestamp_ms')) is int]
                if stamps:
                    latest = max(stamps + ([latest] if latest is not None else []))
                overlap = any(m.get('message_id') in boundary_ids for m in messages)
                with self._lock, self.repo.transaction():
                    self._require_current_read(token)
                    # Revisions on the overlap page still go through normal ingest.
                    changed |= self._ingest(info, messages, has_more=more, next_cursor=next_cursor, full=True,
                                            history=job.get('sync_reason') == 'backfill')
                    native_messages.update((message['message_id'], message) for message in messages)
                    # A later, independently observed activity can identify a body
                    # first encountered by history. A timestamp alone is not proof.
                    self._promote_history_activity(cid, messages, target_version, target_message_id, target_text_hash)
                    if more and overlap:
                        current = self.repo.get('coverage', cid)
                        complete = original['status'] == 'complete'
                        current.update(status='complete' if complete else 'partial',
                                       next_cursor=None if complete else (
                                           original['next_cursor'] if original['next_cursor'] is not None else next_cursor),
                                       message='历史读取完成' if complete else '最新消息已衔接，旧历史待补全')
                        self.repo.put('coverage', cid, current)
                    self.repo.put('sync_gap', cid, {'boundary_ids': sorted(boundary_ids), 'coverage': original,
                                                   'latest_ms': latest}
                                  if more and not overlap and boundary_ids else {})
                    completed = not more or overlap or not boundary_ids
                    if completed:
                        unread.finish_sync(self.repo, cid)
                    if completed and self.mode == 'live':
                        live_head = (cursor is None and job.get('sync_reason') != 'backfill'
                                     and not target_message_id
                                     and self._weak_history_head(cid, messages, target_version))
                        completed = self._complete_message_sync(cid, target, target_version, latest, job_id, token,
                                                                target_message_id, target_text_hash,
                                                                activity_clock=target_clock, changed=changed and not live_head,
                                                                read_messages=list(native_messages.values()))
                        if completed and live_head:
                            self._promote_history_activity(cid, messages, target_version, target_message_id, target_text_hash,
                                                           confirmed_live_head=True)
                self._changed(cid)
                self._yield_to_ready_work()
                if not more or overlap or not boundary_ids:
                    break
                cursor = next_cursor
            if not completed:
                unfinished += 1
                if self.mode == 'live':
                    with self._lock, self.repo.transaction():
                        self._require_current_read(token)
                        reason = 'empty_history' if latest is None else 'index_lag'
                        self._defer_message_sync(cid, target, target_version, reason)
            if completed:
                self._queue_recent_photos(cid)
            changed_count += int(changed)
        return {'_job_status': ('deferred' if self.mode == 'live' else 'failed') if unfinished else 'succeeded',
                'message': f'已读取 {len(infos)} 个会话，更新 {changed_count} 个' +
                           (f'；{unfinished} 个会话待补读，已保留进度并安排稍后同步' if unfinished else ''),
                'changed_count': changed_count}

    @staticmethod
    def _history_cursor(page, cursor):
        more = page.get('has_more', False)
        next_cursor = page.get('next_cursor')
        if (type(more) is not bool or
                (more and (type(next_cursor) is not int or next_cursor <= 0 or
                           (cursor is not None and next_cursor >= cursor))) or
                (not more and next_cursor is not None)):
            raise ServiceError('history_cursor_stalled', '历史分页未推进，已保存已读范围', stage='history')
        return more, next_cursor

    def _task_sync_messages(self, params, job_id, epoch, facade):
        with self._lock:
            job = self.repo.get('job', job_id, {})
            if job.get('sync_generation', self._monitor_generation) != self._monitor_generation:
                raise ServiceError('cancelled', '消息同步排队期间设置已变化')
        return self._sync(params, job_id, facade)

    def _task_fill_history(self, params, job_id, epoch, facade):
        self._ensure_identity(facade)
        cid = params['conversation_id']
        with self._lock:
            token = self._read_token(facade)
            self._require_current_read(token)
            conversation = self._require('conversation', cid)
            cursor = self.repo.get('coverage', cid, coverage())['next_cursor']
            from_head = cursor is None
            target_activity = self.repo.get('message_activity', cid, {})
            history_version = target_activity.get('version', 0)
            task = self.repo.get('message_sync_task', cid, {})
            watermark = self.repo.get('message_watermark', cid, {})
            pending_live = (self._running and self._started_at_ms is not None
                            and max(task.get('last_activity_observed_at', 0), target_activity.get('observed_at', 0)) >= self._started_at_ms
                            and self._message_sync_pending(cid))
            if pending_live:
                history_version = watermark.get('activity_version', 0)
        pages, latest_read, changed = 0, None, False
        head_messages = []
        while not self._stop.is_set():
            with self._executor_health.phase(job_id, 'message_read_ms'):
                page = facade.read_message_page(self.seller_id, conversation['buyer_id'], cid, before_ms=cursor, count=100)
            more, next_cursor = self._history_cursor(page, cursor)
            messages = page.get('messages', [])
            stamps = [message['timestamp_ms'] for message in messages if type(message.get('timestamp_ms')) is int]
            if stamps:
                latest_read = max(stamps + ([latest_read] if latest_read is not None else []))
            with self._lock, self.repo.transaction():
                self._require_current_read(token)
                changed |= self._ingest(conversation, messages, has_more=more, next_cursor=next_cursor, full=True, history=True,
                                        history_activity_version=history_version)
                if pages == 0:
                    head_messages = messages
                if pending_live:
                    self._promote_history_activity(cid, messages, target_activity.get('version', 0),
                                                   target_activity.get('latest_message_id'), target_activity.get('latest_text_sha256'))
                if not more:
                    self.repo.put('sync_gap', cid, {})
            pages += 1
            self._job_update(job_id, message=f'已读取 {pages} 页历史', stage='history')
            self._changed(cid)
            self._yield_to_ready_work()
            if not more:
                if self.mode == 'live':
                    with self._lock, self.repo.transaction():
                        self._require_current_read(token)
                        activity = self.repo.get('message_activity', cid, {})
                        if target_activity and not self._messages(cid):
                            self._defer_message_sync(cid, target_activity['latest_timestamp'], target_activity['version'], 'empty_history')
                            return {'_job_status': 'deferred', 'message': '当前暂无可读正文，已保留读取状态'}
                        if from_head and target_activity and (not pending_live or target_activity.get('latest_message_id')):
                            self._complete_message_sync(cid, target_activity['latest_timestamp'], target_activity['version'],
                                latest_read, job_id, token, target_activity.get('latest_message_id'),
                                target_activity.get('latest_text_sha256'), activity_clock=target_activity.get('activity_clock', 'message'),
                                changed=changed, read_messages=head_messages)
                        if activity and self._message_sync_pending(cid):
                            # A newer monitor observation remains live work; only the
                            # completion of the original backfill inherits its intent.
                            reason = ('backfill' if not pending_live and activity.get('version') == target_activity.get('version')
                                      else 'activity')
                            self._enqueue('sync_messages', {'conversation_id': cid}, read_reason=reason)
                return {'message': f'历史补全完成，共 {pages} 页'}
            cursor = next_cursor
            if pages >= HISTORY_PAGE_LIMIT:
                raise ServiceError('history_limit', '历史页数达到本次上限，可继续补全', stage='history')
        raise ServiceError('cancelled', '服务已关闭')

    def _task_check_ai(self, params, job_id, epoch, facade):
        self._ensure_identity(facade)
        self._set_check('ai', job_id, 'running', '单次 AI 检查中', self.mode == 'demo')
        if self.mode == 'live':
            self._freeze_reception_model()
            self._set_check('ai', job_id, 'passed', '当前接待模型所需文字和结构能力已检查', False)
            return {'message': '当前模型能力检查记录有效'}
        context = synthetic_context(self.seller_id)
        with self._lock:
            if epoch != self._epoch or self._closing or self._running:
                raise ServiceError('cancelled', 'AI 检查期间授权或设置已变化')
            self._job_update(job_id, external_started=True)
        result = facade.generate(context, request_id=job_id)
        diagnostics = getattr(facade, 'last_ai_diagnostics', None)
        if isinstance(diagnostics, dict):
            self._job_update(job_id, diagnostics=copy.deepcopy(diagnostics))
        _, validation = self._validate_ai_output(result, context)
        if not validation['ready']:
            reason = reply_validation_reason(validation)
            messages = {'number_unverified': 'AI 已返回，但合成回复含未核实数值',
                        'commitment_unverified': 'AI 已返回，但合成回复的商业承诺未通过校验',
                        'sources_missing': 'AI 已返回，但合成回复缺少有效来源',
                        'reply_empty': 'AI 已返回，但未生成可用回复正文',
                        'question_repeated': 'AI 已返回，但合成回复重复询问已知条件'}
            raise ServiceError('ai_check_blocked', messages.get(reason, 'AI 已返回，但合成回复未通过正文校验'),
                               stage='analysis', diagnostics={'validation_reason': reason})
        if epoch != self._epoch or self._closing:
            raise ServiceError('cancelled', 'AI 检查期间授权或设置已变化')
        self._set_check('ai', job_id, 'demo' if self.mode == 'demo' else 'passed',
                        '本地模拟 AI 检查，不代表真实 AI 验收' if self.mode == 'demo' else '单次 AI 正文与来源检查通过', self.mode == 'demo')
        return {'message': '演示 AI 检查完成' if self.mode == 'demo' else 'AI 检查完成'}

    def _save_ai_conversation_state(self, conversation, case, output, context):
        # Requirements are applied atomically by the explicit change handler.
        summary = output.get('conversation_summary')
        if isinstance(summary, str) and summary.strip() and len(summary) <= 10000:
            prior = self.repo.get('conversation_summary', conversation['conversation_id']) or {}
            self.repo.put('conversation_summary', conversation['conversation_id'], {
                'conversation_id': conversation['conversation_id'], 'seller_id': conversation['seller_id'],
                'buyer_id': conversation['buyer_id'], 'body': summary.strip(),
                'revision': prior.get('revision', 0) + 1, 'updated_at': now_ms(),
                'source_fingerprint': context.get('fingerprint')}, history=True)
        return False

    def _validate_ai_output(self, output, context):
        if (not isinstance(output, dict) or not isinstance(output.get('facts', {}), dict)
                or not isinstance(output.get('question_keys', []), list)
                or not isinstance(output.get('conflicts', []), list)):
            raise ServiceError('invalid_ai_output', 'AI 返回的业务结构无效', stage='validation')
        normalized = normalize_reply_output(output, allow_legacy=True,
                                            allow_empty=output.get('action') in ('stop', 'skip', 'no_reply'))
        output.update(normalized)
        from .requirement_changes import validate_changes
        validate_changes(output, context)
        for message in output['reply_messages']:
            if not validate_reply(message['body'], message['reply_sources'], context)['ready']:
                raise ServiceError('unverified_reply', '某条回复正文或来源无效', stage='validation')
        facts = {}
        validation = validate_reply(output['reply'], output['reply_sources'], context,
                                    max_chars=1200 + 2 * max(0, len(output['reply_messages']) - 1))
        if not validation['ready'] and output.get('action') not in ('stop', 'skip', 'no_reply'):
            raise ServiceError('unverified_reply', 'AI 正文缺少有效来源或包含未核实承诺', stage='validation',
                               diagnostics={'validation_reason': reply_validation_reason(validation)})
        return facts, validation

    def _task_generate_draft(self, params, job_id, epoch, facade):
        with self._lock:
            if epoch != self._epoch or self._closing or self._manual_only:
                raise ServiceError('cancelled', 'AI 生成权限已撤销')
        with self._job_phase(job_id, 'identity_ms'):
            self._ensure_identity(facade)
        with self._lock:
            if epoch != self._epoch or self._closing or self._manual_only:
                raise ServiceError('cancelled', 'AI 生成权限已撤销')
            case = self._require('case', params['case_id'])
            if case['revision'] != params['expected_case_revision']:
                return {'_job_status': 'stale', 'message': '需求版本已变化，旧任务未执行'}
            conversation = self._require('conversation', case['conversation_id'])
            self._assert_conversation_resolved(case['conversation_id'])
            if conversation['human_owned'] or case['state'] in ('closed', 'human_owned'):
                raise ServiceError('generation_blocked', '当前事项已接管或停止联系')
            job = self._require('job', job_id)
            latest = self._latest_buyer(case['conversation_id'])
            if (case['seller_id'] != self.seller_id or case['buyer_id'] != conversation['buyer_id']
                    or job['case_id'] != case['case_id'] or job['conversation_id'] != case['conversation_id']
                    or job['source_case_revision'] != case['revision']
                    or job['source_message_id'] != (latest['message_id'] if latest else None)):
                raise ServiceError('source_changed', '生成任务的原文归属或版本已变化', stage='context')
            supplement = self._supplement_continuation(case) if params.get('supplement_followup') else None
            work = self.repo.get('supplement_followup', case['conversation_id']) or {}
            if params.get('supplement_followup') and (not supplement or work.get('job_id') != job_id or work.get('status') != 'generating'):
                raise ServiceError('source_changed', '业务补充续答来源已变化')
            if not supplement and not self._post_quote_analysis_allowed(case, latest, excluding=job_id):
                return {'message': '等待报价；没有新的实质买家消息，未调用 AI 或发送'}
            automatic = job.get('automatic') is True
            if automatic and (epoch != self._epoch or not self._running or self._closing
                              or conversation['case_id'] != case['case_id']
                              or not self._in_scope(case['conversation_id'])
                              or not (self._supplement_source(case) if supplement else self._automatic_source(case['conversation_id']))
                              or not supplement and self._generation_attempted(case['conversation_id'], case, latest, excluding=job_id)):
                raise ServiceError('cancelled', '自动生成来源、范围或额度已失效')
            prior_drafts = {d['draft_id']: d['revision'] for d in self.repo.list('draft', case_id=case['case_id'])}
            if latest and self._stop_requested(latest['text']):
                with self.repo.transaction():
                    case.update(state='closed', revision=case['revision'] + 1, updated_at=now_ms())
                    self.repo.put('case', case['case_id'], case, history=True)
                    self._invalidate(case['conversation_id'])
                    self._queue_outbox(case)
                return {'message': '客户已要求停止联系；未调用 AI 或发送'}
            messages = self.repo.list('message', conversation_id=case['conversation_id'])
            has_photos = self.settings.get('reception_images_enabled', True) and bool(get_reception_source_images(self.repo, conversation, case, messages))
        with self._job_phase(job_id, 'photo_prepare_ms'):
            photo_guard = self._prepare_direct_photos(case['conversation_id'], job_id, epoch, facade) if has_photos else None
        with self._lock:
            if (epoch != self._epoch or self._closing or self._manual_only
                    or self._require('case', case['case_id']) != case
                    or automatic and not self._running):
                raise ServiceError('cancelled', '生成前来源或权限已变化')
            if photo_guard is not None:
                photo_guard()
            # Rebuild after this job's observations and charges. An unrelated
            # edit still fails the source guard; no stale context is rebased.
            job = self._require('job', job_id)
            budget = self._budget()
            if automatic:
                budget['ai_used'] += 1  # Context reflects the call when it actually starts.
            with self._job_phase(job_id, 'context_ms'):
                context = self.get_context(case['case_id'])
        model_snapshot = self._freeze_reception_model(allow_unchecked=True) if self.mode == 'live' else None
        found_product, catalog_product = None, None
        attempt_tokens = {}
        from .model_attempts import AttemptLifecycle, claim, job_blocks_replay
        attempt_source = {'conversation_id': case['conversation_id'], 'buyer_id': case['buyer_id'],
                          'message_id': job.get('source_message_id'), 'text_sha256': job.get('source_text_sha256'),
                          'continuation': supplement}
        def on_call_start():
            with self._lock, self.repo.transaction():
                current = self._require('case', case['case_id'])
                conversation_now = self._require('conversation', case['conversation_id'])
                latest_now = self._latest_buyer(case['conversation_id'])
                if (attempt_tokens or epoch != self._epoch or self._closing or self._manual_only
                        or current['revision'] != case['revision'] or conversation_now.get('human_owned')
                        or (latest_now['message_id'] if latest_now else None) != job['source_message_id']
                        or supplement_fingerprint(self.get_context(case['case_id'])) != supplement_fingerprint(context)
                        or automatic and (not self._running or not self._in_scope(case['conversation_id']))):
                    raise ServiceError('cancelled', '生成前来源或权限已变化')
                if photo_guard is not None:
                    photo_guard()
                current_job = self._require('job', job_id)
                if automatic and self.mode == 'live':
                    previous = self.repo.list('job', conversation_id=case['conversation_id'])
                    if any(row.get('job_id') != job_id and row.get('kind') == 'generate_draft'
                           and row.get('automatic') and job_blocks_replay(self.repo, row)
                           and row.get('source_message_id') == current_job.get('source_message_id')
                           and row.get('source_text_sha256') == current_job.get('source_text_sha256')
                           for row in previous):
                        raise ServiceError('model_attempt_exhausted', '该来源的模型请求已开始，请明确操作后重试', stage='generation')
                if self.mode == 'live':
                    attempt_tokens.update(claim(self.repo, seller_id=self.seller_id, purpose='reception',
                                                channel='international', source=attempt_source))
                current_job.update(external_started=True, model_attempt_chain_ids=list(attempt_tokens),
                                   ai_reserved_remaining=0, stage='generate_draft', message='正在生成接待回复')
                self.repo.put('job', job_id, current_job, history=True)
                self._charge_usage(ai=True)
            self._emit('job.updated', current_job)
        attempt_lifecycle = AttemptLifecycle(self.repo, on_call_start, attempt_tokens)
        target = getattr(facade.generate, 'side_effect', None)
        parameters = inspect.signature(target if callable(target) else facade.generate).parameters
        arguments = {'request_id': job_id}
        try:
            photo_deadline = getattr(photo_guard, 'photo_deadline', None)
            if photo_deadline is None and context.get('supplement_image_refs'):
                from .model_media import start_photo_deadline
                photo_deadline = start_photo_deadline(self.settings)
            if 'photo_inputs' in parameters:
                arguments['photo_inputs'] = self._direct_photo_inputs(context)
            if 'cancel_event' in parameters:
                arguments['cancel_event'] = self._generation_cancel_event(job_id)
            if 'photo_deadline' in parameters:
                arguments['photo_deadline'] = photo_deadline
            if 'on_call_start' in parameters:
                arguments['on_call_start'] = attempt_lifecycle
            else:
                attempt_lifecycle()
            try:
                with self._job_phase(job_id, 'ai_elapsed_ms'):
                    output = facade.generate(context, **arguments)
            except BaseException:
                attempt_lifecycle.fallback('unknown')
                raise
        finally:
            owner = getattr(arguments.get('photo_inputs'), 'owner', None)
            if owner is not None:
                owner.close_if_owned()
        attempt_lifecycle.fallback('completed')
        image_reads = output.get('verified_image_reads', []) if isinstance(output, dict) else []
        self._job_update(job_id, diagnostics={'image_reads': len(image_reads)})
        context = with_direct_photo_reads(context, image_reads)
        diagnostics = getattr(facade, 'last_ai_diagnostics', None)
        if isinstance(diagnostics, dict):
            self._job_update(job_id, diagnostics=copy.deepcopy(diagnostics))
        facts, validation = self._validate_ai_output(output, context)
        social_reply = bool(latest and not latest.get('attachments') and social_intent(latest['text'])
                            and validation['ready'] and output.get('action') in ('ask', 'wait_quote', 'skip'))
        if social_reply:
            # Conversational wording does not invalidate grounded requirements
            # supplied earlier in this same frozen customer context.
            facts = {}
            output = dict(output, facts={}, question_keys=[], procurement_key=case['procurement_key'])
        with self._lock, self.repo.transaction():
            verified_model = self._verify_model_result(model_snapshot, output)
            current = self._require('case', case['case_id'])
            current_conversation = self._require('conversation', case['conversation_id'])
            current_drafts = {d['draft_id']: d['revision'] for d in self.repo.list('draft') if d['case_id'] == case['case_id']}
            manual_exists = any(d['origin'] == 'manual' and d['status'] in ('pending', 'stale')
                                for d in self.repo.list('draft') if d['case_id'] == case['case_id'])
            photo_stale = False
            if photo_guard is not None:
                try:
                    photo_guard()
                except ServiceError:
                    photo_stale = True
            if (photo_stale or current['revision'] != case['revision'] or current_conversation['revision'] != context['conversation_revision']
                    or self.get_context(case['case_id'])['fingerprint'] != context['fingerprint']
                    or current_drafts != prior_drafts or manual_exists or current_conversation.get('human_owned')
                    or self._conversation_unresolved(case['conversation_id']) or epoch != self._epoch or self._closing
                    or automatic and self._message_sync_pending(case['conversation_id'])):
                # Keep the exact old response in job history, attached to its original case.
                self.repo.put('ai_result', job_id, {'case_id': case['case_id'], 'reply': output['reply'],
                                                  'context_fingerprint': context['fingerprint'], 'status': 'stale'}, history=True)
                return {'_job_status': 'stale', 'message': '依据、权限或人工编辑已变化；旧建议已保留但未覆盖当前草稿'}
            applied = self._apply_requirement_output(current_conversation, current, output, context, job_id)
            if len(applied['reply_case_ids']) == 1:
                current = self._require('case', applied['reply_case_ids'][0])
            else:
                current = self._require('case', current['case_id'])
            current_conversation = self._require('conversation', current['conversation_id'])
            self._save_ai_conversation_state(current_conversation, None, output, context)
            current['missing'] = missing_fields_for_case(current['facts'], self.settings, current)
            current['procurement_key'] = str(output.get('procurement_key') or current['procurement_key'])
            action = {'human': 'needs_human', 'skip': 'no_reply', 'ask': 'reply', 'wait_quote': 'reply'}.get(output.get('action'), output.get('action', 'reply'))
            latest = self._latest_buyer(current['conversation_id'])
            stopped = latest and re.search(r'(?:stop (?:contacting|messaging)|do not contact|don.t contact|不要联系|停止联系)', latest['text'], re.I)
            if stopped or action == 'stop':
                current['state'] = 'closed'
            elif social_reply or current['state'] == 'closed':
                pass  # Routing to an old case must never reopen a stopped case.
            elif not current['missing']:
                current['state'] = 'waiting_quote'
            else:
                current['state'] = 'clarifying'
            current.update(revision=current['revision'] + 1, updated_at=now_ms())
            self.repo.put('case', current['case_id'], current, history=True)
            self._invalidate(current['conversation_id'])
            if not social_reply or current['facts'] or current['state'] == 'waiting_quote':
                self._queue_outbox(current)
            if current['state'] == 'closed' or action == 'no_reply':
                if supplement:
                    work.update(status='complete', updated_at=now_ms())
                    self.repo.put('supplement_followup', current['conversation_id'], work)
                return {'message': '事项已停止自动回复，需求与依据已保存'}
            new_context = with_direct_photo_reads(self.get_context(current['case_id']), output.get('verified_image_reads', []))
            draft = self._draft(current, output['reply'], output.get('reply_sources', []), new_context,
                                'demo' if self.mode == 'demo' else 'ai', messages=output['reply_messages'])
            if verified_model is not None:
                draft['model_snapshot'] = verified_model
            draft['question_keys'] = [key for key in output.get('question_keys', []) if key in FIELDS]
            draft['reply_case_ids'] = applied['reply_case_ids']
            draft['related_case_versions'] = self._related_case_versions(applied['reply_case_ids'])
            draft['social_reply'] = social_reply
            if new_context.get('continuation'):
                draft['continuation'] = copy.deepcopy(new_context['continuation'])
            self._job_update(job_id, repair_count=len(output.get('repairs', [])))
            self.repo.put('draft', draft['draft_id'], draft, history=True)
            if supplement:
                work.update(status='generated', draft_id=draft['draft_id'], updated_at=now_ms())
                self.repo.put('supplement_followup', current['conversation_id'], work)
            if supplement:
                draft['supplement_issue_ids'] = list(supplement['issue_ids'])
                self.repo.put('draft', draft['draft_id'], draft, history=True)
            self.repo.put('ai_result', job_id, {'case_id': current['case_id'], 'reply': output['reply'],
                                              'context_fingerprint': context['fingerprint'], 'status': 'applied'}, history=True)
        self._changed(current['conversation_id'], current['case_id'])
        if (self._auto_send and self._running and self._in_scope(current['conversation_id']) and draft['validation']['ready']
                and (not supplement or draft.get('supplement_issue_ids'))):
            self._enqueue('automatic_send', {'draft_id': draft['draft_id'], 'expected_revision': draft['revision']}, automatic=True)
        return {'message': '已保存自然回复正文与需求依据', 'draft_id': draft['draft_id'], 'case_id': current['case_id']}

    def _queue_outbox(self, case):
        if any(o['case_id'] == case['case_id'] and o['case_revision'] == case['revision'] for o in self.repo.list('outbox')):
            return
        item = {'operation_id': new_id('outbox'), 'case_id': case['case_id'], 'case_revision': case['revision'],
                'kind': 'case_upsert', 'status': 'pending', 'message': '本地已保存，等待后台同步', 'updated_at': now_ms(),
                'case_snapshot': self._registration_snapshot(case)}
        self.repo.put('outbox', item['operation_id'], item, history=True)

    def _intent_source(self, draft, case):
        source = self.repo.get('message', encoded([draft['conversation_id'], draft['source_message_id']]))
        conversation = self.repo.get('conversation', draft['conversation_id'])
        latest = self._latest_buyer(draft['conversation_id'])
        if (not source or not conversation or not latest or source['role'] != 'buyer' or source['seller_id'] != self.seller_id
                or case['seller_id'] != self.seller_id or source['buyer_id'] != case['buyer_id']
                or conversation['buyer_id'] != case['buyer_id'] or case['conversation_id'] != draft['conversation_id']
                or source['conversation_id'] != case['conversation_id']
                or source['message_id'] != draft['source_message_id'] or latest['message_id'] != source['message_id']):
            raise ServiceError('source_mismatch', '发送来源缺少当前卖家及买家原文依据', stage='send')
        return source

    def _automatic_send_source(self, draft, case, *, excluding=None, allow_pending_sync=False):
        if draft.get('batch_id'):
            return self._reply_batch_basis_current(self._require('reply_batch', draft['batch_id']), excluding=excluding,
                                                  check_sync=not allow_pending_sync)
        source = (self._supplement_source(case, excluding=excluding, allow_pending_sync=allow_pending_sync)
                  if draft.get('continuation', {}).get('phase') == 'supplement'
                  else self._automatic_source(draft['conversation_id'], excluding=excluding or draft.get('batch_id'),
                                              allow_pending_sync=allow_pending_sync))
        conversation = self._require('conversation', draft['conversation_id'])
        return (source is not None and source['message_id'] == draft['source_message_id']
                and case['case_id'] == conversation['case_id']
                and self._in_scope(draft['conversation_id'], excluding=excluding or draft.get('batch_id'),
                                   allow_pending_sync=allow_pending_sync))

    def _intent(self, draft, case, operation_id, epoch, automatic=False):
        source = self._intent_source(draft, case)
        intent = {'seller_id': self.seller_id, 'buyer_id': case['buyer_id'], 'conversation_id': draft['conversation_id'],
                  'reply': draft['body'], 'source_message_id': draft['source_message_id'], 'draft_id': draft['draft_id'],
                  'source_timestamp_ms': source['timestamp_ms'],
                  'case_id': case['case_id'], 'case_version': case['revision'], 'revision': draft['revision'],
                  'epoch': epoch, 'operation_id': operation_id, 'send_mode': 'automatic' if automatic else 'manual'}
        if automatic:
            intent['frozen_scope'] = copy.deepcopy(self._frozen_scope)
            intent['budget_reserved'] = True
            intent['budget_sequence'] = self._budget_used
        return self._reply_batch_intent(draft, intent) if draft.get('batch_id') else intent

    def _task_preview_send(self, params, job_id, epoch, facade):
        self._ensure_identity(facade)
        with self._lock:
            if epoch != self._epoch or self._closing:
                raise ServiceError('cancelled', '预演权限已撤销')
            draft, case, _ = self._validate_send(params['draft_id'], params['expected_revision'], manual=True)
            intent = self._intent(draft, case, new_id('preview'), epoch)
        result = facade.preview_send(intent)
        if epoch != self._epoch or self._closing:
            raise ServiceError('cancelled', '预演期间授权或设置已变化')
        with self._lock:
            self._validate_send(params['draft_id'], params['expected_revision'], manual=True)
        simulated = self.mode == 'demo'
        passed = result.get('status') == 'passed' and result.get('submission_started') is False
        self._set_check('send_preview', job_id, ('demo' if simulated else 'passed') if passed else 'blocked',
                        '本地模拟预演，未验证真实 Native' if simulated else ('原生预演通过，未提交发送' if passed else '原生预演未通过'), simulated)
        if not passed:
            failure = LiveIntegration._send_diagnostics(result)
            code = failure.get('error_code', 'preview_blocked')
            raise ServiceError(code, SNAPSHOT_ERROR_MESSAGES.get(code, '原生发送预演未通过'),
                               stage=failure.get('stage', 'send_preview'), diagnostics=failure.get('diagnostics'))
        return {'message': '演示预演完成' if simulated else '原生预演完成；未发送消息'}

    def _task_confirm_send(self, params, job_id, epoch, facade):
        self._ensure_identity(facade)
        with self._lock, self.repo.transaction():
            confirmation = self._require('confirmation', params['confirmation_id'])
            if (confirmation['invalidated'] or confirmation['used'] or confirmation['expires_at'] < now_ms()
                    or confirmation['epoch'] != self._epoch or not self._running):
                raise ServiceError('confirmation_expired', '发送复核已过期或授权已撤销', stage='send')
            draft, case, _ = self._validate_send(confirmation['draft_id'], confirmation['revision'], manual=True)
            if draft['body'] != confirmation['body'] or draft['source_message_id'] != confirmation['source_message_id']:
                raise ServiceError('confirmation_changed', '确切正文或来源已变化')
            confirmation['used'] = True
            self.repo.put('confirmation', confirmation['confirmation_id'], confirmation)
        return self._perform_send(draft, case, epoch, facade, confirmation=confirmation, job_id=job_id)

    def _task_automatic_send(self, params, job_id, epoch, facade):
        with self._lock, self.repo.transaction():
            waiting = self._reply_batch_preflight_wait(self._require('draft', params['draft_id']))
            if waiting:
                return waiting
        self._ensure_identity(facade)
        with self._lock:
            draft, case, _ = self._validate_send(params['draft_id'], params['expected_revision'])
            if (not self._auto_send
                    or not self._automatic_send_source(draft, case)):
                raise ServiceError('cancelled', '自动发送来源、范围或预算已失效')
        if draft.get('continuation') and not draft.get('batch_id'):
            with self._lock, self.repo.transaction():
                source = self._intent_source(draft, case)
                phase = 'supplement' if draft['continuation'].get('phase') == 'supplement' else 'final'
                plan = self._new_continuity_plan(case, source, phase, draft['body'], draft=draft)
                self._job_update(job_id, plan_id=plan['plan_id'])
            return self._task_continuity_send({'plan_id': plan['plan_id']}, job_id, epoch, facade)
        if len(draft.get('reply_messages', [])) > 1 and not draft.get('batch_id'):
            return self._begin_reply_batch(draft, case, epoch, job_id)
        return self._perform_send(draft, case, epoch, facade, automatic=True, job_id=job_id)

    def _perform_send(self, draft, case, epoch, facade, *, automatic=False, confirmation=None, job_id=None):
        if len(draft.get('reply_messages', [])) > 1 and not draft.get('batch_id'):
            raise ServiceError('invalid_send', '多条回复须通过分条发送批次，不能组合提交')
        with self._lock, self.repo.transaction():
            if epoch != self._epoch or not self._running or self._closing:
                raise ServiceError('cancelled', '接待权限已撤销', stage='send')
            if automatic:
                waiting = self._reply_batch_preflight_wait(draft)
                if waiting:
                    return waiting
            draft, case, _ = self._validate_send(draft['draft_id'], draft['revision'], manual=not automatic)
            self._intent_source(draft, case)
            if automatic:
                retry_state, _, retry_at = send_retry_decision(self.repo, self.seller_id, draft['conversation_id'], draft['source_message_id'],
                                                             **self._reply_batch_retry_options(draft))
                if retry_state in ('blocked', 'exhausted') or retry_state == 'waiting' and retry_at > now_ms():
                    raise ServiceError('send_retry_blocked', '当前消息的自动发送已停止或正在等待重试', stage='send')
                if (not self._auto_send
                        or not self._automatic_send_source(draft, case)):
                    raise ServiceError('cancelled', '自动发送来源、范围或预算已失效', stage='send')
                self._charge_usage()
            operation_id = new_id('send')
            operation = {'operation_id': operation_id, 'job_id': job_id, 'kind': 'send', 'seller_id': self.seller_id,
                         'buyer_id': case['buyer_id'], 'case_id': case['case_id'], 'case_revision': case['revision'],
                         'draft_id': draft['draft_id'], 'draft_revision': draft['revision'],
                         'source_conversation_revision': draft['source_conversation_revision'],
                         'conversation_id': draft['conversation_id'], 'source_message_id': draft['source_message_id'],
                         'status': 'sending', 'message': '等待原生最终检查和权威回执', 'updated_at': now_ms(),
                         'message_id': None, 'submission_started': False}
            intent = self._intent(draft, case, operation_id, epoch, automatic)
            self._reply_batch_attempt_started(draft, operation)
            batch_snapshot = None
            if draft.get('batch_id'):
                operation['intent'] = copy.deepcopy(intent)
                operation['batch_send_basis'] = {
                    'version': 1, 'operation_id': operation_id, 'batch_id': draft['batch_id'], 'part_id': draft['part_id']}
                batch_snapshot = self._reply_batch_send_snapshot(self._require('reply_batch', draft['batch_id']))
                # Save immutable evidence once, without duplicating all messages
                # into every operation receipt revision or exposing them in DTOs.
                self.repo.put('reply_batch_send_basis', operation_id,
                              {**operation['batch_send_basis'], 'snapshot': batch_snapshot})
            self.repo.put('operation', operation_id, operation, history=True)
            draft['status'] = 'sending'
            draft['updated_at'] = now_ms()
            self.repo.put('draft', draft['draft_id'], draft, history=True)
        def evaluate_gate():
            with self._lock:
                if self._closing:
                    return SendGateDecision(False, 'closing')
                if not self._running or epoch != self._epoch or not self.account_verified:
                    return SendGateDecision(False, 'authorization_revoked')
                current = self.repo.get('draft', draft['draft_id'])
                current_case = self.repo.get('case', case['case_id'])
                conversation = self.repo.get('conversation', draft['conversation_id'])
                if automatic and conversation and (conversation.get('human_owned') or
                        current_case and current_case['state'] == 'human_owned'):
                    return SendGateDecision(False, 'conversation_taken_over')
                if (not current or current['status'] != 'sending' or current['revision'] != draft['revision']
                        or current['body'] != draft['body'] or current['source_message_id'] != draft['source_message_id']
                        or not current_case or current_case['revision'] != case['revision']
                        or current_case['state'] == 'closed' or not conversation
                        or conversation['revision'] != draft['source_conversation_revision']):
                    return SendGateDecision(False, 'basis_changed')
                if confirmation:
                    saved = self.repo.get('confirmation', confirmation['confirmation_id'])
                    if not saved or now_ms() > saved['expires_at'] or saved['invalidated']:
                        return SendGateDecision(False, 'authorization_revoked')
                try:
                    source = self._intent_source(current, current_case)
                    context = with_direct_photo_reads(self.get_context(case['case_id']), draft.get('photo_read_images', []))
                except ServiceError:
                    return SendGateDecision(False, 'source_changed')
                if source['timestamp_ms'] != intent['source_timestamp_ms'] or self._stop_requested(source['text']):
                    return SendGateDecision(False, 'source_changed')
                if automatic:
                    if self.mode == 'live':
                        try:
                            self._assert_reception_model(current.get('model_snapshot'))
                        except ServiceError:
                            return SendGateDecision(False, 'authorization_revoked')
                    if (self._manual_only or not self._auto_send or self._frozen_scope != intent['frozen_scope']
                            or not 0 < intent['budget_sequence'] <= self._budget_used):
                        return SendGateDecision(False, 'authorization_revoked')
                    if not self._automatic_send_source(current, current_case, excluding=operation_id, allow_pending_sync=True):
                        return SendGateDecision(False, 'source_changed')
                if self._conversation_unresolved(draft['conversation_id'], excluding=operation_id):
                    return SendGateDecision(False, 'basis_changed')
                if any(o.get('purpose') != 'chat' and o['operation_id'] != operation_id and o['case_id'] == case['case_id']
                       and o['source_message_id'] == current['source_message_id']
                       and o['status'] in ('sending', 'accepted', 'unknown')
                       and not (draft.get('batch_id') and o.get('batch_id') == draft['batch_id']
                                and o.get('part_index', -1) < draft['part_index'] and o['status'] == 'accepted')
                       and not (draft.get('batch_id') and o.get('purpose') == 'continuity'
                                and o.get('intent', {}).get('continuity_phase') == 'ack' and o['status'] == 'accepted')
                       for o in self.repo.list('operation')):
                    return SendGateDecision(False, 'basis_changed')
                policies = {**{p['policy_id']: p['revision'] for p in context['knowledge']}, **context.get('policy_versions', {})}
                expected_photo = draft.get('photo_fingerprint')
                photo_current = (expected_photo == photo_fingerprint(context) if expected_photo is not None
                                 else not context['photo_context']['events'])
                valid = (photo_current and current.get('photo_fingerprint') == expected_photo
                        and self._related_cases_current(current)
                        and current.get('supplement_fingerprint') == draft.get('supplement_fingerprint')
                        and (draft.get('supplement_fingerprint') == supplement_fingerprint(context)
                             or draft.get('supplement_fingerprint') is None and not context['supplement'].get('body'))
                        and policies == draft.get('policy_versions', {})
                        and validate_reply(current['body'], current['validation']['sources'], context)['ready'])
                if not valid:
                    return SendGateDecision(False, 'basis_changed')
                if automatic and self._message_sync_pending(draft['conversation_id']):
                    activity = self.repo.get('message_activity', draft['conversation_id'], {})
                    return SendGateDecision(False, 'source_sync_pending', {
                        'sync_target_version': activity.get('version', 0),
                        'sync_target_timestamp': activity.get('latest_timestamp', 0), 'authorization_epoch': epoch})
                return SendGateDecision(True)
        authorized = SendAuthorization(evaluate_gate)
        try:
            sender = facade.send_chat if draft.get('batch_id') and intent.get('purpose') == 'continuity' else facade.send
            result = sender(intent, authorization=authorized)
            status = result.get('status', 'unknown')
            if status not in ('accepted', 'rejected', 'unknown'):
                status = 'unknown'
            if (status == 'accepted' and (not isinstance(result.get('message_id'), str)
                    or not result['message_id'].strip() or result.get('submission_started') is not True)
                    or status == 'rejected' and result.get('submission_started') is not False):
                status = 'unknown'
            submitted = status == 'unknown' or result.get('submission_started') is True
        except Exception:
            # The external process might have crossed submission before raising.
            result, status, submitted = {}, 'unknown', True
        failure = LiveIntegration._send_diagnostics(result)
        if status == 'rejected' and not failure:
            failure = {'error_code': 'adapter_error', 'stage': 'adapter',
                       'diagnostics': {'code': 'adapter_error', 'stage': 'adapter'}}
        with self._lock, self.repo.transaction():
            batch_basis_current = bool(draft.get('batch_id') and self._reply_batch_receipt_basis_current(
                self._require('reply_batch', draft['batch_id']), operation, result, batch_snapshot, check_sync=False))
            message = {'accepted': '平台已受理发送', 'rejected': '发送已明确拒绝',
                       'unknown': '发送结果未知，禁止自动重试'}[status]
            if status == 'rejected':
                message = SNAPSHOT_ERROR_MESSAGES.get(failure.get('error_code'), message)
            operation.update(status=status, message=message,
                             message_id=result.get('message_id'), submission_started=submitted, updated_at=now_ms(), **failure)
            self.repo.put('operation', operation_id, operation, history=True)
            draft.update(status=status, updated_at=now_ms())
            if automatic and status == 'rejected' and submitted is False:
                if self._draft_basis_current(draft):
                    retry_state, attempts, retry_at = send_retry_decision(
                        self.repo, self.seller_id, draft['conversation_id'], draft['source_message_id'],
                        **self._reply_batch_retry_options(draft))
                    draft.update(status='pending' if retry_state in ('waiting', 'waiting_sync') else retry_state, retry_after=retry_at)
                    if job_id:
                        self._job_update(job_id, retry_at=retry_at, retry_attempt=attempts, retry_status=retry_state)
                    settle_waiting_jobs(self.repo, self._job_update, self.seller_id,
                                        draft['conversation_id'], draft['source_message_id'], **self._reply_batch_retry_options(draft))
                else:
                    # A new buyer message or verified result may arrive while the
                    # native guard rejects the old attempt before submission.
                    draft.update(status='stale', retry_after=None)
                    if job_id:
                        self._job_update(job_id, retry_at=None, retry_status='superseded')
            self.repo.put('draft', draft['draft_id'], draft, history=True)
            if automatic and status in ('accepted', 'unknown'):
                settle_waiting_jobs(self.repo, self._job_update, self.seller_id,
                                    draft['conversation_id'], draft['source_message_id'], **self._reply_batch_retry_options(draft))
            if status == 'accepted':
                for previous in self.repo.list('job', conversation_id=draft['conversation_id']):
                    if (previous['job_id'] != job_id and previous.get('draft_id') == draft['draft_id']
                            and previous['status'] == 'deferred'):
                        self._job_update(previous['job_id'], status='cancelled', retry_at=None, retry_status='completed',
                                         message='后续尝试已受理，原未提交记录保留')
                current_case = self._require('case', case['case_id'])
                conversation = self._require('conversation', draft['conversation_id'])
                if (current_case['revision'] == case['revision'] and not conversation['human_owned']
                        and not draft.get('social_reply')):
                    if current_case['state'] == 'clarifying':
                        current_case['state'] = 'waiting_customer'
                for key in draft.get('question_keys', []):
                    current_case['questions'][key] = current_case['questions'].get(key, 0) + 1
                current_case.update(revision=current_case['revision'] + 1, updated_at=now_ms())
                self.repo.put('case', current_case['case_id'], current_case, history=True)
                if (not draft.get('social_reply') or current_case['facts']
                        or any(row['case_id'] == current_case['case_id'] for row in self.repo.list('outbox'))):
                    self._queue_outbox(current_case)
                # Outgoing bodies become history only after authoritative acceptance.
                msg = {'message_id': str(result['message_id']), 'seller_id': self.seller_id, 'buyer_id': case['buyer_id'],
                       'conversation_id': draft['conversation_id'], 'role': 'seller', 'text': draft['body'],
                       'timestamp_ms': (result['timestamp_ms'] if type(result.get('timestamp_ms')) is int
                                        and result['timestamp_ms'] > 0 else now_ms()), 'attachments': [], 'own': True}
                if (not draft.get('batch_id') or type(result.get('timestamp_ms')) is int and result['timestamp_ms'] > 0):
                    if not draft.get('batch_id') or self.repo.get('message', encoded([draft['conversation_id'], msg['message_id']])) is None:
                        self._ingest(conversation, [msg])
            self._reply_batch_receipt(operation, result, basis_current=batch_basis_current)
        self._changed(draft['conversation_id'], case['case_id'])
        job_message = operation['message']
        if automatic and status == 'rejected' and draft['status'] != 'stale':
            job_message = send_retry_message(retry_state, attempts, retry_at, now_ms())
        return {'_job_status': ('unknown' if status == 'unknown' else 'succeeded' if status == 'accepted' else
                               ('deferred' if draft['status'] == 'pending' else 'stale' if draft['status'] == 'stale' else 'failed') if automatic and submitted is False else 'failed'),
                'operation_id': operation_id, 'status': status, 'message': job_message,
                **({'error': {'code': failure['error_code'], 'message': operation['message']},
                    'stage': failure['stage'], 'diagnostics': failure['diagnostics']} if failure and status == 'rejected' else {})}

    def _outbox_write_allowed(self, epoch):
        return (epoch == self._epoch and not self._closing and not self._stop.is_set()
                and not self._manual_only and self.account_verified
                and (self.mode != 'live' or self.settings['feishu'].get('enabled') is True
                     and self.settings['feishu'].get('write_enabled') is True))

    def _task_run_outbox(self, params, job_id, epoch, facade):
        with self._lock:
            if epoch != self._epoch or self._closing or self._stop.is_set() or self._manual_only:
                raise ServiceError('cancelled', '后台同步权限已撤销')
        self._ensure_identity(facade)
        with self._lock:
            if not self._outbox_write_allowed(epoch):
                raise ServiceError('feishu_write_disabled', '飞书写入未启用；本地数据与同步队列已保留', stage='outbox')
            pending = [o for o in self.repo.list('outbox') if o['status'] in ('pending', 'failed')]
        failures = 0
        for queued in pending:
            with self._lock, self.repo.transaction():
                if not self._outbox_write_allowed(epoch):
                    raise ServiceError('cancelled', '后台同步权限已撤销')
                item = self._require('outbox', queued['operation_id'])
                if item['status'] not in ('pending', 'failed'):
                    continue
                self._receipt_owner(item, registration=True)
                case_items = self.repo.list('outbox', case_id=item['case_id'])
                if any(o['status'] in ('unknown', 'syncing') for o in case_items):
                    failures += 1
                    continue
                previous = self.repo.get('registration', item['case_id'], {})
                succeeded_revision = max([previous.get('case_revision', 0)] +
                                         [o['case_revision'] for o in case_items if o['status'] == 'succeeded'])
                if succeeded_revision > item['case_revision']:
                    item.update(status='superseded', updated_at=now_ms(), message='已有更高版本同步成功，已跳过旧版本')
                    self.repo.put('outbox', item['operation_id'], item, history=True)
                    continue
                item.update(status='syncing', updated_at=now_ms(), message='同步中')
                self.repo.put('outbox', item['operation_id'], item, history=True)
                snapshot = self._registration_snapshot(item['case_snapshot'])

            def authorized(item=copy.deepcopy(item)):
                with self._lock:
                    if not self._outbox_write_allowed(epoch) or facade is not self.integrations:
                        return False
                    current = self.repo.get('outbox', item['operation_id'])
                    if (not current or current['status'] != 'syncing' or current['case_id'] != item['case_id']
                            or current['case_revision'] != item['case_revision']
                            or current['case_snapshot'] != item['case_snapshot']):
                        return False
                    try:
                        self._receipt_owner(current, registration=True)
                    except ServiceError:
                        return False
                    others = self.repo.list('outbox', case_id=item['case_id'])
                    if any(o['operation_id'] != item['operation_id'] and o['status'] in ('unknown', 'syncing') for o in others):
                        return False
                    saved = self.repo.get('registration', item['case_id'], {})
                    return (saved.get('case_revision', 0) <= item['case_revision']
                            and not any(o['status'] == 'succeeded' and o['case_revision'] > item['case_revision'] for o in others))
            try:
                result = facade.register(snapshot, previous, item['operation_id'], authorization=authorized)
                status = self._registration_status(result)
            except ServiceError:
                result, status = {}, 'failed'
            except Exception:
                result, status = {}, 'unknown'
            with self._lock, self.repo.transaction():
                current = self.repo.get('outbox', item['operation_id'])
                if (current != item and status == 'failed' and result.get('submission_started') is False):
                    # A rejected, unsubmitted attempt cannot restore an item that
                    # was superseded or changed while native preparation was running.
                    failures += 1
                    continue
                # Revocation cannot erase a receipt from a submission already made.
                item.update(status=status, message={'succeeded': '已同步', 'failed': '同步失败，本地保存未受影响',
                                                    'unknown': '同步结果未知；保留原操作编号，不重建远端记录'}[status], updated_at=now_ms())
                self.repo.put('outbox', item['operation_id'], item, history=True)
                if status == 'succeeded':
                    self._store_registration(item, result)
                else:
                    failures += 1
        return {'_job_status': 'failed' if failures else 'succeeded', 'message': f'同步处理 {len(pending)} 项，未完成 {failures} 项'}

    def _task_reconcile_operation(self, params, job_id, epoch, facade):
        receipt = self.reconcile_operation(params['operation_id'])
        status = receipt.get('status', 'unknown')
        return {'_job_status': ('succeeded' if status in ('accepted', 'succeeded') else
                               'failed' if status in ('rejected', 'failed') else 'unknown'),
                'message': receipt.get('message', '原操作核查完成；未重新提交'),
                'operation_id': params['operation_id'], 'status': status}

    def _task_check_feishu(self, params, job_id, epoch, facade):
        self._ensure_identity(facade)
        result = facade.check_registration()
        if epoch != self._epoch or self._closing:
            raise ServiceError('cancelled', '结构检查期间授权或设置已变化')
        passed = result.get('status') in ('passed', 'succeeded', 'demo', 'ready')
        self._set_check('feishu', job_id, ('demo' if self.mode == 'demo' else 'passed') if passed else 'blocked',
                        '演示结构检查' if self.mode == 'demo' else ('飞书结构检查通过' if passed else '飞书结构未就绪'), self.mode == 'demo')
        if not passed:
            raise ServiceError('feishu_blocked', '飞书结构未配置或检查失败', stage='feishu')
        return {'message': '演示结构检查完成' if self.mode == 'demo' else '结构只读检查完成'}

    def _release_seller_after_drain(self):
        guard, self._seller_guard = self._seller_guard, None
        if guard is None:
            return
        def stopped():
            return (all(not thread or not thread.is_alive() for thread in
                        (getattr(self, '_worker', None), getattr(self, '_monitor_thread', None)))
                    and not getattr(self, '_generation_active', {}) and native_children_stopped())
        def release_when_stopped():
            while not stopped():
                time.sleep(0.1)
            guard.release()
        if stopped():
            guard.release()
        else:
            threading.Thread(target=release_when_stopped, name='seller-guard-drain', daemon=True).start()

    def close(self):
        with self._close_lock:
            return self._close_execution()

    def _close_execution(self):
        with self._lock:
            if self._closed:
                return
        self._pause(closing=True)
        self._stop_translation()
        self._image_transfer_close()
        # Pool shutdown may wait for an in-flight media reader. Keep its thread
        # and the workspace lock alive if it cannot drain within the deadline.
        media_thread = getattr(self, '_media_shutdown_thread', None)
        if media_thread is None:
            def stop_media():
                try:
                    self._stop_requirement_media()
                except BaseException as error:
                    self._media_shutdown_error = error
            media_thread = threading.Thread(target=stop_media, name='media-shutdown', daemon=True)
            self._media_shutdown_thread = media_thread
            media_thread.start()
        deadline = time.monotonic() + 10
        transfers = getattr(self, '_image_transfers', None)
        transfer_threads = list(transfers.pool._threads) if transfers is not None else []
        for thread in (media_thread, self._health_watchdog, self._monitor_thread, self._worker, *transfer_threads):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    self._executor_health.fail('stalled', 'service_shutdown_timeout')
                    raise ServiceError('executor_unavailable', '后台线程尚未退出，无法安全关闭。', stage='shutdown')
        if getattr(self, '_media_shutdown_error', None) is not None:
            self._executor_health.fail('failed', 'media_shutdown_failed')
            raise ServiceError('executor_unavailable', '图片任务清理失败，无法安全关闭。', stage='shutdown')
        self._join_generation_tasks()
        try:
            self._model_router.close()
        except ServiceError as error:
            if error.code in ('model_cleanup_timeout', 'model_cleanup_failed'):
                self._executor_health.fail('failed', error.code)
            raise
        deadline = time.monotonic() + 10
        while not native_children_stopped():
            if time.monotonic() >= deadline:
                self._executor_health.fail('stalled', 'native_shutdown_timeout')
                raise ServiceError('executor_unavailable', '后台子进程尚未退出，无法安全关闭。', stage='shutdown')
            time.sleep(0.1)
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    errors = []
                    for repository in (self.repo, self._control):
                        try:
                            repository.close()
                        except Exception as exc:
                            errors.append(exc)
                    if not errors:
                        try:
                            from .workspace_backup import _files, RECEIPTS
                            _files(self.data_root, receipt_roots=[str(self.data_root / RECEIPTS)])
                            self._workspace_lock.mark_clean_shutdown()
                        except Exception as exc:
                            errors.append(exc)
                    if errors:
                        raise errors[0]
                finally:
                    self._release_seller_after_drain()
                    self._workspace_lock.release()
