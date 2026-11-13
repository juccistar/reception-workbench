import unittest

from reception_service.business_progress import apply_progress, build_progress_context, project_progress
from reception_service.business_supplement import supplement_context
from reception_service.repository import Repository, encoded


class BusinessProgressTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(None, 's')
        self.owner = dict(seller_id='s', buyer_id='b', conversation_id='c')
        self.case = dict(**self.owner, case_id='case')
        self.msg = dict(**self.owner, message_id='m1', role='buyer', text='Price and freight?', timestamp_ms=1)
        self.repo.put('message', encoded(['c', 'm1']), self.msg)
        self.context = {'case': self.case, 'recent_messages': [self.msg],
                        'supplement': supplement_context(self.repo, 'c')}

    def tearDown(self):
        self.repo.close()

    def apply(self, issues, draft_id=None):
        apply_progress(self.repo, self.owner, self.case, self.context, {'issues': issues}, draft_id=draft_id)

    def issue(self, topic, state='needs_input', **extra):
        return dict(topic=topic, summary=topic, state=state, evidence_ids=['m1'], **extra)

    def test_partial_answer_and_exact_receipt(self):
        self.apply([self.issue('价格'), self.issue('运费')])
        before = project_progress(self.repo, self.owner)['issues']
        self.apply([self.issue('价格', 'waiting_customer', issue_id=before[0]['issue_id'], reply_addresses=True)], 'd1')
        self.assertEqual([x['state'] for x in project_progress(self.repo, self.owner)['issues']], ['needs_reply', 'needs_input'])
        foreign = dict(**self.owner, status='accepted', draft_id='d2', message_id='sent', submission_started=True)
        self.assertEqual(project_progress(self.repo, self.owner, [foreign])['issues'][0]['state'], 'needs_reply')
        receipt = dict(foreign, draft_id='d1')
        self.assertEqual(project_progress(self.repo, self.owner, [receipt])['issues'][0]['state'], 'waiting_customer')
        self.assertEqual(project_progress(self.repo, self.owner, [receipt])['issues'][1]['state'], 'needs_input')

    def test_context_uses_accepted_issue_state_and_omits_resolved(self):
        self.apply([self.issue('价格', 'waiting_customer', reply_addresses=True),
                    self.issue('运费', 'resolved', reply_addresses=True)], 'd1')
        self.repo.put('operation', 'receipt', dict(**self.owner, operation_id='receipt', status='accepted',
                      draft_id='d1', message_id='sent', submission_started=True))
        issues = build_progress_context(self.repo, self.owner, self.case)['issues']
        self.assertEqual([(i['topic'], i['state']) for i in issues], [('价格', 'waiting_customer')])

    def test_pending_receipt_never_projects_waiting(self):
        self.apply([self.issue('价格', 'waiting_customer', reply_addresses=True)], 'd1')
        receipt = dict(**self.owner, status='accepted', draft_id='d1', message_id='sent', submission_started=True)
        pending = dict(**self.owner, status='unknown')
        self.assertEqual(project_progress(self.repo, self.owner, [receipt, pending])['state'], 'needs_reply')

    def test_changed_and_cleared_note_invalidates(self):
        note = dict(conversation_id='c', body='运费20美元', revision=1, updated_at=1)
        self.repo.put('conversation_supplement', 'c', note)
        self.context['supplement'] = supplement_context(self.repo, 'c')
        source = self.context['supplement']['source_id']
        self.apply([dict(topic='运费', summary='可回复', state='needs_reply', evidence_ids=['m1', source])])
        self.assertEqual(project_progress(self.repo, self.owner)['state'], 'needs_reply')
        self.repo.put('conversation_supplement', 'c', dict(note, body='', revision=2))
        result = project_progress(self.repo, self.owner)
        self.assertEqual(result['state'], 'pending_analysis')
        self.assertEqual(result['issues'], [])

    def test_invalid_ids_and_unknown_issues_preserve_prior(self):
        self.apply([self.issue('运费')])
        old = project_progress(self.repo, self.owner)['issues']
        self.apply([dict(topic='运费', summary='伪造', state='resolved', evidence_ids=['foreign']),
                    self.issue('价格', 'resolved', issue_id='foreign')])
        self.assertEqual(project_progress(self.repo, self.owner)['issues'], old)
        self.assertEqual(build_progress_context(self.repo, self.owner, self.case)['issues'][0]['issue_id'], old[0]['issue_id'])

    def test_partial_analysis_does_not_certify_omitted_issue_on_new_source(self):
        self.apply([self.issue('价格'), self.issue('运费')])
        old = project_progress(self.repo, self.owner)['issues']
        message = dict(self.msg, message_id='m2', text='Price is urgent', timestamp_ms=2)
        self.repo.put('message', encoded(['c', 'm2']), message)
        self.context['recent_messages'].append(message)
        self.apply([dict(topic='价格', summary='待回复', state='needs_reply', evidence_ids=['m2'],
                         issue_id=old[0]['issue_id'])])
        result = project_progress(self.repo, self.owner)
        self.assertEqual(result['state'], 'pending_analysis')
        self.assertEqual(len(result['issues']), 2)

    def test_legacy_tasks_are_not_counted_or_modified(self):
        self.repo.put('assistance_task', 'a', dict(task_id='a', conversation_id='c', status='open'))
        self.assertEqual(project_progress(self.repo, self.owner)['state'], 'pending_analysis')
        self.apply([])
        self.assertEqual(project_progress(self.repo, self.owner)['state'], 'idle')
        self.assertEqual(self.repo.get('assistance_task', 'a')['status'], 'open')

    def test_duplicate_topics_and_new_source_pending(self):
        self.apply([self.issue('运费'), self.issue('运费', 'needs_reply')])
        self.assertEqual(len(project_progress(self.repo, self.owner)['issues']), 1)
        message = dict(self.msg, message_id='m2', text='Other question', timestamp_ms=2)
        self.repo.put('message', encoded(['c', 'm2']), message)
        self.assertEqual(project_progress(self.repo, self.owner)['state'], 'pending_analysis')


if __name__ == '__main__':
    unittest.main()
