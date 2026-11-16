"""Feishu Open Platform read-only primitives; never reconcile or submit old CLI operations."""
from __future__ import annotations

import http.client
import json
import re
import time
from urllib.parse import parse_qs, quote, urlsplit

from .accio_native.vendor.registration_fields import CORE_ROLES, FIELD_TYPES


class FeishuOpenError(Exception):
    pass


def parse_table_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise FeishuOpenError('表格链接无效')
    try:
        url = urlsplit(value)
        host = (url.hostname or '').lower()
        if (url.scheme != 'https' or url.username is not None or url.password is not None
                or url.port is not None or url.fragment or not re.fullmatch(r'(?:[a-z0-9-]+\.)?feishu\.cn', host)):
            raise ValueError()
        match = re.fullmatch(r'/base/([A-Za-z0-9]+)', url.path)
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        table = query.get('table', [])
        if not match or len(table) != 1 or not re.fullmatch(r'tbl[A-Za-z0-9]+', table[0]):
            raise ValueError()
        return match.group(1), table[0]
    except (ValueError, TypeError):
        raise FeishuOpenError('请使用包含数据表 ID 的飞书多维表格链接') from None


def _https(method: str, path: str, headers: dict, body: bytes | None) -> dict:
    deadline = time.monotonic() + 20
    connection = http.client.HTTPSConnection('open.feishu.cn', timeout=20)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FeishuOpenError('飞书请求超时')
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, 2 * 1024 * 1024 + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                break
        raw = b''.join(chunks)
        if response.status != 200 or len(raw) > 2 * 1024 * 1024:
            raise FeishuOpenError('飞书请求失败')
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get('code') != 0:
            raise FeishuOpenError('飞书未确认请求成功')
        return result
    except (OSError, ValueError, http.client.HTTPException):
        raise FeishuOpenError('飞书请求失败') from None
    finally:
        connection.close()


def validate_schema(config: dict, fields: list[dict]) -> None:
    """Validate only supported mappings against the actual Open API field metadata."""
    if not isinstance(config, dict) or not isinstance(fields, list):
        raise FeishuOpenError('飞书字段配置无效')
    mapping = config.get('fields')
    if not isinstance(mapping, dict) or not CORE_ROLES.issubset(mapping) or set(mapping) - set(FIELD_TYPES):
        raise FeishuOpenError('飞书必需字段映射缺失或含不支持的字段')
    actual = {}
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get('field_name'), str) or not field['field_name'] or field['field_name'] in actual:
            raise FeishuOpenError('飞书字段名称缺失或重复')
        actual[field['field_name']] = field
    kinds = {'text': 1, 'number': 2, 'select': 3, 'datetime': 5, 'attachment': 17, 'auto_number': 1005}
    names = set()
    for role, meta in mapping.items():
        if not isinstance(meta, dict) or set(meta) != {'name', 'type'}:
            raise FeishuOpenError('飞书字段映射格式无效')
        name, kind = meta.get('name'), meta.get('type')
        if (not isinstance(name, str) or not name.strip() or name != name.strip() or name in names
                or kind != FIELD_TYPES[role] or re.search(r'报价|成本|审批|quotation|cost|approval', name, re.I)):
            raise FeishuOpenError('飞书字段映射名称或类型无效')
        names.add(name)
        info = actual.get(name)
        if not info or type(info.get('type')) is not int or info['type'] != kinds[kind]:
            raise FeishuOpenError('飞书实际字段类型不匹配')
        if role in ('account', 'status'):
            option = config.get('account_option' if role == 'account' else 'initial_status')
            properties = info.get('property')
            options = properties.get('options') if isinstance(properties, dict) else None
            if (not isinstance(option, str) or not option.strip() or not isinstance(options, list)
                    or any(not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'] for item in options)):
                raise FeishuOpenError('飞书单选项配置无效')
            values = [item['name'] for item in options]
            if len(set(values)) != len(values) or option not in values:
                raise FeishuOpenError('飞书实际单选项不匹配')


class FeishuOpenReader:
    """Credentials remain in memory; transport override is for isolated tests only."""
    def __init__(self, app_id: str, app_secret: str, transport=_https):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app_id or '') or not isinstance(app_secret, str) or not app_secret:
            raise FeishuOpenError('应用凭据无效')
        self._app_id, self._secret, self._transport = app_id, app_secret, transport

    def fields(self, table_url: str) -> list[dict]:
        base, table = parse_table_url(table_url)
        auth = self._transport('POST', '/open-apis/auth/v3/tenant_access_token/internal',
                               {'Content-Type': 'application/json'},
                               json.dumps({'app_id': self._app_id, 'app_secret': self._secret}).encode())
        token = auth.get('tenant_access_token')
        if not isinstance(token, str) or not token or '\r' in token or '\n' in token:
            raise FeishuOpenError('飞书未返回有效访问令牌')
        result, seen = [], set()
        page_token = None
        for _ in range(100):
            path = f'/open-apis/bitable/v1/apps/{base}/tables/{table}/fields?page_size=100'
            if page_token:
                path += '&page_token=' + quote(page_token, safe='')
            response = self._transport('GET', path, {'Authorization': 'Bearer ' + token}, None)
            data = response.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('items'), list) or type(data.get('has_more')) is not bool:
                raise FeishuOpenError('飞书字段列表不完整')
            if any(not isinstance(item, dict) for item in data['items']):
                raise FeishuOpenError('飞书字段列表无效')
            result.extend(data['items'])
            if not data['has_more']:
                return result
            page_token = data.get('page_token')
            if not isinstance(page_token, str) or not page_token or page_token in seen:
                raise FeishuOpenError('飞书字段分页无效')
            seen.add(page_token)
        raise FeishuOpenError('飞书字段分页超过限制')
