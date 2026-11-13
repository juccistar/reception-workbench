"""Supplemental photo lookup, tied to freshly verified native history."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from ...errors import ServiceError
from ...photos import is_photo_placeholder
from .vendor.adapters.common import target

_MAX = 9007199254740991


def _fail():
    raise ServiceError('photo_source_unverified', '图片来源无法核实', stage='read')


def _timestamp(value):
    if type(value) is not int or not 0 < value <= _MAX:
        _fail()
    return value


def _identities(record, seller, buyer, cid):
    for key, expected in (('conversationId', cid), ('conversation_id', cid),
                          ('sellerAliId', seller), ('seller_id', seller), ('selfAliId', seller),
                          ('contactAliId', buyer), ('buyerAliId', buyer), ('buyer_id', buyer)):
        if key in record and (type(record[key]) not in (str, int) or str(record[key]) != expected):
            _fail()


def _url(content):
    if not isinstance(content, str):
        _fail()
    match = re.fullmatch(r'\[image\]\s*(https://[^\s]+)\s*', content, re.ASCII)
    if not match:
        _fail()
    value = match.group(1)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in unquote(value)) or '\\' in value or '#' in value:
        _fail()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment
                or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)*', host)
                or not (host == 'clouddisk.alibaba.com' or host == 'alicdn.com' or host.endswith('.alicdn.com'))):
            _fail()
    except ValueError:
        _fail()
    return value


def _window(gateway, name, seller, buyer, cid, when, mid, sender_id):
    cursor = when + 1
    body = gateway._read(name, dict(conversationId=cid, selfAliId=int(seller), domain='icbu',
                                    count=100, limitTimeStamp=cursor, forward=False))
    if not isinstance(body, dict):
        _fail()
    _identities(body, seller, buyer, cid)
    rows = body.get('messages')
    if not isinstance(rows, list) or type(body.get('hasMore')) is not bool or len(rows) > 100:
        _fail()
    next_value = body.get('nextPointTimeStamp')
    if next_value is not None:
        if next_value == 0 and type(next_value) is int and not body['hasMore']:
            next_value = None  # Native terminal pages may use a zero cursor.
        else:
            next_value = _timestamp(next_value)
            if next_value >= cursor:
                _fail()
    if body['hasMore'] and (not rows or next_value is None):
        _fail()
    matches = []
    times = []
    for row in rows:
        if not isinstance(row, dict):
            _fail()
        _identities(row, seller, buyer, cid)
        ts = _timestamp(row.get('timestamp'))
        if ts >= cursor:
            _fail()
        times.append(ts)
        sender = row.get('senderAliId')
        if type(sender) not in (str, int) or str(sender) not in (seller, buyer):
            _fail()
        sender = str(sender)
        for key in ('senderRole', 'role'):
            if key in row and row[key] != ('buyer' if sender == buyer else 'seller'):
                _fail()
        row_id = row.get('messageId')
        if row_id is not None and (type(row_id) not in (str, int) or not str(row_id)):
            _fail()
        if row_id is not None and str(row_id) == mid and (sender != sender_id or ts != when):
            _fail()
        if sender == sender_id and ts == when:
            if row_id is not None and str(row_id) != mid:
                _fail()
            matches.append(row)
    # A page ending in the target timestamp may have split that group even if
    # the advertised cursor moves past it. Do not guess across this boundary.
    if body['hasMore'] and (min(times) >= when or next_value >= when):
        _fail()
    if len(matches) != 1:
        _fail()
    return body, matches[0]


def read_photo_source(gateway, expected_message):
    try:
        if not isinstance(expected_message, dict):
            _fail()
        seller = expected_message.get('seller_id')
        buyer = expected_message.get('buyer_id')
        cid = expected_message.get('conversation_id')
        target(seller, buyer, cid)
        _identities(expected_message, seller, buyer, cid)
        when = _timestamp(expected_message.get('timestamp_ms'))
        mid = expected_message.get('message_id')
        role = expected_message.get('role')
        if (when >= _MAX or not isinstance(mid, str) or not mid or mid.startswith('missing-id-')
                or role not in ('buyer', 'seller') or expected_message.get('own') is not (role == 'seller')
                or not is_photo_placeholder(expected_message.get('text')) or not isinstance(expected_message.get('attachments'), list)):
            _fail()
        gateway._identity(seller)
        sender_id = seller if role == 'seller' else buyer
        body, row = _window(gateway, 'query_conversation_msg', seller, buyer, cid, when, mid, sender_id)
        if row.get('messageId') is None or str(row['messageId']) != mid:
            _fail()
        normalized = gateway._messages(body, seller, buyer, cid)
        verified = [m for m in normalized if m.message_id == mid]
        if len(verified) != 1:
            _fail()
        message = verified[0]
        if (message.role != role or message.timestamp_ms != when or message.text != expected_message['text']
                or message.attachments != expected_message['attachments']):
            _fail()
        _, photo = _window(gateway, 'query_conversation_msg_for_aimode', seller, buyer, cid, when, mid, sender_id)
        return dict(resource_url=_url(photo.get('content')), source='query_conversation_msg_for_aimode',
                    association='unique_sender_timestamp')
    except Exception:
        # Native process failures and response content must not leak into diagnostics.
        raise ServiceError('photo_source_unverified', '图片来源无法核实', stage='read') from None
