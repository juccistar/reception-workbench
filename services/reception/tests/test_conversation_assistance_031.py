import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.service import ReceptionService


class AssistanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ReceptionService(Path(self.temp.name))
        self.cid = self.service.repo.list('conversation')[0]['conversation_id']

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def wait(self, job):
        for _ in range(200):
            value = self.service.repo.get('job', job)
            if value['status'] not in ('queued', 'running'):
                return value
            time.sleep(.01)
        self.fail('job timed out')

    def test_ai_requirements_summary_and_polish_isolation(self):
        s, cid = self.service, self.cid
        case_id = s.repo.get('conversation', cid)['case_id']
        buyer = next(m for m in s._messages(cid) if m['role'] == 'buyer')
        def model(context, request_id, *, on_call_start, cancel_event, photo_inputs=None):
            on_call_start()
            if context.get('unsent_reply'):
                return {'reply': 'Thank you.', 'reply_sources': [buyer['message_id']]}
            ref = next(row['case_ref'] for row in context['case_catalog'] if row['case_id'] == case_id)
            return {'reply_messages': [{'body': 'Thank you.', 'reply_sources': [buyer['message_id']]}],
                    'conversation_summary': '客户正在询问产品，价格需要核实。',
                    'requirement_changes': [{'action': 'update', 'case_ref': ref,
                        'trigger_evidence_ids': [buyer['message_id']], 'set_fields': {'product': {
                            'value': '手提袋', 'evidence_ids': [buyer['message_id']],
                            'source_excerpts': {buyer['message_id']: buyer['text']}}}}],
                    'quality_notes': [{'code': 'scope_unclear', 'detail': '旧提醒'}]}
        with patch.object(s.integrations, 'generate_manual', side_effect=model):
            job = s.generate_manual_suggestion(cid, 'new-schema', case_id=case_id)['job_id']
            self.assertEqual(self.wait(job)['status'], 'succeeded')
            detail = s.get_conversation(cid)
            self.assertEqual(detail['case']['requirements']['product']['value'], '手提袋')
            self.assertIsNone(detail['case']['requirements']['quantity']['value'])
            self.assertEqual(len(detail['case']['requirements']), 13)
            self.assertNotIn('quality_notes', detail['manual_suggestion'])
            self.assertEqual(detail['conversation_summary']['body'], '客户正在询问产品，价格需要核实。')
            summary = detail['conversation_summary']
            requirements = detail['case']['requirements']
            saved = s.save_chat_draft(cid, 'Unsent text', None, 'polish-0313')
            job = s.generate_manual_suggestion(cid, 'polish-0313', case_id=case_id,
                input_body='Unsent text', input_revision=saved['revision'],
                input_hash=hashlib.sha256(b'Unsent text').hexdigest())['job_id']
            self.assertEqual(self.wait(job)['status'], 'succeeded')
            detail = s.get_conversation(cid)
            self.assertEqual(detail['conversation_summary'], summary)
            self.assertEqual(detail['case']['requirements'], requirements)

    def test_display_link_does_not_invalidate_manual_suggestion(self):
        s, cid = self.service, self.cid
        case_id = s.repo.get('conversation', cid)['case_id']
        with s._lock:
            case = s.repo.get('case', case_id)
            case['requirements'] = {'product_url': {'value': None, 'evidence_ids': []}}
            s.repo.put('case', case_id, case)
            _, before = s._manual_context(cid, case_id)
            case['requirements']['product_url']['value'] = 'https://www.alibaba.com/product-detail/_123.html'
            case['product_main_image'] = {'status': 'ready'}
            s.repo.put('case', case_id, case)
            _, after = s._manual_context(cid, case_id)
        self.assertEqual(before, after)

    def test_supplement_cas_and_reopen(self):
        s, cid = self.service, self.cid
        owner = s.repo.get('conversation', cid)
        saved = s.save_conversation_supplement(cid, '报价 12 USD', None, 'mutation')
        self.assertEqual(saved, s.save_conversation_supplement(cid, '报价 12 USD', None, 'mutation'))
        with self.assertRaises(ServiceError):
            s.save_conversation_supplement(cid, 'changed', None, 'mutation')
        with self.assertRaises(ServiceError):
            s.save_conversation_supplement(cid, 'changed', None, 'other')
        self.assertEqual(owner, s.repo.get('conversation', cid))
        s.close()
        self.service = ReceptionService(Path(self.temp.name))
        self.assertEqual(self.service.get_conversation(cid)['supplement'], saved)

    def test_manual_no_case_human_owned_and_latest_source(self):
        s, cid = self.service, self.cid
        s.begin_manual_edit(cid)
        before = s.repo.get('chat_draft', cid)
        result = s.generate_manual_suggestion(cid, 'request', case_id=None)
        self.assertEqual(result, s.generate_manual_suggestion(cid, 'request', case_id=None))
        self.assertEqual(self.wait(result['job_id'])['status'], 'succeeded')
        view = s.get_conversation(cid)
        self.assertIsNone(view['manual_suggestion']['case_id'])
        self.assertEqual(view['manual_suggestion']['status'], 'ready')
        self.assertEqual(before, s.repo.get('chat_draft', cid))
        self.assertTrue(view['conversation_summary']['body'])
        original_case = view['case']['case_id']
        with s._lock, s.repo.transaction():
            owner = s.repo.get('conversation', cid)
            second = s._new_case(owner, 'synthetic-second')
        self.assertEqual(s.get_conversation(cid, case_id=original_case)['conversation_summary'],
                         s.get_conversation(cid, case_id=second['case_id'])['conversation_summary'])
        s.save_conversation_supplement(cid, 'new basis', None, 'note')
        self.assertEqual(s.get_conversation(cid)['manual_suggestion']['status'], 'stale')

    def test_polish_binds_saved_body_and_revision_and_never_edits_draft(self):
        s, cid = self.service, self.cid
        body = '14.5 USD each; shipping excluded.'
        saved = s.save_chat_draft(cid, body, None, 'polish-draft')
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        with self.assertRaises(ServiceError):
            s.generate_manual_suggestion(cid, 'bad-hash', input_body=body,
                                         input_revision=saved['revision'], input_hash='0' * 64)
        with self.assertRaises(ServiceError):
            s.generate_manual_suggestion(cid, 'bad-version', input_body=body,
                                         input_revision=saved['revision'] + 1, input_hash=digest)
        with patch.object(s, '_prepare_direct_photos', side_effect=AssertionError('polish prepared photos')), \
             patch.object(s, '_direct_photo_inputs', side_effect=AssertionError('polish read photos')):
            job = s.generate_manual_suggestion(cid, 'polish', input_body=body,
                                               input_revision=saved['revision'], input_hash=digest)['job_id']
            self.assertEqual(s.generate_manual_suggestion(cid, 'polish', input_body=body,
                             input_revision=saved['revision'], input_hash=digest)['job_id'], job)
            with self.assertRaises(ServiceError):
                s.generate_manual_suggestion(cid, 'polish', input_body='Different',
                    input_revision=saved['revision'], input_hash=hashlib.sha256(b'Different').hexdigest())
            self.assertEqual(self.wait(job)['status'], 'succeeded')
        suggestion = s.get_conversation(cid)['manual_suggestion']
        self.assertEqual(suggestion['mode'], 'polish')
        self.assertEqual(suggestion['body'], body)
        self.assertEqual(suggestion['input_hash'], digest)
        self.assertEqual(s.repo.get('chat_draft', cid), saved)
        s.save_chat_draft(cid, 'Updated input', saved['revision'], 'changed-input')
        self.assertEqual(s.get_conversation(cid)['manual_suggestion']['status'], 'stale')

    def test_manual_suggestion_rejects_multiple_and_legacy_reply(self):
        s, cid = self.service, self.cid
        for index, output in enumerate((
            {'reply_messages': [{'body': 'first', 'reply_sources': []}, {'body': 'second', 'reply_sources': []}]},
            {'reply': 'legacy', 'reply_sources': []},
        )):
            with patch.object(s.integrations, 'generate_manual', return_value=output):
                job = s.generate_manual_suggestion(cid, f'invalid-suggestion-{index}')['job_id']
                self.assertEqual(self.wait(job)['status'], 'failed')
            self.assertIsNone(s.repo.get('manual_suggestion', cid))

    def test_polish_uses_whole_saved_body(self):
        s, cid = self.service, self.cid
        body = 'first\n\nsecond'
        saved = s.save_chat_draft(cid, body, None, 'whole')
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.assertRaises(ServiceError):
            s.call('generate_manual_suggestion', {'conversation_id': cid, 'client_request_id': 'old-part',
                'input_body': body, 'input_revision': saved['revision'], 'input_hash': digest, 'part_index': 0})
        job = s.generate_manual_suggestion(cid, 'whole-polish', input_body=body,
            input_revision=saved['revision'], input_hash=digest)['job_id']
        self.assertEqual(self.wait(job)['status'], 'succeeded')
        suggestion = s.get_conversation(cid)['manual_suggestion']
        self.assertEqual(suggestion['body'], body)
        self.assertNotIn('input_part_index', suggestion)
        self.assertEqual(suggestion['status'], 'ready')

    def test_polish_late_result_does_not_publish_after_draft_changes(self):
        s, cid = self.service, self.cid
        saved = s.save_chat_draft(cid, 'Old input', None, 'old-input')
        started, release = threading.Event(), threading.Event()
        original = s.integrations.generate_manual
        def slow(context, request_id, *, on_call_start, cancel_event):
            self.assertEqual(context['unsent_reply']['body'], 'Old input')
            on_call_start()
            started.set()
            release.wait(3)
            return {'reply': 'Polished old input'}
        s.integrations.generate_manual = slow
        try:
            job = s.generate_manual_suggestion(cid, 'slow-polish', input_body='Old input',
                input_revision=saved['revision'], input_hash=hashlib.sha256(b'Old input').hexdigest())['job_id']
            self.assertTrue(started.wait(2))
            s.save_chat_draft(cid, 'New input', saved['revision'], 'new-input')
        finally:
            release.set()
            s.integrations.generate_manual = original
        self.assertEqual(self.wait(job)['status'], 'cancelled')
        self.assertIsNone(s.get_conversation(cid)['manual_suggestion'])

    def test_manual_late_result_does_not_publish_changed_source(self):
        s, cid = self.service, self.cid
        started, release = threading.Event(), threading.Event()
        original = s.integrations.generate_manual
        def slow(context, request_id, *, on_call_start, cancel_event):
            on_call_start()
            started.set()
            release.wait(3)
            return {'reply_messages': [{'body': 'old', 'reply_sources': []}]}
        s.integrations.generate_manual = slow
        first = s.generate_manual_suggestion(cid, 'first')['job_id']
        self.assertTrue(started.wait(2))
        s.save_conversation_supplement(cid, 'new note', None, 'new-note')
        s.integrations.generate_manual = original
        second = s.generate_manual_suggestion(cid, 'second')['job_id']
        release.set()
        self.assertEqual(self.wait(first)['status'], 'cancelled')
        self.assertEqual(self.wait(second)['status'], 'succeeded')
        self.assertEqual(s.get_conversation(cid)['manual_suggestion']['request_id'], 'second')

    def test_send_snapshot_survives_new_draft(self):
        s, cid = self.service, self.cid
        started, release = threading.Event(), threading.Event()
        seen = []
        def send(intent, authorization):
            started.set()
            release.wait(3)
            self.assertTrue(authorization())
            seen.append(intent['reply'])
            return {'status': 'accepted', 'message_id': 'synthetic-send', 'submission_started': True}
        s.integrations.send_chat = send
        draft = s.save_chat_draft(cid, 'original', None, 'draft-1')
        job = s.call('send_chat', {'conversation_id': cid, 'draft_id': draft['draft_id'],
                                   'expected_revision': draft['revision'], 'client_request_id': 'send-1'})['job_id']
        self.assertTrue(started.wait(2))
        next_draft = s.save_chat_draft(cid, 'next', draft['revision'], 'draft-2')
        release.set()
        self.assertEqual(self.wait(job)['status'], 'succeeded')
        self.assertEqual(seen, ['original'])
        self.assertEqual(s.repo.get('chat_draft', cid)['body'], 'next')
        self.assertEqual(s.repo.get('chat_draft', cid)['revision'], next_draft['revision'])

    def test_reply_pending_ignores_stale_draft_and_unknown_after_answer(self):
        from reception_service.reception_state import ReceptionProjection
        s, cid = self.service, self.cid
        projection = ReceptionProjection(s)
        conversation = s.repo.get('conversation', cid)
        original = s._messages
        buyer = {'role': 'buyer', 'message_id': 'buyer', 'timestamp_ms': 10}
        seller = {'role': 'seller', 'message_id': 'seller', 'timestamp_ms': 20}
        projection.drafts[cid] = [{'status': 'pending'}]
        projection.operations[cid] = [{'status': 'unknown'}]
        projection.jobs[cid] = [{'kind': 'generate_draft', 'status': 'running'}]
        s._messages = lambda _: [buyer, seller]
        self.assertFalse(projection.reply_pending(conversation))
        s._messages = lambda _: [seller, {**buyer, 'timestamp_ms': 30}]
        self.assertTrue(projection.reply_pending(conversation))
        projection.operations[cid].append({'status': 'accepted', 'source_message_id': 'buyer'})
        self.assertFalse(projection.reply_pending(conversation))
        s._messages = original

    def test_manual_fingerprint_tracks_photo_pixel_availability_and_source(self):
        from reception_service.photos import photo_version
        from reception_service.repository import encoded
        s, cid = self.service, self.cid
        owner = s.repo.get('conversation', cid)
        message = dict(message_id='synthetic-manual-photo', conversation_id=cid,
                       seller_id=s.seller_id, buyer_id=owner['buyer_id'], role='buyer', own=False,
                       text='[image]', attachments=[], timestamp_ms=1900000000000)
        s._ingest(dict(conversation_id=cid, buyer_id=owner['buyer_id'], customer_name='Synthetic photo'),
                  [message], has_more=False, full=True)
        case_id = owner['case_id']
        media = s.get_requirement_media(case_id)
        s.set_product_image_source(case_id, message['message_id'], photo_version(message), True,
                                   media['revision'], 'synthetic-photo-source')
        original_generate = s.integrations.generate_manual
        seen_photos = []
        def capture(context, request_id, *, photo_inputs=None, on_call_start=None, cancel_event=None):
            seen_photos.append((context, photo_inputs))
            return original_generate(context, request_id, photo_inputs=photo_inputs,
                                     on_call_start=on_call_start, cancel_event=cancel_event)
        s.integrations.generate_manual = capture
        job = s.generate_manual_suggestion(cid, 'direct-photo', case_id=case_id)['job_id']
        self.assertEqual(self.wait(job)['status'], 'succeeded')
        self.assertEqual(len(seen_photos), 1)
        self.assertEqual(seen_photos[0][1][0]['message']['message_id'], message['message_id'])
        self.assertIn('original_b64', seen_photos[0][1][0]['asset'])
        self.assertNotIn('original_b64', str(seen_photos[0][0]))
        before = s.get_conversation(cid)
        projected = next(m for m in before['messages'] if m['message_id'] == message['message_id'])
        self.assertEqual(projected['attachments'][0]['availability'], 'ready')
        context, fingerprint = s._manual_context(cid, case_id)
        self.assertTrue(context['photo_context']['direct_images'])
        self.assertEqual(before['manual_suggestion']['context_fingerprint'], fingerprint)
        self.assertEqual(before['manual_suggestion']['status'], 'ready')
        self.assertEqual(s.repo.get('message', encoded([cid, message['message_id']]))['text'], '[image]')
        message['text'] += ' revised caption'
        s._ingest(dict(conversation_id=cid, buyer_id=owner['buyer_id'], customer_name='Synthetic photo'),
                  [message], has_more=False, full=True)
        self.assertNotEqual(fingerprint, s._manual_context(cid, case_id)[1])
        self.assertEqual(s.get_conversation(cid)['manual_suggestion']['status'], 'stale')

    def test_auxiliary_calls_recheck_identity_before_sending_content(self):
        s, cid = self.service, self.cid
        # Keep this identity-before-content test independent of startup body sync.
        with patch.object(s, '_schedule', return_value=None), patch.object(s, '_monitor_once', return_value=None):
            s._queue.join()
            s.mode = 'live'
            s.account_verified = True
            s.checks['ai']['status'] = 'passed'
            sent = []
            s.integrations.resolve_identity = lambda expected: 'different-seller'
            s.integrations.generate_manual = lambda *args, **kwargs: sent.append('manual')
            job = s.generate_manual_suggestion(cid, 'identity-manual')['job_id']
            result = self.wait(job)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error']['code'], 'seller_mismatch')
            self.assertEqual(sent, [])



if __name__ == '__main__':
    unittest.main()
