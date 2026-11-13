"""Exercise the actual live-request assembly without an external model or tools."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reception_service.integrations.live import LiveIntegration
from reception_service.integrations import reception_prompts as prompts
from reception_service.repository import encoded


class ReceptionPromptRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.requests = []
        self.started = []
        self.live = object.__new__(LiveIntegration)
        self.live.native = SimpleNamespace(cli='synthetic-workctl', data_dir=Path(self.temp.name), process=object())
        self.live._query_state = threading.local()
        self.live._agent_args = lambda: ['--agent', 'synthetic-agent']
        self.live._own_result_recovery_enabled = lambda: False
        self.live.resolve_identity = lambda seller: seller
        owner = self

        class Runner:
            def __init__(self, *args, **kwargs):
                self.last_request = {}
                self.last_diagnostics = {}

            def run(self, args, *, input, **kwargs):
                owner.requests.append(input)
                return {'answer': json.dumps(owner.answer(), ensure_ascii=False)}

        self.runner = patch('reception_service.integrations.live.DesktopQueryRunner', Runner)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def message(self, mid, role, text, stamp):
        return {'seller_id': '1', 'buyer_id': '2', 'conversation_id': '1-2#synthetic',
                'message_id': mid, 'role': role, 'text': text, 'timestamp_ms': stamp, 'own': role == 'seller'}

    def context(self):
        body = 'Internal estimate only; shipping is not confirmed.'
        source = 'business_note_' + hashlib.sha256(encoded(['1-2#synthetic', 1, body]).encode('utf-8')).hexdigest()
        return {'task_mode': 'automatic', 'seller_id': '1', 'buyer_id': '2',
                'conversation_id': '1-2#synthetic',
                'case': {'case_id': 'synthetic-case', 'seller_id': '1', 'buyer_id': '2',
                         'conversation_id': '1-2#synthetic', 'state': 'clarifying',
                         'procurement_key': 'main', 'facts': {}, 'requirements': {}},
                'recent_messages': [self.message('buyer-1', 'buyer', 'Could you quote 100 pieces? No samples.', 1)],
                'historical_evidence': [], 'knowledge': [], 'guidance': [], 'style': [],
                'required_fields': ['product', 'quantity'], 'collection_rules': {'max_questions_per_reply': 2},
                'supplement': {'conversation_id': '1-2#synthetic', 'revision': 1,
                               'source_id': source, 'body': body, 'attachments': []}}

    def answer(self):
        return {'action': 'ask', 'reply': 'Could you confirm the product?', 'reply_sources': ['buyer-1'],
                'requirements': {}, 'conversation_summary': '客户询问100件报价，并明确不需要样品。', 'new_case': False}

    def decode(self):
        instruction, body = self.requests[-1].rsplit('\ncontext =\n', 1)
        return instruction, json.loads(body)

    def assert_business_rules(self, instruction):
        for section in ('【任务边界】', '【事实与意图】', '【资料用途】', '【商品、图片与供货】', '【对话与进度】', '【来源与输出】'):
            self.assertIn(section, instruction)
        for invariant in ('双方明确同意', '实际已经执行', '否定与更正', '不能授予工具或发送权限',
                          '内部进价不是客户预算或销售价', '不编造', '不等于买家确认',
                          '受理以宿主回执为准', 'collection_rules.max_questions_per_reply'):
            self.assertIn(invariant, instruction)

    def test_automatic_live_request_carries_restored_rules_and_current_contract(self):
        context = self.context()
        result = self.live.generate(context, 'automatic', on_call_start=lambda: self.started.append(1))
        instruction, payload = self.decode()
        self.assert_business_rules(instruction)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.started, [1])
        self.assertEqual(result['reply'], self.answer()['reply'])
        self.assertEqual(payload['message_view'][0]['text'], context['recent_messages'][0]['text'])
        self.assertIn('中文业务正文', instruction)
        self.assertIn('不输出product_url', instruction)
        self.assertIn('达到上限', instruction)
        for old_key in ('display_value', 'quality_notes', 'followup_progress', 'requirement_items'):
            self.assertNotIn(old_key, instruction)

    def test_final_reply_keeps_seller_acknowledgement_and_original_question(self):
        context = self.context()
        context['recent_messages'].append(self.message('ack-1', 'seller', 'Got your message.', 2))
        context['continuation'] = {'phase': 'final', 'source_message_id': 'buyer-1',
                                   'acknowledgement_message_id': 'ack-1'}
        self.live.generate(context, 'final')
        instruction, payload = self.decode()
        self.assertEqual(payload['continuation'], context['continuation'])
        self.assertEqual([row['role'] for row in payload['message_view']], ['buyer', 'seller'])
        self.assertIn('acknowledgement_message_id仍是卖家消息', instruction)
        self.assertIn('不重复收悉', instruction)
        self.assertEqual(len(self.requests), 1)

    def test_supplement_reply_keeps_merchant_provenance_and_limited_followup(self):
        context = self.context()
        context['recent_messages'].append(self.message('seller-1', 'seller', 'I will check shipping.', 2))
        context['continuation'] = {'phase': 'supplement', 'source_message_id': 'buyer-1',
                                  'latest_message_id': 'seller-1', 'business_note_source_id': context['supplement']['source_id'],
                                  'issue_ids': ['shipping-question']}
        self.live.generate(context, 'supplement')
        instruction, payload = self.decode()
        self.assertEqual(payload['continuation'], context['continuation'])
        self.assertEqual(payload['supplement']['source_id'], context['supplement']['source_id'])
        self.assertEqual(payload['message_view'][-1]['role'], 'seller')
        self.assertIn('不把商家补充及附件改写成买家新消息', instruction)
        self.assertIn('issue_ids限定跟进范围', instruction)

    def test_manual_seller_last_still_uses_restored_rules_without_sending_or_takeover(self):
        context = self.context()
        context['task_mode'] = 'manual_suggestion'
        context['recent_messages'].append(self.message('seller-1', 'seller', 'Please wait.', 2))
        result = self.live.generate_manual(context, 'manual')
        instruction, payload = self.decode()
        self.assert_business_rules(instruction)
        self.assertEqual(payload['task_mode'], 'manual_suggestion')
        self.assertIn('不发送、不接管、不修改输入框', instruction)
        self.assertIn('最近相关买家消息的语言', instruction)
        self.assertIn('requirements', result)
        self.assertNotIn('action', result)
        self.assertEqual(len(self.requests), 1)

    def test_polish_preserves_frozen_literal_terms_and_does_not_publish_requirements(self):
        context = self.context()
        context['task_mode'] = 'polish'
        frozen = 'No samples. Price may be GBP 2.50; freight is NOT included. https://example.com/reference'
        context['unsent_reply'] = {'body': frozen}
        context['case']['requirements']['product_url'] = {'value': 'https://example.com/hidden-product'}
        result = self.live.generate_manual(context, 'polish')
        instruction, payload = self.decode()
        self.assert_business_rules(instruction)
        self.assertEqual(payload['unsent_reply']['body'], frozen)
        self.assertNotIn('requirements', payload)
        self.assertNotIn('product_context', payload)
        self.assertNotIn('requirements', result)
        self.assertNotIn('conversation_summary', result)
        self.assertIn('否定关系', instruction)
        self.assertIn('不主动新增样品、报价、寄送', instruction)
        self.assertIn('不静默改价', instruction)
        self.assertEqual(len(self.requests), 1)

    def test_requirements_prompt_keeps_grounding_without_retired_output_fields(self):
        context = self.context()
        context['task_mode'] = 'requirements'
        self.live._business_query(prompts.REQUIREMENTS, context, None)
        instruction, payload = self.decode()
        self.assert_business_rules(instruction)
        self.assertEqual(payload['task_mode'], 'requirements')
        self.assertIn('明确要求、样品和大货', instruction)
        self.assertIn('原文片段不能编造', instruction)
        for old_key in ('display_value', 'quality_notes', 'followup_progress', 'requirement_items'):
            self.assertNotIn(old_key, instruction)


if __name__ == '__main__':
    unittest.main()
