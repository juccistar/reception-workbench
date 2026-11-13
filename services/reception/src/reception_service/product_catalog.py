"""Seller-local product lookup and customer-local references."""
from __future__ import annotations

import hashlib
import re

from .repository import now_ms
from .errors import ServiceError

def buyer_note(text):
    return text


def candidate(context):
    return None


def key_for(seller_id, platform, product_id):
    return 'product_' + hashlib.sha256(('\0'.join((seller_id, platform, product_id))).encode()).hexdigest()


def safe_url(value, product_id):
    from .integrations.product_lookup import _reference_url
    return _reference_url(value, product_id)


def source_url(found, context):
    return None


def remember_source_url(repo, found, context, catalog):
    return catalog


def claim(repo, seller_id, product_id, request_id, *, retry=False):
    key = key_for(seller_id, 'alibaba', product_id)
    with repo.transaction():
        row = repo.get('product_catalog', key)
        if row and (not retry or (row.get('lookup') or {}).get('status') == 'querying'):
            return row, False
        row = {**(row or {}), 'catalog_product_id': key, 'seller_id': seller_id, 'platform': 'alibaba', 'product_id': product_id,
               'lookup': {'status': 'querying', 'reason': '', 'queried_at': None}, 'request_id': request_id}
        repo.put('product_catalog', key, row, history=True)
        return row, True


def lookup(repo, seller_id, found, facade, request_id, cancel_event, *, reserved=False):
    raise ServiceError("product_lookup_retired", "旧商品查询入口已停用")


def project_case(repo, case):
    if not isinstance(case, dict):
        return case
    import copy
    from .product_identity import derive_product_link, product_kind
    from .product_library import _key
    from .product_presentation import project_main_image
    result = copy.deepcopy(case)
    from .requirement_changes import requirement_revision
    result['requirements_revision'] = requirement_revision(case)
    derive_product_link(result)
    pid = ((result.get('requirements') or {}).get('product_id') or {}).get('value')
    platform = product_kind(pid)
    product = repo.get('library_product', _key(case.get('seller_id'), platform, pid)) if platform else None
    result['product_main_image'] = project_main_image(repo, product)
    result.pop('product_ref', None)
    result.pop('summary', None)
    result.pop('requirement_items', None)
    result.pop('last_changes', None)
    result['facts'] = {}
    binding = repo.get('requirement_product_binding', case.get('case_id')) or {}
    if (binding.get('seller_id') == case.get('seller_id') and binding.get('case_id') == case.get('case_id')
            and binding.get('conversation_id') == case.get('conversation_id')):
        result['local_product_id'] = binding.get('local_product_id')
        result['selected_offer_id'] = binding.get('offer_id')
    else:
        result['local_product_id'] = None
        result['selected_offer_id'] = None
    return result


def reference(found, catalog, raw, context, image_refs=None, sku=None):
    return None
