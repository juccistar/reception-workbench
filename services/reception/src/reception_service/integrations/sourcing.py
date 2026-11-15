"""Read-only sourcing with exact owned-session recovery.

Tool inspection happens after execution; it is not a pre-execution sandbox.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from .accio_native.vendor.adapters.common import AdapterError
from .accio_native.vendor.adapters.desktop_query import _OwnRecords, _metadata_object, _unique_object, _IDENTIFIER
from .accio_native.vendor.adapters.owned_process import OwnedProcess
from .accio_native.vendor.adapters.tool_signals import approval_required
from reception_service.product_identity import valid_product_id
from reception_service.seller_guard import native_inheritance
from reception_service.workspace_lock import track_native_child, release_native_child

_INQUIRY_REASON = '定向询价的提交、回执和回复回收尚未验证。'
_ALLOWED = {'skill', 'product_supplier_search', 'read'}


def _error(code):
    raise AdapterError('采购查询未核实（%s）' % code, code=code, stage='cli')


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _reference_image(context):
    value = context.get('main_image_url')
    if value is None or value == '':
        return None
    if (not isinstance(value, str) or len(value) > 8192 or not value.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value):
        _error('args')
    try:
        parts = urlsplit(value)
        host = parts.hostname or ''
        if (parts.scheme != 'https' or not host.endswith('.alicdn.com') or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443) or parts.fragment or not parts.path):
            _error('args')
    except ValueError:
        _error('args')
    return value


def _prompt(context, *, legacy=False):
    product_id = str(context.get('product_id') or '')
    name = context.get('name')
    limit = context.get('supplier_limit', 3)
    if not valid_product_id(product_id) or not isinstance(name, str) or not name.strip() or len(name) > 160 or type(limit) is not int or not 1 <= limit <= 3:
        _error('args')
    # Only the separately verified public product image is eligible here.
    image = _reference_image(context) if not legacy else None
    values = {'product_id': product_id, 'product_name': name, 'supplier_limit': limit}
    if image:
        values['main_image_url'] = image
    data = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    prefix = ('只读查询商品及货源。使用必要的商品读取、采购技能及一次 product_supplier_search；'
              '不要联系客户或供应商，不要询价、发布需求、写入、下单或执行命令。'
              + ('可以将输入的main_image_url原样用于搜索工具reference_image，也可仅文字找货；不得使用其他图片。'
                 if image else '不要上传图片。')
              + '只依据工具结果挑选供应商，返回 JSON：')
    if legacy is True:
        schema = '{"candidates":[{"supplier_product_id":"商品ID","name":"供应商名"}]}。无结果返回空数组。输入：'
    elif legacy == 'url':
        schema = ('{"product_id":"输入的商品ID","product_url":null,"candidates":'
                  '[{"supplier_product_id":"商品ID","name":"供应商名"}]}。'
                  '仅在工具结果明确提供输入商品ID对应链接时填写product_url，否则为null；'
                  '不要使用其他商品链接。无供应商时返回空数组。输入：')
    else:
        schema = ('{"candidates":[{"supplier_product_id":"商品ID","name":"供应商名"}]}。'
                  '不要输出原商品链接。无供应商时返回空数组。'
                  '本地定制编号不是平台商品ID，只用产品名称进行文字找货。输入：')
    return prefix + schema + data


def _refused(value):
    if isinstance(value, list):
        return any(_refused(item) for item in value)
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r'[^a-z]', '', str(key).lower())
            if normalized in ('approvalrequired', 'pendingapproval', 'pendingtoolcalls', 'requiresapproval') and item:
                return True
            if normalized in ('status', 'approvalstatus') and isinstance(item, str) and item.lower() in ('denied', 'rejected', 'refused', 'approval_required', 'pending_approval'):
                return True
            if _refused(item):
                return True
    return False


def _trace(rows, cid, agent, prompt, reference_image=None):
    if not rows or len(rows) > 8192 or any(not isinstance(r, dict) or r.get('conversationId') != cid or r.get('agentId') != agent for r in rows):
        raise ValueError('identity')
    # Tool content can quote approval instructions; inspect host signals in
    # metadata, not the body of a read-only skill or search result.
    metadata = [{k: v for k, v in row.items() if k != 'content'} for row in rows]
    if approval_required(metadata) or _refused(metadata):
        raise ValueError('approval')
    req = rows[0]
    if req.get('role') != 'req' or req.get('status') != 'finished' or req.get('content') != prompt:
        raise ValueError('request')
    rid, root = req.get('requestId'), req.get('messageId')
    if any(not isinstance(x, str) or not _IDENTIFIER.fullmatch(x) for x in (rid, root)):
        raise ValueError('request identity')
    branch = None
    calls, results = {}, {}
    final = None
    for row in rows[1:]:
        role = row.get('role')
        if role == 'res':
            mid = row.get('messageId')
            if not isinstance(mid, str) or not _IDENTIFIER.fullmatch(mid) or row.get('clientRequestId') != rid or row.get('status') != 'finished':
                raise ValueError('response')
            if branch is None:
                if mid == root or row.get('branchParentId') != root:
                    raise ValueError('branch')
                branch = mid
            elif mid != branch or row.get('branchParentId') != branch:
                raise ValueError('branch')
            entries = row.get('tool_calls') or []
            if final is not None or not isinstance(entries, list):
                raise ValueError('final')
            if not entries:
                if not isinstance(row.get('content'), str) or not row['content'].strip():
                    raise ValueError('final')
                final = row['content']
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError('call')
                fn = entry.get('function', entry)
                if not isinstance(fn, dict):
                    raise ValueError('call')
                name, call_id, args = fn.get('name'), entry.get('id'), fn.get('arguments')
                if isinstance(args, str):
                    args = json.loads(args, object_pairs_hook=_unique_object)
                if name not in _ALLOWED or not isinstance(args, dict) or not isinstance(call_id, str) or not _IDENTIFIER.fullmatch(call_id) or call_id in calls:
                    raise ValueError('tool')
                if name == 'skill' and (args.get('action') != 'read' or args.get('plugin_id') != 'accio-com-buyer-assistant' or args.get('skill_id') != 'accio-product-supplier-sourcing'):
                    raise ValueError('skill')
                if (name == 'product_supplier_search' and 'reference_image' in args
                        and (not reference_image or args['reference_image'] != reference_image)):
                    raise ValueError('image')
                calls[call_id] = name
        elif role == 'tool':
            call_id = row.get('tool_call_id')
            if (final is not None or call_id not in calls or call_id in results or row.get('name') != calls[call_id]
                    or row.get('status') != 'finished' or row.get('messageId') != branch
                    or row.get('parentMessageId') != branch or row.get('branchParentId') != branch
                    or not isinstance(row.get('content'), str)):
                raise ValueError('tool result')
            if row.get('is_error') and calls[call_id] != 'read':
                raise ValueError('tool failure')
            results[call_id] = row
        else:
            raise ValueError('role')
    if final is None or set(calls) != set(results) or list(calls.values()).count('product_supplier_search') != 1:
        raise ValueError('incomplete')
    search = next(results[k]['content'] for k, v in calls.items() if v == 'product_supplier_search')
    return final, search, list(calls.values())


def _url(value, product_id):
    try:
        p = urlsplit(value)
        return value if p.scheme == 'https' and p.hostname == 'www.alibaba.com' and not p.username and not p.password and re.search(r'_' + re.escape(product_id) + r'\.html$', p.path) and not p.query and not p.fragment else None
    except (ValueError, TypeError):
        return None


def _normalize(context, record, cid, answer, search, tools, source):
    # The observed search format is a markdown table. Never use a title or
    # infer a number from the model's answer; values come from the same row.
    table = {}
    for line in search.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 11 or not re.fullmatch(r'\d{1,40}', cells[1]):
            continue
        pid = cells[1]
        if pid in table:
            table[pid] = None
        else:
            table[pid] = cells
    try:
        text = answer.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
        answer_data = json.loads(text, object_pairs_hook=_unique_object)
        if not isinstance(answer_data, dict):
            answer_data = {}
    except (ValueError, AttributeError, TypeError):
        answer_data = {}
    selected = answer_data.get('candidates', [])
    suppliers = []
    if isinstance(selected, list):
        for candidate in selected:
            if not isinstance(candidate, dict):
                continue
            pid, name = candidate.get('supplier_product_id'), candidate.get('name')
            cells = table.get(pid) if isinstance(pid, str) else None
            if not cells or not isinstance(name, str) or not name.strip() or name.strip() != cells[7].split(' / ')[0].strip():
                continue
            url = _url(cells[10], pid)
            if not url or any(s['name'] == name for s in suppliers):
                continue
            price = re.fullmatch(r'US\$([0-9]+(?:\.[0-9]+)?)(?:-([0-9]+(?:\.[0-9]+)?))?\s*/\s*([0-9]+)\s+([A-Za-z]+)', cells[4])
            offer = dict(supplier_product_id=pid, product_url=url, specifications='', moq=price.group(3) if price else '', unit=price.group(4) if price else '', unit_price=(price.group(1) if price and not price.group(2) else ''), currency='USD' if price else '', lead_time=cells[6], packaging='', freight='', tax_included=None, valid_until=None, source_type='page_reference', source_note='平台页面参考，非供应商确认报价', customer_wording='', enabled=True, reply_enabled=False)
            suppliers.append(dict(platform_supplier_id=None, name=name, product_url=None, contact='', offers=[offer]))
            if len(suppliers) >= context.get('supplier_limit', 3):
                break
    pid = str(context['product_id'])
    # The separate product-library route owns customer product URLs.
    return dict(product_id=pid, product_url=None, product_images=[], suppliers=suppliers,
                execution=dict(conversation_name=record['conversation_name'], conversation_id=cid, request_sha256=record['prompt_sha256'], request_id=record['request_id'], tool_names=tools, search_calls=1, result_source=source), inquiry_status='blocked', inquiry_reason=_INQUIRY_REASON)


class _SourcingRecords(_OwnRecords):
    def find(self):
        if self.folder is None:
            return None
        self.scan_deadline = min(self.deadline, time.monotonic() + 2)
        try:
            entries, matches = [], []
            for path in self.folder.iterdir():
                self._check()
                entries.append(path)
                if len(entries) > 1024:
                    raise ValueError('entries')
                if path.suffix != '.json' or path.is_symlink():
                    continue
                try:
                    meta = json.loads(self._read(path, 65536), object_pairs_hook=_metadata_object)
                except (OSError, ValueError, UnicodeError):
                    if self.status == 'budget_exhausted':
                        raise
                    continue
                if meta.get('name') == self.name and meta.get('agentId') == self.agent and meta.get('id') == path.stem and _IDENTIFIER.fullmatch(path.stem):
                    matches.append(path.stem)
            if len(matches) != 1:
                if len(matches) > 1:
                    self.status = 'rejected'
                return None
            cid = matches[0]
            self.status = 'existing'
            rows = []
            for path in sorted(entries):
                if path.name.startswith(cid + '.message_') and path.suffix == '.jsonl':
                    for line in self._read(path, 2 * 1024 * 1024).splitlines():
                        if line.strip():
                            rows.append(json.loads(line, object_pairs_hook=_unique_object))
                            if len(rows) > 8192:
                                raise ValueError('rows')
            if not rows or rows[-1].get('role') != 'res' or rows[-1].get('tool_calls') or not rows[-1].get('content'):
                return None
            answer, search, tools = _trace(rows, cid, self.agent, self.prompt, getattr(self, 'reference_image', None))
            self.status = 'completed'
            return cid, answer, search, tools
        except (OSError, ValueError, TypeError, UnicodeError, RuntimeError, RecursionError):
            self.status = 'rejected'
            return None


def reconcile_source_product(native, agent_id, record, context, *, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        _error('cancelled')
    try:
        prompt, request_id = record['prompt'], record['request_id']
        if (record['agent_id'] != agent_id or not isinstance(request_id, str) or not request_id
                or record['conversation_name'] != 'reception-source-' + _hash(request_id)[:32]
                or record['prompt_sha256'] != _hash(prompt) or prompt not in (_prompt(context), _prompt(context, legacy=True), _prompt(context, legacy='url'))):
            _error('args')
        records = _SourcingRecords(native.cli, record['conversation_name'], agent_id, prompt, record['started_at'], time.monotonic() + 3)
        records.reference_image = _reference_image(context) if prompt == _prompt(context) else None
        found = records.find()
    except (KeyError, TypeError, AttributeError):
        _error('args')
    if not found:
        _error('unsafe_trace' if records.status == 'rejected' else 'unverified')
    if cancel_event is not None and cancel_event.is_set():
        _error('cancelled')
    cid, answer, search, tools = found
    return _normalize(context, record, cid, answer, search, tools, 'completed_record')


def run_source_product(native, agent_id, context, request_id, *, cancel_event=None, on_call_start=None, on_request_created=None):
    prompt = _prompt(context)
    if not isinstance(agent_id, str) or not _IDENTIFIER.fullmatch(agent_id) or not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        _error('args')
    record = dict(conversation_name='reception-source-' + _hash(request_id)[:32], agent_id=agent_id, prompt_sha256=_hash(prompt), prompt=prompt, started_at=time.time(), request_id=request_id)
    records = _SourcingRecords(native.cli, record['conversation_name'], agent_id, prompt, record['started_at'], time.monotonic() + 190)
    if records.folder is None:
        _error('unavailable')
    records.find()
    if records.status != 'not_found':
        _error('unverified')
    if cancel_event is not None and cancel_event.is_set():
        _error('cancelled')
    if on_request_created is None:
        _error('args')
    on_request_created(record.copy())
    if cancel_event is not None and cancel_event.is_set():
        _error('cancelled')
    if on_call_start is not None:
        on_call_start()
    args = [str(native.cli), 'desktop', 'query', '--agent-id', agent_id, '--input', '-', '--json', '--conversation-name', record['conversation_name'], '--request-timeout', '180s']
    owned = None
    try:
        with native_inheritance(native.data_dir) as inheritance:
            owned = OwnedProcess(args, inheritance, prompt)
        track_native_child(owned)
        deadline = time.monotonic() + 190
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                _error('cancelled')
            if owned.finished():
                break
            time.sleep(.05)
    except OSError:
        _error('unverified')
    finally:
        if owned is not None:
            if owned.poll() is None:
                owned.stop()
            release_native_child(owned)
    if owned is None or owned.poll() is None:
        _error('unverified')
    stdout, stderr = owned.output()
    if approval_required(stdout, stderr):
        _error('unsafe_trace')
    return reconcile_source_product(native, agent_id, record, context, cancel_event=cancel_event)
