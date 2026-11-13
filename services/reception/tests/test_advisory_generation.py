"""Synthetic-only coverage of content isolation and recoverable generation."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reception_service.context import validate_reply
from reception_service.errors import ServiceError
from reception_service.integrations.analysis import validate_analysis
from reception_service.integrations.live import LiveIntegration
from test_integrations import analysis, context


class AdvisoryGenerationTests(unittest.TestCase):
    def test_invalid_fact_is_discarded_independently_and_human_is_preserved(self):
        raw = analysis('Our price is USD 99. Our team will verify the details.')
        raw.update(action='human', facts={
            'quantity': {'value': '999 pieces', 'evidence_ids': ['buyer-1']},
            'product': {'value': 'ceramic mugs', 'evidence_ids': ['buyer-1']},
        })
        result = validate_analysis(raw, context())
        self.assertEqual(result['action'], 'human')
        self.assertEqual(set(result['facts']), {'product'})
        self.assertTrue(result['fact_issues'])
        self.assertTrue(result['warnings'])
        self.assertFalse(result['conflicts'])
        checked = validate_reply(raw['reply'], raw['reply_sources'], context())
        self.assertTrue(checked['ready'])
        self.assertTrue(checked['warnings'])

    def test_team_confirmation_is_an_assistance_signal(self):
        result = validate_analysis(analysis("We're still checking with our team."), context())
        self.assertEqual(result['assistance_requests'][0]['kind'], 'verify_followup')
        self.assertEqual(result['action'], 'wait_quote')

    def test_optional_fields_are_repaired_and_payloads_are_rejected(self):
        result = validate_analysis({'action': 'ask', 'reply': 'Hello!'}, context())
        self.assertIn('facts', result['repairs'])
        for reply in ('{"reply": "hi"}', '{"reply":', '```json\n{}\n```',
                      '{"tool_calls": [{"name": "send"}]}'):
            with self.subTest(reply=reply):
                with self.assertRaises(ServiceError):
                    validate_analysis({'action': 'ask', 'reply': reply}, context())
                self.assertFalse(validate_reply(reply, ['buyer-1'], context())['ready'])

    def test_fenced_response_and_call_start(self):
        facade = LiveIntegration.__new__(LiveIntegration)
        facade._query_state = threading.local()
        facade.native = SimpleNamespace(data_dir='synthetic', cli='synthetic-cli', process=object())
        facade._visual_workspace_identity = None
        facade.resolve_identity = lambda seller: seller
        facade._agent_args = lambda: ['--agent-id', 'synthetic']
        calls = []
        runner = SimpleNamespace(run=lambda *a, **k: {'answer': '```JSON\n' + json.dumps(analysis()) + '\n```'},
                                 last_diagnostics={}, last_request=None)
        with patch('reception_service.integrations.live.DesktopQueryRunner', return_value=runner), \
             patch('reception_service.integrations.live.account_path', return_value=None):
            result = facade.generate(context(), 'synthetic', on_call_start=lambda: calls.append(1))
        self.assertEqual(calls, [1])
        self.assertIn('json_fence', result['repairs'])
        with self.assertRaises(ServiceError):
            facade.generate(context(), '', on_call_start=lambda: calls.append(2))
        self.assertEqual(calls, [1])

    def test_truncated_model_json_is_retryable_format_error(self):
        facade = LiveIntegration.__new__(LiveIntegration)
        facade._query_state = threading.local()
        facade.native = SimpleNamespace(data_dir='synthetic', cli='synthetic-cli', process=object())
        facade.resolve_identity = lambda seller: seller
        facade._agent_args = lambda: ['--agent-id', 'synthetic']
        runner = SimpleNamespace(run=lambda *a, **k: {'answer': '{"action":"ask","reply":"Hello'},
                                 last_diagnostics={})
        with patch('reception_service.integrations.live.DesktopQueryRunner', return_value=runner), \
             patch('reception_service.integrations.live.account_path', return_value=None):
            with self.assertRaises(ServiceError) as caught:
                facade.generate(context(), 'synthetic')
        self.assertEqual(caught.exception.code, 'answer_non_json')
