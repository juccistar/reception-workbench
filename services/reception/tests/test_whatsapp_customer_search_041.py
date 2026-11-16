import threading
import unittest
from pathlib import Path

from reception_service.errors import ServiceError
from reception_service.repository import Repository
from reception_service.whatsapp_customer_search import search_merchant_customers
from reception_service.whatsapp_merchant_context import WhatsAppMerchantContextMixin
from reception_service.service import FAST


class CustomerSearchTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(None, seller_id='seller-a')

    def tearDown(self):
        self.repo.close()

    def add(self, cid, name='Alice', buyer='buyer-1', seller='seller-a', revision=1, **extra):
        self.repo.put('conversation', cid, {'conversation_id': cid, 'seller_id': seller,
                      'buyer_id': buyer, 'customer_name': name, 'revision': revision, **extra})

    def test_seller_scope_and_mismatched_repository(self):
        self.add('own')
        self.add('foreign', name='Alice', seller='seller-b')
        self.assertEqual([x['conversation_id'] for x in search_merchant_customers(self.repo, 'seller-a', 'alice')], ['own'])
        with self.assertRaises(ServiceError) as error:
            search_merchant_customers(self.repo, 'seller-b', 'Alice')
        self.assertEqual(error.exception.code, 'seller_mismatch')

    def test_search_name_and_buyer_fallback_without_guessing_company(self):
        self.add('one', name='Alice Example', buyer='buyer-A')
        self.add('two', name='', buyer='buyer-B')
        self.repo.put('meta', 'company_profile', {'seller_id': 'seller-a', 'company_name': 'Seller Business'})
        self.assertEqual(search_merchant_customers(self.repo, 'seller-a', 'ALICE')[0]['name'], 'Alice Example')
        self.assertEqual(search_merchant_customers(self.repo, 'seller-a', 'buyer-b')[0]['name'], 'buyer-B')
        self.assertEqual(search_merchant_customers(self.repo, 'seller-a', 'Seller Business'), [])

    def test_inputs_and_result_bounds(self):
        for query in (None, 'x' * 81, 'a\nb', 'a\x85b'):
            with self.subTest(query=query), self.assertRaises(ServiceError):
                search_merchant_customers(self.repo, 'seller-a', query)
        for limit in (0, 26, True, 1.0, None):
            with self.subTest(limit=limit), self.assertRaises(ServiceError):
                search_merchant_customers(self.repo, 'seller-a', '', limit)
        self.add('one', name='x' * 200)
        self.assertEqual(len(search_merchant_customers(self.repo, 'seller-a', '')[0]['name']), 120)

    def test_only_metadata_and_stable_source_revision(self):
        self.add('one', last_message='secret message', credential='secret key', customer_name_extra='secret')
        self.repo.put('message', 'secret', {'text': 'secret message', 'conversation_id': 'one'})
        first = search_merchant_customers(self.repo, 'seller-a', '')[0]
        self.assertEqual(set(first), {'conversation_id', 'name', 'buyer_id', 'revision'})
        self.assertEqual(first['buyer_id'], 'buyer-1')
        self.assertEqual(first, search_merchant_customers(self.repo, 'seller-a', '')[0])
        self.add('one', name='Alice', revision=2)
        self.assertNotEqual(first['revision'], search_merchant_customers(self.repo, 'seller-a', '')[0]['revision'])
        self.add('one', name='New name', revision=2)
        self.assertNotEqual(first['revision'], search_merchant_customers(self.repo, 'seller-a', '')[0]['revision'])

    def test_search_beyond_first_screen(self):
        self.add('older', name='Uncommon Target')
        for i in range(80):
            self.add('new-%d' % i, name='Recent')
        self.assertEqual(len(search_merchant_customers(self.repo, 'seller-a', '')), 25)
        self.assertEqual(search_merchant_customers(self.repo, 'seller-a', 'Target')[0]['conversation_id'], 'older')

    def test_private_dispatch_identity_gate(self):
        self.add('one')
        class Service(WhatsAppMerchantContextMixin):
            pass
        service = Service()
        service.repo = self.repo
        service._lock = threading.RLock()
        service.mode, service.seller_id = 'live', 'seller-a'
        service.account_verified, service._closing, service._closed = True, False, False
        service.data_root = Path('synthetic-root').resolve()
        args = dict(account_id='a', chat_id='c', merchant_id='m', binding_revision=1, query='Alice', limit=25)
        self.assertIn('_wa_export_customer_candidates', FAST)
        result = service._wa_export_customer_candidates(**args)
        self.assertEqual(result['seller_id'], 'seller-a')
        self.assertEqual(result['data_root'], str(service.data_root))
        self.assertEqual(result['candidates'][0]['conversation_id'], 'one')
        for field, value in [('account_verified', False), ('_closing', True), ('mode', 'demo'), ('seller_id', 'foreign')]:
            previous = getattr(service, field)
            setattr(service, field, value)
            with self.assertRaises(ServiceError):
                service._wa_export_customer_candidates(**args)
            setattr(service, field, previous)

    def test_invalid_conversation_and_controls_are_not_returned(self):
        self.add('bad\nid', name='Alice')
        self.add('good', name='A\nlice')
        self.repo.put('conversation', 'mismatch', {'conversation_id': 'elsewhere', 'seller_id': 'seller-a', 'buyer_id': 'b', 'customer_name': 'Alice', 'revision': 1})
        rows = search_merchant_customers(self.repo, 'seller-a', '')
        self.assertEqual([(row['conversation_id'], row['name']) for row in rows], [('good', 'Alice')])


if __name__ == '__main__':
    unittest.main()
