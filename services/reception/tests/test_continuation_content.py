"""Offline synthetic checks for a verified acknowledgement continuation."""
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from reception_service.context import build_context
from reception_service.errors import ServiceError
from reception_service.integrations.analysis import prepare_context, validate_analysis
from reception_service.integrations.demo import DemoIntegration, natural_demo_analysis
from reception_service.integrations.live import LiveIntegration
from test_integrations import context, message, analysis, NOW


META = {'phase': 'final', 'source_message_id': 'buyer-2', 'acknowledgement_message_id': 'ack-1'}


def continued():
    value = context()
    value['recent_messages'].append(message('ack-1', 'We are checking.', 'seller', NOW + 2))
    value['continuation'] = dict(META)
    return value


class Repo:
    def __init__(self, rows):
        self.rows = rows

    def list(self, kind, **kwargs):
        return self.rows if kind == 'message' else []

    def get(self, kind, key, default=None):
        return {'revision': 1} if kind == 'conversation' else default


class ContinuationContentTests(unittest.TestCase):
    def test_build_fingerprint_and_history(self):
        fixture = continued()
        repo = Repo(fixture['recent_messages'])
        args = (repo, fixture['case'], {'required_fields': ['product', 'quantity']}, {})
        normal = build_context(*args)
        built = build_context(*args, continuation=dict(META))
        self.assertNotEqual(normal['fingerprint'], built['fingerprint'])
        self.assertEqual(built['recent_messages'], fixture['recent_messages'])
        self.assertEqual(built['continuation'], META)
        self.assertEqual(build_context(*args, continuation=dict(META))['fingerprint'], built['fingerprint'])
        repo.rows[-1] = dict(repo.rows[-1], text='Changed acknowledgement')
        self.assertNotEqual(build_context(*args, continuation=dict(META))['fingerprint'], built['fingerprint'])

    def test_invalid_metadata_and_changed_messages_fail_every_layer(self):
        variants = []
        for key, value in [('phase', 'ack'), ('source_message_id', 'buyer-1'),
                           ('acknowledgement_message_id', 'other')]:
            item = continued(); item['continuation'][key] = value; variants.append(item)
        item = continued(); item['continuation']['extra'] = True; variants.append(item)
        item = continued(); item['recent_messages'][-1]['own'] = False; variants.append(item)
        item = continued(); item['recent_messages'].append(message('buyer-3', 'New question', timestamp=NOW + 3)); variants.append(item)
        item = continued(); item['recent_messages'].append(message('seller-2', 'Other reply', 'seller', NOW + 3)); variants.append(item)
        item = continued(); item['recent_messages'][-1]['buyer_id'] = 'other-buyer'; variants.append(item)
        item = continued(); item['recent_messages'].insert(-1, message('seller-2', 'Other reply', 'seller', NOW + 1)); variants.append(item)
        for item in variants:
            with self.subTest(item=item):
                for run in (lambda: prepare_context(item), lambda: validate_analysis(analysis(), item),
                            lambda: natural_demo_analysis(item)):
                    with self.assertRaises(ServiceError) as caught:
                        run()
                    self.assertIn(caught.exception.code, ('invalid_continuation', 'identity_mismatch'))
                with self.assertRaises(ServiceError):
                    build_context(Repo(item['recent_messages']), item['case'],
                                  {'required_fields': ['product']}, {}, continuation=item['continuation'])

    def test_demo_and_live_use_buyer_and_keep_seller_history(self):
        item = continued()
        demo = DemoIntegration.__new__(DemoIntegration)
        demo.seller_id = item['case']['seller_id']
        calls = []
        result = demo.generate(item, 'request', on_call_start=lambda: calls.append('demo'))
        self.assertNotEqual(result['action'], 'skip')
        self.assertIn('buyer-2', result['reply_sources'])
        self.assertFalse(result['question_keys'])
        facade = LiveIntegration.__new__(LiveIntegration)
        facade.native = SimpleNamespace(data_dir='synthetic', cli='synthetic-cli', process=object())
        facade.resolve_identity = lambda seller: seller
        facade._agent_args = lambda: ['--agent-id', 'synthetic']
        captured = []
        def run(*args, **kwargs):
            captured.append(kwargs['input'])
            return {'answer': '```json\n' + json.dumps(analysis()) + '\n```'}
        runner = SimpleNamespace(run=run, last_diagnostics={}, last_request=None)
        with patch('reception_service.integrations.live.DesktopQueryRunner', return_value=runner), \
             patch('reception_service.integrations.live.account_path', return_value=None):
            result = facade.generate(item, 'request', on_call_start=lambda: calls.append('live'))
            self.assertIn('json_fence', result['repairs'])
            self.assertIn('"role": "seller"', captured[0])
            self.assertEqual(calls, ['demo', 'live'])
            normal = deepcopy(item); del normal['continuation']
            self.assertEqual(facade.generate(normal, 'request')['action'], 'skip')
            self.assertEqual(demo.generate(normal, 'request')['action'], 'skip')
            bad = deepcopy(item); bad['continuation']['source_message_id'] = 'buyer-1'
            with self.assertRaises(ServiceError):
                facade.generate(bad, 'request', on_call_start=lambda: calls.append('bad'))
            self.assertEqual(calls, ['demo', 'live'])

    def test_warnings_fact_isolation_and_source_repair(self):
        item = continued()
        raw = analysis('Our price is USD 99.')
        raw['reply_sources'] = []
        raw['facts']['product'] = {'value': 'wrong', 'evidence_ids': ['buyer-2']}
        result = validate_analysis(raw, item)
        self.assertEqual(result['facts'].keys(), {'quantity'})
        self.assertTrue(result['fact_issues'])
        self.assertTrue(result['warnings'])
        raw = {'action': 'ask', 'reply': 'I will check this.'}
        result = validate_analysis(raw, item)
        self.assertEqual(result['reply_sources'], ['buyer-2'])
        self.assertIn('facts', result['repairs'])


if __name__ == '__main__':
    unittest.main()
