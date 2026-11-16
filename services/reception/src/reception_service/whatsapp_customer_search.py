"""Read-only, bounded customer metadata for an authenticated merchant service."""
from __future__ import annotations

import hashlib
import json
import unicodedata

from .errors import ServiceError


def _controls(value):
    return any(unicodedata.category(char) == 'Cc' for char in value)


def _display(value):
    if not isinstance(value, str):
        return ''
    return ''.join(char for char in value if unicodedata.category(char) != 'Cc').strip()[:120]


def search_merchant_customers(repo, seller_id, query, limit=25):
    """Search persisted conversation names and buyer IDs within one seller's repository.

    Revision is a SHA-256 over the selected conversation revision and returned
    metadata, so it changes when either the source revision or projection changes.
    Customer company is absent: the repository stores only the seller's company.
    """
    if (not isinstance(seller_id, str) or not seller_id.strip() or len(seller_id) > 300 or _controls(seller_id)
            or not isinstance(query, str) or len(query) > 80 or _controls(query)
            or type(limit) is not int or not 1 <= limit <= 25):
        raise ServiceError('invalid_params', '客户搜索参数无效')
    query = query.strip()
    with repo.lock:
        seller = repo.get('meta', 'seller', {})
        if not isinstance(seller, dict) or seller.get('seller_id') != seller_id:
            raise ServiceError('seller_mismatch', '数据库卖家身份不匹配')
        # Fixed projection excludes message bodies, notes, and all unrelated JSON.
        # SQLite may scan conversations, but only the requested number is returned.
        rows = repo.db.execute("""
            SELECT id, substr(CASE WHEN json_type(data, '$.customer_name')='text'
                   THEN json_extract(data, '$.customer_name') ELSE '' END,1,120),
                   substr(json_extract(data, '$.buyer_id'),1,120),
                   json_extract(data, '$.revision')
            FROM entities
            WHERE kind='conversation'
              AND json_extract(data, '$.seller_id')=?
              AND json_type(data, '$.conversation_id')='text'
              AND json_extract(data, '$.conversation_id')=id
              AND length(id) BETWEEN 1 AND 300
              AND json_type(data, '$.buyer_id')='text'
              AND length(trim(json_extract(data, '$.buyer_id'))) > 0
              AND json_type(data, '$.revision')='integer'
              AND json_extract(data, '$.revision') >= 0
              AND (instr(lower(substr(CASE WHEN json_type(data, '$.customer_name')='text'
                    THEN json_extract(data, '$.customer_name') ELSE '' END,1,120)), lower(?)) > 0
                   OR instr(lower(substr(json_extract(data, '$.buyer_id'),1,120)), lower(?)) > 0)
            ORDER BY rowid DESC LIMIT ?
        """, (seller_id, query, query, limit)).fetchall()
    result = []
    for cid, name, buyer, revision in rows:
        if _controls(cid):
            continue
        display = _display(name) or _display(buyer)
        if not display:
            continue
        projection = [seller_id, cid, display, revision]
        digest = hashlib.sha256(json.dumps(projection, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        result.append({'conversation_id': cid, 'name': display, 'revision': digest})
    return result
