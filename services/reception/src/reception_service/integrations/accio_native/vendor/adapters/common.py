"""Local process boundary and durable, at-most-once side-effect claims."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from typing import Callable, Protocol

from ..diagnostics import NATIVE_ERROR_CATEGORIES, process_diagnostics
from ..models import Analysis, Message
from .tool_signals import approval_required, output_has_tools


class AdapterError(ValueError):
    """The operation could not be verified; callers must retain their watermark."""

    def __init__(self, *args, code='adapter_error', stage='adapter', returncode=None, exit_code=None,
                 diagnostics=None):
        super().__init__(*args)
        self.code = code
        self.stage = stage
        self.exit_code = returncode if returncode is not None else exit_code
        self.returncode = self.exit_code
        self.diagnostics = process_diagnostics(diagnostics)


def _failure_code(stdout, stderr, fallback):
    """Inspect only for fixed categories; never return any external text."""
    # TimeoutExpired can retain byte output even when subprocess used text=True.
    stdout, stderr = tuple(value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value
                           for value in (stdout, stderr))
    text = '\n'.join(value if isinstance(value, str) else '' for value in (stdout, stderr)).lower()
    if 'i/o timeout' in text:
        return 'desktop_response_timeout' if 'desktop response' in text else 'io_timeout'
    categories = (
        ('args', ('unknown option', 'unknown argument', 'unrecognized argument', 'invalid argument', 'usage:')),
        ('tool_approval', ()),
        ('agent_selection', ('agent resolution', 'agent selection', 'agent not found', 'multiple agents', 'agent-id', 'no agent')),
        ('auth', ('unauthorized', 'authentication', 'not logged in', 'token expired', 'invalid token', '401', '403')),
        ('gateway', ('gateway', 'econnrefused', 'connection refused', 'service unavailable', '502', '503')),
        ('rate_limit', ('rate limit', 'rate_limit', 'too many requests')),
        ('backend_timeout', ('backend timeout', 'backend_timeout', 'upstream request timeout')),
        ('cli_unclassified_error', ('cli_unclassified_error',)),
        ('internal_error', ('internal error', 'internal_error', 'internal server error', '内部错误')),
    )
    for code, words in categories:
        if ((code == 'tool_approval' and approval_required(stdout, stderr))
                or any(word in text for word in words)):
            return code
    return fallback


class Gateway(Protocol):
    def capabilities(self) -> dict: ...
    def check_connection(self) -> dict: ...
    def fetch_events(self, seller_id: str, since_ms: int) -> list[Message]: ...
    def history(self, seller_id: str, buyer_id: str, conversation_id: str) -> list[Message]: ...
    def analyze(self, context: dict) -> Analysis: ...
    def send(self, intent: dict) -> dict: ...
    def retry_send(self, intent: dict) -> dict: ...
    def register(self, payload: dict, *, authorization: Callable[[], bool] | None = None) -> dict: ...
    def notify(self, payload: dict) -> dict: ...


def require_registration_authorization(authorization):
    """None supports direct legacy adapters; supplied grants must affirm each write."""
    if authorization is None:
        return
    try:
        allowed = callable(authorization) and authorization() is True
    except Exception:
        allowed = False
    if not allowed:
        raise AdapterError('登记临时权限已撤回', code='auth', stage='register')


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def decode(text: str | bytes) -> dict:
    if isinstance(text, bytes):
        text = text.decode('utf-8-sig')
    text = text.lstrip('﻿')
    # Some native commands print a banner before a complete JSON object.
    decoder = json.JSONDecoder()
    for offset in [0] + [i + 1 for i, c in enumerate(text) if c == '\n']:
        try:
            value, end = decoder.raw_decode(text[offset:].lstrip())
            if not text[offset:].lstrip()[end:].strip() and isinstance(value, dict):
                return value
        except ValueError:
            pass
    raise AdapterError('命令未返回完整 JSON 对象')


def field(value, name):
    found = []
    def visit(node):
        if isinstance(node, dict):
            if name in node:
                found.append(node[name])
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    if not found:
        return None
    if any(item != found[0] for item in found[1:]):
        raise AdapterError('原生返回字段多义：' + name)
    return found[0]


def stamp(value) -> int:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or int(value) != value:
        raise AdapterError('消息时间戳无效')
    return int(value)


def target(seller, buyer, conversation):
    import re
    if not isinstance(seller, str) or not re.fullmatch(r'[1-9][0-9]*', seller):
        raise AdapterError('缺少已核验卖家 ID')
    if not isinstance(buyer, str) or not re.fullmatch(r'[1-9][0-9]*', buyer) or buyer == seller:
        raise AdapterError('买家 ID 无效')
    match = re.fullmatch(r'([0-9]+)-([0-9]+)#.+', conversation or '')
    if not match or match.groups() not in ((seller, buyer), (buyer, seller)):
        raise AdapterError('原始会话 ID 与双方身份不匹配')


def _structured_failure(result):
    return result.get('success') is False or result.get('ok') is False or bool(result.get('error'))


def _process_facts(stdout, stderr, elapsed_ms):
    structured_error = False
    for output in (stdout, stderr):
        if isinstance(output, (str, bytes)) and output:
            try:
                structured_error = _structured_failure(decode(output)) or structured_error
            except (AdapterError, UnicodeError):
                pass
    facts = dict(elapsed_ms=elapsed_ms, stdout_present=bool(stdout), stderr_present=bool(stderr),
                 structured_error=structured_error)
    category = _failure_code(stdout, stderr, None)
    if category in NATIVE_ERROR_CATEGORIES:
        facts['native_error_category'] = category
    return facts


class Process:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def run(self, args, *, timeout=40, input=None, inherit_runtime_config=False):
        args = [str(a) for a in args]
        # Inheritance is reserved for tool-free desktop draft generation. All
        # workflows, native reads, sends and Feishu calls keep app-local state.
        if inherit_runtime_config and (args[1:3] != ['desktop', 'query'] or any(
                arg.split('=', 1)[0] in ('--approve-tools', '--yes', '--tools', '--tool-results')
                for arg in args[3:])):
            raise AdapterError('仅纯桌面生成可继承运行时配置', code='args', stage='cli')
        env = dict(os.environ)
        if not inherit_runtime_config:
            env['WORKCTL_CONFIG_DIR'] = str((self.data_dir / 'workctl-state').resolve())
        started = time.monotonic()

        def facts(stdout=None, stderr=None):
            return _process_facts(stdout, stderr, max(0, int((time.monotonic() - started) * 1000)))

        try:
            from reception_service.seller_guard import native_inheritance
            with native_inheritance(self.data_dir) as inheritance:
                proc = subprocess.run(args, input=input, capture_output=True,
                    text=True, encoding='utf-8', errors='strict', timeout=timeout, env=env,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **inheritance)
        except subprocess.TimeoutExpired as exc:
            raise AdapterError('外部命令超时；已提交的操作须核查，不自动重试', code='timeout', stage='cli',
                               diagnostics=facts(exc.stdout, exc.stderr)) from None
        except UnicodeError:
            raise AdapterError('外部命令输出编码异常', code='encoding', stage='cli',
                               diagnostics=facts()) from None
        except OSError:
            raise AdapterError('外部命令无法执行', code='spawn', stage='cli', diagnostics=facts()) from None
        if proc.returncode:
            # Never forward stdout/stderr: they can contain tokens or customer text.
            code = _failure_code(proc.stdout, proc.stderr, 'exit')
            raise AdapterError('外部命令失败（%s），未核实结果' % code,
                               code=code, stage='cli', returncode=proc.returncode, diagnostics=facts(proc.stdout, proc.stderr))
        if inherit_runtime_config and output_has_tools(proc.stdout, proc.stderr):
            raise AdapterError('桌面查询包含工具或审批信号，已阻断', code='tool_approval', stage='cli',
                               returncode=proc.returncode, diagnostics=facts(proc.stdout, proc.stderr))
        try:
            result = decode(proc.stdout)
        except UnicodeError:
            raise AdapterError('外部命令输出编码异常', code='encoding', stage='cli', returncode=0,
                               diagnostics=facts(proc.stdout, proc.stderr)) from None
        except AdapterError:
            code = _failure_code(proc.stdout, proc.stderr, 'non_json')
            raise AdapterError('外部命令未返回完整 JSON（%s）' % code,
                               code=code, stage='cli', returncode=0, diagnostics=facts(proc.stdout, proc.stderr)) from None
        if _structured_failure(result):
            code = _failure_code(json.dumps(result, ensure_ascii=False), proc.stderr, 'native_error')
            raise AdapterError('外部命令未确认成功（%s）' % code, code=code, stage='cli', returncode=0,
                               diagnostics=facts(proc.stdout, proc.stderr))
        return result


class Ledger:
    """Claim before submit. A crash leaves unknown, never permission to retry."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._established = self.path.exists()

    def _db(self, *, allow_create=False):
        from reception_service.storage_guard import LEDGER_SCHEMA, LEDGER_VERSION, validate_database
        version = validate_database(self.path, 'ledger')
        managed_live = self.path.name == 'adapters-live.sqlite3' and self.path.parent.name == 'live' and (self.path.parent / 'control.sqlite3').exists()
        if version is None and (self._established or managed_live and not allow_create):
            raise AdapterError('发送账本缺失，请保留工作区并从完整备份恢复')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                if version is None:
                    for statement in LEDGER_SCHEMA:
                        db.execute(statement)
                db.execute(f'PRAGMA user_version={LEDGER_VERSION}')
            self._established = True
            return db
        except BaseException:
            db.close()
            raise

    def initialize(self):
        """Create the empty ledger for a newly created workspace only."""
        with self.lock, closing(self._db(allow_create=True)):
            pass

    def get(self, operation_id):
        with self.lock, closing(self._db()) as db:
            row = db.execute('SELECT kind,request,result FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            return (row[0], json.loads(row[1]), json.loads(row[2])) if row else None

    def claim(self, operation_id, logical_key, kind, request, *, allow_rejected_send=False, allow_rejected_registration=False,
              conversation_fence=False):
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise AdapterError('副作用缺少 operation_id')
        with self.lock, closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT kind,request,result,logical_key FROM operations WHERE operation_id=?',
                             (operation_id,)).fetchone()
            if row:
                if row[0] != kind or json.loads(row[1]) != request:
                    raise AdapterError('操作或业务来源已绑定其他内容，禁止复用与重放')
                previous = json.loads(row[2])
                if (allow_rejected_registration is True and kind == 'register' and logical_key.startswith('register:')
                        and row[3] == logical_key and previous.get('status') == 'rejected'
                        and previous.get('submission_started') is False):
                    db.execute('INSERT INTO attempt_history(operation_id,result,stamp) VALUES (?,?,?)',
                               (operation_id, row[2], int(time.time() * 1000)))
                    db.execute('UPDATE operations SET result=? WHERE operation_id=?',
                               (json.dumps(dict(status='unknown', detail='登记已重新认领，结果须核查；禁止重放'), ensure_ascii=False), operation_id))
                    return None
                armed = db.execute('SELECT 1 FROM retry_grants WHERE operation_id=?', (operation_id,)).fetchone()
                if (armed and kind == 'send' and request.get('send_mode') == 'manual'
                        and row[3] == logical_key and previous.get('status') == 'rejected'
                        and previous.get('submission_started') is False):
                    db.execute('DELETE FROM retry_grants WHERE operation_id=?', (operation_id,))
                    db.execute('UPDATE operations SET result=? WHERE operation_id=?',
                               (json.dumps(dict(status='unknown', detail='人工重试已认领，结果须核查；禁止重放'), ensure_ascii=False), operation_id))
                    return None
                return previous
            if conversation_fence and kind == 'send':
                unresolved = db.execute("SELECT result FROM operations WHERE kind='send' "
                    "AND json_extract(request,'$.seller_id')=? AND json_extract(request,'$.conversation_id')=?",
                    (request.get('seller_id'), request.get('conversation_id'))).fetchall()
                if any(json.loads(item[0]).get('status') in ('unknown', 'prepared', 'sending') for item in unresolved):
                    raise AdapterError('本会话存在未决发送，须核查原操作')
            owner = db.execute('SELECT operation_id,kind,result FROM operations WHERE logical_key=?',
                               (logical_key,)).fetchone()
            if owner:
                previous = json.loads(owner[2])
                if not (allow_rejected_send is True and kind == owner[1] == 'send'
                        and request.get('send_mode') in ('manual', 'automatic') and logical_key.startswith('send:')
                        and previous.get('status') == 'rejected' and previous.get('submission_started') is False):
                    raise AdapterError('操作或业务来源已绑定其他内容，禁止复用与重放')
                # Retain the rejected operation and its request/receipt for audit.
                # Releasing its key and claiming the new intent are one transaction.
                db.execute('UPDATE operations SET logical_key=NULL WHERE operation_id=?', (owner[0],))
                db.execute('DELETE FROM retry_grants WHERE operation_id=?', (owner[0],))
            result = dict(status='unknown', detail='操作已认领，结果须核查；禁止重放')
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?)',
                       (operation_id, logical_key, kind, json.dumps(request, ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
            return None

    def retry_armed(self, operation_id):
        with self.lock, closing(self._db()) as db:
            return db.execute('SELECT 1 FROM retry_grants WHERE operation_id=?', (operation_id,)).fetchone() is not None

    def rearm_send(self, operation_id, request):
        """Grant one manual attempt only after definite pre-submission rejection."""
        with self.lock, closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT kind,request,result,logical_key FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            if (not row or row[0] != 'send' or json.loads(row[1]) != request
                    or request.get('send_mode') != 'manual' or row[3] is None):
                raise AdapterError('原发送意图未匹配，不能重试')
            previous = json.loads(row[2])
            if previous.get('status') != 'rejected' or previous.get('submission_started') is not False:
                raise AdapterError('只有确认尚未提交的发送可以人工重试')
            inserted = db.execute('INSERT OR IGNORE INTO retry_grants VALUES (?)', (operation_id,)).rowcount
            if inserted:
                db.execute('INSERT INTO attempt_history(operation_id,result,stamp) VALUES (?,?,?)',
                           (operation_id, row[2], int(time.time() * 1000)))
        return True

    def finish(self, operation_id, result):
        with self.lock, closing(self._db()) as db, db:
            db.execute('UPDATE operations SET result=? WHERE operation_id=?',
                       (json.dumps(result, ensure_ascii=False), operation_id))
            db.execute('DELETE FROM retry_grants WHERE operation_id=?', (operation_id,))
        return result
