"""Product identifiers and the user-verified Alibaba display-link format."""
from __future__ import annotations

import copy
import re

_PLATFORM_ID = re.compile(r'[1-9][0-9]{0,24}\Z')
_CUSTOM_ID = re.compile(r'定制-[0-9]{1,24}\Z')


def product_kind(value):
    if not isinstance(value, str):
        return None
    if _PLATFORM_ID.fullmatch(value):
        return 'alibaba'
    if _CUSTOM_ID.fullmatch(value):
        return 'custom'
    return None


def valid_product_id(value):
    return product_kind(value) is not None


def product_link(value):
    """A display/search reference, never proof of availability or listing facts."""
    return 'https://www.alibaba.com/product-detail/_' + value + '.html' if product_kind(value) == 'alibaba' else None


def derive_product_link(case):
    """Keep the display field separate from AI authored business content."""
    fields = case.get('requirements')
    if not isinstance(fields, dict):
        return case
    identifier = fields.get('product_id')
    identifier = identifier if isinstance(identifier, dict) else {}
    fields['product_url'] = {'value': product_link(identifier.get('value')),
                            'evidence_ids': copy.deepcopy(identifier.get('evidence_ids') or [])}
    return case
