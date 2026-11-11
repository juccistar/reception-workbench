"""Isolated AI probe: no customer reads, no repository or external writes."""
from __future__ import annotations

import copy
import hashlib

from .context import RULES
from .repository import encoded


# The facade requires numeric party IDs and a seller-buyer#suffix conversation.
# Deliberately oversized numeric sentinels never identify a real platform buyer.
SYNTHETIC_BUYERS = ('999999999999999999999999999999999999', '999999999999999999999999999999999998')


def synthetic_context(seller_id: str) -> dict:
    buyer = next(value for value in SYNTHETIC_BUYERS if value != seller_id)
    identity = {'seller_id': seller_id, 'buyer_id': buyer,
                'conversation_id': f'{seller_id}-{buyer}#synthetic-ai-check'}
    case = {**identity, 'case_id': 'synthetic-ai-check-case', 'customer_name': 'SYNTHETIC AI CHECK',
            'procurement_key': 'synthetic-ai-check', 'revision': 1, 'state': 'clarifying',
            'facts': {}, 'missing': ['product', 'quantity'], 'questions': {},
            'summary': 'Synthetic diagnostic only; no customer or procurement exists.', 'updated_at': 0}
    message = {**identity, 'message_id': 'synthetic-ai-check-message', 'role': 'buyer',
               'text': 'SYNTHETIC TEST ONLY. I am interested in ceramic mugs. Could you help clarify my request?',
               'timestamp_ms': 0, 'attachments': [], 'own': False}
    context = {'rules': copy.deepcopy(RULES), 'case': case, 'recent_messages': [message],
               'historical_evidence': [], 'knowledge': [], 'conversation_revision': 1,
               'history_coverage': {'status': 'complete', 'earliest_ms': 0, 'latest_ms': 0,
                                    'updated_at': 0, 'next_cursor': None, 'message': 'Synthetic diagnostic'},
               'budget': {'limit': 1, 'used': 0, 'remaining': 1, 'ai_used': 0, 'ai_remaining': 1},
               'required_fields': ['product', 'quantity']}
    context['fingerprint'] = hashlib.sha256(encoded(context).encode('utf-8')).hexdigest()
    return context
