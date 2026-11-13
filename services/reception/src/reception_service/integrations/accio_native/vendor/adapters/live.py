"""Native protocol extracted from reception_app/adapters/live.py; see SOURCE.txt."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from ..models import Message
from ..diagnostics import CODES, process_diagnostics, safe_snapshot_message_id
from .common import AdapterError, Ledger, Process, digest, field, require_registration_authorization, stamp, target
from .feishu import FeishuAdapter
from .send_receipts import inventory, stdout_evidence, reconcile_receipt

SNAPSHOT_ERROR_MESSAGES = {
    'snapshot_schema_invalid': '原生快照消息结构无效',
    'snapshot_identity_mismatch': '原生快照消息身份不匹配',
    'snapshot_time_invalid': '原生快照消息时间无效',
    'snapshot_time_unverified': '快照时间缺少时区，且无法按消息编号核实平台时间',
    'snapshot_message_mismatch': '快照消息与平台消息不一致',
    'snapshot_message_ambiguous': '快照消息编号重复或多义',
    'snapshot_source_mismatch': '快照来源时间或原文不一致',
    'send_intent_invalid': '发送意图无效',
    'ready_status_invalid': '原生最新快照未就绪',
    'ready_fields_invalid': '原生快照字段不完整',
    'ready_artifacts_invalid': '原生快照工件无效',
    'ready_conversation_mismatch': '快照未唯一命中原始会话',
    'identity_sha_invalid': '原生身份摘要未核实',
    'seal_result_invalid': '原生封装预检未通过',
    'latest_source_changed': '最终消息内容发生变化',
}
SEND_ERROR_STAGES = frozenset({'snapshot', 'send', 'cli', 'identity', 'adapter'})


def _snapshot_error(code, diagnostics=None):
    return AdapterError(SNAPSHOT_ERROR_MESSAGES[code], code=code, stage='snapshot', diagnostics=diagnostics)


def _snapshot_row_facts(row, original=None, role=None, text=None):
    def role_value(key):
        if key not in row:
            return 'missing'
        value = row[key]
        return value if type(value) is str and value in ('buyer', 'seller', 'unknown') else 'invalid'
    content = row.get('content')
    content_type = ('string' if type(content) is str else 'null' if content is None else
                    'object' if type(content) is dict else 'array' if type(content) is list else 'other')
    facts = dict(snapshot_message_id=safe_snapshot_message_id(row.get('messageId')) or 'redacted', snapshot_from=role_value('from'),
                 snapshot_sender_role=role_value('senderRole'), snapshot_from_present='from' in row,
                 snapshot_sender_role_present='senderRole' in row, snapshot_content_type=content_type)
    if type(content) is str:
        facts['snapshot_content_length'] = min(len(content), 1000000)
    if original is not None:
        facts['snapshot_role_equal'] = role == original.role
        facts['snapshot_content_equal'] = text == original.text
        facts['snapshot_platform_content_length'] = min(len(original.text), 1000000)
    return facts

def _content(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def _historical_card_description(text):
    """Recognize observed native card renderings, never arbitrary buyer prose."""
    if type(text) is not str:
        return False
    if re.fullmatch(r'This is a single product card\. Product name is [1-9][0-9]{12}', text):
        return True
    return any(text.startswith(prefix) and bool(text[len(prefix):].strip()) for prefix in (
        '[card] ', 'This is a product card. The product is '))


def _historical_card_view(text, canonical):
    """Require a native card marker or the same descriptor under a known prefix."""
    if type(text) is not str:
        return False
    if text == '[Card]' or (text.startswith('[Card] ') and text[7:].strip()):
        return True
    if not _historical_card_description(text):
        return False
    prefixes = ('This is a single product card. Product name is ', '[card] ',
                'This is a product card. The product is ')
    def payload(value):
        return next((value[len(prefix):] for prefix in prefixes if value.startswith(prefix)), None)
    return payload(text) == payload(canonical)


def _ready_display_time(value):
    """Parse a display clock and its declared precision, without choosing a zone."""
    if type(value) is not str:
        return None
    match = re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}'
                         r'(?:[.,]([0-9]{1,6}))?(?:Z|[+-][0-9]{2}:[0-9]{2})?', value)
    if match is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.microsecond % 1000:
        return None
    precision = 10 ** max(0, 3 - len(match.group(1) or ''))
    wall_ms = (parsed.replace(tzinfo=None) - datetime(1970, 1, 1)) // timedelta(milliseconds=1)
    offset = parsed.utcoffset()
    return wall_ms, precision, offset // timedelta(milliseconds=1) if offset is not None else None


def _ready_source_clock(rows, source):
    matches = [row for row in rows if isinstance(row, dict) and str(row.get('messageId')) == source.message_id]
    display = _ready_display_time(matches[0].get('time')) if len(matches) == 1 else None
    if display is None:
        return None
    wall_ms, precision, offset_ms = display
    if offset_ms is None:
        # Modern display offsets have whole-minute precision. Infer the offset
        # from the verified source and truncation interval, never the host zone.
        delta = wall_ms - source.timestamp_ms
        offset_ms = -(-delta // 60000) * 60000
    if abs(offset_ms) > 14 * 60 * 60 * 1000 or offset_ms % 60000:
        return None
    return offset_ms if wall_ms - offset_ms <= source.timestamp_ms < wall_ms - offset_ms + precision else None


def _latest(messages):
    if not messages:
        raise AdapterError('未取得消息，不能验证最新消息')
    newest = max(m.timestamp_ms for m in messages)
    rows = [m for m in messages if m.timestamp_ms == newest]
    if len(rows) != 1:
        raise AdapterError('最新消息时间相同且多义，需人工核实')
    return rows[0]


class LiveGateway:
    def __init__(self, settings: dict, data_dir: Path):
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.process = Process(self.data_dir)
        self.ledger = Ledger(self.data_dir / 'adapters-live.sqlite3')
        self.lock = threading.RLock()
        self.feishu = FeishuAdapter(settings, self.data_dir, self.process, self.ledger)

    @property
    def cli(self):
        cli = self.settings.get('workctl_path')
        if not isinstance(cli, str) or not cli.strip():
            raise AdapterError('未配置当前授权会话的 workctl 路径')
        return cli

    def _wc(self, *args, before_submit=None):
        command = [self.cli, 'workflow', 'chat-analysis', *args, '--format', 'json']
        if before_submit is not None:
            before_submit()
        return self.process.run(command, timeout=150)

    def _identity(self, expected=None, *, identity_file=None):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if identity_file is not None:
            path = Path(identity_file).resolve()
            if not path.is_relative_to(self.data_dir.resolve()):
                raise AdapterError('身份文件超出本次运行目录', code='identity_invalid', stage='identity')
            path.parent.mkdir(parents=True, exist_ok=True)
            return self._resolve_identity_file(expected, path)
        with tempfile.TemporaryDirectory(prefix='identity-', dir=self.data_dir) as folder:
            return self._resolve_identity_file(expected, Path(folder) / 'account_identity.json')

    def _resolve_identity_file(self, expected, path):
        result = self._wc('resolve-id', '--output-file', str(path))
        try:
            identity = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise AdapterError('原生身份文件缺失或无效') from None
        if not isinstance(identity, dict):
            raise AdapterError('原生身份文件必须为 JSON 对象', code='identity_invalid', stage='identity')
        seller = str(identity.get('CURRENT_SELF_ALIID') or '')
        if not re.fullmatch(r'[1-9][0-9]*', seller):
            raise AdapterError('原生接口未返回有效卖家身份')
        configured = str(self.settings.get('seller_id') or '')
        if (expected and seller != expected) or (configured and seller != configured):
            raise AdapterError('当前授权卖家与目标卖家不一致')
        return seller, field(result, 'identitySha256')

    def _read(self, name, request):
        if name not in ('query_recent_conversation', 'query_conversation_msg', 'query_conversation_msg_for_aimode',
                         'ai_minisite_get_company_info'):
            raise AdapterError('不允许该读取工具')
        node, script = os.environ.get('ACCIO_NODE_BIN'), os.environ.get('ACCIO_MCP_SCRIPT')
        if not node or not script:
            raise AdapterError('缺少当前 Accio 授权会话读取组件，请从 Accio 重新连接')
        if not Path(node).is_file() or not Path(script).is_file():
            raise AdapterError('当前 Accio 读取组件路径无效')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='read-', dir=self.data_dir) as folder:
            path = Path(folder) / 'params.json'
            path.write_text(json.dumps({'request': request}, ensure_ascii=False), encoding='utf-8')
            result = self.process.run([node, script, 'call', name, '--json-file', str(path)], timeout=35)
        if result.get('success') is not True or not isinstance(result.get('data'), dict):
            raise AdapterError('Accio 未确认读取成功或返回结构无效')
        return result['data']

    def _messages(self, body, seller, buyer, conversation):
        for key, expected in (('conversationId', conversation), ('sellerAliId', seller), ('contactAliId', buyer)):
            if body.get(key) is not None and str(body[key]) != expected:
                raise AdapterError('历史响应身份不匹配')
        rows = body.get('messages')
        if not isinstance(rows, list) or type(body.get('hasMore')) is not bool:
            raise AdapterError('历史分页结构无效')
        result = []
        for row in rows:
            if not isinstance(row, dict) or (row.get('conversationId') is not None and row['conversationId'] != conversation):
                raise AdapterError('历史包含其他会话或无效消息')
            timestamp = stamp(row.get('timestamp'))
            sender = str(row.get('senderAliId') or '')
            role = 'buyer' if sender == buyer else 'seller' if sender == seller else 'unknown'
            raw_id = row.get('messageId')
            if raw_id is not None and type(raw_id) not in (str, int):
                raise AdapterError('消息 ID 格式无效')
            mid = str(raw_id) if raw_id not in (None, '') else 'missing-id-' + digest(row)
            text = _content(row.get('content'))
            attachments = []
            if isinstance(row.get('content'), (dict, list)):
                attachments.append(dict(kind='native-content', content=row['content']))
            if row.get('quoteMessage'):
                attachments.append(dict(kind='quote', content=row['quoteMessage']))
            result.append(Message(mid, seller, buyer, conversation, role, text, timestamp,
                                  body.get('contactName') if isinstance(body.get('contactName'), str) else '', attachments))
        return result

    def _history(self, seller, buyer, conversation):
        target(seller, buyer, conversation)
        cursor, seen, messages = 9007199254740991, {}, []
        deadline = time.monotonic() + 180
        max_pages = 200
        for _ in range(max_pages):
            if time.monotonic() >= deadline:
                raise AdapterError('历史读取达到时间预算；历史不完整，未推进水位')
            body = self._read('query_conversation_msg', dict(conversationId=conversation,
                selfAliId=int(seller), domain='icbu', count=100, limitTimeStamp=cursor, forward=False))
            page = self._messages(body, seller, buyer, conversation)
            for message in page:
                previous = seen.get(message.message_id)
                if previous and previous != message:
                    raise AdapterError('同一消息 ID 返回不同内容，历史未核实')
                if not previous:
                    seen[message.message_id] = message
                    messages.append(message)
            if body['hasMore'] is False:
                if not messages:
                    raise AdapterError('未取得会话消息原文，不能确认历史完整')
                return sorted(messages, key=lambda m: (m.timestamp_ms, m.message_id))
            next_cursor = stamp(body.get('nextPointTimeStamp'))
            if not page or not 0 < next_cursor < cursor:
                raise AdapterError('历史分页游标未向过去推进；历史不完整')
            cursor = next_cursor
        raise AdapterError('历史超过200页，未静默截断或推进水位')

    def history(self, seller_id, buyer_id, conversation_id):
        self._identity(seller_id)
        return self._history(seller_id, buyer_id, conversation_id)

    def _source(self, intent, history):
        last = _latest(history)
        if last.role != 'buyer' or last.message_id != intent.get('source_message_id') or last.message_id.startswith('missing-id-'):
            raise AdapterError('最新买家消息与发送意图不一致', code='latest_source_changed', stage='snapshot')
        return last

    def _snapshot_messages(self, snapshot, seller, buyer, conversation, history, *, source=None, projection_bindings=None):
        """Verify Ready rows in memory; never rewrite sealed workflow artifacts."""
        rows = snapshot.get('messages')
        if not isinstance(rows, list):
            raise _snapshot_error('snapshot_schema_invalid')
        authoritative = {}
        for message in history:
            key = (message.seller_id, message.buyer_id, message.conversation_id, message.message_id)
            if key in authoritative:
                raise _snapshot_error('snapshot_message_ambiguous')
            authoritative[key] = message
        verified_source = (source is not None and source.role == 'buyer' and
                           (source.seller_id, source.buyer_id, source.conversation_id) == (seller, buyer, conversation) and
                           not source.message_id.startswith('missing-id-') and _latest(history) == source)
        display_clock_offset = _ready_source_clock(rows, source) if verified_source else None
        source_uses_display_clock = verified_source and any(
            isinstance(row, dict) and str(row.get('messageId')) == source.message_id and type(row.get('time')) is str
            for row in rows)
        ready_messages, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise _snapshot_error('snapshot_schema_invalid')
            for record in (snapshot, row):
                for key, expected in (('conversationId', conversation), ('sellerAliId', seller), ('contactAliId', buyer)):
                    if record.get(key) is not None and str(record[key]) != expected:
                        raise _snapshot_error('snapshot_identity_mismatch')
            mid = row.get('messageId')
            if type(mid) not in (str, int) or not str(mid) or str(mid).startswith('missing-id-'):
                raise _snapshot_error('snapshot_schema_invalid')
            mid = str(mid)
            if mid in seen:
                raise _snapshot_error('snapshot_message_ambiguous')
            seen.add(mid)
            original = authoritative.get((seller, buyer, conversation, mid))
            text = _content(row.get('content'))
            facts = _snapshot_row_facts(row, original, None, text)
            from_role, sender_role = facts['snapshot_from'], facts['snapshot_sender_role']
            if (from_role not in ('buyer', 'seller', 'missing') or
                    sender_role not in ('buyer', 'seller', 'missing') or
                    (from_role == sender_role == 'missing')):
                facts['snapshot_difference'] = 'role'
                raise _snapshot_error('snapshot_message_mismatch', facts)
            if from_role != 'missing' and sender_role != 'missing' and from_role != sender_role:
                facts['snapshot_difference'] = 'role_conflict'
                raise _snapshot_error('snapshot_message_mismatch', facts)
            role = from_role if from_role != 'missing' else sender_role
            facts = _snapshot_row_facts(row, original, role, text)
            if row.get('senderAliId') is not None and str(row['senderAliId']) != {'buyer': buyer, 'seller': seller}[role]:
                facts['snapshot_difference'] = 'sender_identity'
                raise _snapshot_error('snapshot_identity_mismatch', facts)
            # Ready represents this specific platform image marker as a card.
            # Project only the verified message in memory; keep Ready bytes intact.
            if (original is not None and original.role == role and
                    type(row.get('content')) is str and row['content'] == '[Card]' and
                    original.text == '[image]' and
                    not any(attachment.get('kind') == 'native-content' for attachment in original.attachments)):
                text = original.text
            # Historical native card renderings are views, not ordinary prose.
            # Bind the canonical record and recheck it after sealing; never project
            # the current source or edit the sealed Ready artifact.
            projected_card = False
            if (verified_source and original is not None and mid != source.message_id and
                    role == original.role and type(row.get('content')) is str and row['content'].strip() and
                    original.attachments == [] and text != original.text and
                    _historical_card_description(original.text) and _historical_card_view(row['content'], original.text)):
                text = original.text
                projected_card = True
            if original is not None and (original.role != role or original.text != text):
                facts['snapshot_difference'] = ('role_and_content' if original.role != role and original.text != text
                                                else 'role' if original.role != role else 'content')
                raise _snapshot_error('snapshot_message_mismatch', facts)
            when = row.get('time')
            try:
                if isinstance(when, str):
                    when = datetime.fromisoformat(when.replace('Z', '+00:00'))
                    if when.tzinfo is None:
                        # A display clock has no authoritative zone. Use only the
                        # exact platform message read for this seller/buyer/CID.
                        if original is None:
                            raise _snapshot_error('snapshot_time_unverified')
                        if projected_card:
                            display = _ready_display_time(row.get('time'))
                            if (display is None or display_clock_offset is None or
                                    not display[0] - display_clock_offset <= original.timestamp_ms <
                                    display[0] - display_clock_offset + display[1]):
                                facts['snapshot_difference'] = 'time_and_content'
                                raise _snapshot_error('snapshot_message_mismatch', facts)
                        when = original.timestamp_ms
                    else:
                        if when.microsecond % 1000:
                            raise _snapshot_error('snapshot_time_invalid')
                        when = (when - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)
                when = stamp(when)
            except (ValueError, OverflowError) as exc:
                if isinstance(exc, AdapterError) and exc.code in SNAPSHOT_ERROR_MESSAGES:
                    raise
                raise _snapshot_error('snapshot_time_invalid') from None
            if projected_card:
                if ((source_uses_display_clock and display_clock_offset is None) or
                        when != original.timestamp_ms):
                    facts['snapshot_difference'] = 'time_and_content'
                    raise _snapshot_error('snapshot_message_mismatch', facts)
                if projection_bindings is not None:
                    projection_bindings[(seller, buyer, conversation, mid)] = (original.role, original.text, original.timestamp_ms)
            ready_messages.append(Message(mid, seller, buyer, conversation, role, text, when))
        return ready_messages

    def send(self, intent):
        return self._execute_send(intent)

    def preview_send(self, intent):
        return self._execute_send(intent, preview=True)

    def _execute_send(self, intent, *, preview=False):
        with self.lock:
            attempted = False
            claimed = False
            retrying = False
            operation = intent.get('operation_id')
            try:
                previous = self.ledger.get(operation) if operation and not preview else None
                if previous:
                    if previous[0] != 'send' or previous[1] != intent:
                        raise AdapterError('operation_id 已绑定其他发送内容')
                    retrying = (intent.get('send_mode') == 'manual' and previous[2].get('status') == 'rejected' and
                                previous[2].get('submission_started') is False and self.ledger.retry_armed(operation))
                    if not retrying:
                        return dict({'message_id': ''}, **previous[2])
                if not preview:
                    self._send_authorized(intent)
                seller, buyer, conversation = intent.get('seller_id'), intent.get('buyer_id'), intent.get('conversation_id')
                target(seller, buyer, conversation)
                reply = intent.get('reply')
                if not isinstance(reply, str) or not reply.strip() or len(reply) > 1200 or not intent.get('source_message_id'):
                    raise AdapterError('发送意图正文或来源消息无效', code='send_intent_invalid', stage='snapshot')
                self._identity(seller)
                history = self._history(seller, buyer, conversation)
                source = self._source(intent, history)
                if not preview:
                    prior = self.ledger.claim(operation, 'send:' + digest([seller, conversation, source.message_id]), 'send', intent,
                                              allow_rejected_send=intent.get('send_mode') in ('manual', 'automatic'),
                                              conversation_fence=True)
                    if prior:
                        return dict({'message_id': ''}, **prior)
                    claimed = True
                self.data_dir.mkdir(parents=True, exist_ok=True)
                # A Windows file lock during cleanup must not replace the CLI
                # error or turn an authoritative send receipt into another result.
                with tempfile.TemporaryDirectory(prefix='send-', dir=self.data_dir, ignore_cleanup_errors=True) as folder:
                    root = Path(folder).resolve()
                    now = datetime.now(timezone.utc)
                    start = max(now - timedelta(days=29), min(now - timedelta(days=2), datetime.fromtimestamp(source.timestamp_ms / 1000, timezone.utc)))
                    downloaded = self._wc('run', '--account-ids', seller, '--start-date', start.strftime('%Y-%m-%d'),
                        '--end-date', now.strftime('%Y-%m-%d'), '--include', 'messages', '--mode', 'blocking',
                        '--max-run-seconds', '120', '--output-dir', str(root))
                    if field(downloaded, 'status') != 'ready':
                        raise AdapterError('原生最新快照未就绪', code='ready_status_invalid', stage='snapshot')
                    required = [field(downloaded, k) for k in ('storeRoot', 'readyManifestRef', 'readyToken')]
                    if not all(isinstance(v, str) and v for v in required):
                        raise AdapterError('原生快照凭据不完整', code='ready_fields_invalid', stage='snapshot')
                    ready = self._wc('read-ready', '--store-root', required[0], '--ready-manifest-ref', required[1], '--ready-token', required[2])
                    manifest, token = field(ready, 'readyManifestFile'), field(ready, 'readyToken')
                    artifacts = field(ready, 'artifactFiles')
                    path = artifacts.get('messages_ready.json') if isinstance(artifacts, dict) else None
                    if isinstance(path, dict):
                        path = path.get('path')
                    if not manifest or not token or not isinstance(path, str):
                        raise AdapterError('原生 Ready 工件不完整', code='ready_artifacts_invalid', stage='snapshot')
                    file = Path(path).resolve()
                    if not file.is_relative_to(root):
                        raise AdapterError('原生快照工件超出本次隔离目录', code='ready_artifacts_invalid', stage='snapshot')
                    snapshot = json.loads(file.read_text(encoding='utf-8-sig'))
                    matches = [c for c in snapshot.get('conversations', []) if isinstance(c, dict) and c.get('conversationId') == conversation]
                    if len(matches) != 1:
                        raise AdapterError('快照未唯一命中原始会话', code='ready_conversation_mismatch', stage='snapshot')
                    projection_bindings = {}
                    ready_messages = self._snapshot_messages(matches[0], seller, buyer, conversation, history,
                                                             source=source, projection_bindings=projection_bindings)
                    snap_source = self._source(intent, ready_messages)
                    if snap_source.timestamp_ms != source.timestamp_ms or snap_source.text != source.text:
                        row = next(row for row in matches[0]['messages'] if str(row.get('messageId')) == source.message_id)
                        facts = _snapshot_row_facts(row, source, snap_source.role, snap_source.text)
                        facts['snapshot_difference'] = ('time_and_content' if snap_source.timestamp_ms != source.timestamp_ms
                                                        and snap_source.text != source.text else
                                                        'time' if snap_source.timestamp_ms != source.timestamp_ms else 'content')
                        raise _snapshot_error('snapshot_source_mismatch', facts)
                    # seal/send reads the identity file registered by resolve-id;
                    # keep that exact delivery-local file alive through submission.
                    identity_file = root / 'resolvedId/account_identity.json'
                    _, identity_sha = self._identity(seller, identity_file=identity_file)
                    if not isinstance(identity_sha, str) or not identity_sha:
                        raise AdapterError('原生身份摘要未核实', code='identity_sha_invalid', stage='snapshot')
                    request = root / '.runtime/chat-analysis/params/seal-reply-preview.json'
                    request.parent.mkdir(parents=True, exist_ok=True)
                    request.write_text(json.dumps(dict(schemaVersion='chat-analysis-seal-reply-preview-request/v1',
                        sourceReadyToken=token, sourceIdentitySha256=identity_sha, description='报价前接待已批准草稿',
                        items=[dict(conversationId=conversation, schemes=[dict(schemeId='reception', reply=reply)], selectedSchemeId='reception')]), ensure_ascii=False), encoding='utf-8')
                    sealed = self._wc('seal-reply-preview', '--ready-manifest-file', manifest, '--ready-token', token,
                        '--identity-sha256', identity_sha, '--request-file', str(request), '--output-dir', str(root / 'sessions'))
                    plan, selection = field(sealed, 'reply_plan_file'), field(sealed, 'selectionToken')
                    if field(sealed, 'status') != 'ready_to_send' or not isinstance(plan, str) or not plan or not isinstance(selection, str) or not selection:
                        raise AdapterError('原生封装预检未通过', code='seal_result_invalid', stage='snapshot')
                    final_history = self._history(seller, buyer, conversation)
                    if self._source(intent, final_history) != source:
                        raise AdapterError('最终消息内容发生变化', code='latest_source_changed', stage='snapshot')
                    final_index = {(message.seller_id, message.buyer_id, message.conversation_id, message.message_id): message
                                   for message in final_history}
                    for key, binding in projection_bindings.items():
                        current = final_index.get(key)
                        if (current is None or current.attachments != [] or
                                (current.role, current.text, current.timestamp_ms) != binding):
                            raise AdapterError('历史卡片在发送预检期间发生变化', code='latest_source_changed', stage='snapshot',
                                               diagnostics={'snapshot_message_id': safe_snapshot_message_id(key[3]) or 'redacted'})
                    if preview:
                        self._identity(seller, identity_file=identity_file)
                        return dict(ok=True, status='passed', submission_started=False,
                                    message='原生快照和封装预检通过；未提交发送，也未占用生产发送防重键')
                    self._send_authorized(intent)
                    self._identity(seller, identity_file=identity_file)
                    self._send_authorized(intent)
                    baseline = inventory(self)
                    def mark_submission():
                        nonlocal attempted
                        # Persist recovery evidence before entering the submitting CLI.
                        # No token, reply body or selection token enters this journal.
                        self.ledger.finish(operation, dict(status='unknown', message_id='', submission_started=True,
                            receipt_baseline=baseline, receipt_submission_ns=time.time_ns()))
                        attempted = True
                    receipt = self._wc('send-message', '--reply-plan-file', plan, '--selection-token', selection, '--yes',
                                       before_submit=mark_submission)
                    pending = dict(self.ledger.get(operation)[2], **stdout_evidence(receipt, conversation))
                    self.ledger.finish(operation, pending)
                    result = reconcile_receipt(self, intent, pending)
                    return self.ledger.finish(operation, result)
            except (AdapterError, OSError, ValueError, TypeError, KeyError) as exc:
                code, stage, diagnostics = 'adapter_error', 'adapter', {}
                if isinstance(exc, AdapterError):
                    if isinstance(exc.code, str) and (exc.code in CODES or exc.code in SNAPSHOT_ERROR_MESSAGES):
                        code = exc.code
                    if isinstance(exc.stage, str) and exc.stage in SEND_ERROR_STAGES:
                        stage = exc.stage
                    diagnostics = process_diagnostics(exc.diagnostics)
                    if type(exc.exit_code) is int:
                        diagnostics['exit_code'] = exc.exit_code
                diagnostics.update(code=code, stage=stage)
                failure = dict(error_code=code, stage=stage, diagnostics=diagnostics)
                detail = SNAPSHOT_ERROR_MESSAGES.get(code, '原生发送预检未通过；未提交发送')
                if preview:
                    return dict(ok=False, status='failed', submission_started=False, message=detail, **failure)
                saved = self.ledger.get(operation) if attempted else None
                result = dict(saved[2] if saved else {}, status='unknown' if attempted else 'rejected',
                              submission_started=attempted,
                              detail='提交后未取得完整回执，须核查，禁止重发' if attempted else detail, **failure)
                result.setdefault('message_id', '')
                if attempted:
                    try:
                        recovered = reconcile_receipt(self, intent, result)
                        result.update(recovered)
                    except (AdapterError, OSError, ValueError, TypeError, KeyError):
                        # Preserve any exact ID saved before a failed platform read.
                        saved = self.ledger.get(operation)
                        if saved:
                            result.update(saved[2])
                if claimed or retrying:
                    return self.ledger.finish(operation, result)
                return result

    def register(self, payload, *, authorization=None):
        with self.lock:
            if not self.feishu.enabled():
                return self.feishu.register(payload, authorization=authorization)
            operation = payload.get('operation_id')
            case = dict(payload.get('case') or {})
            # A supplied/local clock is never authoritative. Bind only the core's
            # immutable original message ID to freshly verified platform history.
            case.pop('inquiry_timestamp_ms', None)
            source_id = case.get('inquiry_source_id')
            if isinstance(source_id, str) and source_id:
                case['source_message_ids'] = list(dict.fromkeys([*(case.get('source_message_ids') or []), source_id]))
            payload = dict(payload, case=case)
            old = self.ledger.get(operation) if operation else None
            if old:
                stored_case = (old[1].get('payload') or {}).get('case') or {}
                if old[0] == 'register' and source_id == stored_case.get('inquiry_source_id'):
                    # Replays compare the same durable intent, with the previously
                    # derived timestamp, and never fetch/submit the operation again.
                    if 'inquiry_timestamp_ms' in stored_case:
                        case['inquiry_timestamp_ms'] = stored_case['inquiry_timestamp_ms']
                retry_registration = (old[0] == 'register' and callable(authorization)
                                      and old[2].get('status') == 'rejected'
                                      and old[2].get('submission_started') is False)
                if not retry_registration:
                    return self.feishu.register(payload, authorization=authorization)
            previous = payload.get('previous') or {}
            if previous.get('status') == 'unknown':
                return self.feishu.register(payload, authorization=authorization)
            try:
                require_registration_authorization(authorization)
                messages = self.history(case.get('seller_id'), case.get('buyer_id'), case.get('conversation_id'))
                require_registration_authorization(authorization)
                evidence = {m.message_id: m.text for m in messages if m.role == 'buyer'}
                if source_id:
                    sources = [m for m in messages if m.message_id == source_id and m.role == 'buyer'
                               and not m.message_id.startswith('missing-id-')]
                    if (not isinstance(source_id, str) or len(sources) != 1
                            or type(sources[0].timestamp_ms) is not int or sources[0].timestamp_ms <= 0):
                        raise AdapterError('原始询单消息 ID 未在当前买家平台消息中核实')
                    case['inquiry_timestamp_ms'] = sources[0].timestamp_ms
                for fact in (case.get('facts') or {}).values():
                    ids = fact.get('evidence_ids') if isinstance(fact, dict) else None
                    value = fact.get('value') if isinstance(fact, dict) else None
                    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in evidence for i in ids) or not isinstance(value, str) or not value.strip() or not all(value in evidence[i] for i in ids):
                        raise AdapterError('登记事实缺少已核实买家原文证据')
                if case.get('customer_name') and not any(m.customer_name == case['customer_name'] for m in messages):
                    raise AdapterError('登记客户名称不是当前接口原值')
            except (AdapterError, OSError, ValueError, KeyError, TypeError):
                return dict(status='rejected', record_id=previous.get('record_id') or '',
                            number=previous.get('number') or '', case_id=previous.get('case_id') or case.get('case_id') or '',
                            submission_started=False, detail='登记权限、买家原文或接口名称未核实，未写表')
            ids = set(case.get('source_message_ids') or [])
            for fact in (case.get('facts') or {}).values():
                if isinstance(fact, dict):
                    ids.update(fact.get('evidence_ids') or [])
            verified = [m.to_dict() for m in messages if m.role == 'buyer' and m.message_id in ids]
            return self.feishu.register(dict(payload, verified_messages=verified), authorization=authorization)

    def check_registration_schema(self):
        return self.feishu.check_schema()

    def reconcile(self, operation_id, *, receipt_ref=None, expected_mid=None):
        with self.lock:
            record = self.ledger.get(operation_id)
            if not record:
                return dict(status='unknown', detail='没有该操作记录，不能重放')
            kind, request, result = record
            if result['status'] in ('accepted', 'confirmed', 'rejected', 'disabled'):
                return result
            if kind == 'register':
                return self.feishu.reconcile(operation_id, request, result)
            if kind == 'send' and request.get('purpose') != 'chat' and result.get('submission_started') is True:
                try:
                    result = reconcile_receipt(self, request, result, receipt_ref=receipt_ref, expected_mid=expected_mid)
                    return self.ledger.finish(operation_id, result)
                except (AdapterError, OSError, ValueError, TypeError, KeyError):
                    result = self.ledger.get(operation_id)[2]
            return dict(result, detail='原操作尚未取得可核实回执，保留未知，禁止重发')
