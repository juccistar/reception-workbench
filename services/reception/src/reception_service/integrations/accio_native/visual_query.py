"""Own-record verification for a single Accio image read.

A41 verified the completed progress event and exact Loaded 1 image result.
This verifies recorded output; it is not a platform tool sandbox or API whitelist.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
import uuid
from pathlib import Path

from .vendor.adapters.common import AdapterError
from .vendor.adapters.desktop_query import _stop, _metadata_object

_MAX_IMAGE = 6 * 1024 * 1024
_MAX_ENTRIES = 1024
_MAX_READS = 512
_MAX_BYTES = 8 * 1024 * 1024
_MAX_META = 64 * 1024
_MAX_MESSAGES = 2 * 1024 * 1024
_MAX_ROWS = 8192
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate key')
        obj[key] = value
    return obj


def _image(path, expected):
    if not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise ValueError
    p = Path(path)
    if not p.is_absolute() or p.is_symlink() or p.resolve(strict=True) != p or not p.is_file():
        raise ValueError
    with p.open('rb') as stream:
        content = stream.read(_MAX_IMAGE + 1)
    if len(content) > _MAX_IMAGE or not content:
        raise ValueError
    if not (content.startswith(b'\x89PNG\r\n\x1a\n') or content.startswith(b'\xff\xd8\xff')
            or (content.startswith(b'RIFF') and content[8:12] == b'WEBP')):
        raise ValueError
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError
    return p


def validate_read_progress(progress, call_id):
    """Require the successful terminal event observed in the A41 synthetic call."""
    if not isinstance(progress, dict) or set(progress) != {call_id}:
        return False
    events = progress[call_id]
    return (isinstance(events, list) and len(events) == 1
            and isinstance(events[0], dict) and events[0].get('toolCallId') == call_id
            and events[0].get('toolName') == 'read' and events[0].get('status') == 'completed')


def _unsafe(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r'[^a-z]', '', key.lower())
            if normalized in ('approvalrequired', 'pendingtoolcalls', 'pendingapproval', 'requiresapproval') and item:
                return True
            if _unsafe(item):
                return True
    elif isinstance(value, list):
        return any(_unsafe(item) for item in value)
    return False


def validate_trace(rows, cid, agent, prompt, image_path):
    """Return (answer, request ID) only for the exact four-row synthetic shape."""
    if isinstance(image_path, (tuple, list)):
        from .business_image_trace import validate_business_trace
        return validate_business_trace(rows, cid, agent, prompt, image_path)
    if not isinstance(rows, list) or len(rows) != 4 or _unsafe(rows):
        raise ValueError
    req, call, tool, final = rows
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError
    if any(row.get('tool_calls') or row.get('tool_progress') for row in (req, tool, final)):
        raise ValueError
    if not all(isinstance(row, dict) and row.get('conversationId') == cid
               and row.get('agentId') == agent for row in rows):
        raise ValueError
    if [r.get('role') for r in rows] != ['req', 'res', 'tool', 'res']:
        raise ValueError
    if any('seq' in row for row in rows):
        seqs = [row.get('seq') for row in rows]
        if any(type(s) is not int for s in seqs) or any(a >= b for a, b in zip(seqs, seqs[1:])):
            raise ValueError
    if (not isinstance(req.get('content'), str)
            or req['content'].replace('\r\n', '\n') != prompt.replace('\r\n', '\n')
            or req.get('status') != 'finished'):
        raise ValueError
    rid, a, b = req.get('requestId'), req.get('messageId'), call.get('messageId')
    if any(not isinstance(x, str) or not _ID.fullmatch(x) for x in (rid, a, b)) or a == b:
        raise ValueError
    if (call.get('branchParentId') != a or call.get('clientRequestId') != rid
            or final.get('messageId') != b or final.get('branchParentId') != b
            or final.get('clientRequestId') != rid or final.get('status') != 'finished'
            or final.get('tool_calls')):
        raise ValueError
    calls = call.get('tool_calls')
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise ValueError
    entry = calls[0]
    call_id = entry.get('id')
    args = entry.get('arguments')
    if (not isinstance(call_id, str) or not _ID.fullmatch(call_id) or entry.get('name') != 'read'
            or not isinstance(args, dict) or set(args) != {'file_path', 'image_prompt'}
            or args.get('file_path') != str(image_path)
            or not isinstance(args.get('image_prompt'), str)
            or not 0 < len(args['image_prompt'].strip()) <= 4096):
        raise ValueError
    if (tool.get('messageId') != b or tool.get('parentMessageId') != b
            or tool.get('branchParentId') != b or tool.get('tool_call_id') != call_id
            or tool.get('name') != 'read' or tool.get('status') != 'finished'
            or not isinstance(tool.get('content'), str) or not tool['content'].strip()
            or not re.fullmatch(r'Loaded 1 image:\r?\n  ' + re.escape(str(image_path))
                                + r' → https://[A-Za-z0-9-]+\.alicdn\.com/[^\s]+', tool['content'].strip())
            or not validate_read_progress(call.get('tool_progress'), call_id)):
        raise ValueError
    answer = final.get('content')
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError
    return answer, rid


def _binding(value):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('source') != 'default-generated' or value.get('state') != 'ready'
            or type(value.get('rootRevision')) is not int or value['rootRevision'] < 0
            or not isinstance(value.get('rootPath'), str)):
        raise ValueError
    root = Path(value['rootPath'])
    if not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError
    return root


def request_workspace(cli, agent_id, conversation_name):
    """Find one existing task's ready binding, without opening message bodies."""
    try:
        if (not isinstance(cli, (str, Path)) or not isinstance(agent_id, str)
                or not _ID.fullmatch(agent_id) or not isinstance(conversation_name, str)
                or not 1 <= len(conversation_name) <= 200 or any(ord(c) < 32 for c in conversation_name)):
            raise ValueError
        records = _Records(cli, conversation_name, agent_id, '', Path('.'), 0, time.monotonic() + 2)
        if records.folder is None:
            raise ValueError
        matches = []
        count = 0
        for path in records.folder.iterdir():
            count += 1
            if count > _MAX_ENTRIES or time.monotonic() >= records.deadline:
                raise ValueError
            if path.suffix != '.json' or path.is_symlink():
                continue
            try:
                meta = json.loads(records._read(path, _MAX_META), object_pairs_hook=_metadata_object)
            except (OSError, ValueError, UnicodeError, RecursionError):
                if records.status == 'budget_exhausted':
                    raise ValueError
                continue
            if (isinstance(meta, dict) and meta.get('name') == conversation_name
                    and meta.get('agentId') == agent_id and meta.get('id') == path.stem
                    and _ID.fullmatch(path.stem)):
                matches.append(meta)
        if len(matches) != 1:
            raise ValueError
        binding = records.workspace(matches[0])
        _binding(binding)
        return binding
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError, RecursionError):
        error = AdapterError('工作区绑定未核实', code='unverified', stage='cli')
        error.visual_stage, error.visual_status = 'workspace', 'rejected'
        raise error from None


