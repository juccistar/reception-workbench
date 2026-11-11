"""Pure, conservative registration projection from fresh, source-bound messages.

Column policy is a comparison target, never evidence. Unsupported/ambiguous values
remain in the original summary and are not promoted into typed columns.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import ipaddress
import math
import re
from urllib.parse import urlsplit


FIELD_TYPES = dict(buyer_id='text', customer_name='text', customer_company='text',
    customer_email='text', account='select', product_name='text', product_url='text',
    quantity='number', main_image='attachment', sku_image='attachment', status='select',
    inquiry_time='datetime', phone='text', budget='number', number='auto_number')
CORE_ROLES = {'buyer_id', 'account', 'status', 'product_name', 'number'}
DERIVED_ROLES = set(FIELD_TYPES) - CORE_ROLES - {'customer_name'}
TABLE_TIMEZONE = timezone(timedelta(hours=8))
NUMBER = r'(?:[1-9]\d{0,2}(?:,\d{3})+|\d+)(?:\.\d+)?'
UNIT = r'(?:pieces?|pcs?|sets?|pairs?|kilograms?|kgs?|grams?|meters?|metres?|m|g|件|个|套|双|千克|公斤|克|米)'
UNIT_ALIASES = {
    'piece': 'piece', 'pieces': 'piece', 'pc': 'piece', 'pcs': 'piece', '件': 'piece', '个': 'piece',
    'set': 'set', 'sets': 'set', '套': 'set', 'pair': 'pair', 'pairs': 'pair', '双': 'pair',
    'kg': 'kg', 'kgs': 'kg', 'kilogram': 'kg', 'kilograms': 'kg', '千克': 'kg', '公斤': 'kg',
    'g': 'g', 'gram': 'g', 'grams': 'g', '克': 'g',
    'm': 'm', 'meter': 'm', 'meters': 'm', 'metre': 'm', 'metres': 'm', '米': 'm',
}


class RegistrationEvidenceError(ValueError):
    """Fixed messages only: no customer content or URLs in diagnostics."""


def _unit(value):
    return UNIT_ALIASES.get(str(value).strip().lower())


def _number(value):
    try:
        parsed = Decimal(value.replace(',', ''))
        if not parsed.is_finite() or parsed <= 0 or parsed > Decimal('9007199254740991'):
            return None
        result = int(parsed) if parsed == parsed.to_integral_value() else float(parsed)
        # Reject values whose decimal representation would change at JSON encoding.
        return result if Decimal(str(result)) == parsed else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def quantity_value(text, configured_unit='piece'):
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r'\s*(' + NUMBER + r')\s*(' + UNIT + r')\s*', text, re.I)
    if not match or not _unit(configured_unit) or _unit(match[2]) != _unit(configured_unit):
        return None
    return _number(match[1])


def budget_value(text, policy, quantity_text=''):
    if not isinstance(text, str):
        return None
    currency = policy.get('budget_currency', 'CNY')
    if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
        return None
    basis = policy.get('budget_basis', 'unit')
    unit = policy.get('budget_unit', 'piece')
    if basis == 'unit':
        match = re.fullmatch(r'\s*([A-Z]{3})\s*(' + NUMBER + r')\s*(?:/|per\s+|每)\s*(' + UNIT + r')\s*', text, re.I)
        if not match or match[1].upper() != currency or not _unit(unit) or _unit(match[3]) != _unit(unit):
            return None
        return _number(match[2])
    if basis == 'total':
        match = re.fullmatch(r'\s*(?:total(?:\s+budget)?|总预算|总计)\s*[:：]?\s*([A-Z]{3})\s*(' + NUMBER + r')\s*', text, re.I)
        # Total budget still needs an evidenced matching procurement unit.
        if match and match[1].upper() == currency and quantity_value(quantity_text, unit) is not None:
            return _number(match[2])
    return None


def table_time(timestamp_ms):
    if type(timestamp_ms) is not int or timestamp_ms <= 0:
        raise RegistrationEvidenceError('询单时间必须是已核实的平台毫秒时间戳')
    try:
        moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=timestamp_ms)
        return moment.astimezone(TABLE_TIMEZONE).isoformat(sep=' ', timespec='seconds')[:19]
    except (OverflowError, ValueError):
        raise RegistrationEvidenceError('询单时间超出支持范围') from None


def field_equal(actual, expected, kind):
    """Known native readback encodings, including UTC+8 wall clock dates."""
    if isinstance(actual, list) and len(actual) == 1:
        actual = actual[0]
    if kind == 'number':
        return type(actual) in (int, float) and math.isfinite(actual) and actual == expected
    if kind == 'datetime':
        def clock(value):
            if type(value) in (int, float) and math.isfinite(value):
                moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)
            elif isinstance(value, str):
                moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=TABLE_TIMEZONE)
            else:
                raise ValueError('date')
            return moment.astimezone(TABLE_TIMEZONE).replace(microsecond=0)
        try:
            return clock(actual) == clock(expected)
        except (ValueError, TypeError, OverflowError):
            return False
    if isinstance(actual, str) and isinstance(expected, str):
        match = re.fullmatch(r'\[[^\]]*\]\((https?://[^\s]+)\)', actual)
        if match:
            actual = match[1]
    return type(actual) is type(expected) and actual == expected


def safe_https_url(value, allowed_hosts=None):
    if not isinstance(value, str) or len(value) > 4096 or re.search(r'[\s\\\x00-\x1f\x7f]', value):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme != 'https' or not host or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or parts.fragment):
            return False
        host = host.encode('idna').decode('ascii').lower()
        if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        if host.endswith(('.localhost', '.local', '.internal', '.invalid', '.test')):
            return False
        if allowed_hosts is not None:
            if not isinstance(allowed_hosts, list) or any(not isinstance(h, str) for h in allowed_hosts):
                return False
            # Exact hosts only: suffix/wildcard matches would expand authorization.
            if host not in {h.lower() for h in allowed_hosts}:
                return False
        return True
    except (ValueError, UnicodeError):
        return False


def normalize_image_descriptor(item):
    """Project supported buyer image metadata to its source-bound identity."""
    if not isinstance(item, dict) or item.get('kind') not in ('main_image', 'sku_image'):
        return None
    mid, url = item.get('source_message_id'), item.get('url')
    if not isinstance(mid, str) or not mid or not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return dict(kind=item['kind'], source_message_id=mid, url=url)


def _fresh_sources(payload):
    case = payload.get('case') or {}
    rows = payload.get('verified_messages')
    scope = case.get('source_message_ids')
    if not isinstance(rows, list) or not isinstance(scope, list) or not scope or any(not isinstance(i, str) or not i for i in scope):
        raise RegistrationEvidenceError('扩展登记字段需要当前事项来源 ID 和实时核实的买家消息')
    sources = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RegistrationEvidenceError('实时核实消息结构无效')
        if row.get('message_id') not in scope:
            continue
        if (row.get('role') != 'buyer' or any(row.get(key) != case.get(key) for key in
                ('seller_id', 'buyer_id', 'conversation_id')) or not isinstance(row.get('text'), str)):
            raise RegistrationEvidenceError('扩展登记来源不属于当前买家事项')
        mid = row['message_id']
        if mid in sources:
            raise RegistrationEvidenceError('扩展登记来源 ID 重复')
        sources[mid] = row
    if set(scope) - sources.keys():
        raise RegistrationEvidenceError('当前事项来源未在实时买家消息中全部核实')
    return sources


def build_registration_fields(payload, mapping, policy=None):
    """Return role-keyed values, standard image descriptors and skip reasons.

    verified_messages is trusted only as an internal gateway argument. LiveGateway
    must replace it with its own fresh, identity-checked history before calling.
    """
    roles = set(mapping) & DERIVED_ROLES
    values, images, skipped = {}, {}, {}
    if not roles:
        return dict(values=values, images=images, skipped=skipped)
    sources = _fresh_sources(payload)
    case = payload['case']
    facts = case.get('facts') or {}
    policy = policy or {}

    def fact(key):
        item = facts.get(key) or {}
        text, ids = item.get('value'), item.get('evidence_ids')
        if not isinstance(text, str) or not text.strip() or not isinstance(ids, list) or not ids:
            return ''
        if any(not isinstance(mid, str) or mid not in sources or text not in sources[mid]['text'] for mid in ids):
            raise RegistrationEvidenceError('扩展字段的原文事实与当前事项来源不符')
        return text

    quantity = fact('quantity') if 'quantity' in roles or 'budget' in roles else ''
    if 'quantity' in roles:
        value = quantity_value(quantity, policy.get('quantity_unit', 'piece'))
        if value is not None:
            values['quantity'] = value
        else:
            skipped['quantity'] = '数量须为单一原文数值和匹配列口径的单位；未换算或猜填'
    if 'budget' in roles:
        value = budget_value(fact('budget'), policy, quantity)
        if value is not None:
            values['budget'] = value
        else:
            skipped['budget'] = '预算须明确唯一金额、币种及单价/总价口径，并有匹配单位'
    if 'inquiry_time' in roles:
        source = sources.get(case.get('inquiry_source_id'))
        timestamp = case.get('inquiry_timestamp_ms')
        if source and type(timestamp) is int and type(source.get('timestamp_ms')) is int and timestamp == source['timestamp_ms']:
            values['inquiry_time'] = table_time(timestamp)
        else:
            skipped['inquiry_time'] = '询单时间须绑定当前事项原始买家消息的平台毫秒时间，不能用本地时间'
    if roles & {'customer_email', 'phone', 'customer_company'}:
        contact = fact('contact')
        patterns = {
            'customer_email': r'(?<![\w.+-])[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}(?![\w.-])',
            'phone': r'(?im)(?:phone|tel(?:ephone)?|whatsapp|电话|手机)\s*[:：]\s*(\+?[0-9][0-9 ()-]{5,24}[0-9])(?=\s*(?:[;；\n]|$))',
            'customer_company': r'(?im)(?:company|公司)\s*[:：]\s*([^;；\r\n]{1,200})',
        }
        for role in roles & patterns.keys():
            matches = set(re.findall(patterns[role], contact))
            if len(matches) == 1:
                value = next(iter(matches)).strip()
                if role != 'phone' or 7 <= len(re.sub(r'\D', '', value)) <= 15:
                    values[role] = value
            if role not in values:
                skipped[role] = '联系方式须来自当前买家明确原文，且字段值唯一'
    if 'product_url' in roles:
        text = '\n'.join(fact(key) for key in ('product', 'specifications'))
        urls = set(re.findall(r'https://[^\s<>\[\]"\x27]+', text))
        urls = {url.rstrip('.,;，。；') for url in urls}
        if len(urls) == 1 and safe_https_url(next(iter(urls))):
            values['product_url'] = next(iter(urls))
        else:
            skipped['product_url'] = '产品链接须是采购内容或规格原文中的唯一有效 HTTPS 链接'
    for role in roles & {'main_image', 'sku_image'}:
        descriptors = []
        for item in case.get('attachments') or []:
            if not isinstance(item, dict) or item.get('kind') != role:
                continue
            source = sources.get(item.get('source_message_id'))
            standard = normalize_image_descriptor(item)
            if (not source or not standard or not isinstance(source.get('attachments'), list)
                    or standard not in [normalize_image_descriptor(attachment) for attachment in source['attachments']]):
                raise RegistrationEvidenceError('图片描述未在当前买家原始附件中核实，未猜测原生附件结构')
            if not safe_https_url(item['url'], policy.get('allowed_image_hosts', [])):
                skipped[role] = '图片 URL 未通过 HTTPS 精确域名白名单；请核实 allowed_image_hosts'
                continue
            if standard not in descriptors:
                descriptors.append(standard)
        if len(descriptors) > 8:
            raise RegistrationEvidenceError('单个图片列超过8张，未下载或上传')
        if descriptors:
            images[role] = descriptors
        else:
            skipped.setdefault(role, '没有当前事项可核实的标准主图/SKU图附件')
    return dict(values=values, images=images, skipped=skipped)
