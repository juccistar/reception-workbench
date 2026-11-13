import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from reception_service.integrations import sourcing
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError


class SourcingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        account = Path(self.tmp.name) / 'accounts' / 'acct'
        self.folder = account / 'conversations' / 'dm'
        self.folder.mkdir(parents=True)
        self.native = type('Native', (), {'cli': str(account / 'workctl.exe'), 'data_dir': Path(self.tmp.name)})()
        self.context = {'product_id': '12345', 'name': '棉袋', 'supplier_limit': 2}
        prompt = sourcing._prompt(self.context)
        self.record = dict(agent_id='agent', request_id='request', conversation_name='reception-source-' + sourcing._hash('request')[:32], prompt=prompt, prompt_sha256=sourcing._hash(prompt), started_at=0)
        self.common = {'conversationId': 'cid', 'agentId': 'agent', 'status': 'finished'}
        self.search = '| # | Product ID | Product Title | Image | Price / MOQ | Review | Lead Time | Supplier / Location | Badges | Matched Requirements | Product URL |\n| 1 | 99999 | private title | | US$0.85 / 100 pieces | | | Real Supplier / manufacturer | | | https://www.alibaba.com/product-detail/item_99999.html |'

    def rows(self):
        c = self.common
        return [dict(c, role='req', content=self.record['prompt'], requestId='rid', messageId='root'),
                dict(c, role='res', messageId='branch', branchParentId='root', clientRequestId='rid', tool_calls=[{'id': 'call', 'name': 'product_supplier_search', 'arguments': {'query': 'bag'}}]),
                dict(c, role='tool', messageId='branch', parentMessageId='branch', branchParentId='branch', tool_call_id='call', name='product_supplier_search', content=self.search),
                dict(c, role='res', messageId='branch', branchParentId='branch', clientRequestId='rid', content=json.dumps({'candidates': [{'supplier_product_id': '99999', 'name': 'Real Supplier', 'unit_price': '0.01'}]}))]

    def save(self, rows, cid='cid'):
        (self.folder / (cid + '.json')).write_text(json.dumps({'id': cid, 'name': self.record['conversation_name'], 'agentId': 'agent'}), encoding='utf-8')
        (self.folder / (cid + '.message_1.jsonl')).write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding='utf-8')

    def test_evidence_and_wrong_original_id(self):
        self.save(self.rows())
        result = sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)
        self.assertIsNone(result['product_url'])
        offer = result['suppliers'][0]['offers'][0]
        self.assertEqual(offer['unit_price'], '0.85')
        self.assertEqual(offer['moq'], '100')
        self.assertFalse(offer['reply_enabled'])
        self.assertNotIn('private title', json.dumps(result))

    def test_model_cannot_invent_supplier_or_link(self):
        rows = self.rows()
        rows[-1]['content'] = json.dumps({'candidates': [{'supplier_product_id': '99999', 'name': 'Invented'}]})
        self.save(rows)
        self.assertEqual(sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)['suppliers'], [])

    def test_wrong_or_duplicate_trace_rejected(self):
        for mutation in ('prompt', 'duplicate', 'tool', 'branch'):
            with self.subTest(mutation=mutation):
                rows = self.rows()
                if mutation == 'prompt': rows[0]['content'] += ' '
                if mutation == 'duplicate': rows.insert(3, dict(rows[2]))
                if mutation == 'tool': rows[1]['tool_calls'][0]['name'] = 'send_message'
                if mutation == 'branch': rows[-1]['branchParentId'] = 'root'
                self.save(rows)
                with self.assertRaises(AdapterError): sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)

    def test_approval_refusal_is_terminal(self):
        rows = self.rows()
        rows[1]['approvalStatus'] = 'denied'
        self.save(rows)
        with self.assertRaises(AdapterError):
            sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)

    def test_exact_original_link_from_search(self):
        rows = self.rows()
        rows[2]['content'] += '\n| 2 | 12345 | title | | | | | Other / supplier | | | https://www.alibaba.com/product-detail/item_12345.html |'
        self.save(rows)
        result = sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)
        self.assertIsNone(result['product_url'])
        for model_id, model_url, expected in (
                ('12345', 'https://www.alibaba.com/product-detail/item_12345.html', None),
                ('99999', 'https://www.alibaba.com/product-detail/item_99999.html', None),
                ('12345', 'https://www.alibaba.com/product-detail/item_99999.html', None),
                ('99999', 'https://www.alibaba.com/product-detail/item_12345.html', None),
                ('12345', 'https://www.alibaba.com/product-detail/other_12345.html', None)):
            with self.subTest(model_id=model_id, model_url=model_url):
                rows[-1]['content'] = json.dumps({'product_id': model_id, 'product_url': model_url, 'candidates': []})
                self.save(rows)
                result = sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)
                self.assertEqual(result['product_url'], expected)

    def test_legacy_prompt_record_remains_reconcilable(self):
        self.record['prompt'] = sourcing._prompt(self.context, legacy=True)
        self.record['prompt_sha256'] = sourcing._hash(self.record['prompt'])
        self.save(self.rows())
        self.assertIsNone(sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)['product_url'])
        records = []
        sourcing.demo_source_product(self.context, 'legacy-demo', on_request_created=records.append)
        records[0]['prompt'] = sourcing._prompt(self.context, legacy=True)
        records[0]['prompt_sha256'] = sourcing._hash(records[0]['prompt'])
        result = sourcing.demo_reconcile_source_product(records[0], self.context)
        self.assertEqual(result['execution']['request_sha256'], records[0]['prompt_sha256'])

    def test_duplicate_metadata_and_cancel(self):
        self.save(self.rows())
        self.save(self.rows(), 'cid2')
        with self.assertRaises(AdapterError): sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)
        event = threading.Event(); event.set()
        with self.assertRaises(AdapterError): sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context, cancel_event=event)

    def test_custom_product_uses_text_search_and_no_original_url(self):
        context = dict(self.context, product_id='定制-12345')
        prompt = sourcing._prompt(context)
        self.assertIn('本地定制编号不是平台商品ID', prompt)
        self.assertNotIn('"product_url":', prompt)
        result = sourcing._normalize(context, self.record, 'cid', self.rows()[-1]['content'], self.search, ['product_supplier_search'], 'completed_record')
        self.assertEqual(result['product_id'], '定制-12345')
        self.assertIsNone(result['product_url'])
        self.assertEqual(result['suppliers'][0]['offers'][0]['supplier_product_id'], '99999')

    def test_only_frozen_public_image_is_allowed_in_tool_trace(self):
        image = 'https://s.alicdn.com/images/public.jpg'
        context = dict(self.context, main_image_url=image)
        self.record['prompt'] = sourcing._prompt(context)
        self.record['prompt_sha256'] = sourcing._hash(self.record['prompt'])
        self.assertIn(image, self.record['prompt'])
        for supplied, accepted in [(image, True), (None, True), ('https://s.alicdn.com/images/other.jpg', False), ('', False)]:
            with self.subTest(supplied=supplied):
                rows = self.rows()
                if supplied is not None:
                    rows[1]['tool_calls'][0]['arguments']['reference_image'] = supplied
                self.save(rows)
                if accepted:
                    self.assertEqual(sourcing.reconcile_source_product(self.native, 'agent', self.record, context)['execution']['search_calls'], 1)
                else:
                    with self.assertRaises(AdapterError):
                        sourcing.reconcile_source_product(self.native, 'agent', self.record, context)
        with self.assertRaises(AdapterError):
            sourcing.reconcile_source_product(self.native, 'agent', self.record, dict(context, main_image_url='https://s.alicdn.com/changed.jpg'))
        for invalid in ('C:/private.jpg', 'http://s.alicdn.com/a.jpg', 'https://s.alicdn.com.evil.test/a', 'https://user@s.alicdn.com/a'):
            with self.subTest(invalid=invalid), self.assertRaises(AdapterError):
                sourcing._prompt(dict(self.context, main_image_url=invalid))
        self.record['prompt'] = sourcing._prompt(self.context)
        self.record['prompt_sha256'] = sourcing._hash(self.record['prompt'])
        rows = self.rows()
        rows[1]['tool_calls'][0]['arguments']['reference_image'] = image
        self.save(rows)
        with self.assertRaises(AdapterError):
            sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)

    def test_name_length_and_price_ranges(self):
        context = dict(self.context, name='袋' * 160)
        sourcing._prompt(context)
        with self.assertRaises(AdapterError):
            sourcing._prompt(dict(context, name='袋' * 161))
        for price in ('US$0.39–0.85 / 10 pieces', 'US$0.39 - 0.85 / 10 pieces', 'US$0.39-0.85 / 10 pieces'):
            with self.subTest(price=price):
                rows = self.rows()
                rows[2]['content'] = self.search.replace('US$0.85 / 100 pieces', price)
                self.save(rows)
                result = sourcing.reconcile_source_product(self.native, 'agent', self.record, self.context)
                self.assertEqual(result['suppliers'][0]['offers'][0]['unit_price'], '')

    def test_demo_record_reconciles_without_new_call(self):
        records, calls = [], []
        result = sourcing.demo_source_product(self.context, 'demo-request', on_request_created=records.append,
                                               on_call_start=lambda: calls.append('start'))
        self.assertEqual(len(records), 1)
        self.assertEqual(calls, ['start'])
        self.assertEqual(records[0]['conversation_name'], 'reception-source-' + sourcing._hash('demo-request')[:32])
        self.assertEqual(result['execution']['conversation_name'], records[0]['conversation_name'])
        self.assertEqual(result['execution']['request_sha256'], records[0]['prompt_sha256'])
        self.assertEqual(result['execution']['request_id'], records[0]['request_id'])
        with patch.object(sourcing, 'OwnedProcess', side_effect=AssertionError('unexpected process')):
            recovered = sourcing.demo_reconcile_source_product(records[0], self.context)
        self.assertEqual(recovered, result)
        self.assertEqual(calls, ['start'])
        with self.assertRaises(AdapterError):
            sourcing.demo_reconcile_source_product(records[0], dict(self.context, name='其他产品'))
        with self.assertRaises(AdapterError):
            sourcing.demo_reconcile_source_product(dict(records[0], prompt_sha256='bad'), self.context)

    def test_false_failure_recovered_and_persisted_before_spawn(self):
        calls = []
        test = self
        class Process:
            def __init__(self, args, inheritance, prompt):
                calls.append('spawn')
                test.assertEqual(calls, ['persist', 'grant', 'spawn'])
                test.save(test.rows())
                self.proc = type('Proc', (), {'returncode': 70})()
            def finished(self): return True
            def poll(self): return 70
            def output(self): return ('error', '')
        def persist(record):
            calls.append('persist')
            self.assertEqual(record['prompt_sha256'], self.record['prompt_sha256'])
        with patch.object(sourcing, 'OwnedProcess', Process), patch.object(sourcing, 'native_inheritance', return_value=nullcontext({})), patch.object(sourcing, 'track_native_child'), patch.object(sourcing, 'release_native_child'):
            result = sourcing.run_source_product(self.native, 'agent', self.context, 'request', on_request_created=persist, on_call_start=lambda: calls.append('grant'))
            self.assertEqual(result['execution']['result_source'], 'completed_record')
            with self.assertRaises(AdapterError): sourcing.run_source_product(self.native, 'agent', self.context, 'request', on_request_created=persist)
            self.assertEqual(calls, ['persist', 'grant', 'spawn'])


if __name__ == '__main__':
    unittest.main()
