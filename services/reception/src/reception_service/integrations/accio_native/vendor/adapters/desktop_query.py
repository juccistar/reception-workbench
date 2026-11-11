"""Tool-free desktop generation with optional, strictly own-request recovery.

Recovery never generates a second request. Only metadata under the account owning
this exact CLI is searched, and message bodies are opened after a unique metadata
match. The returned answer still requires the gateway's schema/evidence checks.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time
import uuid

from .common import AdapterError, Process, _failure_code, _process_facts, _structured_failure, decode
from .tool_signals import has_tool_signals as _has_tools, output_has_tools as _output_has_tools

_POLL_SECONDS = 2.0
_RECOVERY_SECONDS = 2.0
_MAX_ENTRIES = 1024
_MAX_READS = 512
_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_META_BYTES = 64 * 1024
_MAX_MESSAGE_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 8192
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}')


def _unique_object(pairs):
    """Conflicting or repeated correlation keys must never choose a winner."""
    row = {}
    for key, value in pairs:
        if key in row:
            raise ValueError('duplicate JSON key')
        row[key] = value
    return row


def _metadata_object(pairs):
    row = _unique_object(pairs)
    return {key: row[key] for key in ('id', 'name', 'agentId') if key in row}


def _stop(proc):
    """Compatibility cleanup for the existing visual-query caller.

    Text queries use OwnedProcess; keep the bounded visual helper available.
    """
    if proc.poll() is None:
        proc.kill()
    try:
        return proc.communicate(timeout=1.0)
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        try:
            proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return '', ''


def _prepare(args):
    if isinstance(args, (str, bytes)):
        raise AdapterError('桌面查询参数无效', code='args', stage='cli')
    args = [str(arg) for arg in args]
    if len(args) < 3 or not args[0] or args[1:3] != ['desktop', 'query']:
        raise AdapterError('仅允许纯桌面查询', code='args', stage='cli')
    options = {}
    i = 3
    while i < len(args):
        key, equals, value = args[i].partition('=')
        if key not in ('--agent-id', '--input', '--json', '--conversation-name', '--request-timeout') or key in options:
            raise AdapterError('桌面查询包含未知或重复参数', code='args', stage='cli')
        if key == '--json':
            if equals:
                raise AdapterError('桌面查询 JSON 参数无效', code='args', stage='cli')
            options[key] = None
        else:
            if not equals:
                i += 1
                value = args[i] if i < len(args) else ''
            if not value or value.startswith('--'):
                raise AdapterError('桌面查询参数缺少值', code='args', stage='cli')
            options[key] = value
        i += 1
    if options.get('--input') != '-' or '--json' not in options:
        raise AdapterError('桌面查询必须使用标准输入和 JSON 返回', code='args', stage='cli')
    agent = options.get('--agent-id')
    if agent is not None and not _IDENTIFIER.fullmatch(agent):
        raise AdapterError('桌面查询助手 ID 无效', code='args', stage='cli')
    # Never reuse a caller-supplied conversation, including on separate calls of
    # this runner. This name is the first recovery correlation boundary.
    name = 'reception-draft-' + uuid.uuid4().hex
    options['--conversation-name'] = name
    prepared = args[:3]
    for key, value in options.items():
        prepared.append(key)
        if value is not None:
            prepared.append(value)
    return prepared, name, agent


class _OwnRecords:
    """Bounded account-local metadata reader; no global logs or configuration."""
    def __init__(self, cli, name, agent, prompt, started, deadline):
        self.folder = None
        self.name, self.agent, self.prompt = name, agent, prompt
        self.started, self.deadline = started, deadline
        self.spent = 0.0
        self.reads = self.bytes_read = 0
        self.status = 'unavailable'
        path = Path(cli)
        if not path.is_absolute() or not agent:
            return
        try:
            account = next((p for p in path.parents if p.parent.name == 'accounts'), None)
            if account is None or account.resolve() != account or not path.resolve().is_relative_to(account):
                return
            folder = account / 'conversations' / 'dm'
            if folder.resolve() != folder:
                return
            self.folder = folder
            self.status = 'not_found'
        except (OSError, ValueError, RuntimeError):
            pass

    def _check(self):
        if (time.monotonic() >= self.scan_deadline or self.reads >= _MAX_READS
                or self.bytes_read >= _MAX_TOTAL_BYTES):
            self.status = 'budget_exhausted'
            raise ValueError

    def _read(self, path, limit):
        self._check()
        if path.is_symlink() or path.resolve().parent != self.folder or not path.is_file():
            raise ValueError
        size = path.stat().st_size
        if size > limit or self.bytes_read + size > _MAX_TOTAL_BYTES:
            self.status = 'budget_exhausted'
            raise ValueError
        self.reads += 1
        # A bounded binary read also handles a file growing after stat().
        with path.open('rb') as stream:
            data = stream.read(min(limit, _MAX_TOTAL_BYTES - self.bytes_read) + 1)
        self.bytes_read += len(data)
        self._check()
        if len(data) > limit:
            self.status = 'budget_exhausted'
            raise ValueError
        return data.decode('utf-8-sig')

    def find(self):
        if self.folder is None or self.status in ('budget_exhausted', 'rejected'):
            return None
        began = time.monotonic()
        self.scan_deadline = min(self.deadline, began + max(0, _RECOVERY_SECONDS - self.spent))
        try:
            entries, matches = [], []
            for path in self.folder.iterdir():
                self._check()
                entries.append(path)
                if len(entries) > _MAX_ENTRIES:
                    self.status = 'budget_exhausted'
                    return None
                if path.suffix != '.json' or path.is_symlink():
                    continue
                try:
                    if path.stat().st_mtime < self.started - 2:
                        continue
                    # Only metadata is read before unique correlation. A
                    # temporary unrelated file must not abort the whole scan.
                    meta = json.loads(self._read(path, _MAX_META_BYTES), object_pairs_hook=_metadata_object)
                except (OSError, ValueError, TypeError, UnicodeError, RuntimeError, RecursionError):
                    if self.status == 'budget_exhausted':
                        return None
                    continue
                if (isinstance(meta, dict) and meta.get('name') == self.name
                        and meta.get('agentId') == self.agent and meta.get('id') == path.stem
                        and _IDENTIFIER.fullmatch(path.stem)):
                    matches.append(path.stem)
            if len(matches) != 1:
                return None
            cid = matches[0]
            rows = []
            for path in entries:
                if not (path.name.startswith(cid + '.message_') and path.suffix == '.jsonl'):
                    continue
                for line in self._read(path, _MAX_MESSAGE_BYTES).splitlines():
                    self._check()
                    if not line.strip():
                        continue
                    row = json.loads(line, object_pairs_hook=_unique_object)
                    if not isinstance(row, dict) or len(rows) >= _MAX_ROWS:
                        raise ValueError
                    rows.append(row)
            # Tool/approval signals in this task cannot be hidden by wrapping
            # just a response's text in an otherwise tool-free answer envelope.
            if _has_tools(rows):
                self.status = 'rejected'
                return None
            requests = [row for row in rows if row.get('role') == 'req'
                and row.get('conversationId') == cid and row.get('agentId') == self.agent
                and isinstance(row.get('content'), str)
                and row['content'].replace('\r\n', '\n') == self.prompt.replace('\r\n', '\n')]
            if len(requests) != 1:
                return None
            request = requests[0]
            if any(not isinstance(request.get(key), str) or not _IDENTIFIER.fullmatch(request[key])
                   for key in ('requestId', 'messageId')):
                return None
            if sum(row.get('role') == 'req' and (row.get('requestId') == request['requestId']
                   or row.get('messageId') == request['messageId']) for row in rows) != 1:
                return None
            answers = [row for row in rows if row.get('role') == 'res' and row.get('status') == 'finished'
                and row.get('conversationId') == cid and row.get('agentId') == self.agent
                and row.get('branchParentId') == request['messageId']
                and row.get('clientRequestId') == request['requestId']]
            if len(answers) != 1 or not isinstance(answers[0].get('content'), str) or not answers[0]['content'].strip():
                return None
            answer = answers[0]['content']
            if _has_tools(answer):
                self.status = 'rejected'
                return None
            self._check()
            return {'answer': answer}
        except (OSError, ValueError, TypeError, UnicodeError, RuntimeError, RecursionError):
            return None
        finally:
            self.spent += max(0.0, time.monotonic() - began)


class DesktopQueryRunner:
    def __init__(self, data_dir: Path, process=None):
        self.data_dir = Path(data_dir)
        self.process = process if process is not None else Process(self.data_dir)
        self.last_diagnostics = {}
        self.last_request = {}

    def _facts(self, started, stdout, stderr, *, source='stdout', exit_code=None,
               enabled=False, status='disabled', timed_out=False, terminated=False):
        self.last_diagnostics = dict(_process_facts(stdout, stderr,
            max(0, int((time.monotonic() - started) * 1000))), result_source=source,
            cli_exit_code=exit_code, recovery_enabled=enabled, recovery_status=status,
            cli_timed_out=timed_out, cli_terminated=terminated)

    def _raise(self, code, exit_code=None):
        error = AdapterError('桌面查询未核实结果（%s）' % code, code=code,
                             stage='cli', returncode=exit_code, diagnostics=self.last_diagnostics)
        # These extra fields are all locally generated fixed values. Keep them
        # even when the shared diagnostic allowlist predates this executor.
        error.diagnostics.update(self.last_diagnostics)
        raise error from None

    def run(self, args, *, input: str, timeout=190, recover_own_results=False, cancel_event=None) -> dict:
        started = time.monotonic()
        self.last_request = {}
        self._facts(started, '', '', enabled=recover_own_results is True)
        if (not isinstance(input, str) or not input or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or timeout <= 0 or type(recover_own_results) is not bool):
            self._raise('args')
        try:
            prepared, name, agent = _prepare(args)
        except (AdapterError, TypeError, ValueError):
            self._raise('args')
        started_at = time.time()
        self.last_request = {'conversation_name': name, 'agent_id': agent,
                             'prompt_sha256': hashlib.sha256(input.encode('utf-8')).hexdigest(),
                             'started_at': started_at}
        records = _OwnRecords(prepared[0], name, agent, input, started_at, started + timeout) if recover_own_results else None
        if cancel_event is not None and cancel_event.is_set():
            self._raise('cancelled')
        if (records is None or records.folder is None) and cancel_event is None and type(self.process) is not Process:
            status = 'disabled' if records is None else 'unavailable'
            try:
                result = self.process.run(prepared, input=input, timeout=timeout, inherit_runtime_config=True)
            except AdapterError as exc:
                self._facts(started, '', '', enabled=recover_own_results, status=status,
                            exit_code=exc.exit_code, timed_out=exc.code == 'timeout')
                self.last_diagnostics.update(exc.diagnostics)
                self.last_diagnostics['cli_error_code'] = exc.code
                exc.diagnostics.update(self.last_diagnostics)
                raise
            self._facts(started, '', '', source='stdout', exit_code=0,
                        enabled=recover_own_results, status=status)
            self.last_diagnostics['stdout_present'] = True
            self.last_diagnostics.pop('stderr_present', None)  # Delegated Process does not expose it on success.
            self.last_diagnostics['cli_error_code'] = 'ok'
            if _has_tools(result):
                self.last_diagnostics['cli_error_code'] = 'tool_approval'
                self._raise('tool_approval', 0)
            return result
        stdout = stderr = ''
        proc = None
        owned = None
        recovered = None
        timed_out = terminated = False
        failure = None
        try:
            # Track this child until its exit is proved; failed bounded cleanup
            # must not let the service issue a clean-backup marker.
            from reception_service.workspace_lock import track_native_child, release_native_child
            # Omitting env inherits the native CLI environment unchanged.
            from reception_service.seller_guard import native_inheritance
            from .owned_process import OwnedProcess
            with native_inheritance(self.data_dir) as inheritance:
                owned = OwnedProcess(prepared, inheritance, input)
            proc = owned.proc
            track_native_child(owned)
            next_recovery = time.monotonic() + _POLL_SECONDS
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    failure = 'cancelled'
                    break
                remaining = started + timeout - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    failure = 'timeout'
                    break
                if owned.finished():
                    stdout, stderr = owned.strict_output()
                    break
                if time.monotonic() >= next_recovery and records is not None and records.folder is not None:
                    stdout, stderr = owned.output()
                    if _output_has_tools(stdout, stderr):
                        records.status = 'rejected'
                    recovered = records.find()
                    if recovered is not None:
                        break
                    next_recovery = time.monotonic() + _POLL_SECONDS
                time.sleep(min(0.05, remaining))
        except UnicodeError:
            failure = 'encoding'
        except OSError:
            failure = 'spawn'
        finally:
            if owned is not None:
                if owned.poll() is None:
                    terminated = True
                    try:
                        owned.stop()
                        final_out, final_err = owned.output()
                        stdout, stderr = final_out or stdout, final_err or stderr
                    except OSError:
                        failure = failure or 'spawn'
                release_native_child(owned)
        exit_code = proc.returncode if proc is not None else None
        result = None
        if _output_has_tools(stdout, stderr):
            if records is not None:
                records.status = 'rejected'
            recovered = None
            failure = failure or 'tool_approval'
        if failure is None:
            if exit_code:
                failure = _failure_code(stdout, stderr, 'exit')
            else:
                try:
                    result = decode(stdout)
                    if records is not None and records.status == 'rejected':
                        failure = 'tool_approval'
                    elif _structured_failure(result):
                        failure = _failure_code(json.dumps(result, ensure_ascii=False), stderr, 'native_error')
                except UnicodeError:
                    failure = 'encoding'
                except AdapterError:
                    failure = _failure_code(stdout, stderr, 'non_json')
        if (failure is not None and failure != 'cancelled' and recovered is None and records is not None
                and records.folder is not None and records.status != 'rejected'):
            recovered = records.find()
        if cancel_event is not None and cancel_event.is_set():
            failure, recovered = 'cancelled', None
        # A process still alive after cleanup cannot produce a trusted result.
        if exit_code is None or (owned is not None and owned.poll() is None):
            recovered = None
            failure = failure or 'spawn'
        source = 'completed_record' if failure is not None and recovered is not None else 'stdout'
        status = 'completed' if source == 'completed_record' else (records.status if records is not None else 'disabled')
        self._facts(started, stdout, stderr, source=source, exit_code=exit_code,
                    enabled=recover_own_results, status=status, timed_out=timed_out, terminated=terminated)
        if failure is None:
            return result
        if recovered is not None:
            return recovered
        self._raise(failure, exit_code)
