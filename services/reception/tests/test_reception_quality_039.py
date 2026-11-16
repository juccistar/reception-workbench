"""0.3.9 data boundaries, current intent and real progress, in temporary workspaces."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError, read_diagnostics, safe_diagnostics
from reception_service.integrations.generation_metadata import checked_metadata
from reception_service.reception_context import add_image_references, dialogue_window
from reception_service.service import ReceptionService


class ReceptionQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.service = ReceptionService(Path(self.temp.name))
        self.addCleanup(self.service.close)
        self.cid = self.service.repo.list('conversation')[0]['conversation_id']
        self.case = self.service.get_conversation(self.cid)['case']

    def save(self, policy, **changes):
        values = {key: policy[key] for key in ('kind', 'title', 'content', 'product_scope', 'confirmed', 'public')}
        values.update(policy_id=policy['policy_id'], expected_revision=policy['revision'],
                      category=policy['category'], enabled=policy['enabled'], usage=policy['usage'])
        values.update(changes)
        return self.service.save_policy(**values)

    def test_catalog_and_disabled_edits_do_not_change_model_inputs(self):
        s = self.service
        before = s.get_context(self.case['case_id'])
        before_records = copy.deepcopy(s.repo.list('policy'))
        catalog = s.list_builtin_policies()['items']
        self.assertEqual(len(catalog), 9)
        self.assertTrue(all(not row['enabled'] and row['adopted_policy_id'] is None for row in catalog))
        self.assertEqual(s.repo.list('policy'), before_records)
        off = s.adopt_builtin_policy('B01', enabled=False)
        off = self.save(off, content='DISABLED-STYLE-SENTINEL')
        after = s.get_context(self.case['case_id'])
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertNotIn('DISABLED-STYLE-SENTINEL', str(after))
        self.assertFalse(after['style'])
        self.assertEqual(s.adopt_builtin_policy('B01')['revision'], off['revision'])
        self.assertFalse(s.repo.get('policy', off['policy_id'])['enabled'])

    def test_auto_suggestion_and_polish_share_enabled_rules_and_disable_invalidates_basis(self):
        s = self.service
        policy = s.adopt_builtin_policy('B01', enabled=True)
        automatic = s.get_context(self.case['case_id'])
        manual, fingerprint = s._manual_context(self.cid, self.case['case_id'])
        polished, _ = s._manual_context(self.cid, self.case['case_id'], 'Keep the same terms.', 1)
        self.assertEqual(automatic['style'], manual['style'])
        self.assertEqual(manual['style'], polished['style'])
        self.assertEqual(manual['task_mode'], 'manual_suggestion')
        self.assertEqual(polished['task_mode'], 'polish')
        self.assertTrue(automatic['policy_versions'])
        self.save(policy, enabled=False)
        after, after_fingerprint = s._manual_context(self.cid, self.case['case_id'])
        self.assertEqual(after['style'], [])
        self.assertNotEqual(fingerprint, after_fingerprint)
        self.assertFalse(s._running)
        self.assertFalse(s._auto_send)
        self.assertFalse(s.repo.get('conversation', self.cid)['human_owned'])

    def test_internal_rules_and_public_wording_have_distinct_sources(self):
        s = self.service
        policy = s.save_policy('policy', 'Media handling', 'INTERNAL-REASON-SENTINEL', [], True, False,
                              enabled=True, usage='internal_guidance', category='reception',
                              customer_wording='Additional product details need confirmation.', internal_notes='HUMAN-ONLY-SENTINEL')
        context = s.get_context(self.case['case_id'])
        public = next(row for row in context['knowledge'] if row['policy_id'] == policy['policy_id'] + ':customer')
        private = next(row for row in context['guidance'] if row['policy_id'] == policy['policy_id'] + ':internal')
        self.assertIn('INTERNAL-REASON-SENTINEL', private['content'])
        self.assertNotIn('INTERNAL-REASON-SENTINEL', str(context['knowledge']))
        self.assertNotIn('HUMAN-ONLY-SENTINEL', str(context))
        self.assertNotEqual(public['policy_id'], private['policy_id'])
        legacy = s.save_policy('policy', 'Legacy private', 'LEGACY-NOT-FOR-AI', [], True, False)
        self.assertNotIn(legacy['content'], str(s.get_context(self.case['case_id'])))

    def test_window_only_adds_missing_evidence_and_continuation(self):
        messages = [dict(message_id='m' + str(i), role='seller' if i % 2 == 0 else 'buyer',
                         text='Product photos ' + str(i), timestamp_ms=i + 1) for i in range(70)]
        frozen = copy.deepcopy(messages)
        case = {'facts': {'product': {'evidence_ids': ['m0', 'm30']}},
                'conflicts': [{'evidence_ids': ['m1']}]}
        recent, old = dialogue_window(messages, case, {'source_message_id': 'm2'})
        self.assertEqual(recent, messages[-50:])
        self.assertEqual([m['message_id'] for m in old], ['m0', 'm1', 'm2'])
        self.assertEqual(messages, frozen)

    def test_image_aliases_preserve_sender_order_and_raw_bodies(self):
        rows = [dict(message_id='buyer-image', role='buyer', seller_id='1', buyer_id='2', text='[image]', timestamp_ms=1),
                dict(message_id='seller-text', role='seller', seller_id='1', buyer_id='2', text='Which one?', timestamp_ms=2),
                dict(message_id='seller-image', role='seller', seller_id='1', buyer_id='2', text='[image]', timestamp_ms=3)]
        refs = [dict(image_id='img-1', message_id='buyer-image', source_version='a', image_sha256='b'),
                dict(image_id='img-2', message_id='seller-image', source_version='c', image_sha256='d')]
        frozen = copy.deepcopy(rows)
        context = add_image_references({'recent_messages': rows, 'photo_context': {'direct_images': refs}})
        self.assertEqual([row['text'] for row in context['message_view']], ['[image]', 'Which one?', '[image]'])
        self.assertEqual(context['message_view'][0]['image_refs'], [{'alias': 'p1', 'image_id': 'img-1'}])
        self.assertEqual(context['message_view'][2]['image_refs'], [{'alias': 'p2', 'image_id': 'img-2'}])
        self.assertEqual([row['role'] for row in context['image_manifest']], ['buyer', 'seller'])
        self.assertEqual([row['sender_id'] for row in context['image_manifest']], ['2', '1'])
        self.assertEqual(context['image_transport'], 'read_compat')
        self.assertEqual(rows, frozen)

    def test_processing_is_explicit_idempotent_and_private_resolution_stays_private(self):
        s = self.service
        source = s._latest_buyer(self.cid)
        task = s._create_assistance(self.case, source, None, 'verify_followup', [], detail='Confirm sample availability.')
        pending = s.get_context(self.case['case_id'])
        self.assertNotIn('assistance_state', pending)
        self.assertNotIn('assistance_suggestions', pending)
        self.assertNotIn('quality_notes', pending)
        self.assertNotIn('Confirm sample availability.', str(pending))
        self.assertEqual(s.repo.get('assistance_task', task['task_id']), task)
        context = s.get_context(self.case['case_id'])
        self.assertEqual(pending['fingerprint'], context['fingerprint'])
        self.assertEqual(s.repo.get('assistance_task', task['task_id']), task)
        self.assertFalse(s.repo.get('conversation', self.cid)['human_owned'])
        self.assertFalse(s._auto_send)

    def test_structured_requests_only_reference_this_customer_and_manual_is_advisory(self):
        s = self.service
        context, _ = s._manual_context(self.cid, self.case['case_id'])
        target = context['reply_target']['source_message_id']
        raw = {'reply': 'Let me check that.', 'reply_sources': [target, 'not-this-customer'],
               'conversation_summary': '客户询问样品。', 'assistance_suggestions': [
            {'kind': 'verify_followup', 'detail': 'Confirm the current sample cost.', 'fact_keys': [], 'source_message_ids': [target]},
            {'kind': 'verify_followup', 'detail': 'Foreign customer request', 'fact_keys': [], 'source_message_ids': ['not-this-customer']},
            {'kind': 'send_message', 'detail': 'Execute a send', 'fact_keys': [], 'source_message_ids': [target]}]}
        before = s.repo.list('assistance_task')
        checked = checked_metadata(raw, context, manual=True)
        self.assertEqual(checked['reply_sources'], [target])
        self.assertNotIn('assistance_suggestions', checked)
        self.assertNotIn('quality_notes', checked)
        from reception_service.integrations.analysis import validate_analysis
        automatic = s.get_context(self.case['case_id'])
        result = validate_analysis({**raw, 'action': 'ask',
                                    'reply_messages': [{'body': raw['reply'], 'reply_sources': [target]}]}, automatic)
        self.assertEqual(result['reply'], 'Let me check that.')
        self.assertEqual(result['conversation_summary'], '客户询问样品。')
        self.assertNotIn('quality_notes', result)
        self.assertNotIn('assistance_suggestions', result)
        self.assertEqual(s.repo.list('assistance_task'), before)

    def test_polish_reminders_survive_legacy_shape_without_creating_sources_or_tasks(self):
        s = self.service
        context, _ = s._manual_context(self.cid, self.case['case_id'], 'The sample is $15. Shipping is not included.', 1)
        before = s.repo.list('assistance_task')
        checked = checked_metadata({
            'reply_sources': [{'source_id': 'unsent_reply', 'revision': 1}, 'unsent_reply'],
            'quality_notes': ['原文价格与运费条件需核实。', {'code': [], 'detail': 'Invalid category'},
                              {'code': 'commitment_unverified', 'detail': '保留原文条件，发送前核实。'}],
        }, context, manual=True)
        self.assertEqual(checked['reply_sources'], [])
        self.assertNotIn('quality_notes', checked)
        self.assertNotIn('assistance_suggestions', checked)
        self.assertEqual(context['unsent_reply']['body'], 'The sample is $15. Shipping is not included.')
        self.assertEqual(s.repo.list('assistance_task'), before)

    def test_read_diagnostics_accept_only_fixed_strings_and_hide_raw_data(self):
        self.assertEqual(safe_diagnostics({'read_stage': [], 'read_category': {'raw': 'private'}}), {})
        self.assertEqual(safe_diagnostics({'read_stage': 'http', 'read_category': 'status', 'url': 'private', 'response': 'private'}),
                         {'read_stage': 'http', 'read_category': 'status'})
        error = ServiceError('read_failed', 'Read failed')
        error.stage, error.category = [], {'raw': 'private'}
        self.assertEqual(read_diagnostics(error), {'read_stage': 'unknown', 'read_category': 'unknown'})


if __name__ == '__main__':
    unittest.main()
