"""Merchant-local products, supplier offers and durable, nonrepeating sourcing.

Only validated AI requirements create products. No chat/card/title parser lives
here. A sourcing task owns one external request; unknown is reconciled, never
resubmitted. Supplier inquiry remains blocked until a controlled transport exists.
"""
from __future__ import annotations

import copy
import hashlib
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit, urlunsplit

from .errors import ServiceError
from .repository import encoded, new_id, now_ms
from .product_identity import valid_product_id, product_kind, product_link, derive_product_link
from .product_presentation import ProductPresentationMixin, project_main_image

PRODUCT_METHODS = frozenset({
    'list_products', 'get_product', 'list_suppliers', 'get_supplier', 'save_supplier',
    'save_product_offer', 'select_supplier_offer', 'bind_requirement_product',
    'set_product_default_offer', 'get_procurement_settings', 'save_procurement_settings',
    'set_procurement_enabled', 'list_procurement_tasks', 'request_procurement', 'reconcile_procurement',
})
_PID = re.compile(r'[1-9][0-9]{0,24}\Z')
_SEED_FIELDS = ('product', 'product_id', 'quantity', 'specifications', 'customization',
                'packaging', 'certifications', 'delivery')
_OFFER_TEXT = ('supplier_product_id', 'specifications', 'moq', 'unit', 'unit_price', 'currency',
               'lead_time', 'packaging', 'freight', 'source_note', 'customer_wording')
_OFFER_FIELDS = frozenset((*_OFFER_TEXT, 'product_url', 'tax_included', 'valid_until',
                           'source_type', 'enabled', 'reply_enabled'))
_DEFAULTS = {'auto_find': True, 'auto_inquiry': True, 'supplier_limit': 3}
_UNSET = object()


def requirement_value(case, field):
    row = (case.get('requirements') or {}).get(field)
    return row.get('value') if isinstance(row, dict) else None


def _text(value, maximum=2000, *, empty=True):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value or (not empty and not value.strip()):
        raise ServiceError('invalid_params', '产品或供应商字段无效')
    return value.strip()


def _request_id(value):
    return _text(value, 200, empty=False)


def _url(value, product_id=None):
    if value in (None, ''):
        return None
    if (not isinstance(value, str) or len(value) > 4096 or '\\' in value
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ServiceError('invalid_params', '链接格式无效')
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').lower().rstrip('.')
        if parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None, 443):
            raise ValueError
        domains = ('alibaba.com', '1688.com', 'aliexpress.com')
        if not any(host == d or host.endswith('.' + d) for d in domains):
            raise ValueError
        if product_id and not re.search(r'(?<!\d)' + re.escape(product_id) + r'(?!\d)', parts.path):
            raise ValueError
        return urlunsplit(('https', host, parts.path, '', ''))
    except (ValueError, TypeError):
        raise ServiceError('invalid_params', '链接不属于受支持的平台或产品') from None


def _key(seller, platform, pid):
    return 'product_' + hashlib.sha256(encoded([seller, platform, pid]).encode('utf-8')).hexdigest()


