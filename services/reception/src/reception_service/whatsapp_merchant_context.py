"""Private, read-only export for an independently owned WhatsApp account."""
from __future__ import annotations

import copy
import hashlib

from .business_supplement import supplement_context
from .collection_rules import collection_rules
from .errors import ServiceError
from .policy_library import select_policy_inputs
from .repository import encoded

_MAX_CONTEXT_BYTES = 192 * 1024
_REQUIREMENT_FIELDS = frozenset(('product', 'product_id', 'quantity', 'specifications', 'customization',
                                 'destination', 'delivery', 'budget', 'packaging', 'certifications', 'contact'))


def _identifier(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 300
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ServiceError('invalid_params', 'WhatsApp 标识无效')
    return value


class WhatsAppMerchantContextMixin:
    def _wa_export_merchant_context(self, account_id, chat_id, merchant_id, binding_revision, conversation_id=None):
        _identifier(account_id)
        _identifier(chat_id)
        _identifier(merchant_id)
        if type(binding_revision) is not int or binding_revision < 0:
            raise ServiceError('invalid_params', '绑定修订无效')
        if conversation_id is not None:
            _identifier(conversation_id)
        with self._lock:
            if (self.mode != 'live' or not self.account_verified or not self.seller_id or self._closing or self._closed
                    or self.repo.get('meta', 'seller', {}).get('seller_id') != self.seller_id):
                raise ServiceError('identity_required', '商家服务未核验或已关闭')
            policies = select_policy_inputs(self.repo.list('policy'), {'facts': {}, 'procurement_key': ''})
            context = {key: policies[key] for key in ('knowledge', 'guidance', 'style')}
            context['collection_rules'] = collection_rules(self.settings, None)
            owner = None
            if conversation_id is not None:
                owner = self._chat_owner(conversation_id)
                case = self.repo.get('case', owner.get('case_id')) or {}
                if case and (case.get('seller_id', self.seller_id) != self.seller_id
                             or case.get('conversation_id') != conversation_id):
                    raise ServiceError('operation_owner_mismatch', '需求不属于当前客户')
                messages = [row for row in self.repo.list('message', conversation_id=conversation_id)
                            if row.get('seller_id') == self.seller_id and row.get('buyer_id') == owner['buyer_id']
                            and row.get('conversation_id') == conversation_id]
                messages.sort(key=lambda row: (row.get('timestamp_ms') or 0, row.get('message_id') or ''))
                rows = [{'source_id': 'icbu_' + hashlib.sha256(encoded(row.get('message_id')).encode('utf-8')).hexdigest()[:20],
                         'role': row.get('role'), 'timestamp_ms': row.get('timestamp_ms'),
                         'text': str(row.get('text') or '')[:3000]} for row in messages[-20:]]
                requirements = {key: {field: copy.deepcopy(value[field]) for field in
                                      ('value', 'evidence_ids', 'source_excerpts', 'evidence_versions') if field in value}
                                for key, value in (case.get('requirements') or {}).items()
                                if key in _REQUIREMENT_FIELDS and isinstance(value, dict)}
                note = supplement_context(self.repo, conversation_id)
                note['attachments'] = [{k: a[k] for k in ('id', 'name', 'text') if k in a}
                                       for a in note.get('attachments', []) if isinstance(a.get('text'), str)]
                summary = self.repo.get('conversation_summary', conversation_id) or {}
                context['international_background'] = {'messages': rows, 'summary': {'body': summary.get('body')},
                    'requirements': requirements, 'supplement': note}
            identity = {'seller_id': self.seller_id, 'data_root': str(self.data_root.resolve()),
                        'merchant_id': merchant_id, 'account_id': account_id, 'chat_id': chat_id,
                        'binding_revision': binding_revision, 'conversation_id': conversation_id}
            # Include owner and source revisions even when their public projection is unchanged.
            revision_data = {'identity': identity, 'context': context, 'owner': owner,
                             'case_revision': case.get('revision') if owner else None,
                             'summary_revision': summary.get('revision') if owner else None}
            raw = encoded(revision_data).encode('utf-8')
            if len(raw) > _MAX_CONTEXT_BYTES:
                raise ServiceError('context_too_long', '商家上下文超出大小限制')
            return {'seller_id': self.seller_id, 'data_root': identity['data_root'],
                    'conversation_id': conversation_id, 'revision': hashlib.sha256(raw).hexdigest(), 'context': context}

    def _wa_writeback_capabilities(self):
        return {'format': 'wa-writeback-capabilities-v1', 'available': False, 'enabled': False,
                'reason': '写回能力尚未开放', 'destinations': ['merchant_workbench', 'international_platform'],
                'content_kinds': ['summary', 'requirement_changes', 'note', 'message_reference']}

    def _wa_writeback_preview(self, **_params):
        raise ServiceError('capability_unavailable', '写回能力尚未开放')

    def _wa_writeback_submit(self, **_params):
        raise ServiceError('capability_unavailable', '写回能力尚未开放')
