"""Optional Lark user CLI. Protected fields, native preflight and read-back."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import tempfile
import time
from urllib.parse import urlsplit

from ..registration_fields import (CORE_ROLES as CORE_FIELDS, FIELD_TYPES,
    RegistrationEvidenceError as RegistrationFieldError, build_registration_fields,
    field_equal as same_value, safe_https_url)
from ..models import FACT_LABELS
from .common import AdapterError, digest, require_registration_authorization


def scalar(value):
    return value[0] if isinstance(value, list) and len(value) == 1 else value


def image_extension(raw):
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if raw.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if raw.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        return '.webp'
    raise RegistrationFieldError('图片签名不受支持，仅允许 PNG/JPEG/GIF/WebP')


def download_image(url, allowed_hosts, max_bytes, timeout=20):
    """HTTPS without proxies/redirects; resolve once and connect to a public IP."""
    if not safe_https_url(url, allowed_hosts):
        raise RegistrationFieldError('图片 URL 未通过 HTTPS 精确域名白名单')
    if type(max_bytes) is not int or not 0 < max_bytes <= 20 * 1024 * 1024:
        raise RegistrationFieldError('max_image_bytes 必须在1至20971520之间')
    parsed = urlsplit(url)
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout, context=ssl.create_default_context())

    def public_socket(address, timeout=None, source_address=None):
        addresses = socket.getaddrinfo(address[0], address[1], type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
            raise RegistrationFieldError('图片域名解析含非公网地址，拒绝下载')
        family, kind, protocol, _, endpoint = addresses[0]
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            sock.connect(endpoint)
            return sock
        except BaseException:
            sock.close()
            raise

    connection._create_connection = public_socket
    try:
        target = parsed.path or '/'
        if parsed.query:
            target += '?' + parsed.query
        connection.request('GET', target, headers={'Accept': 'image/png,image/jpeg,image/gif,image/webp', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.status != 200 or response.getheader('Content-Encoding', 'identity') != 'identity':
            raise RegistrationFieldError('图片下载未取得完整200响应，禁止跟随重定向')
        if response.getheader('Content-Type', '').split(';')[0].lower() not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
            raise RegistrationFieldError('图片响应类型不受支持')
        length = response.getheader('Content-Length')
        if length is not None and (not length.isdigit() or not 0 < int(length) <= max_bytes):
            raise RegistrationFieldError('图片响应长度为空或超过限制')
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RegistrationFieldError('图片下载超过总时限')
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise RegistrationFieldError('图片实际大小超过限制')
        if not size or (length is not None and int(length) != size):
            raise RegistrationFieldError('图片下载为空或长度不完整')
        raw = b''.join(chunks)
        image_extension(raw)
        return raw
    finally:
        connection.close()


class FeishuAdapter:
    """Configuration is explicitly supplied; never load legacy or personal config.

    feishu: cli_path, base_token, table (or table_id), form_share_token,
      fields: {buyer_id/account/status/product_name/number: {name, type}, ...},
      account_option, initial_status, write_enabled;
      notification_enabled, chat_id; registration column semantics and image hosts.
      Extended fields require fresh source-bound payload.verified_messages. Original
      quantities and budgets always remain in the evidence-bearing summary.
    """
    def __init__(self, settings, data_dir, process, ledger):
        self.settings, self.data_dir, self.process, self.ledger = settings, Path(data_dir), process, ledger

    @property
    def cfg(self):
        return self.settings.get('feishu') or {}

    def enabled(self):
        return bool(self.cfg.get('write_enabled') is True and all(self.cfg.get(k) for k in
                    ('cli_path', 'base_token', 'form_share_token', 'fields', 'account_option', 'initial_status'))
                    and (self.cfg.get('table') or self.cfg.get('table_id')))

    def notification_enabled(self):
        return bool(self.cfg.get('notification_enabled') is True and self.cfg.get('cli_path') and self.cfg.get('chat_id'))

    @property
    def table(self):
        return self.cfg.get('table') or self.cfg.get('table_id')

    def invoke(self, command, extra=(), *, authorization=None):
        args = [self.cfg['cli_path'], 'base', command]
        if command != '+form-detail':
            args += ['--base-token', self.cfg['base_token']]
        if command not in ('+form-detail', '+form-submit'):
            args += ['--table-id', self.table]
        args += list(extra) + ['--as', 'user']
        if command in ('+form-submit', '+record-batch-update', '+record-upload-attachment') and '--dry-run' not in args:
            require_registration_authorization(authorization)
        result = self.process.run(args, timeout=40)
        if (command in ('+record-list', '+record-get') and result.get('manifest_version') == 'v1'
                and result.get('format') == 'ndjson' and result.get('base_token') == self.cfg['base_token']):
            return result
        if result.get('ok') is not True or result.get('identity') != 'user':
            raise AdapterError('飞书未确认用户身份及成功结果')
        data = result.get('data')
        if not isinstance(data, (dict, list)) or (isinstance(data, dict) and data.get('ignored_fields')):
            raise AdapterError('飞书结果不完整或忽略了目标字段')
        return data

    def _mapping(self):
        mapping = self.cfg.get('fields')
        if not isinstance(mapping, dict):
            raise AdapterError('飞书字段映射未配置')
        if set(mapping) - set(FIELD_TYPES):
            raise AdapterError('飞书映射含未支持角色；不得写入人工报价、成本或审批字段')
        selected = {}
        for key, kind in FIELD_TYPES.items():
            entry = mapping.get(key)
            if entry is None and key not in CORE_FIELDS:
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not entry['name'].strip() or entry.get('type') != kind:
                raise AdapterError('飞书必需字段映射缺失或类型不符：' + key)
            if re.search(r'报价|成本|审批|quotation|cost|approval', entry['name'], re.I):
                raise AdapterError('需求字段映射不得指向报价、成本或审批列')
            selected[key] = entry
        if len({v['name'] for v in selected.values()}) != len(selected):
            raise AdapterError('飞书字段映射重名')
        return selected

    @staticmethod
    def _options(info):
        raw = info.get('options')
        if raw is None and isinstance(info.get('property'), dict):
            raw = info['property'].get('options')
        if not isinstance(raw, list) or any(not isinstance(v, dict) or not isinstance(v.get('name'), str) for v in raw):
            raise AdapterError('飞书实际单选项未取得')
        names = [v['name'] for v in raw]
        if not names or len(set(names)) != len(names):
            raise AdapterError('飞书实际单选项空白或重复')
        return names

    def _schema(self, mapping, creating):
        data = self.invoke('+field-list')
        fields = data if isinstance(data, list) else data.get('fields', data.get('items'))
        if not isinstance(fields, list) or any(not isinstance(f, dict) for f in fields):
            raise AdapterError('飞书实际字段列表无效')
        actual = {f.get('name', f.get('field_name')): f for f in fields}
        if None in actual or len(actual) != len(fields):
            raise AdapterError('飞书实际字段名称多义')
        for key, meta in mapping.items():
            info = actual.get(meta['name']) or {}
            if info.get('type') != meta['type']:
                raise AdapterError('飞书实际字段类型不匹配：' + key)
        account_name = mapping['account']['name']
        if self.cfg['account_option'] not in self._options(actual[account_name]):
            raise AdapterError('联系账号不属于实际单选项')
        if not creating:
            return {}, actual
        form = self.invoke('+form-detail', ['--share-token', self.cfg['form_share_token']])
        if (not isinstance(form, dict) or form.get('base_token') != self.cfg['base_token']
                or (form.get('table_id') and form['table_id'] != self.table)):
            raise AdapterError('飞书原表单来源不匹配')
        rows = form.get('questions')
        if not isinstance(rows, list) or not rows or any(not isinstance(q, dict) or not isinstance(q.get('title'), str) for q in rows):
            raise AdapterError('飞书原表单题目无效')
        questions = {q['title']: q for q in rows}
        if len(questions) != len(rows):
            raise AdapterError('飞书原表单题目重复')
        for key in ('buyer_id', 'account', 'status', 'product_name'):
            meta = mapping[key]
            info = questions.get(meta['name']) or {}
            if info.get('type') != meta['type']:
                raise AdapterError('飞书原表单缺少核心题目：' + key)
            if key in ('account', 'status'):
                value = self.cfg['account_option'] if key == 'account' else self.cfg['initial_status']
                if value not in self._options(info):
                    raise AdapterError('飞书原表单未包含配置单选项')
        for key, meta in mapping.items():
            if key == 'number':
                continue
            question = questions.get(meta['name'])
            if question and question.get('type') != meta['type']:
                raise AdapterError('飞书原表单扩展字段类型不匹配：' + key)
        return questions, actual

    def check_schema(self):
        """Only field/form inspection; never requires or enables write permission."""
        try:
            if not all(self.cfg.get(k) for k in ('cli_path', 'base_token', 'form_share_token', 'account_option', 'initial_status')) or not self.table:
                raise AdapterError('先配置飞书 CLI、表格、表单及账号/初始状态选项')
            mapping = self._mapping()
            questions, actual = self._schema(mapping, True)
            by_name = {meta['name']: role for role, meta in mapping.items() if role != 'number'}
            missing = [name for name, question in questions.items() if question.get('required') and name not in by_name]
            required_evidence = [by_name[name] for name, question in questions.items()
                                 if question.get('required') and name in by_name and by_name[name] not in ('buyer_id', 'account', 'status')]
            return dict(ok=not missing, status='ready' if not missing else 'blocked',
                        detail='只读字段/表单检查完成；实际登记仍需本次买家证据' if not missing else '原表单必填项没有受支持的字段映射：' + '、'.join(missing),
                        required_missing=missing, required_evidence=required_evidence,
                        mapped_roles=list(mapping), field_count=len(actual), write_enabled=self.enabled())
        except (AdapterError, OSError, ValueError, TypeError, KeyError) as exc:
            return dict(ok=False, status='blocked', detail=str(exc) if isinstance(exc, AdapterError) else '飞书只读结构检查失败；检查字段及表单配置',
                        required_missing=[], required_evidence=[], mapped_roles=[])

    def _prepare_images(self, descriptors, folder, required=()):
        config = self.cfg.get('registration') or {}
        limit = config.get('max_image_bytes', 20971520)
        files, expected, skipped, total = {}, {}, {}, 0
        deadline = time.monotonic() + 40
        for name, images in descriptors.items():
            paths, proofs = [], []
            try:
                for image in images:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdapterError('本次图片下载超过40秒总时限')
                    raw = download_image(image['url'], config.get('allowed_image_hosts', []), limit, timeout=min(20, remaining))
                    if not 0 < len(raw) <= limit:
                        raise AdapterError('图片实际大小超过配置限制')
                    total += len(raw)
                    if total > 40 * 1024 * 1024:
                        raise AdapterError('本次图片合计超过40MiB')
                    sha = hashlib.sha256(raw).hexdigest()
                    path = folder / (sha + image_extension(raw))
                    path.write_bytes(raw)
                    paths.append(str(path))
                    proofs.append(dict(name=path.name, size=len(raw), sha256=sha))
            except (AdapterError, RegistrationFieldError, OSError, ValueError, TypeError, http.client.HTTPException) as exc:
                reason = str(exc) if isinstance(exc, (AdapterError, RegistrationFieldError)) else '图片下载或校验失败'
                if name in required:
                    raise AdapterError('原表单必填图片字段未就绪：' + name + '；' + reason) from None
                skipped[name] = '可选图片字段已跳过：' + name + '；' + reason + '；请检查原图是否可访问、图片格式/大小及 allowed_image_hosts 后重新登记'
                continue
            files[name], expected[name] = paths, proofs
        return files, expected, skipped

    def _attachments_match(self, row, expected):
        """Native attachment tokens are not proof of bytes; download and hash."""
        for name, images in expected.items():
            values = row.get(name) or []
            if not isinstance(values, list):
                return False
            for image in images:
                candidates = [v for v in values if isinstance(v, dict) and v.get('name') == image['name']
                              and v.get('size') == image['size'] and isinstance(v.get('file_token'), str)
                              and re.fullmatch(r'[A-Za-z0-9_-]+', v['file_token'])]
                matched = False
                for value in candidates[:8]:
                    with tempfile.TemporaryDirectory(prefix='lark-attachment-read-', dir=self.data_dir) as folder:
                        path = Path(folder) / 'attachment.bin'
                        self.invoke('+record-download-attachment', ['--record-id', row['record_id'],
                            '--file-token', value['file_token'], '--output', str(path)])
                        if path.is_file() and path.stat().st_size == image['size'] and path.stat().st_size <= 20971520:
                            matched = hashlib.sha256(path.read_bytes()).hexdigest() == image['sha256']
                    if matched:
                        break
                if not matched:
                    return False
        return True

    def records(self, mapping, buyer, rid=None):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='lark-read-', dir=self.data_dir) as folder:
            path = Path(folder) / 'page.ndjson'
            offset, revision, seen, rows = 0, None, set(), []
            for _ in range(200):
                path.unlink(missing_ok=True)
                candidates = (path.with_suffix('.manifest.json'), Path(str(path) + '.manifest.json'))
                for candidate in candidates:
                    candidate.unlink(missing_ok=True)
                args = ['--format', 'ndjson', '--output', str(path), '--overwrite']
                for meta in mapping.values():
                    args += ['--field-id', meta['name']]
                if rid:
                    command = '+record-get'
                    args += ['--record-id', rid]
                else:
                    command = '+record-list'
                    query = dict(logic='and', conditions=[[mapping['buyer_id']['name'], '==', buyer]])
                    args += ['--limit', '2000', '--offset', str(offset), '--filter-json', json.dumps(query, ensure_ascii=False)]
                self.invoke(command, args)
                manifests = [p for p in candidates if p.is_file()]
                if len(manifests) != 1 or not path.is_file():
                    raise AdapterError('飞书分页文件或唯一 manifest 缺失')
                meta = json.loads(manifests[0].read_text(encoding='utf-8-sig'))
                if (not isinstance(meta, dict) or meta.get('base_token') != self.cfg['base_token'] or meta.get('table_id') != self.table
                        or type(meta.get('has_more')) is not bool or meta.get('rev') is None
                        or (revision is not None and revision != meta['rev'])):
                    raise AdapterError('飞书分页来源或版本不一致')
                revision = meta['rev']
                page = [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
                if type(meta.get('records_count')) is not int or meta['records_count'] != len(page):
                    raise AdapterError('飞书分页记录数量不一致')
                for row in page:
                    if not isinstance(row, dict):
                        raise AdapterError('飞书分页含无效记录')
                    record_id = row.get('record_id')
                    if not isinstance(record_id, str) or not re.fullmatch(r'rec[A-Za-z0-9_]+', record_id) or record_id in seen:
                        raise AdapterError('飞书真实行 ID 缺失或分页重复')
                    if scalar(row.get(mapping['buyer_id']['name'])) != buyer:
                        raise AdapterError('飞书返回其他买家的记录')
                    seen.add(record_id)
                    rows.append(row)
                if not meta['has_more']:
                    if rid and (len(rows) != 1 or rows[0]['record_id'] != rid):
                        raise AdapterError('飞书目标行未唯一命中')
                    return [r for r in rows if scalar(r.get(mapping['account']['name'])) == self.cfg['account_option']]
                next_offset = meta.get('next_offset')
                if rid or not page or type(next_offset) is not int or next_offset <= offset:
                    raise AdapterError('飞书分页未向前推进')
                offset = next_offset
            raise AdapterError('飞书读取超过200页，不能假装完整')

    def _route(self):
        return digest([self.cfg.get('cli_path'), self.cfg.get('base_token'), self.table,
                       self.cfg.get('form_share_token'), self.cfg.get('account_option')])

    def _verify(self, request):
        if request.get('route') != self._route():
            raise AdapterError('登记目标配置已变化，不能在其他表格核查原操作')
        mapping = request['mapping']
        rows = self.records(mapping, request['buyer'], request.get('record_id'))
        kinds = {meta['name']: meta['type'] for meta in mapping.values()}
        matches = [r for r in rows if (request.get('record_id') or r['record_id'] not in request['before_record_ids'])
                   and all(same_value(r.get(name), value, kinds[name]) for name, value in request['expected_fields'].items())]
        if len(matches) == 1 and scalar(matches[0].get(mapping['number']['name'])) and self._attachments_match(matches[0], request.get('expected_attachments') or {}):
            row = matches[0]
            number = scalar(row[mapping['number']['name']])
            if request.get('number') and request['number'] != number:
                raise AdapterError('原事项自动编号发生变化，保留原行及编号待核查')
            return dict(status='confirmed', record_id=row['record_id'], number=number,
                        case_id=(request.get('payload', {}).get('case') or {}).get('case_id', ''),
                        detail='已只读核实目标行、全部提交字段、附件内容及真实自动编号' +
                               (('；' + '；'.join(request['skipped_fields'].values())) if request.get('skipped_fields') else ''),
                        skipped=request.get('skipped_fields') or {})
        return dict(status='unknown', record_id=request.get('record_id') or '', number=request.get('number') or '',
                    case_id=(request.get('payload', {}).get('case') or {}).get('case_id', ''),
                    detail='未唯一读回全部字段、附件及真实需求编号；禁止重新提交')

    @staticmethod
    def _same_payload(left, right):
        # Case.updated_at is a local observation clock, not the write intent.
        def stable(value):
            copy = dict(value)
            copy.pop('verified_messages', None)
            case = dict(copy.get('case') or {})
            case.pop('updated_at', None)
            copy['case'] = case
            return copy
        return stable(left) == stable(right)

    def register(self, payload, *, authorization=None):
        previous = payload.get('previous') or {}
        if not self.enabled():
            return dict(status='disabled', record_id=previous.get('record_id') or '', number=previous.get('number') or '',
                        case_id=previous.get('case_id') or '', detail='飞书登记未完整配置或写入开关关闭；本地待报价可继续')
        operation = payload.get('operation_id')
        attempted = claimed = False
        retry_registration = False
        request = None
        with ExitStack() as cleanup:
            try:
                old = self.ledger.get(operation) if operation else None
                if old:
                    if old[0] != 'register' or not self._same_payload(old[1].get('payload') or {}, payload):
                        raise AdapterError('operation_id 已绑定其他登记内容')
                    retry_registration = (callable(authorization) and old[2].get('status') == 'rejected'
                                          and old[2].get('submission_started') is False)
                    if not retry_registration:
                        return old[2]
                if previous.get('status') == 'unknown':
                    return dict(status='unknown', record_id=previous.get('record_id') or '', number=previous.get('number') or '',
                                case_id=previous.get('case_id') or '', detail='此前登记结果未知，只可按原 operation_id 核查，禁止重放')
                require_registration_authorization(authorization)
                case = payload.get('case') or {}
                seller, buyer = case.get('seller_id'), case.get('buyer_id')
                if not isinstance(buyer, str) or not re.fullmatch(r'[1-9][0-9]*', buyer) or not self.settings.get('seller_id') or seller != self.settings['seller_id'] or buyer == seller:
                    raise AdapterError('飞书登记买卖双方身份不匹配')
                if not case.get('case_id') or type(case.get('version')) is not int:
                    raise AdapterError('飞书登记缺少事项及版本')
                if previous.get('case_id') and previous['case_id'] != case['case_id']:
                    raise AdapterError('登记回执属于其他采购事项，不能改写原行')
                mapping = self._mapping()
                rid = previous.get('record_id')
                if rid and not re.fullmatch(r'rec[A-Za-z0-9_]+', str(rid)):
                    raise AdapterError('更新必须使用平台真实 rec 行 ID')
                creating = not rid
                # Image requirements come from the original form even on updates.
                questions, actual = self._schema(mapping, creating or any(meta['type'] == 'attachment' for meta in mapping.values()))
                facts = case.get('facts') or {}
                product = facts.get('product') or {}
                if not isinstance(product.get('value'), str) or not product['value'].strip() or not product.get('evidence_ids'):
                    raise AdapterError('登记缺少有买家消息证据的采购内容')
                summary = []
                for key, fact in facts.items():
                    if key not in FACT_LABELS or not isinstance(fact, dict) or not isinstance(fact.get('value'), str) or not fact['value'].strip() or not fact.get('evidence_ids'):
                        raise AdapterError('采购事实不完整，不能写表')
                    summary.append(FACT_LABELS[key] + '：' + fact['value'])
                fields = {mapping['product_name']['name']: '\n'.join(summary)}
                if 'customer_name' in mapping and isinstance(case.get('customer_name'), str) and case['customer_name'].strip():
                    fields[mapping['customer_name']['name']] = case['customer_name']
                derived = build_registration_fields(payload, mapping, self.cfg.get('registration') or {})
                fields.update({mapping[role]['name']: value for role, value in derived['values'].items()})
                descriptors = {mapping[role]['name']: values for role, values in derived['images'].items()}
                before = self.records(mapping, buyer, rid)
                if rid and len(before) != 1:
                    raise AdapterError('目标行不属于当前业务员联系账号')
                if rid and previous.get('number') and scalar(before[0].get(mapping['number']['name'])) != previous['number']:
                    raise AdapterError('目标行自动编号与原事项回执不一致')
                if creating:
                    fields.update({mapping[k]['name']: v for k, v in (('buyer_id', buyer), ('account', self.cfg['account_option']), ('status', self.cfg['initial_status']))})
                    if any(all(same_value(row.get(name), value, actual[name]['type']) for name, value in fields.items()) for row in before):
                        raise AdapterError('已有相同内容，事项关系需人工核查，未重复创建')
                    missing = [name for name, q in questions.items() if q.get('required') and name not in fields and name not in descriptors]
                    if missing:
                        raise AdapterError('原表单必填项缺少可核实来源：' + '、'.join(missing) + '；核对原文单位/币种/口径或标准图片附件及域名允许列表')
                    if any(name not in questions or questions[name].get('type') != actual[name].get('type') for name in set(fields) | set(descriptors)):
                        raise AdapterError('待写字段不属于兼容原表单；将扩展字段加入原表单后再登记')
                else:
                    # Existing original source time is immutable, even when prose changes.
                    time_name = (mapping.get('inquiry_time') or {}).get('name')
                    if time_name and before[0].get(time_name) not in (None, '', []):
                        fields.pop(time_name, None)
                self.data_dir.mkdir(parents=True, exist_ok=True)
                files, expected_images = {}, {}
                if descriptors:
                    folder = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='lark-image-', dir=self.data_dir)))
                    required_images = {name for name, question in questions.items() if question.get('required')}
                    files, expected_images, skipped_images = self._prepare_images(descriptors, folder, required_images)
                    by_name = {meta['name']: role for role, meta in mapping.items()}
                    derived['skipped'].update({by_name[name]: reason for name, reason in skipped_images.items()})
                if not creating:
                    # No supported protocol proves ownership/clearing of these
                    # cells. Keep the original binding and block stale readback.
                    stale = [role for role in derived['skipped'] if role != 'inquiry_time'
                             and before[0].get(mapping[role]['name']) not in (None, '', [])]
                    if stale:
                        raise AdapterError('现有登记列无法以当前买家证据核实，保留原行及编号，未更新：' + '、'.join(stale) + '；' +
                                           '；'.join(derived['skipped'][role] for role in stale))
                    missing_images = [mapping[role]['name'] for role in ('main_image', 'sku_image') if role in mapping
                                      and questions.get(mapping[role]['name'], {}).get('required')
                                      and mapping[role]['name'] not in files]
                    if missing_images:
                        raise AdapterError('原表单必填图片字段缺少可核实来源：' + '、'.join(missing_images))
                submissions = []
                if creating:
                    args = ['--share-token', self.cfg['form_share_token'], '--json', json.dumps(dict(fields=fields, attachments=files), ensure_ascii=False)]
                    self.invoke('+form-submit', args + ['--dry-run'])
                    submissions.append(('+form-submit', args + ['--yes']))
                else:
                    changed = {name: value for name, value in fields.items() if not same_value(before[0].get(name), value, actual[name]['type'])}
                    if changed:
                        args = ['--json', json.dumps(dict(update_records={rid: changed}), ensure_ascii=False)]
                        self.invoke('+record-batch-update', args + ['--dry-run'])
                        submissions.append(('+record-batch-update', args))
                    for name, paths in files.items():
                        if self._attachments_match(before[0], {name: expected_images[name]}):
                            continue
                        args = ['--record-id', rid, '--field-id', name]
                        for path in paths:
                            args += ['--file', path]
                        self.invoke('+record-upload-attachment', args + ['--dry-run'])
                        submissions.append(('+record-upload-attachment', args))
                # Read back all intended values, including unchanged fields and immutable identity.
                expected = dict(fields)
                if not creating and time_name and before[0].get(time_name) not in (None, '', []):
                    expected[time_name] = scalar(before[0][time_name])
                expected.update({mapping['buyer_id']['name']: buyer, mapping['account']['name']: self.cfg['account_option']})
                # Fresh history is an observation, not part of the durable write intent.
                request_payload = {key: value for key, value in payload.items() if key != 'verified_messages'}
                request = dict(payload=request_payload, buyer=buyer, mapping=mapping, record_id=rid, number=previous.get('number') or '', route=self._route(),
                               before_record_ids=[r['record_id'] for r in before], expected_fields=expected, expected_attachments=expected_images,
                               skipped_fields=derived['skipped'])
                if not submissions:
                    require_registration_authorization(authorization)
                    return dict(self._verify(request), submission_started=False)
                prior = self.ledger.claim(operation, 'register:' + digest([seller, case['case_id'], case['version']]), 'register', request,
                                          allow_rejected_registration=retry_registration)
                if prior:
                    return prior
                claimed = True
                def authorize_submission():
                    nonlocal attempted
                    require_registration_authorization(authorization)
                    attempted = True
                    return True
                for command, args in submissions:
                    # invoke checks at each actual process boundary. Revocation
                    # after any earlier write stops the remaining commands and
                    # preserves unknown; only explicit reconciliation may clear it.
                    self.invoke(command, args, authorization=authorize_submission)
                result = dict(self._verify(request), submission_started=attempted)
                return self.ledger.finish(operation, result)
            except (AdapterError, OSError, ValueError, TypeError, KeyError) as exc:
                detail = '写入结果须只读核查，禁止重放' if attempted else (
                    str(exc) if isinstance(exc, (AdapterError, RegistrationFieldError)) else '飞书字段、身份、表单或读取预检未通过，未写入')
                result = dict(status='unknown' if attempted else 'rejected', record_id=(request or {}).get('record_id') or previous.get('record_id') or '',
                              number=previous.get('number') or '', case_id=previous.get('case_id') or (payload.get('case') or {}).get('case_id') or '',
                              submission_started=attempted, detail=detail)
                return self.ledger.finish(operation, result) if claimed else result

    def reconcile(self, operation_id, request, previous):
        try:
            self._schema(request['mapping'], False)
            result = dict(self._verify(request), submission_started=previous.get('submission_started', True))
            return self.ledger.finish(operation_id, result)
        except (AdapterError, OSError, ValueError, TypeError, KeyError):
            return dict(previous, status='unknown', detail='只读核查尚未通过，保留原回执，禁止重放')

    def notify(self, payload):
        if not self.notification_enabled():
            return dict(status='disabled', message_id='', detail='飞书提醒未配置或提醒开关关闭；本地待办不代表已通知')
        operation = payload.get('operation_id')
        claimed = False
        attempted = False
        try:
            case = payload.get('case') or {}
            if case.get('seller_id') != self.settings.get('seller_id') or not case.get('case_id'):
                raise AdapterError('提醒事项身份无效')
            request = dict(payload=payload, chat_id=self.cfg['chat_id'])
            prior = self.ledger.claim(operation, 'notify:' + digest([case['seller_id'], case['case_id'], payload.get('handoff_version')]), 'notify', request)
            if prior:
                return prior
            claimed = True
            text = '待报价交接\n客户：' + str(case.get('customer_name') or '') + '\n事项：' + str(case['case_id'])
            for key, fact in (case.get('facts') or {}).items():
                if key in FACT_LABELS and isinstance(fact, dict) and isinstance(fact.get('value'), str):
                    text += '\n' + FACT_LABELS[key] + '：' + fact['value']
            attempted = True
            result = self.process.run([self.cfg['cli_path'], 'im', '+messages-send', '--as', 'user',
                '--chat-id', self.cfg['chat_id'], '--text', text, '--idempotency-key', operation], timeout=40)
            data = result.get('data') or {}
            if result.get('ok') is not True or result.get('identity') != 'user' or data.get('chat_id') != self.cfg['chat_id'] or not isinstance(data.get('message_id'), str) or not data['message_id']:
                raise AdapterError('飞书未确认目标群的真实消息 ID')
            return self.ledger.finish(operation, dict(status='confirmed', message_id=data['message_id'], detail='飞书已确认目标群提醒消息 ID'))
        except (AdapterError, OSError, ValueError, TypeError, KeyError):
            result = dict(status='unknown' if attempted else 'rejected', message_id='',
                          detail='飞书提醒提交结果未知，禁止重发' if attempted else '飞书提醒意图无效，未发送')
            return self.ledger.finish(operation, result) if claimed else result
