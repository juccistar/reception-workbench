"""Application-identity Feishu registration; credentials are process-local only.

The caller must supply freshly verified buyer messages, as in the native gateway.
The shared ledger is authoritative for operation ownership, even after settings change.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import http.client
import time
from datetime import datetime, timezone, timedelta
import json
import re
from urllib.parse import quote

from .feishu_open import FeishuOpenError, FeishuOpenReader, _https, parse_table_url, validate_schema
from .accio_native.vendor.adapters.common import AdapterError, digest, require_registration_authorization
from .accio_native.vendor.adapters.feishu import download_image, image_extension
from .accio_native.vendor.models import FACT_LABELS
from .accio_native.vendor.registration_fields import build_registration_fields, field_equal


class FeishuOpenAdapter:
    OWNER = 'feishu_open_v1'

    def __init__(self, settings, data_dir, process, ledger, *, transport=_https, secret_resolver=None):
        self.settings, self.ledger = settings, ledger
        self._transport, self._resolver, self._secrets = transport, secret_resolver, {}

    def configure_secret(self, app_id, secret):
        if not isinstance(app_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app_id) or not isinstance(secret, str) or not secret:
            raise FeishuOpenError('应用凭据无效')
        self._secrets[app_id] = secret

    @property
    def cfg(self):
        return self.settings.get('feishu') or {}

    def _secret(self, app_id):
        secret = self._secrets.get(app_id)
        if not secret and self._resolver:
            secret = self._resolver(app_id)
        if not isinstance(secret, str) or not secret:
            raise FeishuOpenError('应用凭据未配置')
        return secret

    def enabled(self):
        cfg = self.cfg
        return bool((cfg.get('enabled') is not False and cfg.get('write_enabled') is True) and cfg.get('app_id') and cfg.get('table_url') and cfg.get('fields') and cfg.get('account_option') and cfg.get('initial_status'))

    def notification_enabled(self):
        return False

    def notify(self, payload):
        return dict(status='disabled', message_id='', detail='应用登记适配器不发送提醒')

    def _reader(self, app_id):
        return FeishuOpenReader(app_id, self._secret(app_id), self._transport)

    def check_schema(self):
        try:
            cfg = self.cfg
            fields = self._reader(cfg['app_id']).fields(cfg['table_url'])
            validate_schema(cfg, fields)
            return dict(ok=True, status='ready', detail='只读字段检查完成；登记仍需核实买家证据', required_missing=[], required_evidence=[], mapped_roles=list(cfg['fields']), field_count=len(fields), write_enabled=self.enabled())
        except (FeishuOpenError, AdapterError, OSError, http.client.HTTPException, ValueError, TypeError, KeyError):
            return dict(ok=False, status='blocked', detail='飞书只读结构检查失败；检查应用和字段配置', required_missing=[], required_evidence=[], mapped_roles=[])

    def _token(self, app_id):
        # Use the same fixed-host transport as the reader. Never persist the token.
        response = self._transport('POST', '/open-apis/auth/v3/tenant_access_token/internal', {'Content-Type': 'application/json'}, json.dumps({'app_id': app_id, 'app_secret': self._secret(app_id)}).encode())
        token = response.get('tenant_access_token')
        if not isinstance(token, str) or not token or '\r' in token or '\n' in token:
            raise FeishuOpenError('飞书未返回有效访问令牌')
        return token

    def _call(self, method, path, token, value=None):
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        result = self._transport(method, path, headers, None if value is None else json.dumps(value, ensure_ascii=False).encode())
        if not isinstance(result, dict) or result.get('code') != 0 or not isinstance(result.get('data'), dict):
            raise FeishuOpenError('飞书未确认请求成功')
        return result['data']

    @staticmethod
    def _path(target):
        return '/open-apis/bitable/v1/apps/' + target['base'] + '/tables/' + target['table'] + '/records'

    @staticmethod
    def _record(data, record_id=None):
        row = data.get('record')
        if not isinstance(row, dict) or not isinstance(row.get('record_id'), str) or not re.fullmatch(r'rec[A-Za-z0-9_]+', row['record_id']) or not isinstance(row.get('fields'), dict) or (record_id and row['record_id'] != record_id):
            raise FeishuOpenError('飞书行回执无效')
        return row

    @staticmethod
    def _value(value):
        if isinstance(value, list) and all(isinstance(x, dict) and x.get('type') == 'text' and isinstance(x.get('text'), str) for x in value):
            return ''.join(x['text'] for x in value)
        return value

    def _get(self, target, token, rid):
        return self._record(self._call('GET', self._path(target) + '/' + rid, token), rid)

    def _list(self, target, token):
        rows, seen, pages = [], set(), set()
        page = None
        for _ in range(200):
            path = self._path(target) + '?page_size=500'
            if page:
                path += '&page_token=' + quote(page, safe='')
            data = self._call('GET', path, token)
            if not isinstance(data.get('items'), list) or type(data.get('has_more')) is not bool:
                raise FeishuOpenError('飞书记录列表不完整')
            for item in data['items']:
                row = self._record({'record': item})
                if row['record_id'] in seen:
                    raise FeishuOpenError('飞书记录分页重复')
                seen.add(row['record_id'])
                rows.append(row)
            if not data['has_more']:
                return rows
            page = data.get('page_token')
            if not isinstance(page, str) or not page or page in pages:
                raise FeishuOpenError('飞书记录分页无效')
            pages.add(page)
        raise FeishuOpenError('飞书记录分页超过限制')

    def _upload_images(self, target, token, descriptors, policy, authorization):
        values, total, deadline = {}, 0, time.monotonic() + 40
        for role, images in descriptors.items():
            tokens = []
            for image in images:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FeishuOpenError('图片准备超时')
                raw = download_image(image['url'], policy.get('allowed_image_hosts', []), policy.get('max_image_bytes', 20971520), timeout=min(20, remaining))
                total += len(raw)
                if total > 40 * 1024 * 1024:
                    raise FeishuOpenError('图片合计超过限制')
                name = hashlib.sha256(raw).hexdigest() + image_extension(raw)
                boundary = 'Reception' + hashlib.sha256(raw).hexdigest()
                parts = []
                for key, value in [('file_name', name), ('parent_type', 'bitable_file'), ('parent_node', target['base']), ('size', str(len(raw)))]:
                    parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + key + '"\r\n\r\n' + value + '\r\n').encode())
                parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="' + name + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + raw + b'\r\n')
                parts.append(('--' + boundary + '--\r\n').encode())
                require_registration_authorization(authorization)
                response = self._transport('POST', '/open-apis/drive/v1/medias/upload_all', {'Authorization': 'Bearer ' + token, 'Content-Type': 'multipart/form-data; boundary=' + boundary}, b''.join(parts))
                data = response.get('data') if isinstance(response, dict) and response.get('code') == 0 else None
                file_token = data.get('file_token') if isinstance(data, dict) else None
                if not isinstance(file_token, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', file_token):
                    raise FeishuOpenError('图片上传回执无效')
                tokens.append({'file_token': file_token})
            values[role] = tokens
        return values

    @staticmethod
    def _stable(payload):
        case = dict(payload.get('case') or {})
        case.pop('updated_at', None)
        return dict(case=case, previous=payload.get('previous') or {}, operation_id=payload.get('operation_id'))

    def _verify(self, request, rid, token):
        row = self._get(request['target'], token, rid)
        fields, mapping = row['fields'], request['mapping']
        for role, value in request['expected'].items():
            actual = fields.get(mapping[role]['name'])
            if mapping[role]['type'] == 'attachment':
                matches = isinstance(actual, list) and sorted(x.get('file_token') for x in actual if isinstance(x, dict) and isinstance(x.get('file_token'), str)) == sorted(x['file_token'] for x in value)
            else:
                matches = field_equal(self._value(actual), value, mapping[role]['type'])
            if not matches:
                raise FeishuOpenError('飞书目标行内容不匹配')
        number = self._value(fields.get(mapping['number']['name']))
        if not isinstance(number, (str, int)) or isinstance(number, bool) or not str(number):
            raise FeishuOpenError('飞书自动编号缺失')
        if request.get('number') and str(number) != str(request['number']):
            raise FeishuOpenError('飞书自动编号不匹配')
        return dict(status='confirmed', record_id=rid, number=number, case_id=request['payload']['case']['case_id'], detail='已按原目标行 ID 核实提交字段及自动编号', skipped=request['skipped'], submission_started=True)

    def register(self, payload, *, authorization=None):
        previous = payload.get('previous') or {}
        operation = payload.get('operation_id')
        case = payload.get('case') or {}
        claimed = attempted = False
        request = None
        try:
            old = self.ledger.get(operation) if operation else None
            if old:
                if old[0] != 'register' or old[1].get('adapter') != self.OWNER:
                    return dict(status='unknown', detail='原操作属于其他适配器，保留原回执，禁止重放')
                if self._stable(old[1]['payload']) != self._stable(payload):
                    raise AdapterError('operation_id 已绑定其他登记内容')
                return old[2]
            if previous.get('status') == 'unknown':
                return dict(status='unknown', record_id=previous.get('record_id') or '', number=previous.get('number') or '', case_id=previous.get('case_id') or '', detail='此前登记结果未知，只可核查原操作')
            if not self.enabled():
                return dict(status='disabled', record_id=previous.get('record_id') or '', number=previous.get('number') or '', case_id=previous.get('case_id') or '', detail='飞书登记未完整配置或关闭')
            require_registration_authorization(authorization)
            seller, buyer = case.get('seller_id'), case.get('buyer_id')
            if seller != self.settings.get('seller_id') or not seller or not isinstance(buyer, str) or not re.fullmatch(r'[1-9][0-9]*', buyer) or buyer == seller or not case.get('case_id') or type(case.get('version')) is not int:
                raise AdapterError('飞书登记买卖双方身份或事项无效')
            if previous.get('case_id') and previous['case_id'] != case['case_id']:
                raise AdapterError('登记回执属于其他事项')
            cfg = deepcopy(self.cfg)
            base, table = parse_table_url(cfg['table_url'])
            target = dict(app_id=cfg['app_id'], base=base, table=table)
            mapping = cfg['fields']
            validate_schema(cfg, self._reader(target['app_id']).fields(cfg['table_url']))
            facts = case.get('facts') or {}
            product = facts.get('product') or {}
            if not isinstance(product.get('value'), str) or not product['value'].strip() or not product.get('evidence_ids'):
                raise AdapterError('登记缺少有买家消息证据的采购内容')
            summary = []
            for key, fact in facts.items():
                if key not in FACT_LABELS or not isinstance(fact, dict) or not isinstance(fact.get('value'), str) or not fact['value'].strip() or not isinstance(fact.get('evidence_ids'), list) or not fact['evidence_ids']:
                    raise AdapterError('采购事实不完整')
                summary.append(FACT_LABELS[key] + '：' + fact['value'])
            derived = build_registration_fields(payload, mapping, cfg.get('registration') or {})
            values = dict(product_name='\n'.join(summary), buyer_id=buyer, account=cfg['account_option'])
            if not previous.get('record_id'):
                values['status'] = cfg['initial_status']
            if 'customer_name' in mapping and isinstance(case.get('customer_name'), str) and case['customer_name'].strip():
                values['customer_name'] = case['customer_name']
            values.update(derived['values'])
            for role, value in list(values.items()):
                if mapping[role]['type'] == 'datetime':
                    values[role] = int(datetime.fromisoformat(value).replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
            token = self._token(target['app_id'])
            rid = previous.get('record_id') or ''
            if rid:
                if not re.fullmatch(r'rec[A-Za-z0-9_]+', rid):
                    raise AdapterError('原行 ID 无效')
                row = self._get(target, token, rid)['fields']
                if self._value(row.get(mapping['buyer_id']['name'])) != buyer or self._value(row.get(mapping['account']['name'])) != cfg['account_option'] or (previous.get('number') and str(self._value(row.get(mapping['number']['name']))) != str(previous['number'])):
                    raise AdapterError('原行身份或编号不匹配')
                if 'inquiry_time' in values and row.get(mapping['inquiry_time']['name']) not in (None, '', []):
                    values['inquiry_time'] = self._value(row[mapping['inquiry_time']['name']])
                if any(role in mapping and row.get(mapping[role]['name']) not in (None, '', []) for role in derived['skipped'] if role != 'inquiry_time'):
                    raise AdapterError('原行含无法核实的扩展字段')
            else:
                for row in self._list(target, token):
                    cells = row['fields']
                    if all(field_equal(self._value(cells.get(mapping[role]['name'])), value, mapping[role]['type']) for role, value in values.items()):
                        raise AdapterError('已有相同内容，事项关系需人工核查')
            if derived['images']:
                values.update(self._upload_images(target, token, derived['images'], cfg.get('registration') or {}, authorization))
            safe_payload = self._stable(payload)
            request = dict(adapter=self.OWNER, payload=safe_payload, target=target, mapping=mapping, expected=values, record_id=rid, number=previous.get('number') or '', skipped=derived['skipped'])
            # Refuse rather than persist a credential accidentally supplied inside
            # business data or a field mapping. Tokens never enter this request.
            serialized = json.dumps(request, ensure_ascii=False)
            if any(secret in serialized for secret in {self._secret(target['app_id']), *self._secrets.values()} if secret):
                raise AdapterError('登记内容包含应用凭据')
            require_registration_authorization(authorization)
            prior = self.ledger.claim(operation, 'register:' + digest([seller, case['case_id'], case['version']]), 'register', request)
            if prior:
                return prior
            claimed = True
            require_registration_authorization(authorization)
            attempted = True
            data = self._call('PUT' if rid else 'POST', self._path(target) + ('/' + rid if rid else ''), token, {'fields': {mapping[role]['name']: value for role, value in values.items()}})
            receipt = self._record(data, rid or None)
            rid = receipt['record_id']
            unknown = dict(status='unknown', record_id=rid, number=previous.get('number') or '', case_id=case['case_id'], submission_started=True, detail='已取得行 ID，等待读回；禁止重放')
            self.ledger.finish(operation, unknown)
            return self.ledger.finish(operation, self._verify(request, rid, token))
        except (FeishuOpenError, AdapterError, OSError, http.client.HTTPException, ValueError, TypeError, KeyError):
            saved = self.ledger.get(operation) if claimed else None
            result = dict(saved[2] if saved else {}, status='unknown' if attempted else 'rejected', record_id=(saved[2].get('record_id') if saved else '') or '', number=previous.get('number') or '', case_id=case.get('case_id') or '', submission_started=attempted, detail='提交结果待核查，禁止重放' if attempted else '登记预检未通过，未提交')
            return self.ledger.finish(operation, result) if claimed else result

    def reconcile(self, operation_id, request, previous):
        try:
            old = self.ledger.get(operation_id)
            if not old or old[0] != 'register' or old[1].get('adapter') != self.OWNER or old[1] != request:
                return dict(previous, status='unknown', detail='原操作不属于应用登记，禁止重放')
            if old[2].get('status') in ('confirmed', 'rejected', 'disabled'):
                return old[2]
            rid = old[2].get('record_id') if old[2].get('submission_started') is True else None
            if not isinstance(rid, str) or not re.fullmatch(r'rec[A-Za-z0-9_]+', rid):
                return dict(old[2], status='unknown', detail='未取得原行 ID，保留未知，禁止重放')
            result = self._verify(request, rid, self._token(request['target']['app_id']))
            return self.ledger.finish(operation_id, result)
        except (FeishuOpenError, AdapterError, OSError, http.client.HTTPException, ValueError, TypeError, KeyError):
            return dict(previous, status='unknown', detail='原行只读核查未通过，禁止重放')
