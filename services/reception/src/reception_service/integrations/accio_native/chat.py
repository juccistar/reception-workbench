"""Direct human chat: request claims, memory-only chat tokens, native readback.

Protocol verified by A02 (2026-11-07). BusinessID is not a conversation ID;
only a platform message read from the exact conversation proves acceptance.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path

from .vendor.adapters.common import AdapterError, digest

# The installed CLI has no stdin argument. This fixed bridge supplies its documented
# --json argument in the child process's memory, never in OS argv or a temp file.
# Only the two explicit chat tools can be invoked through this entry point.
MCP_STDIN_BRIDGE = r'''
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
try {
 const bytes=readFileSync(0);
 if(bytes.length>512000)throw new Error();
 const request=JSON.parse(bytes.toString('utf8'));
 if(!['get_communication_chatToken','send_msg'].includes(request.name)
   ||!request.arguments||typeof request.arguments!=='object'||Array.isArray(request.arguments))throw new Error();
 const script=process.env.ACCIO_MCP_SCRIPT;
 if(!script)throw new Error();
 process.argv=[process.execPath,script,'call','--server','accio',request.name,'--json',JSON.stringify(request.arguments)];
 await import(pathToFileURL(script).href);
} catch { process.stderr.write('native_chat_call_failed\n'); process.exitCode=2; }
'''


def chat_text(body, *, allow_empty=False):
    if (not isinstance(body, str) or len(body) > 50000 or (not allow_empty and not body.strip())
            or any(ord(c) < 32 and c not in '\t\r\n' for c in body)):
        raise AdapterError('人工消息正文为空、过长或含无效控制字符', code='args', stage='send')
    try:
        body.encode('utf-8', errors='strict')
    except UnicodeError:
        raise AdapterError('人工消息正文编码无效', code='args', stage='send') from None
    return body


def authorized(callback):
    try:
        valid = callable(callback) and callback() is True
    except Exception:
        valid = False
    if not valid:
        raise AdapterError('本条人工发送意图已失效', code='auth', stage='send')


def call_chat_tool(gateway, name, arguments, *, before_submit=None):
    gateway.require_runtime()
    if name not in ('get_communication_chatToken', 'send_msg'):
        raise AdapterError('不支持的人工聊天工具', code='args', stage='send')
    node = os.environ.get('ACCIO_NODE_BIN', '')
    if not Path(node).is_file():
        raise AdapterError('当前任务缺少原生聊天运行组件', code='args', stage='runtime')
    payload = json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False, allow_nan=False)
    if before_submit is not None:
        before_submit()
    return gateway.process.run([node, '--input-type=module', '-e', MCP_STDIN_BRIDGE], input=payload, timeout=40)


def read_chat_receipt(gateway, intent, result):
    """Read-only reconciliation of an exact returned ID; never infer from a body."""
    mid = result.get('message_id')
    if not isinstance(mid, str) or not mid.strip():
        return result
    body = gateway._read('query_conversation_msg', {
        'conversationId': intent['conversation_id'], 'selfAliId': int(intent['seller_id']),
        'domain': 'icbu', 'count': 100, 'limitTimeStamp': 9007199254740991, 'forward': False})
    messages = gateway._messages(body, intent['seller_id'], intent['buyer_id'], intent['conversation_id'])
    matches = [row for row in messages if row.message_id == mid]
    if (len(matches) == 1 and matches[0].role == 'seller' and matches[0].text == intent['reply']
            and matches[0].conversation_id == intent['conversation_id']):
        return dict(status='accepted', message_id=mid, submission_started=True,
                    timestamp_ms=matches[0].timestamp_ms)
    return result


def _continuity_verify(gateway, intent):
    from .vendor.adapters.live import _latest
    gateway._send_authorized(intent)
    gateway._identity(intent['seller_id'])
    history = gateway._history(intent['seller_id'], intent['buyer_id'], intent['conversation_id'])
    # Read may overlap revocation, so recheck after the read as well.
    gateway._send_authorized(intent)
    latest = _latest(history)
    buyers = [m for m in history if m.role == 'buyer']
    source = _latest(buyers)
    sha = lambda text: hashlib.sha256(text.encode('utf-8')).hexdigest()
    if (source.message_id.startswith('missing-id-') or source.message_id != intent['source_message_id']
            or source.timestamp_ms != intent['source_timestamp_ms'] or sha(source.text) != intent['source_text_sha256']
            or latest.message_id.startswith('missing-id-') or latest.message_id != intent['latest_message_id']
            or latest.role != intent['latest_message_role'] or sha(latest.text) != intent['latest_text_sha256']):
        raise AdapterError('消息来源或最新消息已变化', code='auth', stage='send')
    phase = intent['continuity_phase']
    if latest.role == 'buyer':
        if latest.message_id != source.message_id or phase == 'final':
            raise AdapterError('续答阶段与最新消息不匹配', code='auth', stage='send')
        return
    if latest.role != 'seller' or phase == 'ack' or latest.timestamp_ms <= source.timestamp_ms:
        raise AdapterError('续答阶段与最新消息不匹配', code='auth', stage='send')
    with gateway.ledger.lock, closing(gateway.ledger._db()) as db:
        rows = db.execute("SELECT request,result FROM operations WHERE kind='send'").fetchall()
    matches = []
    for raw_request, raw_result in rows:
        request, result = json.loads(raw_request), json.loads(raw_result)
        if (result.get('status') == 'accepted' and result.get('message_id') == latest.message_id
                and request.get('reply') == latest.text
                and all(request.get(k) == intent[k] for k in ('seller_id', 'buyer_id', 'conversation_id'))):
            matches.append(request)
    if len(matches) != 1 or (phase == 'final' and not (
            matches[0].get('purpose') == 'continuity' and matches[0].get('continuity_phase') == 'ack'
            and matches[0].get('source_message_id') == source.message_id
            and matches[0].get('source_timestamp_ms') == source.timestamp_ms
            and matches[0].get('source_text_sha256') == sha(source.text))):
        raise AdapterError('最新卖家消息缺少本机受理凭据', code='auth', stage='send')


def _claim_continuity(gateway, intent):
    # Phase/source and client request are both stable across definite rejections.
    ledger = gateway.ledger
    logical = 'continuity:' + digest([intent['seller_id'], intent['buyer_id'], intent['conversation_id'],
                                      intent['source_message_id'], intent['continuity_phase'],
                                      intent['client_request_id'] if intent['continuity_phase'] == 'assistance' else None])
    with ledger.lock, closing(ledger._db()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT operation_id,logical_key,request,result FROM operations WHERE kind='send'").fetchall()
        for operation, key, raw_request, raw_result in rows:
            request, result = json.loads(raw_request), json.loads(raw_result)
            same_request = (request.get('purpose') == 'continuity'
                            and request.get('client_request_id') == intent['client_request_id'])
            if key == logical or same_request:
                if ((key not in (logical, None)) or not same_request or any(request.get(k) != intent.get(k) for k in
                    ('seller_id', 'buyer_id', 'conversation_id', 'source_message_id', 'continuity_phase'))):
                    raise AdapterError('续答请求或来源已绑定其他操作')
                if result.get('status') != 'rejected' or result.get('submission_started') is not False:
                    raise AdapterError('续答来源已有受理或未决操作')
                db.execute('UPDATE operations SET logical_key=NULL WHERE operation_id=?', (operation,))
            if (request.get('seller_id') == intent['seller_id']
                    and request.get('conversation_id') == intent['conversation_id']
                    and result.get('status') in ('unknown', 'prepared', 'sending')):
                raise AdapterError('本会话存在未决发送，须核查原操作')
        result = dict(status='unknown', detail='操作已认领，结果须核查；禁止重放')
        db.execute('INSERT INTO operations VALUES (?,?,?,?,?)',
                   (intent['operation_id'], logical, 'send', json.dumps(intent, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False)))


def execute_chat(gateway, intent, authorization):
    """One platform submission per immutable client request, without AI business gates."""
    from .vendor.adapters.common import target
    intent = deepcopy(intent)
    claimed = attempted = False
    operation = intent.get('operation_id')
    with gateway.lock:
        try:
            continuity = intent.get('purpose') == 'continuity' and intent.get('send_mode') == 'automatic'
            if not continuity and (intent.get('purpose') != 'chat' or intent.get('send_mode') != 'manual'):
                raise AdapterError('聊天意图无效', code='args', stage='send')
            if continuity:
                if (not isinstance(operation, str) or not operation.strip()
                        or intent.get('continuity_phase') not in ('ack', 'final', 'assistance')
                        or intent.get('latest_message_role') not in ('buyer', 'seller')
                        or type(intent.get('source_timestamp_ms')) is not int
                        or intent['source_timestamp_ms'] <= 0
                        or any(not isinstance(intent.get(k), str) or not intent[k].strip() for k in
                               ('source_message_id', 'latest_message_id', 'source_text_sha256', 'latest_text_sha256',
                                'case_id', 'draft_id'))
                        or type(intent.get('revision')) is not int):
                    raise AdapterError('续答意图字段无效', code='args', stage='send')
            seller, buyer, cid = intent.get('seller_id'), intent.get('buyer_id'), intent.get('conversation_id')
            target(seller, buyer, cid)
            chat_text(intent.get('reply'))
            request = intent.get('client_request_id')
            if not isinstance(request, str) or not request.strip() or len(request) > 200:
                raise AdapterError('人工发送缺少有效请求编号', code='args', stage='send')
            previous = gateway.ledger.get(operation)
            if previous:
                if previous[0] != 'send' or previous[1] != intent:
                    raise AdapterError('操作编号已绑定不同人工消息', code='args', stage='send')
                return previous[2]
            authorized(authorization)
            gateway._identity(seller)
            if continuity:
                _continuity_verify(gateway, intent)
                _claim_continuity(gateway, intent)
            else:
                prior = gateway.ledger.claim(operation, 'chat:' + digest([seller, cid, request]), 'send', intent,
                                             conversation_fence=True)
                if prior:
                    return prior
            claimed = True
            token_reply = gateway._chat_call('get_communication_chatToken', {'targetAliId': int(buyer)})
            token = (token_reply.get('data') or {}).get('chatToken') if isinstance(token_reply.get('data'), dict) else None
            if token_reply.get('success') is not True or not isinstance(token, str) or not token.strip():
                raise AdapterError('未取得本次会话通信令牌', code='auth', stage='send')
            gateway._identity(seller)
            authorized(authorization)
            def submit():
                nonlocal attempted
                if continuity:
                    _continuity_verify(gateway, intent)
                    gateway._identity(seller)
                    gateway._send_authorized(intent)
                else:
                    authorized(authorization)
                attempted = True
            native = gateway._chat_call('send_msg', {'sendRequest': {
                'messageType': 'CUSTOMER_TEXT', 'chatTypeEnum': 'SINGLE', 'content': intent['reply'],
                'receiverAliID': [int(buyer), int(seller)], 'conversationID': cid, 'chatToken': token,
                'autoReply': False, 'safeFilter': True, 'needCheckToken': True}, 'domainEnum': 'ICBU'},
                before_submit=submit)
            token = None
            mid = native.get('data', {}).get('messageID') if isinstance(native.get('data'), dict) else None
            result = dict(status='unknown', message_id='', submission_started=True)
            if native.get('success') is True and isinstance(mid, str) and mid.strip():
                result['message_id'] = mid
            # Preserve the returned ID before a possibly interrupted readback. It is
            # the only ID reconciliation may look up; no body-based guessing or resend.
            gateway.ledger.finish(operation, result)
            try:
                result = read_chat_receipt(gateway, intent, result)
            except Exception:
                pass
            return gateway.ledger.finish(operation, result)
        except Exception:
            result = dict(status='unknown' if attempted else 'rejected', message_id='', submission_started=attempted)
            if claimed:
                return gateway.ledger.finish(operation, result)
            return result


def reconcile_chat(gateway, operation_id):
    with gateway.lock:
        previous = gateway.ledger.get(operation_id)
        if not previous or previous[0] != 'send' or previous[1].get('purpose') not in ('chat', 'continuity'):
            return dict(status='unknown', message_id='', submission_started=True)
        intent, result = previous[1], previous[2]
        if result.get('status') != 'unknown':
            return result
        gateway._identity(intent['seller_id'])
        try:
            refreshed = read_chat_receipt(gateway, intent, result)
        except Exception:
            return result
        return gateway.ledger.finish(operation_id, refreshed)
