"""Public native integration facade with fixed, content-free failure messages."""
from __future__ import annotations

from copy import deepcopy
from functools import wraps
import json
import os
from pathlib import Path
import re
import threading
import time

from ..model_payload import model_json, model_request

from ..errors import ServiceError, VISUAL_STAGES, VISUAL_STATUSES
from .analysis import current_message, prepare_context, validate_analysis
from .accio_native.gateway import NativeGateway, account_path
from .accio_native.vendor.adapters.ai_runtime import desktop_agent_args
from .accio_native.vendor.adapters.common import AdapterError, target
from .accio_native.vendor.adapters.desktop_query import DesktopQueryRunner
from .accio_native.vendor.adapters.live import GATE_ERROR_MESSAGES, SEND_ERROR_STAGES, SNAPSHOT_ERROR_MESSAGES
from .accio_native.vendor.adapters.tool_signals import output_has_tools
from .accio_native.vendor.diagnostics import CODES, process_diagnostics

from .reception_prompts import AUTOMATIC, MANUAL, POLISH, REQUIREMENTS, WHATSAPP
from .generation_metadata import checked_metadata

AI_INSTRUCTIONS = AUTOMATIC


def boundary(stage):
    def decorate(method):
        @wraps(method)
        def invoke(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except ServiceError:
                raise
            except AdapterError as exc:
                code = exc.code if exc.code in CODES or exc.code == 'cancelled' else 'native_failed'
                diagnostics = process_diagnostics(exc.diagnostics)
                for key, allowed_values in (('visual_stage', VISUAL_STAGES), ('visual_status', VISUAL_STATUSES)):
                    value = getattr(exc, key, None)
                    if isinstance(value, str) and value in allowed_values:
                        diagnostics[key] = value
                if type(exc.exit_code) is int:
                    diagnostics['exit_code'] = exc.exit_code
                raise ServiceError(code, '原生集成未取得可核实结果，请检查当前连接及运行记录',
                                   stage=stage, diagnostics=diagnostics) from None
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, RecursionError):
                raise ServiceError('native_failed', '原生集成未取得可核实结果，请检查当前连接及运行记录', stage=stage) from None
        return invoke
    return decorate


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


