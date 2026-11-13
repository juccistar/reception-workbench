import unittest
import tempfile
import time
from pathlib import Path

from reception_service.service import ReceptionService
from reception_service.errors import ServiceError

from reception_service.procurement import merge_fact, invalidate_case_sources


def fact(value, mid):
    return {'value': value, 'evidence_ids': [mid], 'status': 'confirmed'}


class ProcurementEvidenceTests(unittest.TestCase):
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
        self.service = ReceptionService(Path(tmp.name))
        self.addCleanup(self.service.close)
        self.service.inject_demo('demo-emma', 'I need 500 pcs of hats.')
        self.view = self.service.get_conversation('demo-emma')

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

    def test_review_candidate_and_reject_are_explicit(self):
        s, case = self.service, self.view['case']
        first = s._latest_buyer('demo-emma')
        s.inject_demo('demo-emma', '800 pcs')
        second = s._latest_buyer('demo-emma')
        case = s.get_conversation('demo-emma')['case']
        messages = {first['message_id']: first, second['message_id']: second}
        case['facts']['quantity'] = merge_fact(fact('500 pcs', first['message_id']), fact('800 pcs', second['message_id']), messages)
        s.repo.put('case', case['case_id'], case)
        candidates = case['facts']['quantity']['candidates']
        result = s.call('review_requirement', {'case_id': case['case_id'], 'field': 'quantity', 'decision': 'candidate',
                        'candidate_id': candidates[0]['candidate_id'], 'expected_revision': case['revision'],
                        'client_mutation_id': 'candidate-select'})
        self.assertEqual(result['facts']['quantity']['value'], '500 pcs')
        self.assertEqual(result['facts']['quantity']['review']['status'], 'confirmed')
        self.assertEqual(result['last_changes'][0]['kind'], 'reviewed')
        self.assertEqual(len(result['facts']['quantity']['candidates']), 2)

    def test_move_single_candidate_preserves_other_case(self):
        s = self.service
        first = s._latest_buyer('demo-emma')
        s.inject_demo('demo-emma', '800 pcs')
        second = s._latest_buyer('demo-emma')
        view = s.get_conversation('demo-emma')
        source = view['case']
        target = s.call('create_procurement_case', {'conversation_id': 'demo-emma',
                        'expected_revision': view['conversation']['revision'], 'client_mutation_id': 'move-create'})
        source['facts']['quantity'] = merge_fact(fact('500 pcs', first['message_id']), fact('800 pcs', second['message_id']),
                                                {first['message_id']: first, second['message_id']: second})
        s.repo.put('case', source['case_id'], source)
        selected = source['facts']['quantity']['candidates'][1]
        result = s.call('move_requirement', {'case_id': source['case_id'], 'target_case_id': target['case_id'],
                        'field': 'quantity', 'candidate_id': selected['candidate_id'],
                        'expected_revision': source['revision'], 'target_expected_revision': target['revision'],
                        'client_mutation_id': 'move-one'})
        self.assertEqual(result['target_case']['facts']['quantity']['status'], 'pending')
        self.assertEqual(result['target_case']['facts']['quantity']['value'], '800 pcs')
        self.assertEqual(result['case']['facts']['quantity']['value'], '500 pcs')

    def test_seller_quote_is_not_buyer_budget(self):
        from reception_service.context import validated_facts
        s = self.service
        s.inject_demo('demo-emma', 'Your quoted price is 500 USD.')
        source = s._latest_buyer('demo-emma')['message_id']
        context = s.get_context(self.view['case']['case_id'])
        result = validated_facts({'facts': {'budget': {'value': '500 USD', 'evidence_ids': [source]}}}, context)
        self.assertNotIn('budget', result)
        case = s.get_conversation('demo-emma')['case']
        with self.assertRaises(ServiceError):
            s.call('review_requirement', {'case_id': case['case_id'], 'field': 'budget', 'decision': 'edit',
                   'value': '500 USD', 'evidence_ids': [source], 'expected_revision': case['revision'],
                   'client_mutation_id': 'seller-quote'})

    def test_review_requires_buyer_evidence_and_preserves_manual_status(self):
        s, case = self.service, self.view['case']
        source = s._latest_buyer('demo-emma')['message_id']
        params = {'case_id': case['case_id'], 'field': 'quantity', 'decision': 'edit', 'value': '500 pcs',
                  'evidence_ids': [source], 'expected_revision': case['revision'], 'client_mutation_id': 'review-1'}
        updated = s.call('review_requirement', params)
        self.assertEqual(updated['facts']['quantity']['review']['status'], 'confirmed')
        self.assertEqual(s.call('review_requirement', params), updated)
        with self.assertRaises(ServiceError):
            s.call('review_requirement', {**params, 'value': '800 pcs', 'expected_revision': updated['revision'],
                                          'client_mutation_id': 'review-2'})
        self.assertEqual(s.repo.list('operation'), [])

    def test_correction_invalidates_every_case(self):
        s, case = self.service, self.view['case']
        msg = s._latest_buyer('demo-emma')
        second = s.call('create_procurement_case', {'conversation_id': 'demo-emma',
                        'expected_revision': self.view['conversation']['revision'], 'client_mutation_id': 'new-1'})
        for row in (case, second):
            row['facts']['quantity'] = fact('500 pcs', msg['message_id'])
            row['state'] = 'waiting_quote'
            s.repo.put('case', row['case_id'], row)
        info = {'conversation_id': 'demo-emma', 'buyer_id': msg['buyer_id']}
        s._ingest(info, [{**msg, 'text': 'I need 800 pcs of hats.'}])
        for row in s.get_conversation('demo-emma')['cases']:
            self.assertEqual(row['facts']['quantity']['status'], 'source_invalid')
            self.assertIn('quantity', row['missing'])
            self.assertTrue(row['quote_review_required'])

    def test_structured_cross_message_quantity_preserves_excerpts(self):
        from reception_service.integrations.analysis import validate_analysis
        s = self.service
        s.inject_demo('demo-emma', '500')
        s.inject_demo('demo-emma', 'pcs')
        messages = s._messages('demo-emma')
        number = next(m for m in messages if m['text'] == '500')
        unit = next(m for m in messages if m['text'] == 'pcs')
        ids = [number['message_id'], unit['message_id']]
        context = s.get_context(self.view['case']['case_id'])
        output = {'action': 'skip', 'reply': '', 'facts': {}, 'requirement_items': [
            {'field': 'quantity', 'label': '大货', 'scope': 'bulk', 'value': '500 pcs',
             'intent': 'requirement', 'evidence_ids': ids,
             'source_excerpts': {ids[0]: '500', ids[1]: 'pcs'}}]}
        item = validate_analysis(output, context)['requirement_items'][0]
        self.assertTrue(item['derived'])
        self.assertEqual(item['source_excerpts'], {ids[0]: '500', ids[1]: 'pcs'})
        self.assertEqual(item['scope'], 'bulk')

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

    def test_question_limits_filter_metadata_not_reply(self):
        from reception_service.integrations.analysis import validate_analysis
        s = self.service
        case = s.get_conversation('demo-emma')['case']
        case['questions'] = {'quantity': 2}
        s.repo.put('case', case['case_id'], case)
        s.settings.update(max_questions_per_reply=1, max_questions_per_field=3)
        context = s.get_context(case['case_id'])
        source = s._latest_buyer('demo-emma')['message_id']
        raw = {'action': 'ask', 'reply': 'What quantity and destination?', 'facts': {},
               'question_keys': ['quantity', 'destination'], 'reply_sources': [source]}
        result = validate_analysis(raw, context)
        self.assertEqual(result['question_keys'], ['quantity'])
        self.assertEqual(result['reply'], raw['reply'])
        self.assertTrue(any(w['code'] == 'question_repeated' for w in result['warnings']))
        case['questions']['quantity'] = 3
        context['case'] = case
        self.assertEqual(validate_analysis(raw, context)['question_keys'], ['destination'])

    def test_visual_item_stays_unconfirmed(self):
        from unittest.mock import patch
        from reception_service.integrations.analysis import validate_analysis
        s = self.service
        context = s.get_context(self.view['case']['case_id'])
        messages = context['historical_evidence'] + context['recent_messages']
        recent = context['recent_messages']
        context['photo_context']['read_images'] = [{'image_id': 'photo-1', 'message_id': messages[-1]['message_id'],
                                                     'source_version': 'v1', 'role': 'seller'}]
        raw = {'action': 'skip', 'reply': '', 'facts': {}, 'requirement_items': [
            {'field': 'specifications', 'value': 'blue', 'intent': 'requirement', 'evidence_ids': ['photo-1']}]}
        with patch('reception_service.integrations.analysis.prepare_context', return_value=(context, messages, recent)):
            item = validate_analysis(raw, context)['requirement_items'][0]
        self.assertTrue(item['visual'])
        self.assertEqual(item['source_message_ids'], [messages[-1]['message_id']])
        case = {'facts': {}, 'state': 'clarifying', 'requirement_items': [dict(item, status='unconfirmed')]}
        self.assertTrue(invalidate_case_sources(case, {messages[-1]['message_id']}))
        self.assertEqual(case['requirement_items'][0]['status'], 'source_invalid')

    def test_sample_and_bulk_remain_distinct(self):
        from reception_service.integrations.analysis import validate_analysis
        s = self.service
        s.inject_demo('demo-emma', 'Sample 10 pcs, bulk 500 pcs.')
        source = s._latest_buyer('demo-emma')['message_id']
        context = s.get_context(self.view['case']['case_id'])
        items = [{'field': 'quantity', 'label': label, 'scope': scope, 'value': value,
                  'intent': 'requirement', 'evidence_ids': [source]} for label, scope, value in
                 [('样品', 'sample', '10 pcs'), ('大货', 'bulk', '500 pcs')]]
        checked = validate_analysis({'action': 'skip', 'reply': '', 'facts': {}, 'requirement_items': items}, context)
        self.assertEqual([(item['scope'], item['value']) for item in checked['requirement_items']],
                         [('sample', '10 pcs'), ('bulk', '500 pcs')])

    def test_manual_organize_creates_candidate_then_applies(self):
        s, case = self.service, self.view['case']
        job_id = s.call('organize_requirements', {'conversation_id': 'demo-emma', 'case_id': case['case_id'],
                                                  'client_request_id': 'org-1'})['job_id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = s.repo.get('job', job_id)
            if job['status'] not in ('queued', 'running'):
                break
            time.sleep(.01)
        self.assertEqual(job['status'], 'succeeded', job)
        proposal = s.get_conversation('demo-emma')['requirement_proposals'][0]
        self.assertEqual(proposal['status'], 'pending')
        result = s.call('apply_requirement_proposal', {'proposal_id': proposal['proposal_id'],
                         'expected_revision': case['revision'], 'client_mutation_id': 'apply-1'})
        self.assertEqual(result['revision'], case['revision'] + 1)
        self.assertEqual(s.repo.list('operation'), [])
        self.assertFalse(s.get_conversation('demo-emma')['conversation']['human_owned'])


if __name__ == '__main__':
    unittest.main()
