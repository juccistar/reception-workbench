"""Direct chat submission: request claims, memory-only tokens, native readback.

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


def _batch_part(intent):
    keys = ('batch_id', 'part_id', 'part_index', 'batch_message_hashes', 'batch_root_request_id')
    if not any(key in intent for key in keys):
        return None
    hashes, index = intent.get('batch_message_hashes'), intent.get('part_index')
    if (any(not isinstance(intent.get(key), str) or not intent[key].strip() or len(intent[key]) > 200
            for key in ('batch_id', 'part_id', 'batch_root_request_id'))
            or not isinstance(hashes, list) or not 1 <= len(hashes) <= 3
            or type(index) is not int or not 0 <= index < len(hashes)
            or any(not isinstance(value, str) or len(value) != 64
                   or any(char not in '0123456789abcdef' for char in value) for value in hashes)
            or not isinstance(intent.get('reply'), str)
            or hashlib.sha256(intent['reply'].encode('utf-8')).hexdigest() != hashes[index]):
        raise AdapterError('分条发送的冻结清单或正文无效', code='args', stage='send')
    return index


def _batch_prefix(gateway, intent):
    index = _batch_part(intent)
    if index is None or index == 0:
        return
    with gateway.ledger.lock, closing(gateway.ledger._db()) as db:
        rows = db.execute("SELECT logical_key,request,result FROM operations WHERE kind='send'").fetchall()
    prefix = {}
    for logical, raw_request, raw_result in rows:
        request, result = json.loads(raw_request), json.loads(raw_result)
        if (request.get('batch_id') != intent['batch_id']
                or any(request.get(key) != intent.get(key) for key in
                       ('seller_id', 'buyer_id', 'conversation_id', 'source_message_id', 'purpose',
                        'send_mode', 'continuity_phase', 'batch_root_request_id'))):
            continue
        previous = request.get('part_index')
        if (type(previous) is not int or not 0 <= previous < index
                or result.get('status') != 'accepted' or not result.get('message_id')
                or result.get('submission_started') is not True):
            continue
        if (previous in prefix or request.get('batch_message_hashes') != intent['batch_message_hashes']
                or _batch_part(request) != previous):
            raise AdapterError('分条前序回执或冻结清单不唯一', code='auth', stage='send')
        prefix[previous] = (logical, request, result)
    if set(prefix) != set(range(index)) or len({row[2]['message_id'] for row in prefix.values()}) != index:
        raise AdapterError('前序消息尚未逐条受理，禁止继续提交', code='auth', stage='send')
    first_logical, first, _ = prefix[0]
    if intent.get('purpose') == 'continuity':
        expected = 'continuity:' + digest([intent['seller_id'], intent['buyer_id'], intent['conversation_id'],
                    intent['source_message_id'], intent['continuity_phase'],
                    intent['batch_root_request_id'] if intent['continuity_phase'] in ('assistance', 'supplement') else None])
    elif intent.get('purpose') == 'reception':
        expected = 'send:' + digest([intent['seller_id'], intent['conversation_id'], intent['source_message_id']])
    else:
        expected = 'chat:' + digest([intent['seller_id'], intent['conversation_id'], intent['batch_root_request_id']])
    if first_logical != expected:
        raise AdapterError('首条缺少原来源认领，不能更换批次绕过防重', code='auth', stage='send')
    if intent.get('purpose') == 'continuity':
        previous = prefix[index - 1][2]
        if intent.get('latest_message_id') != previous['message_id'] or intent.get('latest_message_role') != 'seller':
            raise AdapterError('续答分条缺少上一条确切受理来源', code='auth', stage='send')


def _continuity_verify(gateway, intent):
    """Check the local phase/receipt binding; source freshness is owned by the service."""
    gateway._send_authorized(intent)
    _batch_prefix(gateway, intent)
    phase, latest_role = intent['continuity_phase'], intent['latest_message_role']
    if latest_role == 'buyer':
        if (intent['latest_message_id'] != intent['source_message_id'] or phase == 'final'
                or intent['latest_text_sha256'] != intent['source_text_sha256']):
            raise AdapterError('续答阶段与本地最新消息不匹配', code='auth', stage='send')
        return
    if latest_role != 'seller' or phase == 'ack':
        raise AdapterError('续答阶段与本地最新消息不匹配', code='auth', stage='send')
    with gateway.ledger.lock, closing(gateway.ledger._db()) as db:
        rows = db.execute("SELECT request,result FROM operations WHERE kind='send'").fetchall()
    matches = []
    for raw_request, raw_result in rows:
        request, result = json.loads(raw_request), json.loads(raw_result)
        body = request.get('reply')
        if (result.get('status') == 'accepted' and result.get('message_id') == intent['latest_message_id']
                and isinstance(body, str) and hashlib.sha256(body.encode('utf-8')).hexdigest() == intent['latest_text_sha256']
                and type(result.get('timestamp_ms')) is int and result['timestamp_ms'] > intent['source_timestamp_ms']
                and all(request.get(k) == intent[k] for k in ('seller_id', 'buyer_id', 'conversation_id'))):
            matches.append(request)
    if len(matches) != 1 or (phase == 'final' and not ((_batch_part(intent) or 0) > 0) and not (
            matches[0].get('purpose') == 'continuity' and matches[0].get('continuity_phase') == 'ack'
            and all(matches[0].get(k) == intent[k] for k in
                    ('source_message_id', 'source_timestamp_ms', 'source_text_sha256')))):
        raise AdapterError('最新卖家消息缺少本机受理凭据', code='auth', stage='send')
    gateway._send_authorized(intent)


def _claim_continuity(gateway, intent):
    # Phase/source and client request are both stable across definite rejections.
    ledger = gateway.ledger
    logical = 'continuity:' + digest([intent['seller_id'], intent['buyer_id'], intent['conversation_id'],
                                      intent['source_message_id'], intent['continuity_phase'],
                                      intent['client_request_id'] if intent['continuity_phase'] in ('assistance', 'supplement') else None])
    if (_batch_part(intent) or 0) > 0:
        logical = 'continuity-part:' + digest([intent['seller_id'], intent['conversation_id'],
                    intent['source_message_id'], intent['continuity_phase'], intent['batch_id'], intent['part_index']])
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
    """Submit a frozen intent; automatic business checks stay in its service callback."""
    from .vendor.adapters.common import target
    intent = deepcopy(intent)
    claimed = attempted = automatic = False
    returned_id = ''
    operation = intent.get('operation_id')
    with gateway.lock:
        try:
            continuity = intent.get('purpose') == 'continuity' and intent.get('send_mode') == 'automatic'
            reception = intent.get('purpose') == 'reception' and intent.get('send_mode') == 'automatic'
            automatic = continuity or reception
            if not automatic and (intent.get('purpose') != 'chat' or intent.get('send_mode') != 'manual'):
                raise AdapterError('聊天意图无效', code='args', stage='send')
            if reception:
                if (any(not isinstance(intent.get(k), str) or not intent[k].strip()
                        for k in ('source_message_id', 'draft_id', 'case_id'))
                        or intent['source_message_id'].startswith('missing-id-')
                        or type(intent.get('source_timestamp_ms')) is not int or intent['source_timestamp_ms'] <= 0
                        or type(intent.get('revision')) is not int or intent['revision'] < 1
                        or not isinstance(intent.get('reply'), str) or len(intent['reply']) > 1200):
                    raise AdapterError('自动回复正文或来源无效', code='args', stage='send')
            if continuity:
                if (not isinstance(operation, str) or not operation.strip()
                        or intent.get('continuity_phase') not in ('ack', 'final', 'assistance', 'supplement')
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
            if automatic:
                gateway._send_authorized(intent)
            gateway._identity(seller)
            _batch_prefix(gateway, intent)
            if continuity:
                _continuity_verify(gateway, intent)
                _claim_continuity(gateway, intent)
            else:
                logical = ('send:' + digest([seller, cid, intent['source_message_id']]) if reception
                           else 'chat:' + digest([seller, cid, request]))
                if (_batch_part(intent) or 0) > 0:
                    logical = ('send-part:' if reception else 'chat-part:') + digest([
                        seller, cid, intent.get('source_message_id'), intent['batch_id'], intent['part_index']])
                prior = gateway.ledger.claim(operation, logical, 'send', intent,
                                             allow_rejected_send=reception, conversation_fence=True)
                if prior:
                    return prior
            claimed = True
            token_reply = gateway._chat_call('get_communication_chatToken', {'targetAliId': int(buyer)})
            token = (token_reply.get('data') or {}).get('chatToken') if isinstance(token_reply.get('data'), dict) else None
            if token_reply.get('success') is not True or not isinstance(token, str) or not token.strip():
                raise AdapterError('未取得本次会话通信令牌', code='auth', stage='send')
            gateway._identity(seller)
            authorized(authorization)
            if automatic:
                gateway._send_authorized(intent)
            def submit():
                nonlocal attempted
                if continuity:
                    _continuity_verify(gateway, intent)
                if automatic:
                    gateway._send_authorized(intent)
                else:
                    authorized(authorization)
                gateway.ledger.finish(operation, dict(status='unknown', message_id='', submission_started=True))
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
                returned_id = result['message_id'] = mid
            # Preserve the returned ID before a possibly interrupted readback. It is
            # the only ID reconciliation may look up; no body-based guessing or resend.
            gateway.ledger.finish(operation, result)
            try:
                result = read_chat_receipt(gateway, intent, result)
            except Exception:
                pass
            return gateway.ledger.finish(operation, result)
        except Exception as exc:
            # A failed final receipt write must not erase the platform ID already
            # returned (and possibly journaled) before the readback.
            result = dict(status='unknown' if attempted else 'rejected',
                          message_id=returned_id if attempted else '', submission_started=attempted)
            if automatic:
                from .vendor.diagnostics import CODES, process_diagnostics
                from .vendor.adapters.live import SEND_ERROR_STAGES
                code = exc.code if isinstance(exc, AdapterError) and exc.code in CODES else 'adapter_error'
                stage = exc.stage if isinstance(exc, AdapterError) and exc.stage in SEND_ERROR_STAGES else 'adapter'
                facts = process_diagnostics(exc.diagnostics) if isinstance(exc, AdapterError) else {}
                facts.update(code=code, stage=stage)
                result.update(error_code=code, stage=stage, diagnostics=facts)
            if claimed:
                return gateway.ledger.finish(operation, result)
            return result


def reconcile_chat(gateway, operation_id):
    with gateway.lock:
        previous = gateway.ledger.get(operation_id)
        if not previous or previous[0] != 'send' or previous[1].get('purpose') not in ('chat', 'continuity', 'reception'):
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