class LiveIntegration:
    def __init__(self, data_dir: Path, settings: dict):
        # Runtime send grants can never be restored from persistent settings.
        self.settings = {key: deepcopy(value) for key, value in settings.items() if not key.startswith('_runtime_')}
        self.native = NativeGateway(self.settings, Path(data_dir))
        self._visual_workspace_identity = None
        self._query_state = threading.local()
        self._model_router = None
        self._whatsapp_image_resolver = None

    def configure_model_router(self, router):
        self._model_router = router

    def configure_whatsapp_image_resolver(self, resolver):
        self._whatsapp_image_resolver = resolver

    @property
    def last_ai_diagnostics(self):
        return getattr(self._query_state, 'diagnostics', {})

    @last_ai_diagnostics.setter
    def last_ai_diagnostics(self, value):
        self._query_state.diagnostics = value

    @property
    def _last_query_request(self):
        return getattr(self._query_state, 'request', {})

    @_last_query_request.setter
    def _last_query_request(self, value):
        self._query_state.request = value

    def capabilities(self):
        runtime = self.runtime_status()
        return dict(mode='live', simulated=False, demo=False, local_only=False,
                    verified=bool(self.native._seller_id), runtime_available=runtime['ok'],
                    workctl_configured=bool(self.settings.get('workctl_path')),
                    read=True, ai=True, send=True, send_enabled=False,
                    registration_enabled=self.native.feishu.enabled(),
                    message='真实模式：连接、AI、预演及发送分别核验；发信仅接受服务的本次临时授权')

    def runtime_status(self):
        return self.native.runtime_status()

    @boundary('identity')
    def resolve_identity(self, expected_seller_id=''):
        seller, _ = self.native._identity(expected_seller_id)
        return seller

    @boundary('read')
    def lookup_product(self, seller_id, product_id, request_id, *, cancel_event=None):
        from .product_lookup import read_product
        return read_product(self.native, seller_id, product_id, request_id, cancel_event=cancel_event)

    @boundary('read')
    def company_profile(self, seller_id):
        from .company_profile import read_company_profile
        return read_company_profile(self.native, seller_id)

    @boundary('read')
    def read_recent_conversations(self, seller_id, before_ms=None, count=20):
        return self.native.read_recent_conversations(seller_id, before_ms, count)

    @boundary('read')
    def read_message_page(self, seller_id, buyer_id, conversation_id, before_ms=None, count=100):
        return self.native.read_message_page(seller_id, buyer_id, conversation_id, before_ms, count)

    @boundary('read')
    def read_photo_source(self, message):
        return self.native.read_photo_source(deepcopy(message))

    @staticmethod
    def fetch_photo_source(source):
        from ..image_assets import fetch_image, ImageAssetError
        try:
            image = fetch_image(source['resource_url'])
        except ImageAssetError as error:
            failure = ServiceError('image_download_failed', '图片下载或内容校验未通过', stage=error.stage)
            failure.category = error.category
            raise failure from None
        return {**image, 'association': source['association']}

    @boundary('read')
    def load_photo(self, message):
        return self.fetch_photo_source(self.read_photo_source(message))

    @boundary('analysis')
    def observe_photo(self, message, asset, request_id, *, on_start):
        from .photo_vision import observe_photo
        target(message['seller_id'], message['buyer_id'], message['conversation_id'])
        self.resolve_identity(message['seller_id'])
        return observe_photo(self.native, self._agent_args()[1], self._visual_workspace_identity,
                             message, asset, request_id, on_start=on_start)

    def _agent_args(self):
        ai = self.settings.get('ai') or {}
        configured = ai.get('agent_id') if isinstance(ai, dict) else None
        if configured is not None:
            if not isinstance(configured, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', configured):
                raise ServiceError('agent_missing', '当前账号助手选择无效', stage='agent_selection')
            return ['--agent-id', configured]
        args = desktop_agent_args(self.native.cli)
        if not args:
            raise ServiceError('agent_missing', '当前授权账号缺少明确的国际站助手选择', stage='agent_selection')
        return args

    def _own_result_recovery_enabled(self):
        # Only recover from the account explicitly supplied by this host.
        cli_account = account_path(self.native.cli)
        supplied = [root for root in (account_path(os.environ.get('ACCIO_MCP_SCRIPT')),
                    account_path(os.environ.get('PHOENIX_PLUGIN_CLI_WORKCTL_PATH'))) if root is not None]
        return bool(cli_account and supplied and all(root == cli_account for root in supplied))

    @boundary('analysis')
    def read_product_display(self, seller_id, product_id, request_id, *, cancel_event=None):
        from .product_display import read_product_display
        return read_product_display(self.native, seller_id, product_id, request_id, cancel_event=cancel_event)

    def source_product(self, context, request_id, *, cancel_event=None, on_call_start=None, on_request_created=None):
        from .sourcing import run_source_product
        return run_source_product(self.native, self._agent_args()[1], context, request_id,
                                  cancel_event=cancel_event, on_call_start=on_call_start,
                                  on_request_created=on_request_created)

    def reconcile_source_product(self, record, context, *, cancel_event=None):
        from .sourcing import reconcile_source_product
        return reconcile_source_product(self.native, self._agent_args()[1], record, context, cancel_event=cancel_event)

    def generate(self, context, request_id, *, on_call_start=None, photo_inputs=None, cancel_event=None, photo_deadline=None):
        context, _, recent = prepare_context(context)
        case = context['case']
        target(case['seller_id'], case['buyer_id'], case['conversation_id'])
        self.resolve_identity(case['seller_id'])
        if not isinstance(request_id, str) or not request_id.strip():
            raise ServiceError('invalid_request', '生成请求缺少任务标识', stage='context')
        if current_message(context, recent)['role'] != 'buyer' or case.get('state') in ('human_owned', 'closed'):
            return dict(action='skip', facts={}, question_keys=[], reply='', summary='', missing=[], conflicts=[],
                        procurement_key=case.get('procurement_key') or 'main', new_case=False, reply_sources=[], reply_messages=[])
        result, image_reads = self._business_query(AI_INSTRUCTIONS, context, photo_inputs,
                                                  on_call_start=on_call_start, cancel_event=cancel_event,
                                                  photo_deadline=photo_deadline)
        if output_has_tools(result):
            raise ServiceError('tool_approval', 'AI 结果包含工具或审批信号，已阻断', stage='answer')
        if result.get('success') is False or result.get('ok') is False or result.get('error'):
            raise ServiceError('native_error', 'AI 请求未被原生服务确认成功', stage='answer')
        data = result.get('data') if isinstance(result.get('data'), dict) else result
        answer = next((data[key] for key in ('answer', 'finalAnswer', 'final_answer') if data.get(key) is not None), None)
        repaired_fence = False
        if isinstance(answer, str):
            answer = answer.strip()
            fence = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```', answer, re.I | re.S)
            repaired_fence = fence is not None
            if fence:
                answer = fence.group(1).strip()
            try:
                answer = json.loads(answer, object_pairs_hook=unique_object)
            except (ValueError, TypeError, RecursionError):
                raise ServiceError('answer_non_json', 'AI 未返回完整且唯一的 JSON 分析', stage='answer') from None
        if not isinstance(answer, dict):
            raise ServiceError('answer_missing', 'AI 未返回可核实的最终分析', stage='answer')
        from ..photos import with_direct_photo_reads
        validated = validate_analysis(answer, with_direct_photo_reads(context, image_reads))
        validated['verified_image_reads'] = image_reads
        validated.update(deepcopy(self._query_state.model_result))
        if repaired_fence:
            validated['repairs'].append('json_fence')
        # Workspace discovery is based only on this facade's own successful
        # synthetic AI capability check, never a scan of customer AI content.
        from ..synthetic_check import SYNTHETIC_BUYERS
        if (case.get('case_id') == 'synthetic-ai-check-case' and case.get('buyer_id') in SYNTHETIC_BUYERS
                and case.get('conversation_id', '').endswith('#synthetic-ai-check')
                and isinstance(getattr(self, '_last_query_request', None), dict)):
            self._visual_workspace_identity = deepcopy(self._last_query_request)
        return validated

    def _business_query(self, instructions, context, photo_inputs, *, on_call_start=None, cancel_event=None, photo_deadline=None):
        owner = getattr(photo_inputs, 'owner', None)
        try:
            return self._business_query_owned(instructions, context, photo_inputs, on_call_start=on_call_start,
                                              cancel_event=cancel_event, photo_deadline=photo_deadline)
        finally:
            if owner is not None:
                owner.close_if_owned()

    def _business_query_owned(self, instructions, context, photo_inputs, *, on_call_start=None, cancel_event=None, photo_deadline=None):
        now = time.monotonic()
        seconds = self.settings.get('image_prepare_seconds', 60)
        seconds = min(seconds, 60) if type(seconds) is int and 1 <= seconds <= 600 else 60
        started = min(now, getattr(photo_deadline, 'started', photo_deadline - seconds)) if photo_deadline is not None else now
        chain_deadline = started + 180
        photo_deadline = min(photo_deadline, started + 60) if photo_deadline is not None else started + 60
        if now >= chain_deadline:
            raise ServiceError('model_timeout', '模型请求超过时间预算', stage='model')
        self.last_ai_diagnostics = {}
        self._last_query_request = {}
        if cancel_event is not None and cancel_event.is_set():
            raise ServiceError('cancelled', '请求已取消', stage='analysis')
        from ..model_media import prepare_model_images
        if self._model_router is None:
            raise ServiceError('model_not_ready', '接待模型尚未配置', stage='model_configuration')
        from ..model_media import OwnedInput, estimate_input_reservation
        from ..media_resources import ResourceLimitError
        from ..model_router import ModelRouter
        owner = getattr(photo_inputs, 'owner', None)
        try:
            if owner is None:
                owner = OwnedInput(estimate_input_reservation(context, photo_inputs, instructions))
            else:
                owner.reserve_more(estimate_input_reservation(context, None, instructions))
        except ResourceLimitError as error:
            if owner is not None:
                owner.close_if_owned()
            raise ServiceError('model_resource_over_limit' if error.code == 'resource_too_large' else 'model_resource_busy',
                               '模型输入超出资源预算', stage='model') from None
        try:
            ownership = {'_owned_input': owner} if isinstance(self._model_router, ModelRouter) else {}
            payload = deepcopy(context)
            if isinstance(payload, dict) and payload.get('task_mode') == 'polish':
                # Frozen-body polish excludes historical chat photos. Explicit
                # conversation supplements remain applicable to this same request.
                payload['photo_context'] = {}
                supplement_ids = {ref.get('image_id') for ref in payload.get('supplement_image_refs', [])}
                payload['image_manifest'] = [row for row in payload.get('image_manifest', [])
                                             if row.get('origin') == 'business_supplement'
                                             and row.get('image_id') in supplement_ids]
                photo_inputs = [item for item in photo_inputs or []
                                if (item.get('attachment') or {}).get('origin') == 'business_supplement']
            settings = self.settings if isinstance(self.settings, dict) else {}
            batch_mib = settings.get('image_batch_mib', 64)
            if type(batch_mib) is not int or not 1 <= batch_mib <= 1024:
                batch_mib = 64
            if isinstance(payload, dict) and payload.get('task_mode') == 'whatsapp_reply':
                from ..model_inputs import resolve_whatsapp_images
                images, manifest = resolve_whatsapp_images(payload, getattr(self, '_whatsapp_image_resolver', None),
                                                           cancel=cancel_event, deadline=photo_deadline)
                payload.pop('model_image_refs', None)
                payload['image_manifest'] = manifest
                refs = []
            else:
                images, refs = prepare_model_images(payload, photo_inputs, cancel=cancel_event,
                                                    deadline=photo_deadline, batch_mib=batch_mib)
            if payload.get('task_mode') != 'whatsapp_reply':
                # Derive the public manifest from the validated bytes, never from
                # unencoded context entries that the model cannot actually see.
                payload['image_manifest'] = [
                    {**{'alias': image['alias'], 'image_id': image['image_id'], 'role': image['role']},
                     **({'origin': 'business_supplement', 'attachment_id': image['source_id']}
                        if ref.get('origin') == 'business_supplement' else {'message_id': image['source_id']})}
                    for image, ref in zip(images, refs)
                ]
            payload['image_transport'] = 'inline' if images else 'none'
            if images:
                from .reception_prompts import IMAGE_NATIVE
                instructions += '\n' + IMAGE_NATIVE
            required = ['text', 'structure']
            if images:
                required.extend(['single_image' if len(images) == 1 else 'multi_image', 'combined'])
            result = self._model_router.generate('reception', instructions, model_json(payload), images=images,
                        required_capabilities=required, cancel=cancel_event, on_call_start=on_call_start,
                        deadline=chain_deadline, **ownership)
            if not isinstance(result, dict) or not isinstance(result.get('output'), dict):
                raise ServiceError('answer_missing', 'AI 未返回可核实结果', stage='answer')
            evidence = result.get('image_input_evidence')
            if images:
                expected = [{key: image[key] for key in ('alias', 'image_id', 'source_id', 'source_version', 'role', 'sha256')} for image in images]
                if (not isinstance(evidence, dict) or evidence.get('status') != 'completed'
                        or evidence.get('images') != expected):
                    raise ServiceError('photo_input_unavailable', '图片请求缺少完整的发送证据', stage='analysis')
            self._query_state.model_result = {key: deepcopy(result.get(key)) for key in
                ('model_snapshot', 'image_input_evidence', 'usage', 'request_id', 'model', 'elapsed_ms', 'stop_reason')}
            return {'answer': result['output']}, refs
        finally:
            owner.close_if_owned()

    def _assistance_query(self, prompt, payload, request_id, on_call_start, cancel_event, *, photo_inputs=None, photo_deadline=None):
        if not isinstance(request_id, str) or not request_id.strip():
            raise ServiceError('invalid_request', '生成请求缺少任务标识', stage='context')
        if cancel_event is not None and cancel_event.is_set():
            raise ServiceError('cancelled', '请求已取消', stage='analysis')
        result, image_reads = self._business_query(prompt, payload, photo_inputs,
                                        on_call_start=on_call_start, cancel_event=cancel_event,
                                        photo_deadline=photo_deadline)
        if output_has_tools(result):
            raise ServiceError('tool_approval', 'AI 结果包含工具或审批信号，已阻断', stage='answer')
        if result.get('success') is False or result.get('ok') is False or result.get('error'):
            raise ServiceError('native_error', 'AI 请求未被原生服务确认成功', stage='answer')
        data = result.get('data') if isinstance(result.get('data'), dict) else result
        answer = next ( (data[k] for k in ('answer', 'finalAnswer', 'final_answer') if data.get(k) is not None), None)
        if isinstance(answer, str):
            answer = answer.strip()
            fence = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```', answer, re.I | re.S)
            try:
                answer = json.loads(fence.group(1) if fence else answer, object_pairs_hook=unique_object)
            except (ValueError, TypeError, RecursionError):
                raise ServiceError('answer_non_json', 'AI 未返回完整且唯一的 JSON', stage='answer') from None
        if not isinstance(answer, dict):
            raise ServiceError('answer_missing', 'AI 未返回可核实结果', stage='answer')
        answer['_host_image_reads'] = image_reads
        answer['_host_model_result'] = deepcopy(self._query_state.model_result)
        return answer

    @boundary('analysis')
    def generate_whatsapp(self, context, request_id, *, on_call_start=None, cancel_event=None):
        from .analysis import invalid_reply_body
        if not isinstance(context, dict) or context.get('task_mode') != 'whatsapp_reply':
            raise ServiceError('invalid_context', 'WhatsApp 上下文无效', stage='context')
        if self.resolve_identity(context.get('seller_id')) != context.get('seller_id'):
            raise ServiceError('seller_mismatch', '当前平台卖家身份已变化', stage='identity')
        answer = self._assistance_query(WHATSAPP, context, request_id, on_call_start, cancel_event)
        from ..reply_messages import normalize_reply_output
        try:
            answer = normalize_reply_output(answer)
        except ValueError:
            raise ServiceError('answer_non_json', 'AI 未返回有效 WhatsApp 回复', stage='answer') from None
        summary = answer.get('wa_summary')
        if (any(invalid_reply_body(item['body']) for item in answer['reply_messages'])
                or invalid_reply_body(answer['reply']) or not isinstance(summary, str) or len(summary) > 4000):
            raise ServiceError('answer_non_json', 'AI 未返回有效 WhatsApp 回复', stage='answer')
        return {key: answer[key] for key in ('reply', 'reply_sources', 'reply_messages')} | {'wa_summary': summary.strip()} | answer['_host_model_result']

    @boundary('analysis')
    def generate_manual(self, context, request_id, *, on_call_start=None, cancel_event=None, photo_inputs=None, photo_deadline=None):
        from .analysis import invalid_reply_body
        if not isinstance(context, dict):
            raise ServiceError('invalid_context', '建议上下文无效', stage='context')
        target(context.get('seller_id'), context.get('buyer_id'), context.get('conversation_id'))
        self.resolve_identity(context['seller_id'])
        prompt = POLISH if context.get('unsent_reply') else MANUAL
        answer = self._assistance_query(prompt, context, request_id, on_call_start, cancel_event,
                                        photo_inputs=photo_inputs, photo_deadline=photo_deadline)
        from ..reply_messages import normalize_reply_output
        if not context.get('unsent_reply'):
            try:
                answer = normalize_reply_output(answer)
                if len(answer['reply_messages']) != 1:
                    raise ValueError('manual suggestions require exactly one message')
            except ValueError:
                raise ServiceError('answer_non_json', 'AI 未返回有效回复正文', stage='answer') from None
            if any(invalid_reply_body(item['body']) for item in answer['reply_messages']):
                raise ServiceError('answer_non_json', 'AI 未返回有效回复正文', stage='answer')
        reply = answer.get('reply')
        if not isinstance(reply, str) or not reply.strip() or (context.get('unsent_reply') and len(reply) > 1200) or invalid_reply_body(reply):
            raise ServiceError('answer_non_json', 'AI 未返回有效回复正文', stage='answer')
        from ..photos import with_direct_photo_reads
        image_reads = answer.pop('_host_image_reads', [])
        checked_context = with_direct_photo_reads(context, image_reads)
        metadata = checked_metadata(answer, checked_context, manual=True)
        if context.get('unsent_reply'):
            metadata.pop('assistance_suggestions', None)
        metadata.pop('quality_notes', None)
        metadata.pop('assistance_suggestions', None)
        if not context.get('unsent_reply'):
            from ..requirement_changes import validate_changes
            validate_changes(answer, checked_context)
            for item in answer['reply_messages']:
                item['reply_sources'] = checked_metadata({**answer, 'reply_sources': item['reply_sources']}, checked_context, manual=True)['reply_sources']
            answer = normalize_reply_output(answer)
            from ..context import validate_reply
            for item in answer['reply_messages']:
                if not validate_reply(item['body'], item['reply_sources'], checked_context)['ready']:
                    raise ServiceError('answer_non_json', 'AI 未返回有效回复正文', stage='answer')
            validation = validate_reply(answer['reply'], answer['reply_sources'], checked_context)
            if not validation['ready'] and not (len(answer['reply']) > 1200 and validation['issues'] == ['回复正文格式无效']):
                raise ServiceError('answer_non_json', 'AI 未返回有效回复正文', stage='answer')
            metadata['reply_sources'] = answer['reply_sources']
            reply = answer['reply']
        return {'reply': reply if not context.get('unsent_reply') else reply.strip(), 'verified_image_reads': image_reads, **metadata, **answer['_host_model_result'],
                **({'reply_messages': answer['reply_messages']} if not context.get('unsent_reply') else {}),
                **({key: answer[key] for key in ('requirement_changes', 'reply_case_refs', 'conversation_summary') if key in answer}
                   if not context.get('unsent_reply') else {})}

    @boundary('analysis')
    def generate_requirements(self, context, request_id, *, on_call_start=None, cancel_event=None, photo_inputs=None, photo_deadline=None):
        if not isinstance(context, dict):
            raise ServiceError('invalid_context', '需求上下文无效', stage='context')
        case = context.get('case') or context
        target(case.get('seller_id'), case.get('buyer_id'), case.get('conversation_id'))
        self.resolve_identity(case['seller_id'])
        payload = dict(context, task_mode='requirements')
        if not photo_inputs:
            # Requirement candidates are grounded in buyer text; without an
            # explicitly prepared image request do not advertise unread images.
            payload.update(photo_context={}, image_manifest=[], image_transport='none')
            payload.pop('message_view', None)
        answer = self._assistance_query(REQUIREMENTS, payload, request_id, on_call_start, cancel_event,
                                        photo_inputs=photo_inputs, photo_deadline=photo_deadline)
        from ..requirement_changes import validate_changes
        from ..photos import with_direct_photo_reads
        validate_changes(answer, with_direct_photo_reads(context, answer.pop('_host_image_reads', [])))
        return {key: answer[key] for key in ('requirement_changes', 'reply_case_refs', 'conversation_summary') if key in answer} | answer['_host_model_result']

    @boundary('analysis')
    def translate(self, items, request_id, *, on_call_start=None, cancel_event=None):
        from .assistance_prompts import TRANSLATE
        if (not isinstance(items, list) or not items or any(not isinstance(row, dict) or
                not isinstance(row.get('key'), str) or not row['key'] or
                not isinstance(row.get('text'), str) or not row['text'] for row in items)):
            raise ServiceError('invalid_request', '翻译来源无效', stage='context')
        keys = [row['key'] for row in items]
        if len(set(keys)) != len(keys):
            raise ServiceError('invalid_request', '翻译来源重复', stage='context')
        answer = self._assistance_query(TRANSLATE, items, request_id, on_call_start, cancel_event)
        rows = answer.get('items')
        if (not isinstance(rows, list) or len(rows) != len(keys) or any(not isinstance(row, dict) or
                not isinstance(row.get('key'), str) or not isinstance(row.get('text'), str) or not row['text'].strip()
                for row in rows) or {row['key'] for row in rows} != set(keys)
                or len({row['key'] for row in rows}) != len(rows)):
            raise ServiceError('answer_non_json', '翻译结果与来源不匹配', stage='answer')
        by_key = {row['key']: row['text'] for row in rows}
        return {'items': [{'key': key, 'text': by_key[key]} for key in keys]}

    def preview_send(self, intent):
        if not self.runtime_status()['ok']:
            return dict(status='blocked', message='当前授权会话或原生组件未就绪，不能预演', submission_started=False)
        try:
            result = self.native.preview_send(deepcopy(intent))
            passed = result.get('status') == 'passed'
            failure = self._send_diagnostics(result)
            message = ('本次原生快照和封装预演通过，未提交发送' if passed else
                       SNAPSHOT_ERROR_MESSAGES.get(failure.get('error_code'), '原生预演未通过，请核对身份及最新消息'))
            return dict(status='passed' if passed else 'failed', submission_started=False, message=message, **failure)
        except (ServiceError, AdapterError, OSError, ValueError, TypeError, KeyError, AttributeError):
            return dict(status='failed', submission_started=False, message='原生预演未通过，请核对身份及最新消息')

    @staticmethod
    def _send_diagnostics(result):
        code, stage = result.get('error_code'), result.get('stage')
        if not isinstance(code, str) or (code not in CODES and code not in SNAPSHOT_ERROR_MESSAGES):
            return {}
        stage = stage if isinstance(stage, str) and stage in SEND_ERROR_STAGES else 'adapter'
        source = result.get('diagnostics')
        diagnostics = process_diagnostics(source)
        if isinstance(source, dict) and type(source.get('exit_code')) is int:
            diagnostics['exit_code'] = source['exit_code']
        diagnostics.update(code=code, stage=stage)
        return dict(error_code=code, stage=stage, diagnostics=diagnostics)

    @staticmethod
    def _send_result(result):
        status = result.get('status')
        status = status if status in ('accepted', 'rejected', 'unknown') else 'unknown'
        if status == 'rejected' and result.get('submission_started') is not False:
            status = 'unknown'
        messages = {'accepted': '原生已确认发送并取得消息编号', 'rejected': '身份、来源、预演或本次发送权限未通过',
                    'unknown': '尚未取得权威发送回执，保留原操作编号，禁止重发'}
        failure = LiveIntegration._send_diagnostics(result)
        message = messages[status]
        if status == 'rejected':
            code = failure.get('error_code')
            message = GATE_ERROR_MESSAGES.get(code, SNAPSHOT_ERROR_MESSAGES.get(code, message))
        mid = result.get('message_id')
        receipt = dict(status=status, message_id=mid if isinstance(mid, str) else '',
                       submission_started=result.get('submission_started') is True or status == 'unknown', message=message, **failure)
        if type(result.get('timestamp_ms')) is int and result['timestamp_ms'] > 0:
            receipt['timestamp_ms'] = result['timestamp_ms']
        return receipt

    def send(self, intent, authorization):
        # Do not convert unexpected exceptions after a possible submission into a
        # retryable rejection. The durable native ledger remains authoritative.
        try:
            result = self.native.send(deepcopy(intent), authorization)
        except (ServiceError, AdapterError, OSError, ValueError, TypeError, KeyError, AttributeError):
            record = self.native.ledger.get(intent.get('operation_id')) if isinstance(intent, dict) else None
            result = record[2] if record else dict(status='rejected', submission_started=False)
        return self._send_result(result)

    def send_chat(self, intent, authorization):
        result = self.native.send_chat(deepcopy(intent), authorization)
        receipt = self._send_result(result)
        if type(result.get('timestamp_ms')) is int and result['timestamp_ms'] > 0:
            receipt['timestamp_ms'] = result['timestamp_ms']
        return receipt

    @staticmethod
    def _registration_result(result):
        status = result.get('status')
        status = status if status in ('disabled', 'blocked', 'rejected', 'unknown', 'confirmed', 'ready') else 'unknown'
        messages = {'disabled': '飞书未完整配置或写入已关闭；需求保存在本机', 'blocked': '飞书配置、字段或本次授权尚未就绪',
                    'rejected': '飞书登记的字段或买家原文证据未通过，未写入', 'unknown': '飞书登记结果尚未核实，保留原操作并禁止重建行',
                    'confirmed': '已按原表单和目标行读取核实飞书登记', 'ready': '飞书字段和原表单只读检查通过，未写入'}
        value = dict(status=status, message=messages[status],
                     submission_started=result.get('submission_started') is True or status == 'unknown')
        for key in ('record_id', 'number'):
            if type(result.get(key)) in (str, int):
                value[key] = result[key]
        return value

    def register(self, case, previous, operation_id, *, authorization=None):
        if not self.native.feishu.enabled():
            return self._registration_result(dict(status='disabled', record_id=previous.get('record_id', ''), number=previous.get('number', '')))
        try:
            self.resolve_identity(case.get('seller_id'))
            native_case = deepcopy(case)
            native_case['version'] = native_case.get('revision', native_case.get('version'))
            # Internal core metadata, not a new public Case field. Core must bind
            # this ID once when the case starts; fact evidence may change later.
            source_id = native_case.pop('_inquiry_source_id', native_case.get('inquiry_source_id', ''))
            native_case['inquiry_source_id'] = source_id
            native_case.pop('inquiry_timestamp_ms', None)
            native_case['source_message_ids'] = list(dict.fromkeys([
                mid for fact in (native_case.get('facts') or {}).values() for mid in fact.get('evidence_ids', [])]
                + ([source_id] if isinstance(source_id, str) and source_id else [])))
            result = self.native.register(dict(case=native_case, previous=deepcopy(previous), operation_id=operation_id),
                                          authorization=authorization)
            return self._registration_result(result)
        except (ServiceError, AdapterError, OSError, ValueError, TypeError, KeyError, AttributeError):
            record = self.native.ledger.get(operation_id) if isinstance(operation_id, str) else None
            return self._registration_result(record[2] if record else dict(status='blocked'))

    def check_registration(self):
        cfg = self.settings.get('feishu') or {}
        if not cfg:
            return self._registration_result(dict(status='disabled'))
        try:
            self.resolve_identity(self.settings.get('seller_id') or '')
            return self._registration_result(self.native.check_registration_schema())
        except (ServiceError, AdapterError, OSError, ValueError, TypeError, KeyError, AttributeError):
            return self._registration_result(dict(status='blocked'))

    def revoke(self):
        self.native.revoke()

    @boundary('reconcile')
    def reconcile_operation(self, operation_id):
        record = self.native.ledger.get(operation_id)
        if not record:
            return dict(status='unknown', message='没有该操作的权威回执，禁止重放')
        if record[0] == 'send':
            if record[1].get('purpose') in ('chat', 'continuity', 'reception'):
                return self._send_result(self.native.reconcile_chat(operation_id))
            return self._send_result(self.native.reconcile(operation_id))
        self.resolve_identity(self.settings.get('seller_id') or '')
        return self._registration_result(self.native.reconcile(operation_id))
