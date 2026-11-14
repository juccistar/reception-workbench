import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from reception_service.integrations.demo import DemoIntegration
from reception_service.integrations.live import LiveIntegration
from reception_service.integrations.analysis import validate_analysis
from reception_service.integrations.accio_native.vendor.adapters.desktop_query import DesktopQueryRunner
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError


class AssistanceTests(unittest.TestCase):
    def test_demo_manual_seller_last_and_no_case(self):
        with tempfile.TemporaryDirectory() as root:
            demo = DemoIntegration(Path(root), {'seller_id': 's'})
            calls = []
            result = demo.generate_manual({'seller_id': 's', 'buyer_id': 'b', 'conversation_id': 'c',
                'case_id': None, 'supplement': '', 'recent_messages': [
                    {'role': 'buyer', 'text': 'Hello'}, {'role': 'seller', 'text': 'Please wait'}]},
                'request', on_call_start=lambda: calls.append(1))
            self.assertTrue(result['reply'])
            self.assertEqual(calls, [1])
            self.assertEqual(demo.translate([{'key': 'a', 'text': 'I need 100 pieces'},
                                             {'key': 'b', 'text': 'Bonjour'}], 'r')['items'],
                             [{'key': 'a', 'text': '我需要100件'}, {'key': 'b', 'text': '你好'}])

    def test_live_manual_allows_seller_last_and_empty_supplement(self):
        live = object.__new__(LiveIntegration)
        live.resolve_identity = lambda seller: seller
        seen = []
        live._assistance_query = lambda prompt, context, request, started, event, **options: (
            seen.append((prompt, context)) or {'reply_messages': [
                {'body': 'Thank you for waiting.', 'reply_sources': ['buyer-message']}]})
        context = {'seller_id': '1', 'buyer_id': '2', 'conversation_id': '1-2#test', 'case_id': None,
                   'case': {'seller_id': '1', 'buyer_id': '2', 'conversation_id': '1-2#test', 'requirements': {}},
                   'supplement': '', 'historical_evidence': [], 'recent_messages': [
                       {'message_id': 'buyer-message', 'role': 'buyer', 'text': 'Price?'},
                       {'message_id': 'seller-message', 'role': 'seller', 'text': 'Please wait.'}]}
        self.assertEqual(live.generate_manual(context, 'r')['reply'], 'Thank you for waiting.')
        self.assertIs(seen[0][1], context)
        self.assertIn('supplement', seen[0][0])

    def test_translation_validates_association_and_allows_foreign_output(self):
        live = object.__new__(LiveIntegration)
        live._assistance_query = lambda *args: {'items': [{'key': 'b', 'text': 'Bonjour'}, {'key': 'a', 'text': '100 pcs'}]}
        result = live.translate([{'key': 'a', 'text': '100 pcs'}, {'key': 'b', 'text': 'Hello'}], 'r')
        self.assertEqual(result['items'], [{'key': 'a', 'text': '100 pcs'}, {'key': 'b', 'text': 'Bonjour'}])
        live._assistance_query = lambda *args: {'items': [{'key': 'a', 'text': 'x'}, {'key': 'a', 'text': 'y'}]}
        with self.assertRaises(Exception):
            live.translate([{'key': 'a', 'text': 'x'}, {'key': 'b', 'text': 'y'}], 'r')

    def test_chinese_requirement_change_keeps_original_evidence(self):
        msg = {'seller_id': 's', 'buyer_id': 'b', 'conversation_id': 'c', 'message_id': 'm', 'role': 'buyer', 'text': 'I need 100 blue bags'}
        context = {'case': {'case_id': 'case-1', 'seller_id': 's', 'buyer_id': 'b', 'conversation_id': 'c', 'requirements': {}},
                   'case_catalog': [{'case_ref': 'c1', 'case_id': 'case-1', 'requirements': {}}],
                   'recent_messages': [msg], 'historical_evidence': [], 'knowledge': [], 'required_fields': ['product']}
        raw = {'action': 'ask', 'reply_messages': [{'body': 'Thanks', 'reply_sources': ['m']}],
               'conversation_summary': '客户需要100个蓝色袋子。',
               'requirement_changes': [{'action': 'update', 'case_ref': 'c1', 'trigger_evidence_ids': ['m'],
                   'set_fields': {'product': {'value': '蓝色袋子', 'evidence_ids': ['m'],
                                               'source_excerpts': {'m': 'blue bags'}}}}]}
        output = validate_analysis(raw, context)
        product = output['requirement_changes'][0]['set_fields']['product']
        self.assertEqual(product['value'], '蓝色袋子')
        self.assertEqual(product['source_excerpts'], {'m': 'blue bags'})
        self.assertNotIn('display_value', product)
        self.assertEqual(context['recent_messages'][0]['text'], 'I need 100 blue bags')

    def test_cancellation_and_timeout_with_and_without_recovery(self):
        class Records:
            def __init__(self, *args):
                self.folder = True
                self.status = 'not_found'
            def find(self):
                return None
        with tempfile.TemporaryDirectory() as root:
            args = [sys.executable, '-c', 'import time; time.sleep(30)']
            for recovery in (False, True):
                for cancel in (False, True):
                    runner = DesktopQueryRunner(Path(root))
                    event = threading.Event()
                    timer = threading.Timer(.25, event.set) if cancel else None
                    if timer:
                        timer.start()
                    start = time.monotonic()
                    try:
                        with patch('reception_service.integrations.accio_native.vendor.adapters.desktop_query._prepare', return_value=(args, 'name', 'agent')), \
                             patch('reception_service.integrations.accio_native.vendor.adapters.desktop_query._OwnRecords', Records):
                            with self.assertRaises(AdapterError) as caught:
                                runner.run(['x', 'desktop', 'query'], input='prompt', timeout=.6,
                                           recover_own_results=recovery, cancel_event=event)
                        self.assertEqual(caught.exception.code, 'cancelled' if cancel else 'timeout')
                        self.assertLess(time.monotonic() - start, 5)
                    finally:
                        if timer:
                            timer.cancel()


if __name__ == '__main__':
    unittest.main()
