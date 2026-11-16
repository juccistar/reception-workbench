"""Page-based native reads and revocable native send permission."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import threading

from ...errors import ServiceError
from .vendor.adapters.common import AdapterError, digest, require_registration_authorization, stamp, target
from .vendor.adapters.live import LiveGateway, _content

MAX_CURSOR = 9007199254740991
RUNTIME_VARIABLES = ('ACCIO_GATEWAY_TOKEN', 'ACCIO_NODE_BIN', 'ACCIO_MCP_SCRIPT')


def account_path(path):
    """Only inspect a supplied path; never enumerate accounts or read their files."""
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
        return next((parent for parent in resolved.parents if parent.parent.name.casefold() == 'accounts'), None)
    except (OSError, ValueError, RuntimeError):
        return None


class NativeGateway(LiveGateway):
    def __init__(self, settings, data_dir):
        super().__init__(settings, data_dir)
        self._seller_id = ''
        self._permission_lock = threading.Lock()
        self._permission_epoch = 0
        self._attempt_epoch = None
        self._authorization = None
        self._active_intent = None

    def runtime_status(self):
        missing = [name for name in RUNTIME_VARIABLES if not os.environ.get(name, '').strip()]
        if not self.settings.get('workctl_path'):
            missing.append('WORKCTL_PATH')
        invalid = False
        try:
            invalid = any(not Path(os.environ[name]).is_file() for name in RUNTIME_VARIABLES[1:] if name not in missing)
            cli = self.settings.get('workctl_path')
            if cli and (not isinstance(cli, str) or not Path(cli).is_file()):
                invalid = True
        except (OSError, TypeError, ValueError):
            invalid = True
        cli_account = account_path(self.settings.get('workctl_path'))
        mcp_account = account_path(os.environ.get('ACCIO_MCP_SCRIPT'))
        host_account = account_path(os.environ.get('PHOENIX_PLUGIN_CLI_WORKCTL_PATH'))
        host_supplied = bool(os.environ.get('PHOENIX_PLUGIN_CLI_WORKCTL_PATH', '').strip())
        supplied = mcp_account or host_account
        mismatch = (not supplied or not cli_account or cli_account != supplied
                    or (host_supplied and (not host_account or host_account != supplied)))
        ok = not missing and not invalid and not mismatch
        message = ('当前会话未授权或缺少原生运行组件' if missing else '原生运行组件路径无效' if invalid
                   else '原生工具不属于同一 Accio 账号' if mismatch else '运行组件存在，仍需本次只读核验卖家身份')
        return dict(ok=ok, missing_variables=missing, message=message, simulated=False)

    def require_runtime(self):
        if not self.runtime_status()['ok']:
            raise ServiceError('runtime_blocked', '当前 Accio 授权会话或运行组件未就绪', stage='runtime')

    def _identity(self, expected=None, *, identity_file=None):
        self.require_runtime()
        if expected and self._seller_id and expected != self._seller_id:
            raise ServiceError('identity_mismatch', '本次服务已绑定其他卖家，不能切换账号', stage='identity')
        seller, identity_sha = super()._identity(expected or self._seller_id or None, identity_file=identity_file)
        if self._seller_id and seller != self._seller_id:
            raise ServiceError('identity_mismatch', '当前授权卖家已变化，必须重新启动工作台', stage='identity')
        self._seller_id = seller
        self.settings['seller_id'] = seller
        return seller, identity_sha

    def _read(self, name, request):
        self.require_runtime()
        return super()._read(name, request)

    def _wc(self, *args, before_submit=None):
        self.require_runtime()
        if args and args[0] == 'send-message':
            # Check before marking submission so a revoked grant is a definite
            # rejection, while process errors remain ambiguous after invocation.
            def submit():
                self._send_authorized(self._active_intent or {})
                if before_submit is not None:
                    before_submit()
            return super()._wc(*args, before_submit=submit)
        return super()._wc(*args, before_submit=before_submit)

    @staticmethod
    def page_args(before_ms, count):
        if type(count) is not int or not 1 <= count <= 100:
            raise ServiceError('invalid_page', '分页数量必须在允许范围内', stage='read')
        if before_ms is not None and (type(before_ms) is not int or not 0 < before_ms <= MAX_CURSOR):
            raise ServiceError('invalid_cursor', '分页游标无效', stage='read')
        return MAX_CURSOR if before_ms is None else before_ms

    @staticmethod
    def next_cursor(body, key, rows, cursor):
        if type(body.get('hasMore')) is not bool:
            raise ServiceError('invalid_page', '原生分页结果缺少明确的后续状态', stage='read')
        if not body['hasMore']:
            return None
        value = stamp(body.get(key))
        if not rows or value >= cursor:
            raise ServiceError('invalid_cursor', '原生分页游标未向历史推进', stage='read')
        return value

    def read_recent_conversations(self, seller_id, before_ms=None, count=20):
        cursor = self.page_args(before_ms, count)
        self._identity(seller_id)
        request = dict(count=count, domain='icbu', selfAliId=int(seller_id))
        # The first page uses the server's current boundary. A synthetic maximum
        # timestamp is not a real pagination cursor and need not behave as "now".
        if before_ms is not None:
            request['limitTimeStamp'] = cursor
        body = self._read('query_recent_conversation', request)
        rows = body.get('conversations')
        if not isinstance(rows, list):
            raise ServiceError('invalid_page', '原生会话列表结构无效', stage='read')
        items = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or str(row.get('sellerAliId')) != seller_id:
                raise ServiceError('identity_mismatch', '原生会话列表不属于当前卖家', stage='read')
            buyer, cid = str(row.get('contactAliId') or ''), row.get('conversationId')
            target(seller_id, buyer, cid)
            if cid in seen:
                raise ServiceError('invalid_page', '原生会话列表包含重复会话', stage='read')
            seen.add(cid)
            customer_name = row.get('contactName') or row.get('customerName') or ''
            if not isinstance(customer_name, str):
                raise ServiceError('invalid_page', '原生客户名称结构无效', stage='read')
            latest = row.get('latestMessage')
            if latest is not None and not isinstance(latest, dict):
                raise ServiceError('invalid_page', '原生最新消息摘要结构无效', stage='read')
            activity_at = stamp(row.get('latestMessageTimestamp') or row.get('lastMessageTime'))
            # Exclude unread/read flags: reading a chat should not cause another
            # body fetch. Stable message fields also catch changes within one ms.
            signature = digest(dict(activity_at=activity_at,
                                    latest={key: latest[key] for key in ('messageId', 'timestamp', 'sendTime', 'content',
                                                                       'senderAliId', 'quoteMessage')
                                            if isinstance(latest, dict) and key in latest}))
            item = dict(conversation_id=cid, buyer_id=buyer, customer_name=customer_name,
                        latest_timestamp=activity_at, activity_signature=signature, activity_clock='conversation')
            if isinstance(latest, dict):
                mid = latest.get('messageId')
                if type(mid) in (str, int) and str(mid):
                    item['latest_message_id'] = str(mid)
                    if 'content' in latest:
                        item['latest_text_sha256'] = hashlib.sha256(_content(latest['content']).encode('utf-8')).hexdigest()
            items.append(item)
        next_value = self.next_cursor(body, 'nextTimeStamp', rows, cursor)
        # Preserve native page order. A timestamp alone proves no global sort.
        return dict(items=items, has_more=body['hasMore'], next_cursor=next_value)

    def read_message_page(self, seller_id, buyer_id, conversation_id, before_ms=None, count=100):
        cursor = self.page_args(before_ms, count)
        target(seller_id, buyer_id, conversation_id)
        self._identity(seller_id)
        body = self._read('query_conversation_msg', dict(conversationId=conversation_id, selfAliId=int(seller_id),
                          domain='icbu', count=count, limitTimeStamp=cursor, forward=False))
        messages = self._messages(body, seller_id, buyer_id, conversation_id)
        rows = []
        for message in messages:
            if message.role not in ('buyer', 'seller') or message.message_id.startswith('missing-id-'):
                raise ServiceError('message_unverified', '原生消息缺少可核实的方向或消息编号', stage='read')
            rows.append(dict(message_id=message.message_id, seller_id=seller_id, buyer_id=buyer_id,
                conversation_id=conversation_id, role=message.role, text=message.text, timestamp_ms=message.timestamp_ms,
                attachments=message.attachments, own=message.role == 'seller'))
        return dict(messages=rows, has_more=body['hasMore'],
                    next_cursor=self.next_cursor(body, 'nextPointTimeStamp', messages, cursor))

    def read_photo_source(self, expected_message: dict):
        from .media import read_photo_source
        return read_photo_source(self, expected_message)

    def _send_authorized(self, intent, checkpoint='before_claim'):
        from ...send_gate import SendGateDecision, evaluate_authorization
        from .chat import reject_decision
        with self._permission_lock:
            revoked = self._attempt_epoch != self._permission_epoch
            callback = self._authorization
        decision = (SendGateDecision(False, 'authorization_revoked') if revoked
                    else evaluate_authorization(callback, checkpoint))
        # Revocation may run while the service callback checks its own state.
        with self._permission_lock:
            revoked = self._attempt_epoch != self._permission_epoch
        if revoked:
            decision = SendGateDecision(False, 'authorization_revoked')
        if not decision.allowed:
            reject_decision(decision, checkpoint,
                            token_acquired=True if checkpoint in ('after_token', 'before_submit') else None)
        mode = intent.get('send_mode')
        if mode not in ('manual', 'automatic'):
            raise AdapterError('发送方式未明确', code='auth', stage='send')
        if mode == 'automatic':
            scope = intent.get('frozen_scope')
            if (not isinstance(scope, dict) or scope.get('mode') not in ('selected', 'all', 'incoming')
                    or intent.get('budget_reserved') is not True
                    or (scope['mode'] == 'selected' and (not isinstance(scope.get('conversation_ids'), list)
                        or intent.get('conversation_id') not in scope['conversation_ids']))):
                raise AdapterError('自动发送范围或预留额度未就绪', code='auth', stage='send')
            if scope['mode'] == 'incoming':
                started_at = scope.get('started_at_ms')
                source_at = intent.get('source_timestamp_ms')
                seller = intent.get('seller_id')
                if (not isinstance(seller, str) or not seller or scope.get('seller_id') != seller
                        or type(started_at) is not int or not 0 < started_at <= MAX_CURSOR
                        or type(source_at) is not int or not started_at <= source_at <= MAX_CURSOR):
                    raise AdapterError('新消息自动发送账号或授权起点不匹配', code='auth', stage='send')

    def _source(self, intent, history):
        source = super()._source(intent, history)
        scope = intent.get('frozen_scope')
        if (intent.get('send_mode') == 'automatic' and isinstance(scope, dict)
                and scope.get('mode') == 'incoming'
                and source.timestamp_ms != intent.get('source_timestamp_ms')):
            raise AdapterError('新消息来源时间与原生消息不一致', code='auth', stage='send')
        return source

    def send(self, intent, authorization):
        if not callable(authorization):
            return dict(status='rejected', message_id='', submission_started=False)
        with self.lock:
            with self._permission_lock:
                self._attempt_epoch = self._permission_epoch
                self._authorization = authorization
                self._active_intent = deepcopy(intent)
            try:
                if intent.get('send_mode') == 'automatic':
                    from .chat import execute_chat
                    # Old snapshot operations keep their original request and receipt.
                    # Replaying one must never turn an unknown submit into a new send.
                    previous = self.ledger.get(intent.get('operation_id'))
                    if previous and previous[0] == 'send' and previous[1] == self._active_intent:
                        return previous[2]
                    direct = deepcopy(self._active_intent)
                    direct.update(purpose='reception')
                    direct.setdefault('client_request_id', direct.get('operation_id'))
                    return execute_chat(self, direct, authorization)
                # Explicit legacy manual confirmation/preview remains compatible;
                # ordinary human chat already uses send_chat directly.
                return self._execute_send(self._active_intent)
            finally:
                with self._permission_lock:
                    self._authorization = self._attempt_epoch = self._active_intent = None

    def _chat_call(self, name, arguments, *, before_submit=None):
        from .chat import call_chat_tool
        return call_chat_tool(self, name, arguments, before_submit=before_submit)

    def send_chat(self, intent, authorization):
        # Manual chat remains independent of automatic reception revocation.
        from .chat import execute_chat
        if intent.get('purpose') != 'continuity':
            return execute_chat(self, intent, authorization)
        with self._permission_lock:
            epoch = self._permission_epoch
        with self.lock:
            with self._permission_lock:
                self._attempt_epoch = epoch
                self._authorization = authorization
                self._active_intent = deepcopy(intent)
            try:
                return execute_chat(self, self._active_intent, authorization)
            finally:
                with self._permission_lock:
                    self._authorization = self._attempt_epoch = self._active_intent = None

    def reconcile_chat(self, operation_id):
        from .chat import reconcile_chat
        return reconcile_chat(self, operation_id)

    def register(self, payload, *, authorization=None):
        # This closure stays outside business payloads, hashes and durable rows.
        # Capture before waiting for the gateway lock or reading platform history.
        with self._permission_lock:
            epoch = self._permission_epoch
        def authorized():
            with self._permission_lock:
                if epoch != self._permission_epoch:
                    return False
            try:
                require_registration_authorization(authorization)
            except AdapterError:
                return False
            with self._permission_lock:
                return epoch == self._permission_epoch
        case = payload.get('case') or {}
        owner = self.feishu._owner(payload.get('operation_id')) if hasattr(self.feishu, '_owner') else self.feishu
        from ..feishu_open_adapter import FeishuOpenAdapter
        if case.get('requirements_format') != 'ai-v1' or not isinstance(owner, FeishuOpenAdapter):
            return super().register(payload, authorization=authorized)
        from ..feishu_requirements import verify_requirements
        with self.lock:
            case = dict(case)
            source_id = case.get('inquiry_source_id')
            ids = []
            for row in (case.get('requirements') or {}).values():
                if isinstance(row, dict) and isinstance(row.get('evidence_ids'), list):
                    ids.extend(row['evidence_ids'])
            if isinstance(source_id, str) and source_id:
                ids.append(source_id)
            case['source_message_ids'] = list(dict.fromkeys(ids))
            case.pop('inquiry_timestamp_ms', None)
            operation = payload.get('operation_id')
            old = self.ledger.get(operation) if operation else None
            if old:
                stored = (old[1].get('payload') or {}).get('case') or {}
                if source_id == stored.get('inquiry_source_id') and 'inquiry_timestamp_ms' in stored:
                    case['inquiry_timestamp_ms'] = stored['inquiry_timestamp_ms']
                return self.feishu.register(dict(payload, case=case), authorization=authorized)
            previous = payload.get('previous') or {}
            if previous.get('status') == 'unknown' or not self.feishu.enabled():
                return self.feishu.register(dict(payload, case=case), authorization=authorized)
            try:
                require_registration_authorization(authorized)
                history = self.history(case.get('seller_id'), case.get('buyer_id'), case.get('conversation_id'))
                require_registration_authorization(authorized)
                selected = [m for m in history if m.message_id in ids]
                verified = [m.to_dict() for m in selected]
                verify_requirements(case, verified)
                if case.get('customer_name') and not any(m.customer_name == case['customer_name'] for m in history):
                    raise AdapterError('客户名称未在当前接口核实')
                if source_id:
                    origin = [m for m in selected if m.message_id == source_id and m.role == 'buyer'
                              and m.seller_id == case.get('seller_id') and m.buyer_id == case.get('buyer_id')
                              and m.conversation_id == case.get('conversation_id') and type(m.timestamp_ms) is int and m.timestamp_ms > 0]
                    if len(origin) != 1:
                        raise AdapterError('原始询单来源未核实')
                    case['inquiry_timestamp_ms'] = origin[0].timestamp_ms
                require_registration_authorization(authorized)
                return self.feishu.register(dict(payload, case=case, verified_messages=verified), authorization=authorized)
            except (AdapterError, ServiceError, OSError, ValueError, KeyError, TypeError):
                return dict(status='rejected', record_id=previous.get('record_id') or '', number=previous.get('number') or '',
                            case_id=previous.get('case_id') or case.get('case_id') or '', submission_started=False,
                            detail='登记权限或买家需求来源未核实，未写表')

    def revoke(self):
        # Never acquire the gateway's network/ledger lock while pausing.
        with self._permission_lock:
            self._permission_epoch += 1
