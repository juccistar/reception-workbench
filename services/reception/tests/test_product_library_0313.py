"""0.3.13 product behavior through the real service, with isolated Demo data."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.repository import new_id, now_ms
from reception_service.service import ReceptionService

FIELDS = ('product', 'product_id', 'product_url', 'product_images', 'quantity', 'specifications',
          'customization', 'destination', 'delivery', 'budget', 'packaging', 'certifications', 'contact')


class ProductLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        self.scheduler.start()
        self.service = ReceptionService(Path(self.tmp.name))

    def tearDown(self):
        self.service.close()
        self.scheduler.stop()
        self.tmp.cleanup()

    def demand(self, cid='demo-emma', pid='1600000000123', **values):
        with self.service._lock, self.service.repo.transaction():
            case = self.service.get_conversation(cid)['case']
            requirements = {key: {'value': [] if key == 'product_images' else None, 'evidence_ids': []} for key in FIELDS}
            for key, value in dict(product='棉布袋', product_id=pid, quantity='100件', **values).items():
                requirements[key] = {'value': value, 'evidence_ids': ['synthetic-source']}
            case.update(requirements=requirements, requirements_format='ai-v1')
            self.service.repo.put('case', case['case_id'], case)
            self.service._sync_requirement_product(case)
            return case

    def product(self):
        return self.service.call('list_products')['items'][0]

    def supplier(self):
        return self.service.call('save_supplier', dict(name='合成供货商', platform='alibaba',
            platform_supplier_id='synthetic-supplier', product_url=None, contact='', notes='', client_mutation_id=new_id('mutation')))

    def offer(self, product=None, supplier=None, **data):
        product = product or self.product()
        supplier = supplier or self.supplier()
        return self.service.call('save_product_offer', dict(local_product_id=product['local_product_id'],
            supplier_id=supplier['supplier_id'], data=dict(source_type='manual_verified', enabled=True,
                reply_enabled=True, moq='50', unit='pieces', unit_price='0.80', currency='USD',
                customer_wording='交期为确认规格后7天', **data), client_mutation_id=new_id('mutation')))

    def select(self, case, offer):
        case = self.service.repo.get('case', case['case_id'])
        return self.service.call('select_supplier_offer', dict(case_id=case['case_id'], offer_id=offer['offer_id'] if offer else None,
            expected_case_revision=case['revision'], client_mutation_id=new_id('mutation')))

    def wait_task(self, task_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            # The reception scheduler is deliberately disabled in this fixture;
            # advance only the independent product/image dispatch as production does.
            self.service._schedule_procurement()
            task = self.service.repo.get('procurement_task', task_id)
            if task['status'] not in ('queued', 'running'):
                return task
            threading.Event().wait(.01)
        self.fail('Synthetic procurement did not finish')

    def test_same_id_creates_one_product_and_task_without_sharing_buyer_fields(self):
        a = self.demand(destination='客户甲私有地址', contact='private-a', budget='20美元', product_images=['private-image'])
        b = self.demand('demo-sofia', destination='客户乙私有地址', contact='private-b', budget='40美元')
        self.assertEqual(len(self.service.list_products()['items']), 1)
        self.assertEqual(len(self.service.list_procurement_tasks()['items']), 1)
        stored = self.service.repo.get('library_product', self.product()['local_product_id'])
        self.assertNotIn('original_title', stored)
        self.assertNotIn('title', stored)
        self.assertEqual(stored['product_images'], [])
        context = self.service._product_context_for_case(b)
        self.assertNotIn('private-a', str(context))
        self.assertNotIn('客户甲私有地址', str(context))
        self.assertNotIn('private-image', str(context))
        self.assertEqual(self.service.repo.get('case', a['case_id'])['requirements']['contact']['value'], 'private-a')

    def test_customer_counts_deduplicate_and_validate_ownership_and_follow_binding_changes(self):
        case = self.demand()
        product = self.product()
        product_id = product['local_product_id']
        repo = self.service.repo
        binding = repo.get('requirement_product_binding', case['case_id'])
        with self.service._lock, repo.transaction():
            duplicate = dict(case, case_id='case-duplicate')
            repo.put('case', duplicate['case_id'], duplicate)
            repo.put('requirement_product_binding', duplicate['case_id'], dict(binding, case_id=duplicate['case_id']))
            foreign = dict(case, case_id='case-foreign', seller_id='foreign-seller')
            repo.put('case', foreign['case_id'], foreign)
            repo.put('requirement_product_binding', foreign['case_id'], dict(binding, case_id=foreign['case_id']))
            invalid = dict(case, case_id='case-invalid', buyer_id='wrong-buyer')
            repo.put('case', invalid['case_id'], invalid)
            repo.put('requirement_product_binding', invalid['case_id'], dict(binding, case_id=invalid['case_id']))
            repo.put('requirement_product_binding', 'missing-case', dict(binding, case_id='missing-case'))
            foreign_binding_case = dict(case, case_id='foreign-binding')
            repo.put('case', foreign_binding_case['case_id'], foreign_binding_case)
            repo.put('requirement_product_binding', 'foreign-binding', dict(binding, case_id='foreign-binding', seller_id='foreign-seller'))
            foreign_conversation = dict(repo.get('conversation', case['conversation_id']),
                                        conversation_id='foreign-conversation', seller_id='foreign-seller', buyer_id='foreign-buyer')
            repo.put('conversation', foreign_conversation['conversation_id'], foreign_conversation)
            foreign_conversation_case = dict(case, case_id='foreign-conversation-case',
                                             conversation_id='foreign-conversation', buyer_id='foreign-buyer')
            repo.put('case', foreign_conversation_case['case_id'], foreign_conversation_case)
            repo.put('requirement_product_binding', foreign_conversation_case['case_id'],
                     dict(binding, case_id=foreign_conversation_case['case_id'], conversation_id='foreign-conversation'))
        self.assertEqual(self.product()['customer_count'], 1)
        self.assertEqual(self.service.get_product(product_id)['product']['customer_count'], 1)
        second = self.demand('demo-sofia')
        self.assertEqual(self.product()['customer_count'], 2)
        with self.service._lock, repo.transaction():
            updated = repo.get('requirement_product_binding', second['case_id'])
            updated['local_product_id'] = 'another-product'
            repo.put('requirement_product_binding', second['case_id'], updated)
        self.assertEqual(self.product()['customer_count'], 1)
        with self.service._lock, repo.transaction():
            for case_id in (case['case_id'], duplicate['case_id']):
                updated = repo.get('requirement_product_binding', case_id)
                updated['local_product_id'] = 'another-product'
                repo.put('requirement_product_binding', case_id, updated)
        self.assertEqual(self.service.get_product(product_id)['product']['customer_count'], 0)

    def test_product_link_is_derived_from_id_and_ignores_model_url(self):
        case = self.demand(product_url='https://example.invalid/guessed_999.html')
        expected = 'https://www.alibaba.com/product-detail/_1600000000123.html'
        self.assertEqual(self.product()['product_url'], expected)
        self.assertEqual(self.service.get_conversation('demo-emma')['case']['requirements']['product_url']['value'], expected)
        self.assertNotIn('product_url', self.service._product_context_for_case(case)['product_context'])

    def test_clearing_ai_product_removes_old_supplier_from_context(self):
        case = self.demand()
        self.select(case, self.offer())
        self.assertTrue(self.service._product_context_for_case(case)['supplier_context']['usable'])
        cleared = self.demand(pid=None)
        self.assertIsNone(self.service._product_context_for_case(cleared)['supplier_context'])
        self.assertEqual(len(self.service.list_products()['items']), 1)
        self.assertEqual(len(self.service.list_procurement_tasks()['items']), 1)

    def test_supplier_crud_revision_and_idempotency(self):
        data = dict(name='供货方', platform='alibaba', platform_supplier_id=None, product_url=None,
                    contact='', notes='', client_mutation_id='same-mutation')
        first = self.service.save_supplier(**data)
        self.assertEqual(self.service.save_supplier(**data), first)
        with self.assertRaises(ServiceError) as conflict:
            self.service.save_supplier(**dict(data, name='Different'))
        self.assertEqual(conflict.exception.code, 'mutation_conflict')
        update = dict(data, client_mutation_id='updated', supplier_id=first['supplier_id'], expected_revision=1, contact='合成联系人')
        saved = self.service.save_supplier(**update)
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(self.service.save_supplier(**update), saved)
        with self.assertRaises(ServiceError) as stale:
            self.service.save_supplier(**dict(update, client_mutation_id='stale'))
        self.assertEqual(stale.exception.code, 'revision_conflict')

    def test_default_offer_only_applies_to_new_bindings(self):
        a = self.demand()
        product = self.product()
        offer = self.offer()
        self.service.set_product_default_offer(product['local_product_id'], offer['offer_id'], product['revision'], 'default')
        self.assertIsNone(self.service._product_context_for_case(a)['supplier_context'])
        b = self.demand('demo-sofia')
        self.assertEqual(self.service._product_context_for_case(b)['supplier_context']['offer_id'], offer['offer_id'])
        product = self.product()
        self.service.set_product_default_offer(product['local_product_id'], None, product['revision'], 'default-clear')
        self.assertEqual(self.service._product_context_for_case(b)['supplier_context']['offer_id'], offer['offer_id'])
        detail = self.service.get_conversation('demo-sofia')
        self.assertEqual(detail['case'].get('local_product_id'), product['local_product_id'])
        self.assertEqual(detail['case'].get('selected_offer_id'), offer['offer_id'])

    def test_selection_and_offer_edit_change_generation_fingerprint(self):
        case = self.demand()
        before = self.service.get_context(case['case_id'])['fingerprint']
        offer = self.offer()
        self.select(case, offer)
        selected = self.service.get_context(case['case_id'])['fingerprint']
        self.assertNotEqual(before, selected)
        data = {k: v for k, v in offer.items() if k in ('moq', 'unit', 'unit_price', 'currency', 'source_type',
            'enabled', 'reply_enabled', 'customer_wording')}
        data['customer_wording'] = '交期为确认规格后10天'
        self.service.save_product_offer(offer['local_product_id'], offer['supplier_id'], data, 'quote-revision',
                                        offer_id=offer['offer_id'], expected_revision=offer['revision'])
        self.assertNotEqual(selected, self.service.get_context(case['case_id'])['fingerprint'])

    def test_selected_offer_expiry_and_moq_are_not_customer_commitments(self):
        case = self.demand()
        quote = self.offer(valid_until=now_ms() - 1)
        self.select(case, quote)
        self.assertFalse(self.service._product_context_for_case(case)['supplier_context']['usable'])
        data = dict(source_type='manual_verified', enabled=True, reply_enabled=True, moq='500', unit='pieces', unit_price='0.50')
        offer = self.service.save_product_offer(self.product()['local_product_id'], quote['supplier_id'], data, 'moq-quote')
        self.select(case, offer)
        context = self.service._product_context_for_case(case)['supplier_context']
        self.assertFalse(context['usable'])
        self.assertFalse(context['is_customer_selling_price'])
        self.assertTrue(context['internal_only'])

    def test_reference_price_cannot_enable_a_customer_commitment(self):
        self.demand()
        supplier = self.supplier()
        with self.assertRaises(ServiceError) as error:
            self.service.save_product_offer(self.product()['local_product_id'], supplier['supplier_id'],
                dict(source_type='page_reference', reply_enabled=True, unit_price='1.00'), 'bad-reference')
        self.assertEqual(error.exception.code, 'reference_only')
        self.assertEqual(self.service.get_product(self.product()['local_product_id'])['offers'], [])

    def test_wrong_product_offer_and_stale_case_are_rejected(self):
        a = self.demand()
        offer = self.offer()
        b = self.demand('demo-sofia', '1600000000456')
        with self.assertRaises(ServiceError) as error:
            self.select(b, offer)
        self.assertEqual(error.exception.code, 'ownership_mismatch')
        with self.assertRaises(ServiceError) as error:
            self.service.select_supplier_offer(a['case_id'], offer['offer_id'], a['revision'] + 99, 'stale-case')
        self.assertEqual(error.exception.code, 'revision_conflict')

    def test_single_durable_demo_search_no_repeat_after_other_customer_or_reconcile(self):
        self.demand()
        product = self.product()
        task_id = self.service.list_procurement_tasks()['items'][0]['task_id']
        original = self.service.integrations.source_product
        with patch.object(self.service.integrations, 'source_product', wraps=original) as search:
            self.service.set_procurement_enabled(True)
            task = self.wait_task(task_id)
            self.assertEqual(task['status'], 'blocked', task)
            self.assertEqual(search.call_count, 1)
            self.assertIn('prompt_sha256', task['execution'])
            self.assertEqual(len(self.service.get_product(product['local_product_id'])['suppliers']), 1)
            self.demand('demo-sofia')
            self.service._schedule_procurement()
            self.assertEqual(search.call_count, 1)
            self.service.reconcile_procurement(task_id)
            self.assertEqual(self.wait_task(task_id)['status'], 'blocked')
            self.assertEqual(search.call_count, 1)
            self.assertEqual(len(self.service.get_product(product['local_product_id'])['offers']), 1)

    def test_queue_full_keeps_the_valid_requirement_and_durable_pending_task(self):
        self.service.set_procurement_enabled(True)
        with patch.object(self.service._queue, 'put', side_effect=ServiceError('task_queue_full', 'synthetic full')):
            case = self.demand()
            self.service._schedule_procurement()
        self.assertEqual(self.service.repo.get('case', case['case_id'])['requirements']['product_id']['value'], '1600000000123')
        task = self.service.list_procurement_tasks()['items'][0]
        self.assertEqual(task['status'], 'waiting_input')
        self.assertIsNone(task['job_id'])
        self.service._schedule_procurement()
        self.assertEqual(self.wait_task(task['task_id'])['status'], 'blocked')

    def test_pause_during_submission_unknown_is_reconciled_without_resend(self):
        self.demand()
        entered, release = threading.Event(), threading.Event()
        original = self.service.integrations.source_product
        def slow(context, request_id, **kwargs):
            # Real helper persists the exact record; stop after it has produced a
            # deterministic result to model a completed external call in flight.
            result = original(context, request_id, **kwargs)
            entered.set()
            release.wait(3)
            return result
        with patch.object(self.service.integrations, 'source_product', side_effect=slow) as search:
            self.service.set_procurement_enabled(True)
            self.assertTrue(entered.wait(3))
            self.service.set_procurement_enabled(False)
            release.set()
            task = self.service.list_procurement_tasks()['items'][0]
            deadline = time.monotonic() + 3
            while any(j['status'] == 'running' for j in self.service.repo.list('job')) and time.monotonic() < deadline:
                threading.Event().wait(.01)
            self.assertEqual(self.service.repo.get('procurement_task', task['task_id'])['status'], 'unknown')
            with self.assertRaises(ServiceError):
                self.service.request_procurement(self.product()['local_product_id'], 'new-key')
            self.service.reconcile_procurement(task['task_id'])
            self.assertEqual(self.wait_task(task['task_id'])['status'], 'blocked')
            self.assertEqual(search.call_count, 1)

    def test_identity_change_stops_before_any_external_sourcing_request(self):
        self.demand()
        with patch.object(self.service, '_ensure_identity', side_effect=ServiceError('seller_mismatch', 'synthetic identity change')), \
                patch.object(self.service.integrations, 'source_product') as search:
            self.service.set_procurement_enabled(True)
            task = self.service.list_procurement_tasks()['items'][0]
            result = self.wait_task(task['task_id'])
            self.assertEqual(result['status'], 'failed')
            self.assertNotIn('execution', result)
            search.assert_not_called()

    def test_repeated_ai_update_preserves_explicit_product_binding_and_offer(self):
        a = self.demand()
        original_product = self.product()
        b = self.demand('demo-sofia', '1600000000456')
        products = self.service.list_products()['items']
        chosen = next(p for p in products if p['local_product_id'] != original_product['local_product_id'])
        quote = self.offer(product=chosen)
        self.service.bind_requirement_product(a['case_id'], chosen['local_product_id'], a['revision'], 'manual-bind')
        self.select(a, quote)
        a = self.demand()
        context = self.service._product_context_for_case(a)
        self.assertEqual(context['supplier_context']['offer_id'], quote['offer_id'])
        self.assertEqual(context['product_context']['product_id'], chosen['product_id'])
        a = self.demand(pid='1600000000789')
        self.assertIsNone(self.service._product_context_for_case(a)['supplier_context'])
        self.assertEqual(self.service._product_context_for_case(a)['product_context']['product_id'], '1600000000789')

    def test_sourcing_does_not_overwrite_derived_links_or_private_conditions(self):
        a = self.demand(destination='客户甲地址')
        b = self.demand('demo-sofia', destination='客户乙地址')
        self.demand('demo-noah', '1600000000456')
        original = self.service.integrations.source_product
        def fixture(context, request_id, **options):
            result = original(context, request_id, **options)
            if context['product_id'] == '1600000000123':
                result['product_url'] = 'https://www.alibaba.com/product-detail/synthetic_1600000000123.html'
            return result
        with patch.object(self.service.integrations, 'source_product', side_effect=fixture):
            self.service.set_procurement_enabled(True)
            for task in self.service.list_procurement_tasks()['items']:
                self.assertEqual(self.wait_task(task['task_id'])['status'], 'blocked')
        for case, address in ((a, '客户甲地址'), (b, '客户乙地址')):
            stored = self.service.repo.get('case', case['case_id'])
            self.assertEqual(stored['requirements']['product_url']['value'], 'https://www.alibaba.com/product-detail/_1600000000123.html')
            self.assertEqual(stored['requirements']['destination']['value'], address)
        noah = self.service.get_conversation('demo-noah')['case']
        self.assertEqual(noah['requirements']['product_url']['value'], 'https://www.alibaba.com/product-detail/_1600000000456.html')

    def test_settings_revoke_grant_and_restart_does_not_restore_it(self):
        self.demand()
        settings = self.service.set_procurement_enabled(True)
        self.assertTrue(settings['authorized'])
        self.assertFalse(settings['inquiry_available'])
        self.assertFalse(self.service.save_procurement_settings(True, False, 1)['authorized'])
        task = self.service.list_procurement_tasks()['items'][0]
        if task['status'] == 'unknown':
            self.service.reconcile_procurement(task['task_id'])
            self.wait_task(task['task_id'])
        self.service.close()
        self.service = ReceptionService(Path(self.tmp.name))
        self.assertFalse(self.service.get_procurement_settings()['authorized'])
        self.assertEqual(self.service.get_procurement_settings()['supplier_limit'], 1)
        self.assertEqual(len(self.service.list_procurement_tasks()['items']), 1)


if __name__ == '__main__':
    unittest.main()
