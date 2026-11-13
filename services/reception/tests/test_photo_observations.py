"""Legacy observation validation; new business requests use original image refs."""
import copy
import hashlib
import unittest

from reception_service.context import build_context, validated_facts
from reception_service.errors import ServiceError
from reception_service.integrations.analysis import prepare_context, reply_grounding_issues, validate_analysis
from reception_service.photos import (photo_version, photo_observation_id, valid_photo_observation,
                                     photo_context, photo_fingerprint, with_photo_notice)
from reception_service.repository import Repository, encoded


class PhotoObservationTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(None, 'seller')
        self.addCleanup(self.repo.close)
        self.message = dict(seller_id='seller', buyer_id='buyer', conversation_id='cid', message_id='image-1',
                            role='buyer', own=False, text='[image]', attachments=[], timestamp_ms=1000)
        self.case = dict(seller_id='seller', buyer_id='buyer', conversation_id='cid', case_id='case',
                         facts={}, revision=1, procurement_key='main', summary='', questions={}, state='clarifying')
        self.sha = 'a' * 64
        self.record = self.observation(self.message)
        self.repo.put('conversation', 'cid', dict(revision=1))
        self.put_image(self.message, self.record)

    def observation(self, message, sha=None):
        sha = sha or self.sha
        return {**{k: message[k] for k in ('seller_id', 'buyer_id', 'conversation_id', 'message_id')},
                'status': 'ready', 'source_version': photo_version(message), 'image_sha256': sha,
                'observation_id': photo_observation_id(message, sha), 'description': 'A blue bag.',
                'visible_text': ['CODE 42'], 'uncertainties': ['Material is unconfirmed.'],
                'request_id': 'native-request-1', 'job_id': 'local-job'}

    def put_image(self, message, record):
        key = encoded([message['conversation_id'], message['message_id']])
        self.repo.put('message', key, message)
        self.repo.put('message_image', key, {**{k: message[k] for k in
                      ('seller_id', 'buyer_id', 'conversation_id', 'message_id')},
                      'source_version': photo_version(message), 'status': 'ready', 'sha256': self.sha})
        self.repo.put('image_asset', self.sha, {'sha256': self.sha})
        self.repo.put('photo_observation', key, record)

    def context(self):
        # Explicit historical context fixture. Current build_context deliberately
        # excludes these old reports; the legacy projection still validates them.
        result = build_context(self.repo, self.case, {'required_fields': ['product', 'quantity']}, {'ai_remaining': 4})
        records = []
        for message in self.repo.list('message', conversation_id='cid'):
            key = encoded(['cid', message['message_id']])
            state = self.repo.get('message_image', key, {})
            asset = self.repo.get('image_asset', state.get('sha256'), {})
            if asset.get('sha256') == state.get('sha256') and state.get('source_version') == photo_version(message):
                record = valid_photo_observation(message, self.repo.get('photo_observation', key), state.get('sha256'))
                if record:
                    records.append(record)
        result['photo_context'] = photo_context(result['historical_evidence'] + result['recent_messages'], records)
        result['fingerprint'] = hashlib.sha256(encoded({key: value for key, value in result.items()
            if key not in ('budget', 'fingerprint')}).encode()).hexdigest()
        return result

    def test_new_business_context_does_not_import_legacy_observation_reports(self):
        result = build_context(self.repo, self.case, {'required_fields': ['product', 'quantity']}, {})
        self.assertEqual(result['photo_context']['mode'], 'direct')
        self.assertEqual(result['photo_context']['observations'], [])
        self.assertFalse(result['photo_context']['pixel_access'])
        self.assertNotIn(self.record['description'], str(result))

    def test_projection_keeps_raw_identity_and_observation_provenance(self):
        before = copy.deepcopy(self.message)
        context = self.context()
        photo = context['photo_context']
        self.assertFalse(photo['pixel_access'])
        self.assertTrue(photo['observations_available'])
        self.assertEqual(photo['observations'][0]['request_id'], 'native-request-1')
        self.assertEqual(photo['events'][0]['source_version'], photo_version(self.message))
        self.assertEqual(photo['events'][0]['availability'], 'observed')
        self.assertEqual(prepare_context(context)[0]['photo_context'], photo)
        self.assertEqual(before, self.repo.get('message', encoded(['cid', 'image-1'])))
        self.assertNotIn('job_id', photo['observations'][0])
        self.assertNotIn('image_asset', str(photo))

    def test_wrong_identity_version_hash_and_missing_asset_are_unobserved(self):
        key = encoded(['cid', 'image-1'])
        for field, value in (('seller_id', 'other'), ('buyer_id', 'other'), ('conversation_id', 'other'),
                             ('message_id', 'other'), ('source_version', 'b' * 64), ('image_sha256', 'b' * 64),
                             ('observation_id', 'forged'), ('request_id', ''), ('status', 'failed')):
            with self.subTest(field=field):
                altered = dict(self.record, **{field: value})
                self.repo.put('photo_observation', key, altered)
                self.assertFalse(self.context()['photo_context']['observations_available'])
        self.repo.put('photo_observation', key, self.record)
        state = self.repo.get('message_image', key)
        self.repo.put('message_image', key, dict(state, sha256='b' * 64))
        self.assertFalse(self.context()['photo_context']['observations_available'])
        self.repo.put('message_image', key, state)
        self.repo.put('image_asset', self.sha, {'sha256': 'b' * 64})
        self.assertFalse(self.context()['photo_context']['observations_available'])

    def test_raw_attachment_cannot_override_actual_version(self):
        raw = dict(self.message, attachments=[{'kind': 'image', 'source_version': photo_version(self.message)}])
        self.assertNotEqual(photo_version(raw), photo_version(self.message))
        self.assertIsNone(valid_photo_observation(raw, self.record))
        projected = with_photo_notice(self.message)
        self.assertIsNotNone(valid_photo_observation(projected, self.record, self.sha))
        projected['attachments'][0]['source_version'] = 'c' * 64
        self.assertIsNone(valid_photo_observation(projected, self.record, self.sha))

    def test_observation_and_raw_revision_change_fingerprints_but_budget_is_separate(self):
        before = self.context()
        other_budget = self.context()
        other_budget['budget'] = {'ai_remaining': 3}
        self.assertEqual(before['fingerprint'], other_budget['fingerprint'])
        self.assertEqual(photo_fingerprint(before), photo_fingerprint(other_budget))
        self.repo.put('photo_observation', encoded(['cid', 'image-1']), dict(self.record, description='A red bag.'))
        after = self.context()
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertNotEqual(photo_fingerprint(before), photo_fingerprint(after))
        self.repo.put('message', encoded(['cid', 'image-1']), dict(self.message, text=' [image] '))
        revised = self.context()
        self.assertFalse(revised['photo_context']['observations_available'])
        self.assertNotEqual(photo_fingerprint(after), photo_fingerprint(revised))

    def test_only_visible_text_provides_numeric_photo_evidence(self):
        record = dict(self.record, description='A 999 cm bag.', uncertainties=['Maybe 888 g.'])
        self.repo.put('photo_observation', encoded(['cid', 'image-1']), record)
        context = self.context()
        source = [record['observation_id']]
        self.assertFalse(reply_grounding_issues('The label says CODE 42.', source, context))
        for number in ('999', '888'):
            issues = reply_grounding_issues('The label says ' + number + ' cm.', source, context)
            self.assertTrue(any('缺少引用依据' in issue for issue in issues), issues)

    def test_observations_never_confirm_procurement_or_seller_promises(self):
        context = self.context()
        for source in ('image-1', self.record['observation_id']):
            with self.subTest(source=source):
                warnings = []
                facts = validated_facts({'facts': {'product': {'value': 'blue bag', 'evidence_ids': [source]}}}, context, warnings=warnings)
                self.assertNotIn('product', facts)
                self.assertTrue(warnings)
        issues = reply_grounding_issues('Shipping is free.', [self.record['observation_id']], context)
        self.assertTrue(any('卖家资料' in issue for issue in issues), issues)
        answer = dict(action='ask', facts={}, question_keys=[], reply='Could you confirm the product name?',
                      summary='inferred blue bag', missing=['product', 'quantity'], conflicts=[],
                      procurement_key='inferred', new_case=True, reply_sources=[self.record['observation_id']])
        checked = validate_analysis(answer, context)
        self.assertFalse(checked['new_case'])
        self.assertEqual(checked['procurement_key'], 'main')
        self.assertEqual(checked['summary'], '')

    def test_mixed_photos_require_each_own_citation_for_generic_sight_claim(self):
        context = self.context()
        claim = 'Your photo shows a blue bag. Is this the style you need?'
        self.assertFalse(reply_grounding_issues(claim, [self.record['observation_id']], context))
        self.assertTrue(reply_grounding_issues(claim, ['image-1'], context))
        second = dict(self.message, message_id='image-2', timestamp_ms=2000)
        self.repo.put('message', encoded(['cid', 'image-2']), second)
        self.assertTrue(reply_grounding_issues(claim, [self.record['observation_id']], self.context()))
        second_record = self.observation(second)
        self.put_image(second, second_record)
        self.assertTrue(reply_grounding_issues(claim, [self.record['observation_id']], self.context()))
        self.assertFalse(reply_grounding_issues(claim, [self.record['observation_id'], second_record['observation_id']], self.context()))

    def test_duplicate_and_forged_context_observations_are_not_promoted(self):
        self.assertFalse(photo_context([self.message], [self.record, self.record])['observations_available'])
        context = self.context()
        context['photo_context']['observations'][0]['buyer_id'] = 'another-buyer'
        checked, _, _ = prepare_context(context)
        self.assertFalse(checked['photo_context']['observations_available'])
        self.assertTrue(reply_grounding_issues('Could you confirm the style?', [self.record['observation_id']], context))


if __name__ == '__main__':
    unittest.main()
