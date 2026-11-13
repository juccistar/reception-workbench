import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from reception_service.service import ReceptionService


class SupplementFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.real_schedule = ReceptionService._schedule
        self.schedule = patch.object(ReceptionService, '_schedule', return_value=None)
        self.schedule.start()
        self.service = ReceptionService(Path(self.temp.name))
        self.cid = self.service.repo.list('conversation')[0]['conversation_id']
        self.calls = []
        self.two_issues = False
        self.generate_patch = patch.object(self.service.integrations, 'generate', side_effect=self.generate)
        self.generate_patch.start()
        self.service.set_scope('selected', [self.cid], None)
        self.service.set_reception(True, auto_send=True)
        self.service.inject_demo(self.cid, 'How much is freight?')

    def tearDown(self):
        self.generate_patch.stop()
        self.service.close()
        self.schedule.stop()
        self.temp.cleanup()

    def generate(self, context, request_id, *, on_call_start=None, photo_inputs=None):
        if on_call_start:
            on_call_start()
        self.calls.append(context)
        continuation = context.get('continuation') or {}
        followup = continuation.get('phase') == 'supplement'
        buyer = next(m for m in reversed(context['recent_messages']) if m['role'] == 'buyer')
        issue = {'topic': '运费', 'summary': '已有运费' if followup else '需要运费',
                 'state': 'waiting_customer' if followup else 'needs_input',
                 'evidence_ids': [buyer['message_id']], 'reply_addresses': followup}
        if followup:
            issue.update(issue_id=continuation['issue_ids'][0], evidence_ids=[buyer['message_id'], context['supplement']['source_id']])
        issues = [issue]
        if self.two_issues and not followup:
            issues.append({'topic': '库存', 'summary': '需要库存', 'state': 'needs_input',
                           'evidence_ids': [buyer['message_id']]})
        return {'action': 'ask', 'reply': 'We will check freight.' if not followup else 'The freight is 20 USD.',
                'facts': {}, 'question_keys': [], 'conflicts': [], 'summary': '', 'new_case': False,
                'procurement_key': context['case']['procurement_key'],
                'reply_sources': [buyer['message_id']] if not followup else [buyer['message_id'], context['supplement']['source_id']],
                'requirements': {'product': {'value': '样品', 'evidence_ids': [buyer['message_id']]}},
                'conversation_summary': '客户询问运费，运费条件需要核实。',
                'quality_notes': [{'code': 'scope_unclear', 'detail': '旧提醒'}],
                'followup_progress': {'issues': issues}}

    def wait_for(self, predicate):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        self.fail('timed out: jobs=%r operations=%r work=%r' % (
            self.service.repo.list('job', conversation_id=self.cid),
            self.service.repo.list('operation', conversation_id=self.cid),
            self.service.repo.get('supplement_followup', self.cid)))

    def initial_reply(self):
        s = self.service
        case = s.repo.get('case', s.repo.get('conversation', self.cid)['case_id'])
        s._enqueue('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']}, automatic=True)
        self.wait_for(lambda: any(o.get('status') == 'accepted' for o in s.repo.list('operation', conversation_id=self.cid)))

    def schedule_work(self):
        s = self.service
        with s._lock:
            work = s.repo.get('supplement_followup', self.cid)
            work['due_at'] = 0
            s.repo.put('supplement_followup', self.cid, work)
        # Avoid an unrelated account poll racing the targeted follow-up.
        s._next_poll = float('inf')
        self.real_schedule(s)

    def test_canonical_requirements_drive_waiting_quote(self):
        def complete(context, request_id, *, on_call_start=None, photo_inputs=None):
            on_call_start()
            mid = next(m['message_id'] for m in reversed(context['recent_messages']) if m['role'] == 'buyer')
            return {'action': 'wait_quote', 'reply': 'Thank you.', 'facts': {}, 'question_keys': [],
                    'conflicts': [], 'reply_sources': [mid], 'conversation_summary': '客户正在询问产品报价。',
                    'requirements': {key: {'value': value, 'evidence_ids': [mid]} for key, value in
                        {'product': '手提袋', 'quantity': '100件', 'specifications': '蓝色棉布', 'destination': '法国'}.items()}}
        with patch.object(self.service.integrations, 'generate', side_effect=complete):
            case = self.service.repo.get('case', self.service.repo.get('conversation', self.cid)['case_id'])
            ack = self.service._enqueue('generate_draft', {'case_id': case['case_id'], 'expected_case_revision': case['revision']}, automatic=True)
            self.wait_for(lambda: self.service.repo.get('job', ack['job_id'])['status'] not in ('queued', 'running'))
            updated = self.service.repo.get('case', case['case_id'])
            self.assertEqual(updated['state'], 'waiting_quote')
            self.assertEqual(updated['missing'], [])
            self.assertEqual(updated['facts'], {})

    def test_accepted_supplement_followup_is_once(self):
        self.initial_reply()
        s = self.service
        detail = s.get_conversation(self.cid)
        self.assertEqual(detail['case']['requirements']['product']['value'], '样品')
        self.assertEqual(detail['conversation_summary']['body'], '客户询问运费，运费条件需要核实。')
        self.assertNotIn('quality_warnings', detail['draft'])
        self.assertNotIn('warnings', detail['draft']['validation'])
        s.save_conversation_supplement(self.cid, 'Freight is 20 USD.', None, 'note1')
        self.schedule_work()
        self.wait_for(lambda: any(o.get('intent', {}).get('continuity_phase') == 'supplement' and o.get('status') == 'accepted'
                                  for o in s.repo.list('operation', conversation_id=self.cid)))
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('business_progress', s.get_conversation(self.cid)['conversation'])
        self.assertFalse(s.repo.list('business_progress', conversation_id=self.cid))
        note = s._supplement(self.cid)
        s.save_conversation_supplement(self.cid, note['body'], note['revision'], 'same')
        self.schedule_work()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(sum(o.get('intent', {}).get('continuity_phase') == 'supplement' for o in s.repo.list('operation', conversation_id=self.cid)), 1)

    def test_selected_scope_followup_accepts_existing_buyer_source(self):
        self.initial_reply()
        s = self.service
        s.save_conversation_supplement(self.cid, 'Freight is 20 USD.', None, 'note1')
        with patch.object(s, '_automatic_source', return_value=None):
            self.schedule_work()
            self.wait_for(lambda: any(o.get('intent', {}).get('continuity_phase') == 'supplement' and o.get('status') == 'accepted'
                                      for o in s.repo.list('operation', conversation_id=self.cid)))
        self.assertEqual(len(self.calls), 2)

    def test_two_issues_can_be_answered_in_separate_followups(self):
        self.two_issues = True
        self.initial_reply()
        s = self.service
        note = s.save_conversation_supplement(self.cid, 'Freight is 20 USD.', None, 'note1')
        self.schedule_work()
        self.wait_for(lambda: sum(o.get('intent', {}).get('continuity_phase') == 'supplement' and o.get('status') == 'accepted'
                                  for o in s.repo.list('operation', conversation_id=self.cid)) == 1)
        s.save_conversation_supplement(self.cid, 'Freight is 20 USD. Stock is available.', note['revision'], 'note2')
        self.schedule_work()
        self.wait_for(lambda: sum(o.get('intent', {}).get('continuity_phase') == 'supplement' and o.get('status') == 'accepted'
                                  for o in s.repo.list('operation', conversation_id=self.cid)) == 2)
        plans = [p for p in s.repo.list('continuity_plan', conversation_id=self.cid) if p.get('phase') == 'supplement']
        self.assertNotEqual(plans[0]['client_request_id'], plans[1]['client_request_id'])
        self.assertFalse(set(plans[0]['supplement_issue_ids']) & set(plans[1]['supplement_issue_ids']))

    def test_changed_note_does_not_reset_failed_issue_retry_chain(self):
        self.initial_reply()
        s = self.service
        note = s.save_conversation_supplement(self.cid, 'Freight is 20 USD.', None, 'note1')
        work = s.repo.get('supplement_followup', self.cid)
        for index in range(3):
            plan_id, job_id = 'plan%d' % index, 'failed-job%d' % index
            s.repo.put('continuity_plan', plan_id, {'plan_id': plan_id, 'phase': 'supplement',
                'conversation_id': self.cid, 'status': 'exhausted', 'supplement_issue_ids': work['issue_ids']})
            s.repo.put('job', job_id, {'job_id': job_id, 'conversation_id': self.cid, 'automatic': True,
                'kind': 'continuity_send', 'status': 'failed', 'plan_id': plan_id, 'created_at': index + 1})
            s.repo.put('operation', 'op%d' % index, {'operation_id': 'op%d' % index, 'job_id': job_id,
                'seller_id': s.seller_id, 'conversation_id': self.cid, 'source_message_id': work['source_message_id'],
                'kind': 'send', 'purpose': 'continuity', 'plan_id': plan_id,
                'intent': {'continuity_phase': 'supplement'}, 'status': 'rejected',
                'submission_started': False, 'error_code': 'rate_limit', 'updated_at': index + 1})
        s.save_conversation_supplement(self.cid, 'Freight is 25 USD.', note['revision'], 'note2')
        self.schedule_work()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(s.repo.get('supplement_followup', self.cid)['issue_ids'], work['issue_ids'])

    def test_paused_human_and_unknown_do_not_generate(self):
        self.initial_reply()
        s = self.service
        s.set_reception(False)
        s.save_conversation_supplement(self.cid, 'Freight is 20 USD.', None, 'note1')
        self.schedule_work()
        self.assertEqual(len(self.calls), 1)
        s.set_reception(True, auto_send=True)
        s.takeover(self.cid, True)
        self.schedule_work()
        self.assertEqual(len(self.calls), 1)
        s.takeover(self.cid, False)
        with s._lock:
            owner = s.repo.get('conversation', self.cid)
            s.repo.put('operation', 'unknown-test', dict(operation_id='unknown-test', kind='send', status='unknown',
                seller_id=owner['seller_id'], buyer_id=owner['buyer_id'], conversation_id=self.cid,
                case_id=owner['case_id'], source_message_id='x', updated_at=1, next_reconcile_at=9999999999999))
        self.schedule_work()
        self.assertEqual(len(self.calls), 1)