class ProductLibraryMixin(ProductPresentationMixin):
    def _init_product_library(self):
        self._init_product_display()
        self._procurement_authorized = False
        self._procurement_epoch = 0
        self._product_repository = None

    def _library_ready(self):
        if not self.account_verified or not self.seller_id:
            raise ServiceError('identity_unverified', '请先核实当前商家身份')
        if not hasattr(self, '_procurement_authorized'):
            self._init_product_library()
        if self._product_repository is not self.repo:
            self._procurement_authorized = False
            self._procurement_epoch += 1
            self._product_repository = self.repo
            with self.repo.transaction():
                for image in self.repo.list('product_main_image'):
                    if image.get('seller_id') == self.seller_id and image.get('status') == 'loading':
                        image.update(status='unknown', message='上次主图读取未完成；不会重复查询', updated_at=now_ms())
                        self.repo.put('product_main_image', image['local_product_id'], image, history=True)
                for task in self.repo.list('procurement_task'):
                    if task.get('seller_id') != self.seller_id:
                        continue
                    if task.get('status') in ('queued', 'running'):
                        task.update(status='unknown' if task.get('execution') else 'waiting_authorization',
                                    phase='recovery', message='核查原采购任务；不会自动重新提交' if task.get('execution') else '等待本次采购授权',
                                    updated_at=now_ms())
                        self.repo.put('procurement_task', task['task_id'], task, history=True)

    def _library_entity(self, kind, identifier):
        self._library_ready()
        value = self.repo.get(kind, identifier)
        if not value or value.get('seller_id') != self.seller_id:
            raise ServiceError('not_found', '当前商家没有这条产品或供应商记录')
        return value

    def _library_rows(self, kind):
        self._library_ready()
        return [r for r in self.repo.list(kind) if r.get('seller_id') == self.seller_id]

    def _product_customer_counts(self):
        # Join only identity fields; never load messages or customer details into products.
        with self.repo.lock:
            rows = self.repo.db.execute("""SELECT json_extract(b.data, '$.local_product_id'),
                    COUNT(DISTINCT json_extract(c.data, '$.buyer_id'))
                FROM entities b
                JOIN entities k ON k.kind='case' AND k.id=b.id
                JOIN entities c ON c.kind='conversation' AND c.id=json_extract(b.data, '$.conversation_id')
                WHERE b.kind='requirement_product_binding'
                  AND json_extract(b.data, '$.seller_id')=?
                  AND json_extract(b.data, '$.case_id')=b.id
                  AND json_extract(k.data, '$.case_id')=k.id
                  AND json_extract(k.data, '$.seller_id')=?
                  AND json_extract(c.data, '$.seller_id')=?
                  AND json_extract(c.data, '$.conversation_id')=c.id
                  AND json_extract(k.data, '$.conversation_id')=c.id
                  AND json_extract(k.data, '$.buyer_id')=json_extract(c.data, '$.buyer_id')
                  AND typeof(json_extract(c.data, '$.buyer_id'))='text'
                  AND json_extract(c.data, '$.buyer_id')<>''
                  AND typeof(json_extract(b.data, '$.local_product_id'))='text'
                GROUP BY json_extract(b.data, '$.local_product_id')""",
                (self.seller_id, self.seller_id, self.seller_id)).fetchall()
        return dict(rows)

    def _product_customers(self, product_id):
        with self.repo.lock:
            rows = self.repo.db.execute("""SELECT json_extract(c.data, '$.buyer_id'),
                    json_extract(c.data, '$.customer_name'), c.id, k.id
                FROM entities b
                JOIN entities k ON k.kind='case' AND k.id=b.id
                JOIN entities c ON c.kind='conversation' AND c.id=json_extract(b.data, '$.conversation_id')
                WHERE b.kind='requirement_product_binding'
                  AND json_extract(b.data, '$.local_product_id')=?
                  AND json_extract(b.data, '$.seller_id')=? AND json_extract(k.data, '$.seller_id')=?
                  AND json_extract(c.data, '$.seller_id')=? AND json_extract(b.data, '$.case_id')=b.id
                  AND json_extract(k.data, '$.case_id')=k.id AND json_extract(c.data, '$.conversation_id')=c.id
                  AND json_extract(k.data, '$.conversation_id')=c.id
                  AND json_extract(k.data, '$.buyer_id')=json_extract(c.data, '$.buyer_id')
                  AND typeof(json_extract(c.data, '$.buyer_id'))='text' AND json_extract(c.data, '$.buyer_id')<>''
                ORDER BY json_extract(c.data, '$.buyer_id'), c.id, k.id""",
                (product_id, self.seller_id, self.seller_id, self.seller_id)).fetchall()
        customers = {}
        for buyer, name, conversation, case in rows:
            if buyer not in customers:
                customers[buyer] = {'buyer_id': buyer, 'customer_name': name, 'conversation_id': conversation,
                                    'case_id': case, 'case_ids': []}
            customers[buyer]['case_ids'].append(case)
        return list(customers.values())

    def _product_projection(self, product, customer_counts, offers=None):
        value = copy.deepcopy(product)
        value.pop('creation_case_id', None)
        value['product_url'] = product_link(product['product_id'])
        value['product_main_image'] = project_main_image(self.repo, product)
        value['customer_count'] = customer_counts.get(product['local_product_id'], 0)
        offers = offers if offers is not None else self._library_rows('supplier_offer')
        product_offers = [o for o in offers if o['local_product_id'] == product['local_product_id']]
        value['supplier_count'] = len({o['supplier_id'] for o in product_offers})
        value['offer_count'] = len(product_offers)
        task = self.repo.get('procurement_task', product.get('sourcing_task_id')) if product.get('sourcing_task_id') else None
        specifications = ((task or {}).get('source_context') or {}).get('requirement_fields', {}).get('specifications')
        value['specifications_summary'] = specifications if isinstance(specifications, str) and specifications.strip() else None
        value['procurement_status'] = task.get('status') if task else None
        return value

    def list_products(self):
        with self._lock:
            products = self._library_rows('library_product')
            counts = self._product_customer_counts()
            offers = self._library_rows('supplier_offer')
            return {'items': [self._product_projection(r, counts, offers) for r in products]}

    def get_product(self, local_product_id):
        with self._lock:
            product = self._library_entity('library_product', local_product_id)
            offers = [r for r in self._library_rows('supplier_offer') if r['local_product_id'] == local_product_id]
            suppliers = [r for r in self._library_rows('supplier') if r['supplier_id'] in {o['supplier_id'] for o in offers}]
            names = {r['supplier_id']: r['name'] for r in suppliers}
            return {'product': self._product_projection(product, self._product_customer_counts()),
                    'offers': [dict(r, supplier_name=names.get(r['supplier_id'], '')) for r in offers],
                    'suppliers': suppliers, 'customers': self._product_customers(local_product_id),
                    'tasks': [self._task_projection(t) for t in self._library_rows('procurement_task')
                              if t['local_product_id'] == local_product_id]}

    def list_suppliers(self):
        with self._lock:
            suppliers = self._library_rows('supplier')
            products = {p['local_product_id'] for p in self._library_rows('library_product')}
            counts = {}
            for offer in self._library_rows('supplier_offer'):
                if offer['local_product_id'] in products:
                    counts.setdefault(offer['supplier_id'], set()).add(offer['local_product_id'])
            return {'items': [dict(s, region=s.get('region') or None,
                                   product_count=len(counts.get(s['supplier_id'], set()))) for s in suppliers]}

    def get_supplier(self, supplier_id):
        with self._lock:
            supplier = self._library_entity('supplier', supplier_id)
            offers = [r for r in self._library_rows('supplier_offer') if r['supplier_id'] == supplier_id]
            ids = {o['local_product_id'] for o in offers}
            counts = self._product_customer_counts()
            return {'supplier': supplier, 'offers': offers,
                    'products': [self._product_projection(p, counts) for p in self._library_rows('library_product') if p['local_product_id'] in ids]}

    def _library_mutation(self, mutation_id, payload):
        mutation_id = _request_id(mutation_id)
        digest = hashlib.sha256(encoded(payload).encode('utf-8')).hexdigest()
        prior = self.repo.get('library_mutation', mutation_id)
        if prior:
            if prior.get('seller_id') != self.seller_id or prior.get('request_hash') != digest:
                raise ServiceError('mutation_conflict', '同一操作编号不能用于不同内容')
            return mutation_id, digest, copy.deepcopy(prior['result'])
        return mutation_id, digest, None

    def _save_library_mutation(self, identifier, digest, result):
        self.repo.put('library_mutation', identifier, {'seller_id': self.seller_id,
            'request_hash': digest, 'result': result, 'created_at': now_ms()})
        return copy.deepcopy(result)

    def save_supplier(self, name, platform, platform_supplier_id, product_url, contact, notes,
                      client_mutation_id, supplier_id=None, expected_revision=None, region=_UNSET):
        data = {'name': _text(name, 160, empty=False), 'platform': _text(platform, 40, empty=False),
                'platform_supplier_id': _text(platform_supplier_id, 200) if platform_supplier_id else None,
                'product_url': _url(product_url), 'contact': _text(contact), 'notes': _text(notes, 5000)}
        if region is not _UNSET:
            data['region'] = _text(region, 200) or None if region is not None else None
        if data['platform'] not in ('alibaba', '1688', 'aliexpress', 'other'):
            raise ServiceError('invalid_params', '供应商平台无效')
        with self._lock, self.repo.transaction():
            self._library_ready()
            mid, digest, prior = self._library_mutation(client_mutation_id, dict(method='save_supplier', supplier_id=supplier_id,
                                                    expected_revision=expected_revision, data=data))
            if prior is not None:
                return prior
            old = self._library_entity('supplier', supplier_id) if supplier_id else None
            if (expected_revision != (old['revision'] if old else None)
                    or expected_revision is not None and type(expected_revision) is not int):
                raise ServiceError('revision_conflict', '供应商资料已变化，请保留输入并刷新')
            stamp = now_ms()
            if region is _UNSET and old and 'region' in old:
                data['region'] = old['region']
            value = {**data, 'seller_id': self.seller_id, 'supplier_id': supplier_id or new_id('supplier'),
                     'revision': old['revision'] + 1 if old else 1,
                     'created_at': old['created_at'] if old else stamp, 'updated_at': stamp}
            self.repo.put('supplier', value['supplier_id'], value, history=True)
            result = self._save_library_mutation(mid, digest, value)
        self._changed()
        return result

    def _offer_data(self, data):
        if not isinstance(data, dict) or set(data) - _OFFER_FIELDS:
            raise ServiceError('invalid_params', '供货方案包含未知字段')
        value = {key: _text(data.get(key, '') or '', 3000 if key in ('source_note', 'customer_wording') else 1000)
                 for key in _OFFER_TEXT}
        value['supplier_product_id'] = value['supplier_product_id'] or None
        value['product_url'] = _url(data.get('product_url'))
        if value['unit_price']:
            try:
                price = Decimal(value['unit_price'])
                if not price.is_finite() or price < 0:
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ServiceError('invalid_params', '供货单价应为非负十进制数') from None
        value['source_type'] = data.get('source_type', 'manual_verified')
        if value['source_type'] not in ('page_reference', 'supplier_reply', 'manual_verified'):
            raise ServiceError('invalid_params', '供货资料来源类型无效')
        for key in ('enabled', 'reply_enabled'):
            item = data.get(key, key == 'enabled')
            if type(item) is not bool:
                raise ServiceError('invalid_params', '供货方案开关无效')
            value[key] = item
        tax, expires = data.get('tax_included'), data.get('valid_until')
        if tax is not None and type(tax) is not bool or expires is not None and (type(expires) is not int or expires < 0):
            raise ServiceError('invalid_params', '税费或有效期无效')
        value.update(tax_included=tax, valid_until=expires)
        if value['reply_enabled'] and value['source_type'] == 'page_reference':
            raise ServiceError('reference_only', '页面参考资料不能直接启用为已核实接待条件')
        return value

    def save_product_offer(self, local_product_id, supplier_id, data, client_mutation_id,
                           offer_id=None, expected_revision=None):
        value = self._offer_data(data)
        with self._lock, self.repo.transaction():
            self._library_entity('library_product', local_product_id)
            self._library_entity('supplier', supplier_id)
            mid, digest, prior = self._library_mutation(client_mutation_id,
                dict(method='save_product_offer', local_product_id=local_product_id, supplier_id=supplier_id,
                     offer_id=offer_id, expected_revision=expected_revision, data=value))
            if prior is not None:
                return prior
            old = self._library_entity('supplier_offer', offer_id) if offer_id else None
            if old and (old['local_product_id'] != local_product_id or old['supplier_id'] != supplier_id):
                raise ServiceError('ownership_mismatch', '供货方案不属于所选产品和供应商')
            if (expected_revision != (old['revision'] if old else None)
                    or expected_revision is not None and type(expected_revision) is not int):
                raise ServiceError('revision_conflict', '供货方案已变化，请保留输入并刷新')
            stamp = now_ms()
            value.update(offer_id=offer_id or new_id('offer'), seller_id=self.seller_id,
                         local_product_id=local_product_id, supplier_id=supplier_id,
                         revision=old['revision'] + 1 if old else 1,
                         created_at=old['created_at'] if old else stamp, updated_at=stamp)
            self.repo.put('supplier_offer', value['offer_id'], value, history=True)
            result = self._save_library_mutation(mid, digest, value)
            affected = [b['conversation_id'] for b in self._library_rows('requirement_product_binding') if b.get('offer_id') == value['offer_id']]
            for cid in set(affected):
                self._invalidate(cid)
        self._changed()
        return result

    def _binding_case(self, case_id, expected):
        self._library_ready()
        case = self.repo.get('case', case_id)
        if not case or case.get('seller_id') != self.seller_id:
            raise ServiceError('not_found', '当前商家没有这份需求')
        if type(expected) is not int or case['revision'] != expected:
            raise ServiceError('revision_conflict', '需求版本已变化，请刷新后操作')
        return case

    def bind_requirement_product(self, case_id, local_product_id, expected_case_revision, client_mutation_id):
        with self._lock, self.repo.transaction():
            self._library_ready()
            mid, digest, prior = self._library_mutation(client_mutation_id,
                dict(method='bind_requirement_product', case_id=case_id, local_product_id=local_product_id,
                     expected_case_revision=expected_case_revision))
            if prior is not None:
                return prior
            case = self._binding_case(case_id, expected_case_revision)
            product = self._library_entity('library_product', local_product_id)
            old = self.repo.get('requirement_product_binding', case_id, {})
            binding = {'seller_id': self.seller_id, 'case_id': case_id, 'conversation_id': case['conversation_id'],
                       'local_product_id': local_product_id,
                       'offer_id': old.get('offer_id') if old.get('local_product_id') == local_product_id else product.get('default_offer_id'),
                       'origin': 'manual', 'requirement_product_id_at_binding': requirement_value(case, 'product_id'),
                       'revision': old.get('revision', 0) + 1, 'updated_at': now_ms()}
            self.repo.put('requirement_product_binding', case_id, binding, history=True)
            case.update(revision=case['revision'] + 1, updated_at=now_ms())
            self.repo.put('case', case_id, case, history=True)
            self._invalidate(case['conversation_id'])
            result = self._save_library_mutation(mid, digest, {'case_id': case_id, 'local_product_id': local_product_id})
        self._changed(case['conversation_id'], case_id)
        return result

    def select_supplier_offer(self, case_id, offer_id, expected_case_revision, client_mutation_id):
        with self._lock, self.repo.transaction():
            self._library_ready()
            mid, digest, prior = self._library_mutation(client_mutation_id,
                dict(method='select_supplier_offer', case_id=case_id, offer_id=offer_id, expected_case_revision=expected_case_revision))
            if prior is not None:
                return prior
            case = self._binding_case(case_id, expected_case_revision)
            binding = self.repo.get('requirement_product_binding', case_id)
            if not binding or binding.get('seller_id') != self.seller_id:
                raise ServiceError('product_unbound', '请先关联产品')
            if offer_id is not None:
                offer = self._library_entity('supplier_offer', offer_id)
                if offer['local_product_id'] != binding['local_product_id']:
                    raise ServiceError('ownership_mismatch', '供货方案不属于本需求产品')
            binding.update(offer_id=offer_id, revision=binding['revision'] + 1, updated_at=now_ms())
            self.repo.put('requirement_product_binding', case_id, binding, history=True)
            case.update(revision=case['revision'] + 1, updated_at=now_ms())
            self.repo.put('case', case_id, case, history=True)
            self._invalidate(case['conversation_id'])
            result = self._save_library_mutation(mid, digest, {'case_id': case_id, 'offer_id': offer_id})
        self._changed(case['conversation_id'], case_id)
        return result

    def set_product_default_offer(self, local_product_id, offer_id, expected_revision, client_mutation_id):
        with self._lock, self.repo.transaction():
            self._library_ready()
            mid, digest, prior = self._library_mutation(client_mutation_id,
                dict(method='set_product_default_offer', local_product_id=local_product_id,
                     offer_id=offer_id, expected_revision=expected_revision))
            if prior is not None:
                return prior
            product = self._library_entity('library_product', local_product_id)
            if type(expected_revision) is not int or product['revision'] != expected_revision:
                raise ServiceError('revision_conflict', '产品版本已变化')
            if offer_id is not None and self._library_entity('supplier_offer', offer_id)['local_product_id'] != local_product_id:
                raise ServiceError('ownership_mismatch', '默认供货方案不属于本产品')
            product.update(default_offer_id=offer_id, revision=product['revision'] + 1, updated_at=now_ms())
            self.repo.put('library_product', local_product_id, product, history=True)
            result = self._save_library_mutation(mid, digest, self._product_projection(product, self._product_customer_counts()))
        self._changed()
        return result

    def _source_context(self, product, case=None):
        fields = {key: requirement_value(case or {}, key) for key in _SEED_FIELDS}
        fields = {key: value for key, value in fields.items() if isinstance(value, str) and value.strip()}
        return {'seller_id': self.seller_id, 'local_product_id': product['local_product_id'],
                'product_id': product['product_id'], 'name': product['name'], 'product_url': product.get('product_url'),
                'product_images': [], 'main_image_url': self._source_main_image_url(product['local_product_id']),
                'requirement_fields': fields,
                'supplier_limit': self._procurement_config()['supplier_limit'],
                'auto_inquiry': self._procurement_config()['auto_inquiry']}

    def _sync_requirement_product(self, case):
        self._library_ready()
        pid = requirement_value(case, 'product_id')
        derive_product_link(case)
        self.repo.put('case', case['case_id'], case)
        if case.get('requirements_format') != 'ai-v1' or not valid_product_id(pid):
            # Clearing/replacing an AI product must also remove its old selected
            # supplier from this customer's context. The public product/task
            # and all external execution evidence are retained.
            old = self.repo.get('requirement_product_binding', case.get('case_id'))
            if old and old.get('origin') == 'manual' and old.get('requirement_product_id_at_binding') == pid:
                return
            if old and old.get('seller_id') == self.seller_id and old.get('local_product_id'):
                old.update(local_product_id=None, offer_id=None, revision=old['revision'] + 1, updated_at=now_ms())
                self.repo.put('requirement_product_binding', case['case_id'], old, history=True)
            return
        if case.get('seller_id') != self.seller_id:
            raise ServiceError('ownership_mismatch', '产品需求不属于当前商家')
        platform, stamp = product_kind(pid), now_ms()
        identifier = _key(self.seller_id, platform, pid)
        product = self.repo.get('library_product', identifier)
        if platform == 'custom' and product and product.get('creation_case_id') != case['case_id']:
            binding = self.repo.get('requirement_product_binding', case['case_id']) or {}
            row = (case.get('requirements') or {}).get('product_id') or {}
            sources = {m['message_id']: m for m in self.repo.list('message', conversation_id=case['conversation_id'])}
            explicit = any(pid in sources.get(mid, {}).get('text', '') for mid in row.get('evidence_ids', []))
            if binding.get('local_product_id') != identifier and not explicit:
                # A model's coincidentally repeated local code must not merge
                # two unrelated custom requirements. Allocate once, atomically.
                for attempt in range(100):
                    suffix = 1000000 + int(hashlib.sha256(encoded([self.seller_id, case['case_id'], attempt]).encode()).hexdigest(), 16) % 9000000
                    candidate = '定制-' + str(suffix)
                    candidate_key = _key(self.seller_id, platform, candidate)
                    if not self.repo.get('library_product', candidate_key):
                        pid, identifier, product = candidate, candidate_key, None
                        case['requirements']['product_id']['value'] = pid
                        derive_product_link(case)
                        self.repo.put('case', case['case_id'], case)
                        break
                else:
                    raise ServiceError('custom_product_id_conflict', '本地定制编号暂时无法分配')
        # User-verified public display format; this never proves listing details
        # and never enters reception model fields.
        url = product_link(pid)
        name = requirement_value(case, 'product')
        if not product:
            product = {'local_product_id': identifier, 'seller_id': self.seller_id, 'platform': platform,
                       'product_id': pid, 'name': name[:160] if isinstance(name, str) and name.strip() else '未命名产品',
                       'product_url': url, 'product_images': [], 'default_offer_id': None,
                       'revision': 1, 'created_at': stamp, 'updated_at': stamp,
                       'creation_case_id': case['case_id'] if platform == 'custom' else None}
            task_id = new_id('procurement')
            product['sourcing_task_id'] = task_id
            task = {'task_id': task_id, 'seller_id': self.seller_id, 'local_product_id': identifier, 'product_id': pid,
                    'status': 'waiting_authorization', 'phase': 'created', 'message': '等待启用自动采购',
                    'job_id': None, 'request_id': None, 'conversation_name': None, 'conversation_id': None,
                    'created_at': stamp, 'updated_at': stamp, 'completed_at': None,
                    'source_context': self._source_context(product, case)}
            self.repo.put('library_product', identifier, product, history=True)
            self.repo.put('procurement_task', task_id, task, history=True)
        else:
            changed, name_changed = False, False
            if product.get('product_url') != url:
                product['product_url'], changed = url, True
            if isinstance(name, str) and name.strip() and product['name'] == '未命名产品':
                product['name'], changed, name_changed = name[:160], True, True
            if changed:
                product.update(revision=product['revision'] + (1 if name_changed else 0), updated_at=stamp)
                self.repo.put('library_product', identifier, product, history=True)
            task = self.repo.get('procurement_task', product.get('sourcing_task_id'))
        self._main_image_record(product)
        binding = self.repo.get('requirement_product_binding', case['case_id'])
        manual_binding_current = bool(binding and binding.get('origin') == 'manual'
                                      and binding.get('requirement_product_id_at_binding') == pid)
        if not manual_binding_current and (not binding or binding.get('local_product_id') != identifier):
            self.repo.put('requirement_product_binding', case['case_id'], {
                'seller_id': self.seller_id, 'case_id': case['case_id'], 'conversation_id': case['conversation_id'],
                'local_product_id': identifier, 'offer_id': product.get('default_offer_id'),
                'revision': (binding or {}).get('revision', 0) + 1, 'updated_at': stamp}, history=True)
        if task and task['status'] in ('waiting_authorization', 'waiting_input') and not task.get('execution'):
            task['source_context'] = self._source_context(product, case)
            self.repo.put('procurement_task', task['task_id'], task)

    def _schedule_procurement(self):
        with self._lock:
            if self._closing or self._stop.is_set() or not self.account_verified:
                return
            self._library_ready()
            self._schedule_product_main_images()
            if not self._procurement_authorized or not self._procurement_config()['auto_find']:
                return
            with self.repo.transaction():
                for task in self._library_rows('procurement_task'):
                    if task['status'] in ('waiting_authorization', 'waiting_input') and not task.get('execution'):
                        if self._queue_procurement(task) is None:
                            break

    def _product_context_for_case(self, case):
        if not case or not getattr(self, 'account_verified', False):
            return {'supplier_context': None}
        self._library_ready()
        binding = self.repo.get('requirement_product_binding', case.get('case_id'))
        if not binding or binding.get('seller_id') != self.seller_id:
            return {'supplier_context': None}
        product = self.repo.get('library_product', binding['local_product_id'])
        if not product or product.get('seller_id') != self.seller_id:
            return {'supplier_context': None}
        result = {'product_context': {'product_id': product['product_id'],
                  'reference_only': True, 'allow_product_lookup': False}, 'supplier_context': None}
        offer = self.repo.get('supplier_offer', binding.get('offer_id')) if binding.get('offer_id') else None
        if not offer or offer.get('seller_id') != self.seller_id or offer.get('local_product_id') != product['local_product_id']:
            return result
        supplier = self.repo.get('supplier', offer['supplier_id'])
        valid = bool(supplier and supplier.get('seller_id') == self.seller_id and offer.get('enabled') and offer.get('reply_enabled')
                     and offer.get('source_type') in ('supplier_reply', 'manual_verified')
                     and (offer.get('valid_until') is None or offer['valid_until'] > now_ms()))
        context = {'selection_revision': binding['revision'], 'offer_id': offer['offer_id'],
                   'offer_revision': offer['revision'], 'product_revision': product['revision'],
                   'source_id': 'supplier_offer:' + offer['offer_id'] + ':' + str(offer['revision']),
                   'usable': valid, 'source_type': offer.get('source_type'), 'valid_until': offer.get('valid_until'),
                   'internal_only': True, 'is_customer_selling_price': False}
        if valid:
            for key in ('specifications', 'moq', 'unit', 'unit_price', 'currency', 'lead_time', 'packaging',
                        'freight', 'tax_included', 'customer_wording'):
                context[key] = copy.deepcopy(offer.get(key))
            # Conservative exact applicability: unknown/nonintegral MOQ does not
            # become proof it fits the customer's order. No chat parsing occurs.
            minimum = offer.get('moq', '').strip()
            quantity = requirement_value(case, 'quantity')
            if minimum and not minimum.isdecimal():
                context['usable'] = False
                context['applicability'] = 'MOQ未明确，不能据此承诺'
            elif minimum:
                amount = re.fullmatch(r'\s*(\d+)\s*(?:件|个|pcs?|pieces?|units?|sets?|套)\s*', quantity or '', re.I)
                if not amount or int(amount[1]) < int(minimum):
                    context['usable'] = False
                    context['applicability'] = '当前数量未满足已知MOQ或尚不明确'
        result['supplier_context'] = context
        return result

    def _procurement_config(self):
        value = self._control.get('meta', 'procurement_settings', {})
        if not isinstance(value, dict):
            value = {}
        return {'auto_find': value.get('auto_find', True) if type(value.get('auto_find', True)) is bool else True,
                'auto_inquiry': value.get('auto_inquiry', True) if type(value.get('auto_inquiry', True)) is bool else True,
                'supplier_limit': value.get('supplier_limit', 3) if type(value.get('supplier_limit', 3)) is int
                    and 1 <= value.get('supplier_limit', 3) <= 3 else 3}

    def get_procurement_settings(self):
        with self._lock:
            self._library_ready()
            return {**self._procurement_config(), 'authorized': self._procurement_authorized,
                    'inquiry_available': False, 'inquiry_reason': '定向供应商询价的可控提交、回执及回复接口尚未验收，当前不会自动发送'}

    def save_procurement_settings(self, auto_find, auto_inquiry, supplier_limit):
        if type(auto_find) is not bool or type(auto_inquiry) is not bool or type(supplier_limit) is not int or not 1 <= supplier_limit <= 3:
            raise ServiceError('invalid_params', '采购设置无效；当前每个产品最多3家供应商')
        with self._lock, self._control.transaction():
            self._library_ready()
            self._procurement_authorized = False
            self._procurement_epoch += 1
            self._control.put('meta', 'procurement_settings', dict(auto_find=auto_find, auto_inquiry=auto_inquiry,
                                                                  supplier_limit=supplier_limit))
            self._cancel_procurement()
        self._changed()
        return self.get_procurement_settings()

    def set_procurement_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ServiceError('invalid_params', '采购授权开关无效')
        with self._lock, self.repo.transaction():
            self._library_ready()
            self._procurement_authorized = enabled
            self._procurement_epoch += 1
            if enabled:
                self._schedule_product_main_images()
            if enabled and self._procurement_config()['auto_find']:
                for task in self._library_rows('procurement_task'):
                    if task['status'] in ('waiting_authorization', 'waiting_input') and not task.get('execution'):
                        self._queue_procurement(task)
            if not enabled:
                self._cancel_procurement()
        self._changed()
        return self.get_procurement_settings()

    def _cancel_procurement(self):
        for task in self._library_rows('procurement_task'):
            if task['status'] not in ('queued', 'running'):
                continue
            job_id = task.get('job_id')
            if job_id:
                self._generation_cancel_event(job_id).set()
            task.update(status='unknown' if task.get('execution') else 'waiting_authorization',
                        phase='paused', message='已暂停；仅核查原任务' if task.get('execution') else '已暂停，未提交', updated_at=now_ms())
            self.repo.put('procurement_task', task['task_id'], task, history=True)

    @staticmethod
    def _task_projection(task):
        return {key: copy.deepcopy(value) for key, value in task.items()
                if key not in ('source_context', 'execution', 'raw_result', 'prompt')}

    def list_procurement_tasks(self, local_product_id=None):
        with self._lock:
            if local_product_id is not None:
                self._library_entity('library_product', local_product_id)
            return {'items': [self._task_projection(t) for t in self._library_rows('procurement_task')
                              if local_product_id is None or t['local_product_id'] == local_product_id]}

    def _queue_procurement(self, task, *, reconcile=False):
        if task['status'] in ('queued', 'running'):
            return task.get('job_id')
        if task.get('execution') and not reconcile:
            raise ServiceError('procurement_unresolved', '原任务已有外部执行记录，只能核查，不能重新搜索')
        if not reconcile:
            settings = self._procurement_config()
            task['source_context'].update(supplier_limit=settings['supplier_limit'], auto_inquiry=settings['auto_inquiry'])
        stamp, job_id = now_ms(), new_id('job')
        job = {'job_id': job_id, 'kind': 'source_product', 'conversation_id': None, 'case_id': None,
               'status': 'queued', 'stage': 'queued', 'message': '等待核查原采购任务' if reconcile else '等待自动找货',
               'created_at': stamp, 'started_at': None, 'completed_at': None, 'automatic': False,
               'procurement_task_id': task['task_id']}
        task.update(status='queued', phase='reconcile' if reconcile else 'queued', job_id=job_id,
                    message=job['message'], updated_at=stamp)
        self.repo.put('job', job_id, job, history=True)
        self.repo.put('procurement_task', task['task_id'], task, history=True)
        try:
            self._queue.put((job_id, 'source_product', {'task_id': task['task_id'], 'reconcile': reconcile}, self._epoch, False))
        except ServiceError as error:
            if error.code != 'task_queue_full':
                raise
            job.update(status='cancelled', stage='queue', message='后台队列已满，采购尚未执行', completed_at=now_ms())
            task.update(status='unknown' if reconcile else 'waiting_input', phase='waiting_capacity', job_id=None,
                        message='后台队列已满，等待空位；未新提交', updated_at=now_ms())
            self.repo.put('job', job_id, job, history=True)
            self.repo.put('procurement_task', task['task_id'], task, history=True)
            return None
        self._emit('job.updated', job)
        return job_id

    def request_procurement(self, local_product_id, client_request_id):
        with self._lock, self.repo.transaction():
            product = self._library_entity('library_product', local_product_id)
            mid, digest, prior = self._library_mutation(client_request_id, dict(method='request_procurement', local_product_id=local_product_id))
            if prior is not None:
                return prior
            if not self._procurement_authorized:
                raise ServiceError('procurement_unauthorized', '请先启用本次自动采购')
            task = self._library_entity('procurement_task', product['sourcing_task_id'])
            if task.get('execution') or task['status'] in ('succeeded', 'waiting_reply', 'blocked', 'unknown'):
                raise ServiceError('procurement_already_attempted', '原任务已经执行，请查看或核查原结果')
            self._queue_procurement(task)
            result = self._save_library_mutation(mid, digest, {'task_id': task['task_id']})
        self._changed()
        return result

    def reconcile_procurement(self, task_id):
        with self._lock, self.repo.transaction():
            task = self._library_entity('procurement_task', task_id)
            if not task.get('execution'):
                raise ServiceError('procurement_not_submitted', '没有可核查的外部请求记录')
            if task['status'] in ('queued', 'running'):
                return {'job_id': task['job_id']}
            job_id = self._queue_procurement(task, reconcile=True)
            if job_id is None:
                raise ServiceError('task_queue_full', '后台队列已满，原任务仍保留，可稍后核查')
        self._changed()
        return {'job_id': job_id}

    def _task_source_product(self, params, job_id, epoch, facade):
        with self._lock, self.repo.transaction():
            task = self._library_entity('procurement_task', params['task_id'])
            reconcile = params.get('reconcile') is True
            if task.get('job_id') != job_id:
                raise ServiceError('cancelled', '采购任务已被更新')
            if not reconcile and (not self._procurement_authorized or self._closing):
                task.update(status='waiting_authorization', phase='paused', updated_at=now_ms())
                self.repo.put('procurement_task', task['task_id'], task)
                return {'_job_status': 'cancelled', 'message': '采购未授权，未执行'}
            if not reconcile:
                task['source_context']['main_image_url'] = self._source_main_image_url(task['local_product_id'])
                task['source_context']['product_url'] = product_link(task['product_id'])
            task.update(status='running', phase='reconcile' if reconcile else 'sourcing', updated_at=now_ms())
            self.repo.put('procurement_task', task['task_id'], task, history=True)
            context = copy.deepcopy(task['source_context'])
            grant_epoch = self._procurement_epoch
        event = self._generation_cancel_event(job_id)
        def started():
            with self._lock:
                if event.is_set() or self._closing or not self._procurement_authorized or self._procurement_epoch != grant_epoch:
                    raise ServiceError('cancelled', '采购权限已变化')
        def record_created(record):
            if (not isinstance(record, dict) or record.get('request_id') != task['task_id']
                    or not isinstance(record.get('prompt'), str) or not 0 < len(record['prompt']) <= 1000000
                    or record.get('prompt_sha256') != hashlib.sha256(record['prompt'].encode('utf-8')).hexdigest()
                    or record.get('conversation_name') != 'reception-source-' + hashlib.sha256(task['task_id'].encode('utf-8')).hexdigest()[:32]
                    or not isinstance(record.get('agent_id'), str) or not record['agent_id']
                    or type(record.get('started_at')) not in (float, int)):
                raise ServiceError('invalid_sourcing_record', '采购请求记录无效')
            record = {k: record[k] for k in ('request_id', 'prompt', 'prompt_sha256', 'conversation_name', 'agent_id', 'started_at')}
            with self._lock, self.repo.transaction():
                started()
                current = self._library_entity('procurement_task', task['task_id'])
                if current.get('execution'):
                    raise ServiceError('procurement_already_attempted', '本任务已有外部请求，禁止重复提交')
                current.update(execution=copy.deepcopy(record), request_id=record.get('request_id', task['task_id']),
                               conversation_name=record.get('conversation_name'), updated_at=now_ms())
                self.repo.put('procurement_task', task['task_id'], current, history=True)
        try:
            # Recheck the account of this execution facade, including cloned
            # Live facades, before either a query or an owned-record recovery.
            self._ensure_identity(facade)
            if reconcile:
                method = getattr(facade, 'reconcile_source_product', None)
                if not callable(method):
                    raise ServiceError('sourcing_recovery_unavailable', '当前适配器不能核查原采购记录')
                result = method(copy.deepcopy(task['execution']), context, cancel_event=event)
            else:
                started()
                method = getattr(facade, 'source_product', None)
                if not callable(method):
                    raise ServiceError('sourcing_unavailable', '当前适配器没有采购搜索入口')
                result = method(context, task['task_id'], cancel_event=event,
                                on_call_start=started, on_request_created=record_created)
            if not isinstance(result, dict) or result.get('product_id') != task['product_id']:
                raise ServiceError('invalid_sourcing_result', '采购结果产品归属无效')
            with self._lock, self.repo.transaction():
                current = self._library_entity('procurement_task', task['task_id'])
                if event.is_set() or self._closing or not reconcile and self._procurement_epoch != grant_epoch:
                    current.update(status='unknown', phase='cancelled', message='原任务可能已执行，仅允许核查', updated_at=now_ms())
                    self.repo.put('procurement_task', current['task_id'], current, history=True)
                    return {'_job_status': 'unknown', 'message': current['message']}
                self._import_sourcing_result(current, result)
            self._changed()
            return {'message': '已保存产品和供应商；询价接口尚未验收，未发送' if current['status'] == 'blocked' else '已保存采购结果'}
        except Exception as exc:
            with self._lock, self.repo.transaction():
                current = self._library_entity('procurement_task', task['task_id'])
                submitted = bool(current.get('execution'))
                current.update(status='unknown' if submitted else 'failed', phase='recovery' if submitted else 'failed',
                               message='原请求结果尚未核实，请核查原任务' if submitted else '采购未执行，请查看任务记录',
                               error_code=getattr(exc, 'code', 'sourcing_failed'), updated_at=now_ms())
                self.repo.put('procurement_task', current['task_id'], current, history=True)
            self._changed()
            raise

    def _import_sourcing_result(self, task, result):
        product = self._library_entity('library_product', task['local_product_id'])
        evidence = result.get('execution')
        record = task.get('execution')
        if (not isinstance(evidence, dict) or not isinstance(record, dict)
                or evidence.get('request_id') != record['request_id']
                or evidence.get('conversation_name') != record['conversation_name']
                or evidence.get('request_sha256') != record['prompt_sha256']):
            raise ServiceError('invalid_sourcing_result', '采购结果未绑定已保存的确切请求')
        # Original-product links are deterministic display data. Model/search
        # results cannot replace them or mutate customer requirements.
        candidates = result.get('suppliers') or []
        if not isinstance(candidates, list) or len(candidates) > task['source_context']['supplier_limit']:
            raise ServiceError('invalid_sourcing_result', '采购供应商结果超出范围')
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                raise ServiceError('invalid_sourcing_result', '供应商结果格式无效')
            name = _text(candidate.get('name'), 160, empty=False)
            platform = candidate.get('platform', 'alibaba')
            supplier_pid = candidate.get('platform_supplier_id')
            supplier_url = _url(candidate.get('product_url'))
            identity = [self.seller_id, platform, supplier_pid] if supplier_pid else [self.seller_id, task['task_id'], index]
            supplier_id = 'supplier_' + hashlib.sha256(encoded(identity).encode('utf-8')).hexdigest()
            old = self.repo.get('supplier', supplier_id)
            stamp = now_ms()
            supplier = {'supplier_id': supplier_id, 'seller_id': self.seller_id, 'platform': platform,
                        'platform_supplier_id': supplier_pid or None, 'name': name, 'product_url': supplier_url,
                        'contact': '', 'notes': '', 'revision': 1, 'created_at': stamp, 'updated_at': stamp}
            if old:
                supplier = old
            else:
                self.repo.put('supplier', supplier_id, supplier, history=True)
            offers = candidate.get('offers') or []
            if not isinstance(offers, list) or len(offers) > 10:
                raise ServiceError('invalid_sourcing_result', '供应商供货方案无效')
            for n, raw in enumerate(offers):
                # Search output never promotes itself to a supplier reply/verified
                # customer commitment, regardless of model-supplied flags.
                allowed = {k: v for k, v in raw.items() if k in _OFFER_FIELDS}
                allowed.update(source_type='page_reference', reply_enabled=False)
                data = self._offer_data(allowed)
                oid = 'offer_' + hashlib.sha256(encoded([task['task_id'], supplier_id, n]).encode()).hexdigest()
                if not self.repo.get('supplier_offer', oid):
                    self.repo.put('supplier_offer', oid, {**data, 'offer_id': oid, 'seller_id': self.seller_id,
                        'local_product_id': product['local_product_id'], 'supplier_id': supplier_id,
                        'revision': 1, 'created_at': stamp, 'updated_at': stamp}, history=True)
        execution = result.get('execution') or {}
        task.pop('error_code', None)
        task.update(status='blocked' if task['source_context'].get('auto_inquiry') else 'succeeded',
                    phase='inquiry' if task['source_context'].get('auto_inquiry') else 'completed',
                    message='已录入货源；自动询价接口尚未验收，未发送' if task['source_context'].get('auto_inquiry') else '已录入产品与供应商',
                    conversation_id=execution.get('conversation_id') or task.get('conversation_id'),
                    updated_at=now_ms(), completed_at=now_ms(),
                    result_evidence={k: execution[k] for k in ('tool_names', 'search_calls', 'result_source', 'request_sha256') if k in execution})
        self.repo.put('procurement_task', task['task_id'], task, history=True)