class _Records:
    def __init__(self, cli, name, agent, prompt, image, started, deadline):
        self.name, self.agent, self.prompt, self.image = name, agent, prompt, image
        self.started, self.deadline = started, deadline
        self.reads = self.bytes = 0
        self._read_cache = {}
        self.status = 'unavailable'
        self.folder = None
        p = Path(cli)
        if not p.is_absolute():
            return
        try:
            account = next((x for x in p.parents if x.parent.name == 'accounts'), None)
            if account is None or account.resolve() != account or not p.resolve().is_relative_to(account):
                return
            folder = account / 'conversations' / 'dm'
            if folder.resolve() != folder:
                return
            self.folder, self.status = folder, 'not_found'
        except (OSError, ValueError, RuntimeError):
            pass

    def _read(self, path, limit):
        if time.monotonic() >= self.deadline:
            self.status = 'budget_exhausted'
            raise ValueError
        if path.is_symlink() or path.resolve().parent != self.folder or not path.is_file():
            raise ValueError
        info = path.stat()
        signature = (info.st_mtime_ns, info.st_size, info.st_ino)
        cached = self._read_cache.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        if self.reads >= _MAX_READS or self.bytes >= _MAX_BYTES:
            self.status = 'budget_exhausted'
            raise ValueError
        self.reads += 1
        with path.open('rb') as stream:
            data = stream.read(min(limit, _MAX_BYTES - self.bytes) + 1)
        self.bytes += len(data)
        if len(data) > limit or self.bytes > _MAX_BYTES:
            self.status = 'budget_exhausted'
            raise ValueError
        text = data.decode('utf-8-sig')
        after = path.stat()
        if signature == (after.st_mtime_ns, after.st_size, after.st_ino):
            self._read_cache[path] = (signature, text)
        return text

    def workspace(self, identity):
        # Only the uniquely matched task's full metadata is inspected.
        meta = json.loads(self._read(self.folder / (identity['id'] + '.json'), _MAX_META),
                          object_pairs_hook=_object)
        if any(meta.get(key) != identity[key] for key in ('id', 'name', 'agentId')):
            raise ValueError
        metadata = meta.get('metadata')
        if not isinstance(metadata, dict):
            raise ValueError
        return metadata.get('workspaceBinding')

    def find(self):
        if self.folder is None or self.status in ('budget_exhausted', 'rejected'):
            return None
        try:
            entries, matches = [], []
            for path in self.folder.iterdir():
                if time.monotonic() >= self.deadline or len(entries) >= _MAX_ENTRIES:
                    self.status = 'budget_exhausted'
                    return None
                entries.append(path)
                if path.suffix != '.json' or path.is_symlink():
                    continue
                try:
                    if path.stat().st_mtime < self.started - 2:
                        continue
                    meta = json.loads(self._read(path, _MAX_META), object_pairs_hook=_metadata_object)
                except (OSError, ValueError, UnicodeError, RecursionError):
                    if self.status == 'budget_exhausted':
                        return None
                    continue
                if (isinstance(meta, dict) and meta.get('name') == self.name
                        and meta.get('agentId') == self.agent and meta.get('id') == path.stem
                        and _ID.fullmatch(path.stem)):
                    matches.append(meta)
            if len(matches) != 1:
                if len(matches) > 1:
                    self.status = 'rejected'
                return None
            meta = matches[0]
            binding = self.workspace(meta)
            root = _binding(binding)
            images = self.image if isinstance(self.image, (tuple, list)) else [self.image]
            if any(not image.is_relative_to(root) for image in images):
                self.status = 'rejected'
                return None
            cid = meta['id']
            rows = []
            for path in sorted(entries):
                if path.name.startswith(cid + '.message_') and path.suffix == '.jsonl':
                    for line in self._read(path, _MAX_MESSAGES).splitlines():
                        if line.strip():
                            rows.append(json.loads(line, object_pairs_hook=_object))
                            if len(rows) > _MAX_ROWS:
                                self.status = 'budget_exhausted'
                                return None
            if (len(rows) < 4 or not isinstance(rows[-1], dict) or rows[-1].get('status') != 'finished'
                    or isinstance(self.image, (tuple, list))
                    and (rows[-1].get('role') != 'res' or rows[-1].get('tool_calls'))):
                return None
            answer, rid = validate_trace(rows, cid, self.agent, self.prompt, self.image)
            self.status = 'completed'
            return {'answer': answer, 'request_id': rid, 'workspace_binding': binding}
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Native JSONL is written while we poll. A torn trailing JSON or
            # UTF-8 sequence is pending, never an accepted result. The overall
            # deadline bounds persistent malformed records without another call.
            return None
        except (OSError, ValueError, TypeError, UnicodeError, RuntimeError, RecursionError):
            if self.status != 'budget_exhausted':
                self.status = 'rejected'
            return None


