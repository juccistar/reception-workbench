"""Exercise the actual live-request assembly without an external model or tools."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
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

        class Router:
            def generate(self, purpose, instruction, payload, *, images, required_capabilities, cancel, on_call_start, deadline, _owned_input=None):
                owner.assertGreater(deadline, time.monotonic())
                owner.requests.append(instruction + '\ncontext =\n' + payload)
                if on_call_start:
                    on_call_start()
                return {'output': owner.answer(), 'model_snapshot': {}}

        self.live.settings = {}
        self.live.configure_model_router(Router())

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
                'reply_messages': [{'body': 'Could you confirm the product?', 'reply_sources': ['buyer-1']}],
                'conversation_summary': '客户询问100件报价，并明确不需要样品。'}

    def decode(self):
        instruction, body = self.requests[-1].rsplit('\ncontext =\n', 1)
        return instruction, json.loads(body)['context']

    def assert_business_rules(self, instruction):
        # Inspect the instruction that actually reached the configured router.
        # Wording is reviewed in Markdown; do not pin every sentence in this test.
        self.assertTrue(instruction.startswith(prompts.COMMON))
        self.assertEqual(instruction.count(prompts.COMMON.strip()), 1)
        self.assertNotIn(prompts.IMAGE_COMPAT.strip(), instruction)
        self.assertNotIn(prompts.IMAGE_NATIVE.strip(), instruction)

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
        self.assertIn(prompts.AUTOMATIC_TASK.strip(), instruction)
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
        self.assertIn(prompts.AUTOMATIC_TASK.strip(), instruction)
        self.assertIn('issue_ids限定跟进范围', instruction)

    def test_manual_seller_last_still_uses_restored_rules_without_sending_or_takeover(self):
        context = self.context()
        context['task_mode'] = 'manual_suggestion'
        context['recent_messages'].append(self.message('seller-1', 'seller', 'Please wait.', 2))
        result = self.live.generate_manual(context, 'manual')
        instruction, payload = self.decode()
        self.assert_business_rules(instruction)
        self.assertEqual(payload['task_mode'], 'manual_suggestion')
        self.assertIn(prompts.MANUAL_TASK.strip(), instruction)
        self.assertIn(prompts.REQUIREMENT_CHANGE_RULES.strip(), instruction)
        self.assertNotIn(prompts.POLISH_TASK.strip(), instruction)
        self.assertNotIn('requirements', result)
        self.assertNotIn('requirement_changes', result)
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
        self.assertIn(prompts.POLISH_TASK.strip(), instruction)
        self.assertNotIn(prompts.REQUIREMENT_CHANGE_RULES.strip(), instruction)
        self.assertNotIn('requirement_changes', result)
        self.assertEqual(len(self.requests), 1)

    def test_live_change_contract_accepts_target_alias_and_preserves_unchanged_omission(self):
        context = self.context()
        context['case_catalog'] = [{'case_ref': 'c1', 'case_id': 'synthetic-case', 'revision': 1,
                                    'requirements': {}, 'state': 'clarifying'}]
        self.answer = lambda: {'action': 'ask', 'reply_messages': [{'body': 'Noted.', 'reply_sources': ['buyer-1']}],
            'reply_case_refs': ['c1'], 'requirement_changes': [{'action': 'update', 'case_ref': 'c1',
                'trigger_evidence_ids': ['buyer-1'], 'set_fields': {'quantity': {'value': '100件',
                    'evidence_ids': ['buyer-1'], 'source_excerpts': {'buyer-1': '100 pieces'}}}}]}
        result = self.live.generate(context, 'explicit-change')
        self.assertEqual(result['requirement_changes'][0]['case_ref'], 'c1')
        self.assertEqual(result['reply_case_refs'], ['c1'])
        _, payload = self.decode()
        self.assertEqual(payload['case_catalog'][0]['case_ref'], 'c1')
        self.assertNotIn('case_id', payload['case_catalog'][0])

    def test_multiple_messages_skip_and_strict_new_contract(self):
        from reception_service.errors import ServiceError
        context = self.context()
        self.answer = lambda: {'action': 'ask', 'reply_messages': [
            {'body': 'Noted.', 'reply_sources': ['buyer-1']},
            {'body': 'Which product?', 'reply_sources': ['buyer-1']}], 'conversation_summary': '客户询价。'}
        result = self.live.generate(context, 'multi')
        self.assertEqual(result['reply'], 'Noted.\n\nWhich product?')
        self.assertEqual(len(result['reply_messages']), 2)
        with self.assertRaises(ServiceError):
            self.live.generate_manual(context, 'manual-multi')
        self.answer = lambda: {'action': 'ask', 'reply_messages': [
            {'body': 'a' * 600, 'reply_sources': ['buyer-1']},
            {'body': 'b' * 600, 'reply_sources': ['buyer-1']}], 'conversation_summary': ''}
        self.assertEqual(len(self.live.generate(context, 'budget')['reply']), 1202)
        with self.assertRaises(ServiceError):
            self.live.generate_manual(context, 'manual-budget')
        self.answer = lambda: {'action': 'ask', 'reply_messages': [
            {'body': 'Valid', 'reply_sources': ['buyer-1']},
            {'body': '{invalid}', 'reply_sources': ['buyer-1']}]}
        with self.assertRaises(ServiceError):
            self.live.generate(context, 'invalid-part')
        context['case_catalog'] = [{'case_ref': 'c1', 'case_id': 'synthetic-case', 'revision': 1,
                                    'requirements': {}, 'state': 'clarifying'}]
        change = {'action': 'update', 'case_ref': 'c1', 'trigger_evidence_ids': ['buyer-1'],
                  'set_fields': {'quantity': {'value': '100件', 'evidence_ids': ['buyer-1'],
                                              'source_excerpts': {'buyer-1': '100 pieces'}}}}
        self.answer = lambda: {'action': 'skip', 'reply_messages': [], 'conversation_summary': '客户致谢。',
                               'requirement_changes': [change]}
        skipped = self.live.generate(context, 'skip')
        self.assertEqual(skipped['reply_messages'], [])
        self.assertEqual(skipped['requirement_changes'], [change])
        self.answer = lambda: {'action': 'ask', 'reply': 'Old format', 'reply_sources': []}
        with self.assertRaises(ServiceError):
            self.live.generate(context, 'old')
        with self.assertRaises(ServiceError):
            self.live.generate_manual(context, 'old-manual')

    def test_whatsapp_multiple_messages_and_legacy_rejection(self):
        from reception_service.errors import ServiceError
        context = {'task_mode': 'whatsapp_reply', 'seller_id': '1', 'whatsapp_messages': []}
        self.answer = lambda: {'reply_messages': [{'body': 'Hello', 'reply_sources': ['m1']},
                                                  {'body': 'Thanks', 'reply_sources': ['m2']}],
                               'wa_summary': '客户询问。'}
        result = self.live.generate_whatsapp(context, 'wa')
        self.assertEqual(result['reply'], 'Hello\n\nThanks')
        self.assertEqual(result['reply_sources'], ['m1', 'm2'])
        self.answer = lambda: {'reply': 'old', 'reply_sources': [], 'wa_summary': '摘要'}
        with self.assertRaises(ServiceError):
            self.live.generate_whatsapp(context, 'wa-old')

    def test_merchant_future_followup_is_accepted_without_prior_task_or_result(self):
        body = 'Je vais demander au magasinier pour la couleur de la boîte et je vous réponds ensuite.'
        self.answer = lambda: {'action': 'ask',
            'reply_messages': [{'body': body, 'reply_sources': []}],
            'conversation_summary': '客户询问包装颜色，尚无仓库核实结果。'}
        for mode in ('automatic', 'manual_suggestion'):
            with self.subTest(mode=mode):
                self.requests.clear()
                context = self.context()
                context['task_mode'] = mode
                context['recent_messages'][0]['text'] = 'La boîte est rouge ?'
                call = self.live.generate if mode == 'automatic' else self.live.generate_manual
                result = call(context, 'future-' + mode)
                instruction, payload = self.decode()
                self.assertEqual(result['reply'], body)
                self.assertEqual(len(result['reply_messages']), 1)
                self.assertEqual(payload['message_view'][0]['text'], 'La boîte est rouge ?')
                envelope = json.loads(self.requests[-1].rsplit('\ncontext =\n', 1)[1])
                self.assertEqual(envelope['knowledge'], [])
                self.assertNotIn('assistance_task', payload)
                self.assert_business_rules(instruction)
                self.assertEqual(len(self.requests), 1)

    def test_polish_preserves_future_followup_without_external_confirmation(self):
        context = self.context()
        context['task_mode'] = 'polish'
        frozen = 'I will check with our warehouse and get back to you.'
        context['unsent_reply'] = {'body': frozen}
        self.answer = lambda: {'reply': frozen, 'reply_sources': []}
        result = self.live.generate_manual(context, 'polish-future')
        instruction, payload = self.decode()
        self.assertEqual(result['reply'], frozen)
        self.assertEqual(payload['unsent_reply']['body'], frozen)
        self.assertNotIn('conversation_summary', result)
        self.assertNotIn('requirement_changes', result)
        self.assertNotIn(prompts.REQUIREMENT_CHANGE_RULES.strip(), instruction)
        self.assertIn(prompts.POLISH_TASK.strip(), instruction)
        self.assertEqual(len(self.requests), 1)

    def test_requirements_prompt_keeps_grounding_without_retired_output_fields(self):
        context = self.context()
        context['task_mode'] = 'requirements'
        self.live._business_query(prompts.REQUIREMENTS, context, None)
        instruction, payload = self.decode()
        self.assert_business_rules(instruction)
        self.assertEqual(payload['task_mode'], 'requirements')
        self.assertIn(prompts.REQUIREMENTS_TASK.strip(), instruction)
        self.assertIn(prompts.REQUIREMENT_CHANGE_RULES.strip(), instruction)
        for old_key in ('display_value', 'quality_notes', 'followup_progress', 'requirement_items'):
            self.assertNotIn(old_key, instruction)


if __name__ == '__main__':
    unittest.main()
