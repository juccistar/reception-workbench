"""Exact read-only native product boundary, with no real process or account."""
from copy import deepcopy
import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from reception_service.errors import ServiceError
from reception_service.integrations.product_lookup import normalize_product_result, read_product

PID = '1601904072453'
SELLER = '9000000000312'
URL = 'https://www.alibaba.com/product-detail/Synthetic-Bracelet_' + PID + '.html'


def response(text):
    return {'success': True, 'data': json.dumps({'prod_info': text}), 'meta': {'agent_output_contract': 'v2'}}


class NativeProductTests(unittest.TestCase):
    def native(self, reply=None, error=None, hook=None):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            if hook:
                hook()
            if error:
                raise error
            return deepcopy(reply)
        return SimpleNamespace(cli='synthetic-workctl.exe', process=SimpleNamespace(run=run),
                               _identity=Mock(return_value=(SELLER, 'identity')), calls=calls)

    def test_verified_prod_info_envelope_preserves_reference_without_inventing_link(self):
        result = normalize_product_result(response('prod_title: Synthetic bracelet\nproduct_info: Custom Logo\nprice_info: 10 USD'), PID)
        self.assertIsNone(result['product_url'])
        self.assertEqual(result['original_title'], 'Synthetic bracelet')
        self.assertTrue(result['reference_only'])
        self.assertIn('10 USD', result['summary'])
        self.assertEqual(result['product_images'], [])
        self.assertIsNone(result['sku'])

    def test_returned_link_must_match_exact_product_and_drops_tracking(self):
        result = normalize_product_result(response('prod_title: Synthetic\nproduct_url: ' + URL + '?tracking=demo#view'), PID)
        self.assertEqual(result['product_url'], URL)
        for bad in [URL.replace(PID, '1601904072454'), URL.replace('www.alibaba.com', 'www.alibaba.com.attacker.invalid'),
                    URL.replace('https://', 'http://'), URL.replace('www.alibaba.com', 'user:secret@www.alibaba.com')]:
            self.assertIsNone(normalize_product_result(response(bad), PID)['product_url'])

    def test_single_exact_documented_command_and_identity(self):
        native = self.native(response('prod_title: Synthetic'))
        result = read_product(native, SELLER, PID, 'request-1')
        native._identity.assert_called_once_with(SELLER)
        self.assertEqual(len(native.calls), 1)
        self.assertEqual(native.calls[0], ([native.cli, 'icbu', 'tm', 'get-product', '--prod-id', PID,
                                           '--self-ali-id', SELLER, '--buyer-country-code', 'US', '--format', 'json'], {'timeout': 35}))
        self.assertEqual(result['request_id'], 'request-1')

    def test_input_errors_do_not_execute_identity_or_native_tool(self):
        for seller, product, request in [('not-seller', PID, 'r'), (SELLER, '--send', 'r'),
                                         (SELLER, PID, ''), (SELLER, PID, 'bad\nrequest')]:
            native = self.native(response('x'))
            with self.assertRaises(ServiceError):
                read_product(native, seller, product, request)
            native._identity.assert_not_called()
            self.assertEqual(native.calls, [])

    def test_failure_has_no_fallback_or_retry(self):
        native = self.native(error=ServiceError('timeout', 'synthetic failure'))
        with self.assertRaises(ServiceError):
            read_product(native, SELLER, PID, 'r')
        self.assertEqual(len(native.calls), 1)

    def test_cancellation_before_and_after_submit_does_not_retry(self):
        event = threading.Event()
        event.set()
        native = self.native(response('x'))
        with self.assertRaises(ServiceError):
            read_product(native, SELLER, PID, 'r', cancel_event=event)
        self.assertEqual(native.calls, [])
        event.clear()
        native = self.native(response('x'), hook=event.set)
        with self.assertRaises(ServiceError) as caught:
            read_product(native, SELLER, PID, 'r', cancel_event=event)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(len(native.calls), 1)

    def test_unconfirmed_empty_or_failed_responses_do_not_become_completed(self):
        for reply in [{'data': 'unconfirmed'}, {'success': False, 'data': 'failed'},
                      {'success': True, 'data': '{}'}, response('')]:
            native = self.native(reply)
            with self.assertRaises(ServiceError):
                read_product(native, SELLER, PID, 'r')
            self.assertEqual(len(native.calls), 1)

    def test_large_reference_is_bounded_and_explicitly_marked(self):
        result = normalize_product_result(response('x' * 9000), PID)
        self.assertEqual(len(result['summary']), 8000)
        self.assertTrue(result['summary_truncated'])
        self.assertEqual(len(result['result_sha256']), 64)


if __name__ == '__main__':
    unittest.main()
