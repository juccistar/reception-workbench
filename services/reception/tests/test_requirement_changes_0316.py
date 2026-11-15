"""Real local service transactions for optional, multi-case AI requirement changes."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test_support'))
from reception_test_support.service import create_synthetic_service
from reception_service.errors import ServiceError
from reception_service.model_payload import project_model_payload
from reception_service.requirement_changes import requirement_revision


class RequirementChangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = create_synthetic_service(Path(self.temp.name))
        self.owner = {'conversation_id': 'multi-synthetic', 'buyer_id': 'synthetic-buyer',
                      'seller_id': 'demo-seller', 'customer_name': 'Synthetic multi-case'}
        self.messages = []
        self.message('m1', 'I need blue glasses, 500 pairs, gift boxes.')
        self.initial_case = self.service.get_conversation(self.owner['conversation_id'])['case']['case_id']

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def message(self, mid, text):
        row = {**self.owner, 'message_id': mid, 'role': 'buyer', 'text': text,
               'timestamp_ms': len(self.messages) * 1000 + 1000, 'attachments': []}
        self.messages.append(row)
        self.service._ingest(self.owner, [row])
        return row

    def context(self):
        return self.service.get_context(self.service.repo.get('conversation', self.owner['conversation_id'])['case_id'])

    def field(self, value, mid, excerpt):
        return {'value': value, 'evidence_ids': [mid], 'source_excerpts': {mid: excerpt}}

    def create(self, ref='new:1', mid='m1', name='蓝色眼镜', excerpt='blue glasses'):
        return {'action': 'create', 'case_ref': ref, 'trigger_evidence_ids': [mid],
                'set_fields': {'product': self.field(name, mid, excerpt)}}

    def update(self, ref, key, value, mid, excerpt):
        return {'action': 'update', 'case_ref': ref, 'trigger_evidence_ids': [mid],
                'set_fields': {key: self.field(value, mid, excerpt)}}

    def apply(self, changes=None, refs=None, request='apply', context=None):
        output = {}
        if changes is not None:
            output['requirement_changes'] = changes
        if refs is not None:
            output['reply_case_refs'] = refs
        context = context or self.context()
        with self.service._lock, self.service.repo.transaction():
            conversation = self.service.repo.get('conversation', self.owner['conversation_id'])
            case = self.service.repo.get('case', conversation['case_id'])
            return self.service._apply_requirement_output(conversation, case, output, context, request)

    def seed(self):
        change = self.create()
        change['set_fields'].update(quantity=self.field('500副', 'm1', '500 pairs'),
                                    packaging=self.field('礼盒', 'm1', 'gift boxes'))
        return self.apply([change])

    def case(self, identifier=None):
        return self.service.repo.get('case', identifier or self.initial_case)

    def test_create_reuses_empty_shell_and_omitted_or_empty_result_preserves_everything(self):
        result = self.seed()
        self.assertEqual(result['reply_case_ids'], [self.initial_case])
        before = self.case()
        self.assertEqual(len(before['requirements']), 13)
        self.assertEqual(before['requirements_revision'], 1)
        self.assertEqual(len(self.service.repo.list('case', conversation_id=self.owner['conversation_id'])), 1)
        self.apply(request='unchanged')
        self.apply([], request='empty')
        self.assertEqual(self.case(), before)

    def test_null_does_not_clear_and_exact_same_patch_is_a_noop(self):
        self.seed()
        before = self.case()
        self.apply([self.update('c1', 'quantity', None, 'm1', '500 pairs')], request='null')
        result = self.apply([self.update('c1', 'quantity', '500副', 'm1', '500 pairs')], request='same')
        self.assertEqual(result['changed_case_ids'], [])
        self.assertEqual(self.case(), before)

    def test_update_one_field_preserves_others_and_explicit_clear_needs_evidence(self):
        self.seed()
        self.message('m2', 'Change the glasses quantity to 800 pairs. Cancel the gift boxes.')
        before = self.case()['requirements']
        change = self.update('c1', 'quantity', '800副', 'm2', '800 pairs')
        change['clear_fields'] = {'packaging': {'evidence_ids': ['m2'], 'source_excerpts': {'m2': 'Cancel the gift boxes.'}}}
        self.apply([change], request='updated')
        after = self.case()['requirements']
        self.assertEqual(after['quantity']['value'], '800副')
        self.assertIsNone(after['packaging']['value'])
        self.assertEqual(after['product'], before['product'])
        self.assertEqual(self.case()['requirements_revision'], 2)

    def test_bad_clear_or_foreign_reference_is_atomic(self):
        self.seed()
        before = self.case()
        self.message('m2', 'Change to 800 pairs.')
        valid = self.update('c1', 'quantity', '800副', 'm2', '800 pairs')
        bad = self.update('c999', 'budget', '100美元', 'm2', '800 pairs')
        with self.assertRaises(ServiceError):
            self.apply([valid, bad], request='invalid-batch')
        self.assertEqual(self.case()['requirements'], before['requirements'])
        clear = {'action': 'update', 'case_ref': 'c1', 'trigger_evidence_ids': ['m2'],
                 'clear_fields': {'packaging': {'evidence_ids': ['m2']}}}
        with self.assertRaises(ServiceError):
            self.apply([clear], request='invalid-clear')
        self.assertEqual(self.case()['requirements'], before['requirements'])

    def test_update_old_a_and_create_b_in_same_transaction_then_route_without_rewriting(self):
        self.seed()
        self.message('m2', 'Glasses: 800 pairs. Also I need red caps.')
        result = self.apply([self.update('c1', 'quantity', '800副', 'm2', '800 pairs'),
                             self.create(mid='m2', name='红色帽子', excerpt='red caps')],
                            refs=['c1', 'new:1'], request='multi')
        second = result['mapping']['new:1']
        self.assertNotEqual(second, self.initial_case)
        self.assertEqual(self.case()['requirements']['quantity']['value'], '800副')
        self.assertEqual(self.case(second)['requirements']['product']['value'], '红色帽子')
        self.apply(refs=['c2'], request='route-b')
        before = copy.deepcopy(self.case())
        self.apply(refs=['c1'], request='route-a')
        self.assertEqual(self.service.repo.get('conversation', self.owner['conversation_id'])['case_id'], self.initial_case)
        self.assertEqual(self.case(), before)

    def test_new_order_same_product_is_distinct_but_repeated_origin_does_not_duplicate_or_revert(self):
        self.seed()
        self.message('m2', 'Another separate order: blue glasses, 900 pairs.')
        result = self.apply([self.create(mid='m2')], request='second-order')
        second = result['reply_case_ids'][0]
        self.assertNotEqual(second, self.initial_case)
        self.message('m3', 'Change the second order to 950 pairs.')
        self.apply([self.update('c2', 'quantity', '950副', 'm3', '950 pairs')], request='second-update')
        repeated = self.create(mid='m2')
        repeated['set_fields']['quantity'] = self.field('900副', 'm2', '900 pairs')
        self.apply([repeated], request='regenerate-old-origin')
        self.assertEqual(len(self.service.repo.list('case', conversation_id=self.owner['conversation_id'])), 2)
        self.assertEqual(self.case(second)['requirements']['quantity']['value'], '950副')

    def test_same_request_replay_uses_original_mapping_and_different_payload_is_rejected(self):
        change = self.create()
        first = self.apply([change], request='once')
        self.assertEqual(self.apply([change], request='once'), first)
        with self.assertRaises(ServiceError):
            self.apply([], request='once')
        self.assertEqual(len(self.service.repo.list('case', conversation_id=self.owner['conversation_id'])), 1)

    def test_stale_catalogue_rejected_before_mutation(self):
        self.seed()
        context = self.context()
        current = self.case()
        current['revision'] += 1
        self.service.repo.put('case', current['case_id'], current)
        with self.assertRaises(ServiceError) as caught:
            self.apply([self.update('c1', 'quantity', '500副', 'm1', '500 pairs')], request='stale', context=context)
        self.assertEqual(caught.exception.code, 'source_changed')

    def test_old_contract_and_empty_create_are_rejected(self):
        with self.service._lock, self.service.repo.transaction(), self.assertRaises(ServiceError):
            self.service._apply_requirement_output(self.service.repo.get('conversation', self.owner['conversation_id']),
                self.case(), {'requirements': {}}, self.context(), 'legacy')
        empty = {'action': 'create', 'case_ref': 'new:1', 'trigger_evidence_ids': ['m1'], 'set_fields': {}}
        with self.assertRaises(ServiceError):
            self.apply([empty], request='empty-create')
        self.assertNotIn('requirements', self.case())

    def test_metadata_change_keeps_requirements_and_text_change_invalidates_only_its_sources(self):
        self.seed()
        self.message('m2', 'Change to 800 pairs.')
        self.apply([self.update('c1', 'quantity', '800副', 'm2', '800 pairs')], request='quantity')
        before = copy.deepcopy(self.case())
        row = {**self.messages[0], 'timestamp_ms': 500}
        self.service._ingest(self.owner, [row])
        self.assertEqual(self.case()['requirements'], before['requirements'])
        self.assertEqual(requirement_revision(self.case()), requirement_revision(before))
        row['text'] = 'I need green glasses.'
        self.service._ingest(self.owner, [row])
        self.assertIsNone(self.case()['requirements']['product']['value'])
        self.assertEqual(self.case()['requirements']['quantity']['value'], '800副')
        self.assertEqual(requirement_revision(self.case()), requirement_revision(before) + 1)

    def test_catalogue_preserves_typed_product_id_and_does_not_expose_derived_links(self):
        self.seed()
        change = self.update('c1', 'product_id', '定制-1234567', 'm1', 'blue glasses')
        self.apply([change], request='custom-id')
        payload = project_model_payload(self.context())
        self.assertEqual(payload['case_catalog'][0]['requirements']['product_id']['value'], '定制-1234567')
        self.assertEqual(payload['requirements']['product']['value'], '蓝色眼镜')
        self.assertNotIn('product_url', payload['case_catalog'][0]['requirements'])
        count = len(self.service.repo.list('procurement_task'))
        binding = self.service.repo.get('requirement_product_binding', self.initial_case)
        self.apply(request='no-procurement-repeat')
        self.assertEqual(len(self.service.repo.list('procurement_task')), count)
        self.assertEqual(self.service.repo.get('requirement_product_binding', self.initial_case), binding)

    def test_reply_sources_cannot_borrow_another_case_offer(self):
        from reception_service.integrations.generation_metadata import checked_metadata
        context = self.context()
        context['case_catalog'] = [
            {'case_ref': 'c1', 'supplier_context': {'usable': True, 'customer_wording': 'For glasses', 'source_id': 'offer-a'}},
            {'case_ref': 'c2', 'supplier_context': {'usable': True, 'customer_wording': 'For caps', 'source_id': 'offer-b'}}]
        output = {'reply_case_refs': ['c1'], 'reply_sources': ['offer-a', 'offer-b']}
        self.assertEqual(checked_metadata(output, context)['reply_sources'], ['offer-a'])

    def wait(self, ack):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.repo.get('job', ack['job_id'])
            if job['status'] not in ('queued', 'running'):
                self.assertEqual(job['status'], 'succeeded', job)
                return job
            threading.Event().wait(.01)
        self.fail('local task did not finish')

    def test_real_manual_job_saves_only_explicit_changes_before_adoption(self):
        self.seed()
        self.message('m2', 'Change to 800 pairs.')
        output = {'reply_messages': [{'body': 'Noted.', 'reply_sources': ['m2']}],
                  'requirement_changes': [self.update('c1', 'quantity', '800副', 'm2', '800 pairs')]}
        self.service.integrations.generate_manual = lambda context, request_id, **options: copy.deepcopy(output)
        self.wait(self.service.call('generate_manual_suggestion', {'conversation_id': self.owner['conversation_id'],
            'case_id': self.initial_case, 'client_request_id': 'manual-1'}))
        self.assertEqual(self.case()['requirements']['quantity']['value'], '800副')
        before = self.case()
        output.pop('requirement_changes')
        self.wait(self.service.call('generate_manual_suggestion', {'conversation_id': self.owner['conversation_id'],
            'case_id': self.initial_case, 'client_request_id': 'manual-2'}))
        self.assertEqual(self.case(), before)
        self.assertFalse(self.service.repo.get('conversation', self.owner['conversation_id'])['human_owned'])

    def test_real_automatic_job_tracks_all_related_case_versions(self):
        self.seed()
        self.message('m2', 'Also I need red caps.')
        result = self.apply([self.create(mid='m2', name='红色帽子', excerpt='red caps')], request='caps')
        second = result['reply_case_ids'][0]
        self.service.integrations.generate = lambda context, request_id, **options: {
            'action': 'ask', 'reply': 'Noted.', 'reply_sources': ['m2'], 'reply_case_refs': ['c1', 'c2']}
        before = requirement_revision(self.case(second))
        job = self.wait(self.service.call('generate_draft', {'case_id': second, 'expected_case_revision': self.case(second)['revision']}))
        draft = self.service.repo.get('draft', job['result']['draft_id'])
        self.assertEqual(set(draft['reply_case_ids']), {self.initial_case, second})
        self.assertEqual(requirement_revision(self.case(second)), before)
        self.assertTrue(self.service._related_cases_current(draft))
        old = self.case()
        old['revision'] += 1
        self.service.repo.put('case', old['case_id'], old)
        self.assertFalse(self.service._related_cases_current(draft))
        with self.assertRaises(ServiceError) as caught:
            self.service._validate_send(draft['draft_id'], draft['revision'], manual=True)
        self.assertEqual(caught.exception.code, 'stale_draft')

    def test_manual_prepares_images_from_a_referenced_older_case(self):
        from unittest.mock import patch
        self.seed()
        self.message('m2', 'Also I need red caps.')
        second = self.apply([self.create(mid='m2', name='红色帽子', excerpt='red caps')], request='caps')['reply_case_ids'][0]
        prepared = []
        self.service.integrations.generate_manual = lambda context, request_id, **options: {'reply_messages': [{'body': 'Noted.', 'reply_sources': ['m2']}]}
        self.service._prepare_direct_photos = lambda cid, job, epoch, facade: (prepared.append(cid) or (lambda: None))
        def sources(repo, case, messages):
            return [{'message_id': 'm1'}] if case and case['case_id'] == self.initial_case else []
        with patch('reception_service.conversation_assistance.get_product_source_images', sources):
            self.wait(self.service.call('generate_manual_suggestion', {'conversation_id': self.owner['conversation_id'],
                'case_id': second, 'client_request_id': 'older-case-image'}))
        self.assertEqual(prepared, [self.owner['conversation_id']])

    def test_automatic_prepares_images_from_a_referenced_older_case(self):
        from unittest.mock import patch
        self.seed()
        self.message('m2', 'Also I need red caps.')
        second = self.apply([self.create(mid='m2', name='红色帽子', excerpt='red caps')], request='caps')['reply_case_ids'][0]
        prepared = []
        self.service.integrations.generate = lambda context, request_id, **options: {'action': 'ask', 'reply': 'Noted.', 'reply_sources': ['m2']}
        self.service._prepare_direct_photos = lambda cid, job, epoch, facade: (prepared.append(cid) or (lambda: None))
        def sources(repo, case, messages):
            return [{'message_id': 'm1'}] if case and case['case_id'] == self.initial_case else []
        with patch('reception_service.service.get_product_source_images', sources):
            self.wait(self.service.call('generate_draft', {'case_id': second, 'expected_case_revision': self.case(second)['revision']}))
        self.assertEqual(prepared, [self.owner['conversation_id']])

    def test_routing_to_closed_old_case_does_not_reopen_it_or_create_a_reply(self):
        self.seed()
        self.message('m2', 'Also I need red caps.')
        second = self.apply([self.create(mid='m2', name='红色帽子', excerpt='red caps')], request='caps')['reply_case_ids'][0]
        old = self.case()
        old['state'] = 'closed'
        self.service.repo.put('case', old['case_id'], old)
        self.service.integrations.generate = lambda context, request_id, **options: {
            'action': 'ask', 'reply': 'Noted.', 'reply_sources': ['m2'], 'reply_case_refs': ['c1']}
        result = self.wait(self.service.call('generate_draft', {'case_id': second, 'expected_case_revision': self.case(second)['revision']}))
        self.assertEqual(self.case()['state'], 'closed')
        self.assertNotIn('draft_id', result['result'])
        self.assertEqual(self.service.repo.get('conversation', self.owner['conversation_id'])['case_id'], second)

    def test_history_tasks_are_deduplicated_per_conversation(self):
        release = threading.Event()
        started = threading.Event()
        original = self.service._task_fill_history
        def fill(params, job_id, epoch, facade):
            started.set()
            release.wait(2)
            return {'message': 'Synthetic history filled'}
        self.service._task_fill_history = fill
        try:
            first = self.service.call('fill_history', {'conversation_id': self.owner['conversation_id']})
            self.assertTrue(started.wait(2))
            second = self.service.call('fill_history', {'conversation_id': self.owner['conversation_id']})
            self.assertEqual(second, first)
        finally:
            release.set()
            self.service._task_fill_history = original
        self.wait(first)


if __name__ == '__main__':
    unittest.main()
