"""Service-level merges preserve the source fact and its matching display text."""
from pathlib import Path
import tempfile
import time
import unittest

from reception_service.service import ReceptionService


class FactDisplayMergeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='arw031-display-')
        self.addCleanup(temporary.cleanup)
        self.service = ReceptionService(Path(temporary.name))
        self.addCleanup(self.service.close)
        self.cid = 'demo-emma'

    def merge(self, previous, incoming, conflicts=None):
        s = self.service
        s.inject_demo(self.cid, 'We need large blue cotton bags or red paper boxes.')
        source = s._latest_buyer(self.cid)
        case = s.get_conversation(self.cid)['case']
        case['facts']['product'] = dict(previous, evidence_ids=[source['message_id']], status='confirmed')
        s.repo.put('case', case['case_id'], case)
        def generate(context, request_id, *, on_call_start=None):
            if on_call_start:
                on_call_start()
            return dict(action='ask', reply='Thanks for the product details.',
                        facts={'product': dict(incoming, evidence_ids=[source['message_id']])},
                        question_keys=[], summary='采购需求整理', missing=[], conflicts=conflicts or [],
                        procurement_key=context['case']['procurement_key'] or 'main',
                        new_case=False, reply_sources=[source['message_id']])
        s.integrations.generate = generate
        job_id = s.call('generate_draft', {'case_id': case['case_id'],
                                         'expected_case_revision': case['revision']})['job_id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = s.repo.get('job', job_id)
            if job['status'] not in ('queued', 'running'):
                break
            time.sleep(.01)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(s.repo.list('operation'), [])
        fact = s.get_conversation(self.cid)['case']['facts']['product']
        self.assertEqual(fact['evidence_ids'], [source['message_id']])
        return fact

    def test_longer_original_retains_its_own_chinese_display(self):
        fact = self.merge({'value': 'large blue cotton bags', 'display_value': '大号蓝色棉布袋'},
                          {'value': 'blue cotton bags', 'display_value': '蓝色棉布袋'})
        self.assertEqual(fact['value'], 'large blue cotton bags')
        self.assertEqual(fact['display_value'], '大号蓝色棉布袋')

    def test_longer_original_does_not_inherit_shorter_display(self):
        fact = self.merge({'value': 'large blue cotton bags'},
                          {'value': 'blue cotton bags', 'display_value': '蓝色棉布袋'})
        self.assertEqual(fact['value'], 'large blue cotton bags')
        self.assertNotIn('display_value', fact)

    def test_conflicting_original_drops_display_without_rewriting_evidence(self):
        fact = self.merge({'value': 'large blue cotton bags', 'display_value': '大号蓝色棉布袋'},
                          {'value': 'red paper boxes', 'display_value': '红色纸盒'})
        self.assertEqual(fact['value'], 'red paper boxes')
        self.assertEqual(fact['status'], 'conflict')
        self.assertNotIn('display_value', fact)

    def test_explicit_conflict_drops_otherwise_matching_display(self):
        fact = self.merge({'value': 'blue cotton bags', 'display_value': '蓝色棉布袋'},
                          {'value': 'blue cotton bags', 'display_value': '蓝色棉布袋'}, ['product'])
        self.assertEqual(fact['value'], 'blue cotton bags')
        self.assertEqual(fact['status'], 'conflict')
        self.assertNotIn('display_value', fact)


if __name__ == '__main__':
    unittest.main()
