"""Procurement evidence preservation and the current AI-authored requirement contract."""
import copy
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test_support'))
from reception_test_support.service import create_synthetic_service
from reception_service.errors import ServiceError
from reception_service.procurement import merge_fact, invalidate_case_sources
from reception_service.requirement_changes import validate_changes


def fact(value, mid):
    return {'value': value, 'evidence_ids': [mid], 'status': 'confirmed'}


class ProcurementEvidenceTests(unittest.TestCase):
    # Legacy persisted facts remain readable; these helpers do not author ai-v1.
    def test_conflict_survives_repeated_extraction(self):
        messages = {'a': {'text': '500 pcs'}, 'b': {'text': '800 pcs'}}
        conflict = merge_fact(fact('500 pcs', 'a'), fact('800 pcs', 'b'), messages)
        self.assertEqual(conflict['status'], 'conflict')
        self.assertEqual([c['value'] for c in conflict['candidates']], ['500 pcs', '800 pcs'])
        again = merge_fact(conflict, fact('800 pcs', 'b'), messages)
        self.assertEqual(again['status'], 'conflict')
        self.assertEqual(len(again['candidates']), 2)
        self.assertEqual(again['candidates'][0]['source_excerpts']['a'], '500 pcs')

    def test_explicit_correction_and_manual_review(self):
        messages = {'a': {'text': '500 pcs'}, 'b': {'text': 'change 500 pcs to 800 pcs instead'}}
        result = merge_fact(fact('500 pcs', 'a'), fact('800 pcs', 'b'), messages)
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['value'], '800 pcs')
        previous = dict(fact('500 pcs', 'a'), review={'status': 'confirmed'})
        result = merge_fact(previous, fact('500 pcs', 'a'), messages)
        self.assertEqual(result['review']['status'], 'confirmed')

    def test_source_invalidation_preserves_quote_history(self):
        case = {'facts': {'quantity': fact('500 pcs', 'a')}, 'state': 'waiting_quote'}
        self.assertTrue(invalidate_case_sources(case, {'a'}))
        self.assertEqual(case['facts']['quantity']['status'], 'source_invalid')
        self.assertTrue(case['quote_review_required'])


class ProcurementServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = create_synthetic_service(Path(tmp.name))
        self.addCleanup(self.service.close)
        self.service.inject_demo('demo-emma', 'I need 500 pcs of hats.')
        self.view = self.service.get_conversation('demo-emma')

    def update(self, field, value, messages, excerpts=None):
        context = self.service.get_context(self.view['case']['case_id'])
        change = {'action': 'update', 'case_ref': context['current_case_ref'],
                  'trigger_evidence_ids': messages,
                  'set_fields': {field: {'value': value, 'evidence_ids': messages,
                                        'source_excerpts': excerpts or {}}}}
        return change, context

    def test_category_cas_and_idempotency_without_takeover(self):
        s, case = self.service, self.view['case']
        params = dict(case_id=case['case_id'], category_label='帽子', expected_revision=case['revision'], client_mutation_id='cat-1')
        result = s.call('set_case_category', params)
        self.assertEqual(s.call('set_case_category', params), result)
        self.assertEqual(result['category_label'], '帽子')
        with self.assertRaises(ServiceError):
            s.call('set_case_category', {**params, 'client_mutation_id': 'cat-2'})
        self.assertFalse(s.get_conversation('demo-emma')['conversation']['human_owned'])
        self.assertEqual(s.repo.list('operation'), [])

    def test_retired_review_move_and_proposal_rpcs_preserve_existing_evidence(self):
        s = self.service
        case = s.repo.get('case', self.view['case']['case_id'])
        mid = s._latest_buyer('demo-emma')['message_id']
        case['facts']['quantity'] = dict(fact('500 pcs', mid), review={'status': 'confirmed'})
        s.repo.put('case', case['case_id'], case, history=True)
        receipt = {'operation_id': 'synthetic-unknown', 'conversation_id': 'demo-emma', 'status': 'unknown'}
        s.repo.put('operation', receipt['operation_id'], receipt)
        before = {kind: s.repo.list(kind) for kind in ('case', 'conversation', 'operation', 'job')}
        params = {'case_id': case['case_id'], 'field': 'quantity', 'decision': 'edit', 'value': '800 pcs',
                  'evidence_ids': [mid], 'expected_revision': case['revision'], 'client_mutation_id': 'retired'}
        for method in ('review_requirement', 'move_requirement', 'organize_requirements', 'apply_requirement_proposal'):
            with self.subTest(method=method), self.assertRaises(ServiceError) as caught:
                s.call(method, params)
            self.assertEqual(caught.exception.code, 'unknown_method')
            self.assertEqual({kind: s.repo.list(kind) for kind in before}, before)

    def test_ai_requirement_change_requires_current_buyer_evidence(self):
        s = self.service
        buyer = s._latest_buyer('demo-emma')
        seller = {**buyer, 'message_id': 'synthetic-seller', 'role': 'seller', 'own': True,
                  'text': 'Our suggested quantity is 800 pcs.', 'timestamp_ms': buyer['timestamp_ms'] + 1}
        s._ingest({'conversation_id': 'demo-emma', 'buyer_id': buyer['buyer_id']}, [seller])
        before = s.repo.get('case', self.view['case']['case_id'])
        change, context = self.update('quantity', '800件', [seller['message_id']])
        with self.assertRaises(ServiceError) as error:
            validate_changes({'requirement_changes': [change]}, context)
        self.assertEqual(error.exception.code, 'invalid_requirement_change')
        self.assertEqual(s.repo.get('case', before['case_id']), before)
        change['trigger_evidence_ids'] = change['set_fields']['quantity']['evidence_ids'] = ['foreign-message']
        with self.assertRaises(ServiceError):
            validate_changes({'requirement_changes': [change]}, context)
        self.assertEqual(s.repo.list('operation'), [])

    def test_seller_quote_is_not_buyer_budget(self):
        from reception_service.context import validated_facts
        s = self.service
        s.inject_demo('demo-emma', 'Your quoted price is 500 USD.')
        source = s._latest_buyer('demo-emma')['message_id']
        context = s.get_context(self.view['case']['case_id'])
        result = validated_facts({'facts': {'budget': {'value': '500 USD', 'evidence_ids': [source]}}}, context)
        self.assertNotIn('budget', result)

    def test_source_edit_invalidates_formal_fields_in_every_case_without_rewriting_other_evidence(self):
        s, case = self.service, self.view['case']
        msg = s._latest_buyer('demo-emma')
        second = s.call('create_procurement_case', {'conversation_id': 'demo-emma',
                        'expected_revision': self.view['conversation']['revision'], 'client_mutation_id': 'new-1'})
        for row in (case, second):
            row.update(requirements_format='ai-v1', requirements_revision=4, requirements={
                'quantity': {'value': '500件', 'evidence_ids': [msg['message_id']]},
                'destination': {'value': '西班牙', 'evidence_ids': ['unchanged-source']}})
            s.repo.put('case', row['case_id'], row)
        s._ingest({'conversation_id': 'demo-emma', 'buyer_id': msg['buyer_id']}, [{**msg, 'text': 'I need 800 pcs of hats.'}])
        for row in (case, second):
            stored = s.repo.get('case', row['case_id'])
            self.assertEqual(stored['requirements']['quantity'], {'value': None, 'evidence_ids': []})
            self.assertEqual(stored['requirements']['destination'], row['requirements']['destination'])
            self.assertEqual(stored['requirements_revision'], 5)

    def test_structured_cross_message_quantity_preserves_excerpts(self):
        s = self.service
        s.inject_demo('demo-emma', '500')
        number = s._latest_buyer('demo-emma')
        s.inject_demo('demo-emma', 'pcs')
        unit = s._latest_buyer('demo-emma')
        ids = [number['message_id'], unit['message_id']]
        excerpts = {ids[0]: '500', ids[1]: 'pcs'}
        change, context = self.update('quantity', '500件', ids, excerpts)
        changes, _ = validate_changes({'requirement_changes': [change]}, context)
        item = changes[0]['set_fields']['quantity']
        self.assertEqual(item['value'], '500件')
        self.assertEqual(item['source_excerpts'], excerpts)
        self.assertEqual(set(item['evidence_versions']), set(ids))
        self.assertNotIn('display_value', item)
        self.assertNotIn('review', item)

    def test_collection_configuration_and_three_model_projections(self):
        from reception_service.context import missing_fields_for_case
        from reception_service.model_payload import project_model_payload
        s = self.service
        s.settings.update(required_fields=['product', 'destination'], required_fields_by_category={'帽子': ['quantity']},
                          max_questions_per_reply=1, max_questions_per_field=3)
        case = s.get_conversation('demo-emma')['case']
        case['category_label'] = '帽子'
        self.assertEqual(missing_fields_for_case({}, s.settings, case), ['product', 'quantity'])
        self.assertEqual(missing_fields_for_case({}, s.settings, {}), ['product', 'destination'])
        s.repo.put('case', case['case_id'], case)
        contexts = [s.get_context(case['case_id']), s._manual_context('demo-emma', case['case_id'])[0],
                    s._manual_context('demo-emma', case['case_id'], 'hello', 1)[0]]
        for context in contexts:
            rules = project_model_payload(context)['collection_rules']
            self.assertEqual(rules['required_fields'], ['product', 'quantity'])
            self.assertEqual(rules['max_questions_per_reply'], 1)
            self.assertEqual(rules['max_questions_per_field'], 3)

    def test_retired_question_and_warning_metadata_does_not_rewrite_reply(self):
        from reception_service.integrations.analysis import validate_analysis
        s = self.service
        s.settings.update(max_questions_per_reply=1, max_questions_per_field=3)
        context = s.get_context(self.view['case']['case_id'])
        source = s._latest_buyer('demo-emma')['message_id']
        raw = {'action': 'ask', 'reply_messages': [{'body': 'What quantity and destination?', 'reply_sources': [source]}],
               'question_keys': ['quantity', 'destination'], 'warnings': [{'code': 'old-warning'}]}
        result = validate_analysis(raw, context)
        self.assertEqual(result['reply'], raw['reply_messages'][0]['body'])
        self.assertEqual(result['question_keys'], [])
        self.assertNotIn('warnings', result)
        self.assertNotIn('requirement_changes', result)

    def test_unread_image_cannot_become_a_formal_requirement_image(self):
        s = self.service
        source = s._latest_buyer('demo-emma')['message_id']
        change, context = self.update('product_images', ['unread-image'], [source])
        before = copy.deepcopy(context)
        with self.assertRaises(ServiceError) as error:
            validate_changes({'requirement_changes': [change]}, context)
        self.assertEqual(error.exception.code, 'invalid_requirement_change')
        self.assertEqual(context, before)

    def test_sample_and_bulk_scopes_remain_in_chinese_business_value(self):
        s = self.service
        s.inject_demo('demo-emma', 'Sample 10 pcs, bulk 500 pcs.')
        source = s._latest_buyer('demo-emma')['message_id']
        change, context = self.update('quantity', '样品10件；大货500件', [source],
                                      {source: 'Sample 10 pcs, bulk 500 pcs.'})
        changes, _ = validate_changes({'requirement_changes': [change]}, context)
        self.assertEqual(changes[0]['set_fields']['quantity']['value'], '样品10件；大货500件')
        self.assertEqual(changes[0]['set_fields']['quantity']['source_excerpts'],
                         {source: 'Sample 10 pcs, bulk 500 pcs.'})


if __name__ == '__main__':
    unittest.main()
