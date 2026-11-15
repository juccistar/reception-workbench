import copy
from pathlib import Path
import threading
import unittest

from reception_service.errors import ServiceError
from reception_service.manual_chat import ManualChatMixin
from reception_service.whatsapp_merchant_context import WhatsAppMerchantContextMixin


class MemoryRepo:
    def __init__(self):
        self.rows = {'meta': {'seller': {'seller_id': '123'}}, 'conversation': {
            'c1': {'seller_id': '123', 'conversation_id': 'c1', 'buyer_id': 'b1', 'revision': 1, 'case_id': 'case1'},
            'c2': {'seller_id': '999', 'conversation_id': 'c2', 'buyer_id': 'b2'}},
            'case': {'case1': {'seller_id': '123', 'conversation_id': 'c1', 'revision': 1,
                               'requirements': {'product': {'value': 'bags'}, 'product_url': {'value': 'secret-link'},
                                                'product_images': {'value': ['private-image']}}}},
            'message': {'m1': {'seller_id': '123', 'buyer_id': 'b1', 'conversation_id': 'c1', 'message_id': 'm1',
                               'role': 'buyer', 'text': 'hello', 'timestamp_ms': 1},
                        'm2': {'seller_id': '123', 'buyer_id': 'other', 'conversation_id': 'c1', 'message_id': 'm2',
                               'role': 'buyer', 'text': 'other customer', 'timestamp_ms': 2}}}
    def get(self, kind, key, default=None):
        return copy.deepcopy(self.rows.get(kind, {}).get(key, default))
    def list(self, kind, conversation_id=None):
        rows = list(self.rows.get(kind, {}).values())
        return copy.deepcopy([r for r in rows if conversation_id is None or r.get('conversation_id') == conversation_id])


class FakeService(WhatsAppMerchantContextMixin, ManualChatMixin):
    def __init__(self):
        self.repo = MemoryRepo(); self._lock = threading.RLock(); self.account_verified = True
        self.seller_id = '123'; self.data_root = Path('merchant-root').resolve(); self.mode = 'live'
        self._closing = False; self._closed = False; self.settings = {}
    def _require(self, kind, key):
        row = self.repo.get(kind, key)
        if row is None:
            raise ServiceError('not_found', 'not found')
        return row


class MerchantContextTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.args = dict(account_id='account', chat_id='chat', merchant_id='merchant', binding_revision=1)
    def export(self, **extra):
        return self.service._wa_export_merchant_context(**self.args, **extra)
    def test_scoped_read_and_revision(self):
        first = self.export(conversation_id='c1')
        bg = first['context']['international_background']
        self.assertEqual([m['text'] for m in bg['messages']], ['hello'])
        self.assertEqual(bg['requirements'], {'product': {'value': 'bags'}})
        self.assertEqual(first['seller_id'], '123')
        self.assertEqual(first['data_root'], str(self.service.data_root))
        self.service.repo.rows['case']['case1']['revision'] = 2
        self.assertNotEqual(first['revision'], self.export(conversation_id='c1')['revision'])
        self.service.settings['max_questions_per_reply'] = 1
        self.assertNotEqual(first['revision'], self.export(conversation_id='c1')['revision'])
    def test_owner_identity_and_shutdown(self):
        with self.assertRaises(ServiceError): self.export(conversation_id='c2')
        self.service.repo.rows['case']['case1']['conversation_id'] = 'c2'
        with self.assertRaises(ServiceError): self.export(conversation_id='c1')
        self.service.account_verified = False
        with self.assertRaises(ServiceError): self.export()
        self.service.account_verified = True; self.service._closing = True
        with self.assertRaises(ServiceError): self.export()
    def test_budget_and_disabled_writeback(self):
        self.service.repo.rows['case']['case1']['requirements']['product']['value'] = 'x' * 200000
        with self.assertRaises(ServiceError) as error: self.export(conversation_id='c1')
        self.assertEqual(error.exception.code, 'context_too_long')
        self.assertFalse(self.service._wa_writeback_capabilities()['available'])
        before = copy.deepcopy(self.service.repo.rows)
        for method in (self.service._wa_writeback_preview, self.service._wa_writeback_submit):
            with self.assertRaises(ServiceError): method(anything='ignored')
        self.assertEqual(before, self.service.repo.rows)


if __name__ == '__main__':
    unittest.main()
