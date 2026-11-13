"""Read the current seller's company name through the normal MCP channel."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

from ..errors import ServiceError
from ..image_assets import _resolve, _PinnedHTTPS, _remaining

_MAX_DOCUMENT = 512 * 1024


def unavailable(stage='read', category='unknown'):
    error = ServiceError('company_profile_unavailable', '公司名称读取失败，可重试', stage=stage,
                         diagnostics={'native_error_category': category})
    error.category = category
    return error


def _document_url(url):
    if (not isinstance(url, str) or len(url) > 8192 or not url.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in url) or '\\' in url):
        raise unavailable('source', 'invalid_url')
    try:
        parts = urlsplit(url)
        host = parts.hostname or ''
        if (parts.scheme != 'https' or parts.username is not None or parts.password is not None
                or parts.fragment or parts.port not in (None, 443)
                or not (host == 'skill.accio.com' or any(host.endswith('.' + domain) or host == domain
                                                        for domain in ('alicdn.com', 'aliyuncs.com')))
                or any(not label or len(label) > 63 or label[0] == '-' or label[-1] == '-'
                       or not all(c.isascii() and (c.isalnum() or c == '-') for c in label)
                       for label in host.split('.'))):
            raise unavailable('source', 'invalid_url')
        return parts, host
    except ValueError:
        raise unavailable('source', 'invalid_url') from None


def fetch_company_document(url):
    """Read a bounded document, validating and pinning every HTTPS redirect."""
    deadline = time.monotonic() + 15
    for hop in range(3):
        parts, host = _document_url(url)
        connection = None
        timer = None
        try:
            try:
                addresses = _resolve(host, deadline)
            except Exception:
                raise unavailable('network', 'resolution') from None
            try:
                ips = [ipaddress.ip_address(address) for address in addresses]
                if not ips or any(not ip.is_global or getattr(ip, 'ipv4_mapped', None) is not None for ip in ips):
                    raise unavailable('source', 'unsafe_address')
            except ValueError:
                raise unavailable('source', 'unsafe_address') from None
            connection = _PinnedHTTPS(host, addresses[0], deadline)

            def abort():
                sock = connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    sock.close()

            timer = threading.Timer(_remaining(deadline), abort)
            timer.daemon = True
            timer.start()
            target = (parts.path or '/') + ('?' + parts.query if parts.query else '')
            connection.request('GET', target, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if (hop == 2 or not isinstance(location, str) or not location or len(location) > 8192
                        or not location.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in location)
                        or '\\' in location):
                    raise unavailable('source', 'redirect')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise unavailable('http', 'status')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise unavailable('http', 'content_encoding')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > _MAX_DOCUMENT):
                raise unavailable('http', 'size_limit')
            body = bytearray()
            while len(body) <= _MAX_DOCUMENT:
                _remaining(deadline)
                if connection.sock is not None:
                    connection.sock.settimeout(_remaining(deadline))
                chunk = response.read(min(65536, _MAX_DOCUMENT + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            _remaining(deadline)
            if len(body) > _MAX_DOCUMENT:
                raise unavailable('http', 'size_limit')
            try:
                document = json.loads(body.decode('utf-8-sig'))
            except (ValueError, UnicodeError):
                raise unavailable('parse', 'invalid_json') from None
            if not isinstance(document, dict):
                raise unavailable('parse', 'invalid_document')
            return document
        except ServiceError:
            raise
        except Exception:
            raise unavailable('network', 'timeout' if time.monotonic() >= deadline else 'download') from None
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()


def read_company_profile(native, seller_id):
    if not isinstance(seller_id, str) or not re.fullmatch(r'[1-9][0-9]*', seller_id):
        raise unavailable('identity', 'invalid_seller')
    try:
        native._identity(seller_id)
    except Exception:
        raise unavailable('identity', 'verification') from None
    try:
        reply = native._read('ai_minisite_get_company_info', {'dataType': 'all', 'language': 'zh_CN'})
    except Exception:
        raise unavailable('native', 'read_failed') from None
    url = None
    for _ in range(8):
        if not isinstance(reply, dict) or reply.get('businessSuccess') is False or reply.get('success') is False:
            raise unavailable('native', 'business_failure')
        if url is None and isinstance(reply.get('ossUrl'), str):
            url = reply['ossUrl']
        if 'data' not in reply or reply['data'] is None:
            break
        reply = reply['data']
    else:
        raise unavailable('native', 'invalid_response')
    if url is None:
        raise unavailable('native', 'invalid_response')
    document = fetch_company_document(url)
    outer = document.get('companyBasicInfo')
    inner = outer.get('companyBasicInfo') if isinstance(outer, dict) else None
    name = inner.get('companyName') if isinstance(inner, dict) else None
    if (not isinstance(name, str) or not name.strip() or len(name) > 400
            or any(ord(char) < 32 for char in name)):
        raise unavailable('parse', 'invalid_name')
    try:
        native._identity(seller_id)
    except Exception:
        raise unavailable('identity', 'verification') from None
    return {'seller_id': seller_id, 'company_name': name.strip()}