class VisualQueryRunner:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.last_diagnostics = {}
        self.last_request = {}

    def _raise(self, code):
        status = self.last_diagnostics.get('status', 'invalid')
        stage = ('preflight' if code == 'args' else 'workspace' if code == 'unavailable'
                 else 'image_changed' if code == 'image_changed'
                 else 'process' if status in ('process_error', 'cancelled') or code == 'cancelled'
                 else 'read_trace')
        self.last_diagnostics.update(visual_stage=stage, visual_status=status)
        error = AdapterError('视觉查询未核实结果（%s）' % code, code=code, stage='cli',
                             diagnostics=self.last_diagnostics)
        error.visual_stage, error.visual_status = stage, status
        raise error from None

    def run_many(self, cli, agent_id, images, prompt, *, timeout=190, cancel_event=None, expected_manifest=None):
        return self.run(cli, agent_id, [row[0] for row in images], [row[1] for row in images],
                        prompt, timeout=timeout, cancel_event=cancel_event, expected_manifest=expected_manifest)

    def run(self, cli, agent_id, image_path, image_sha256, prompt, timeout=100, cancel_event=None, expected_manifest=None):
        self.last_request = {}
        self.last_diagnostics = {'status': 'invalid', 'reads': 0, 'bytes_read': 0}
        try:
            if (not isinstance(cli, (str, Path)) or not isinstance(agent_id, str)
                    or not _ID.fullmatch(agent_id) or not isinstance(prompt, str)
                    or not prompt or len(prompt) > 140000 or type(timeout) not in (int, float)
                    or not math.isfinite(timeout) or timeout <= 0):
                raise ValueError
            multiple = isinstance(image_path, (tuple, list))
            if multiple:
                if (not isinstance(image_sha256, (tuple, list)) or not image_path
                        or len(image_path) != len(image_sha256)):
                    raise ValueError
                image = tuple(_image(p, sha) for p, sha in zip(image_path, image_sha256))
                if len(set(image)) != len(image):
                    raise ValueError
            else:
                image = _image(image_path, image_sha256)
            if multiple:
                marker = '\nimage_inputs = '
                if marker not in prompt:
                    raise ValueError
                manifest = json.loads(prompt.rsplit(marker, 1)[1], object_pairs_hook=_object)
                if (not isinstance(manifest, list) or len(manifest) != len(image)
                        or manifest != expected_manifest
                        or any(not isinstance(row, dict) for row in manifest)):
                    raise ValueError
                for row, path, sha in zip(manifest, image, image_sha256):
                    source_key = 'attachment_id' if row.get('origin') == 'business_supplement' else 'message_id'
                    if (row.get('file_path') != str(path) or row.get('image_sha256') != sha
                            or row.get('role') not in ('buyer', 'seller')
                            or source_key == 'attachment_id' and (row.get('role') != 'seller' or 'message_id' in row)
                            or source_key == 'message_id' and row.get('origin') is not None
                            or any(not isinstance(row.get(key), str) or not row[key]
                                   for key in ('alias', 'image_id', source_key, 'sender_id'))):
                        raise ValueError
                if any(len({row[key] for row in manifest}) != len(manifest)
                       for key in ('alias', 'image_id', 'file_path')):
                    raise ValueError
                if len({(row.get('origin', 'chat'), row.get('attachment_id', row.get('message_id'))) for row in manifest}) != len(manifest):
                    raise ValueError
            elif str(image) not in prompt:
                raise ValueError
            name = '接待分析草稿-视觉-' + uuid.uuid4().hex
            started = time.monotonic()
            started_at = time.time()
            self.last_request = {'conversation_name': name, 'agent_id': agent_id,
                                 'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                                 'started_at': started_at}
            records = _Records(cli, name, agent_id, prompt, image, started_at, started + timeout)
            if records.folder is None:
                self._raise('unavailable')
        except AdapterError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, RuntimeError):
            self._raise('args')
        args = [str(cli), 'desktop', 'query', '--agent-id', agent_id, '--input', '-', '--json',
                '--conversation-name', name, '--request-timeout', '180s' if multiple else '90s']
        if cancel_event is not None and cancel_event.is_set():
            self._raise('cancelled')
        proc = None
        result = None
        try:
            from reception_service.workspace_lock import track_native_child, release_native_child
            from reception_service.seller_guard import native_inheritance
            with native_inheritance(self.data_dir) as inheritance:
                proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', errors='strict',
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **inheritance)
            track_native_child(proc)
            first = True
            while time.monotonic() < started + timeout:
                if cancel_event is not None and cancel_event.is_set():
                    records.status = 'cancelled'
                    break
                remaining = started + timeout - time.monotonic()
                try:
                    proc.communicate(input=prompt if first else None, timeout=min(2.0, remaining))
                    first = False
                except subprocess.TimeoutExpired:
                    first = False
                except UnicodeError:
                    first = False
                result = records.find()
                if result is not None or records.status in ('rejected', 'budget_exhausted'):
                    break
                if proc.poll() is not None:
                    time.sleep(min(0.2, max(0, started + timeout - time.monotonic())))
        except (OSError, ValueError):
            records.status = 'process_error'
        finally:
            if proc is not None:
                if proc.poll() is None:
                    try:
                        _stop(proc)
                    except OSError:
                        records.status = 'process_error'
                release_native_child(proc)
        self.last_diagnostics = {'status': records.status, 'reads': records.reads,
                                 'bytes_read': records.bytes, 'child_stopped': proc is not None and proc.poll() is not None}
        if result is None or proc is None or proc.poll() is None:
            self._raise('unverified')
        try:
            if multiple:
                for item, sha in zip(image, image_sha256):
                    _image(item, sha)
            else:
                _image(image, image_sha256)
        except (OSError, ValueError, TypeError, RuntimeError):
            self._raise('image_changed')
        result.update(conversation_name=name, image_sha256=image_sha256)
        return result
