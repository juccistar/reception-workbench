import tempfile
import threading
import time
import unittest
from pathlib import Path

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
        s.save_conversation_supplement(cid, 'new basis', None, 'note')
        self.assertEqual(s.get_conversation(cid)['manual_suggestion']['status'], 'stale')

    def test_manual_late_result_does_not_publish_changed_source(self):
        s, cid = self.service, self.cid
        started, release = threading.Event(), threading.Event()
        original = s.integrations.generate_manual
        def slow(context, request_id, *, on_call_start, cancel_event):
            on_call_start()
            started.set()
            release.wait(3)
            return {'reply': 'old'}
        s.integrations.generate_manual = slow
        first = s.generate_manual_suggestion(cid, 'first')['job_id']
        self.assertTrue(started.wait(2))
        s.save_conversation_supplement(cid, 'new note', None, 'new-note')
        s.integrations.generate_manual = original
        second = s.generate_manual_suggestion(cid, 'second')['job_id']
        release.set()
        self.assertEqual(self.wait(first)['status'], 'stale')
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

    def test_manual_fingerprint_ignores_photo_projection_but_tracks_source(self):
        from reception_service.photos import photo_version
        from reception_service.repository import encoded
        s, cid = self.service, self.cid
        owner = s.repo.get('conversation', cid)
        message = dict(message_id='synthetic-manual-photo', conversation_id=cid,
                       seller_id=s.seller_id, buyer_id=owner['buyer_id'], role='buyer', own=False,
                       text='[image]', attachments=[], timestamp_ms=1900000000000)
        s._ingest(dict(conversation_id=cid, buyer_id=owner['buyer_id'], customer_name='Synthetic photo'),
                  [message], has_more=False, full=True)
        job = s.generate_manual_suggestion(cid, 'before-photo-load')['job_id']
        self.assertEqual(self.wait(job)['status'], 'succeeded')
        before = s.get_conversation(cid)
        projected = next(m for m in before['messages'] if m['message_id'] == message['message_id'])
        self.assertEqual(projected['attachments'][0]['availability'], 'not_loaded')
        _, fingerprint = s._manual_context(cid, None)
        params = dict(conversation_id=cid, message_id=message['message_id'], source_version=photo_version(message))
        image_job = s.call('load_message_image', params)['job_id']
        self.assertEqual(self.wait(image_job)['status'], 'succeeded')
        after = s.get_conversation(cid)
        projected = next(m for m in after['messages'] if m['message_id'] == message['message_id'])
        self.assertEqual(projected['attachments'][0]['availability'], 'ready')
        self.assertEqual(fingerprint, s._manual_context(cid, None)[1])
        self.assertEqual(before['manual_suggestion']['suggestion_id'], after['manual_suggestion']['suggestion_id'])
        self.assertEqual(after['manual_suggestion']['status'], 'ready')
        self.assertEqual(s.repo.get('message', encoded([cid, message['message_id']]))['text'], '[image]')
        message['text'] += ' revised caption'
        s._ingest(dict(conversation_id=cid, buyer_id=owner['buyer_id'], customer_name='Synthetic photo'),
                  [message], has_more=False, full=True)
        self.assertNotEqual(fingerprint, s._manual_context(cid, None)[1])
        self.assertEqual(s.get_conversation(cid)['manual_suggestion']['status'], 'stale')

    def test_auxiliary_calls_recheck_identity_before_sending_content(self):
        s, cid = self.service, self.cid
        s.mode = 'live'
        s.account_verified = True
        s.checks['ai']['status'] = 'passed'
        sent = []
        s.integrations.resolve_identity = lambda expected: 'different-seller'
        s.integrations.generate_manual = lambda *args, **kwargs: sent.append('manual')
        job = s.generate_manual_suggestion(cid, 'identity-manual')['job_id']
        self.assertEqual(self.wait(job)['status'], 'failed')
        message = next(m for m in s._messages(cid) if m.get('text'))
        class TranslationFacade:
            def resolve_identity(self, expected):
                return 'different-seller'
            def translate(self, *args, **kwargs):
                sent.append('translation')
        s._translation_facade = TranslationFacade()
        job = s.request_translations(cid, [{'kind': 'message', 'source_id': message['message_id']}], 'identity-translation')['job_id']
        result = self.wait(job)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result.get('error_code'), 'seller_mismatch')
        self.assertEqual(sent, [])

    def test_live_translation_requires_ai_readiness_before_queuing(self):
        s, cid = self.service, self.cid
        message = next(m for m in s._messages(cid) if m.get('text'))
        item = {'kind': 'message', 'source_id': message['message_id']}
        s.mode = 'live'
        before = s.list_jobs()
        for status in ('not_checked', 'running', 'failed'):
            with self.subTest(status=status):
                s.checks['ai']['status'] = status
                with self.assertRaises(ServiceError) as caught:
                    s.request_translations(cid, [item], 'not-ready-' + status)
                self.assertEqual(caught.exception.code, 'ai_capability')
                self.assertEqual(s.list_jobs(), before)
                self.assertTrue(s._translation_queue.empty())
        self.assertEqual(s.get_translations(cid, [item])['items'][0]['status'], 'pending')

    def test_shutdown_cancels_translation_before_repository_close(self):
        s, cid = self.service, self.cid
        message = next(m for m in s._messages(cid) if m.get('text'))
        started, stopped = threading.Event(), threading.Event()
        class Slow:
            def translate(self, items, request_id, *, on_call_start, cancel_event):
                started.set()
                cancel_event.wait(3)
                stopped.set()
                raise RuntimeError('cancelled')
        s._translation_facade = Slow()
        s.request_translations(cid, [{'kind': 'message', 'source_id': message['message_id']}], 'close')
        self.assertTrue(started.wait(2))
        s.close()
        self.assertTrue(stopped.is_set())
        self.assertFalse(s._translation_worker.is_alive())

    def test_translation_cache_scope_and_preemption(self):
        s, cid = self.service, self.cid
        message = next(m for m in s._messages(cid) if m.get('text'))
        item = {'kind': 'message', 'source_id': message['message_id']}
        started = threading.Event()
        cancelled = threading.Event()
        class Slow:
            def translate(self, items, request_id, *, on_call_start, cancel_event):
                on_call_start()
                started.set()
                cancel_event.wait(3)
                if cancel_event.is_set():
                    cancelled.set()
                    raise RuntimeError('cancelled')
                return {'items': [{'key': i['key'], 'text': '译文'} for i in items]}
        s._translation_facade = Slow()
        job = s.request_translations(cid, [item], 'translate')['job_id']
        self.assertTrue(started.wait(2))
        business = s.call('check_connection', {})['job_id']
        self.assertTrue(cancelled.wait(2))
        self.assertEqual(self.wait(job)['status'], 'cancelled')
        self.assertNotIn(self.wait(business)['status'], ('queued', 'running'))
        with self.assertRaises(ServiceError):
            s.get_translations(cid, [{'kind': 'message', 'source_id': 'foreign'}])
        with self.assertRaises(ServiceError):
            s.get_translations(cid, [{**item, 'source_hash': 'wrong'}])
        class Fast:
            def translate(self, items, request_id, *, on_call_start, cancel_event):
                on_call_start()
                return {'items': [{'key': i['key'], 'text': '译文'} for i in items]}
        s._translation_facade = Fast()
        retry = s.request_translations(cid, [item], 'retry')['job_id']
        self.assertEqual(self.wait(retry)['status'], 'succeeded')
        self.assertEqual(s.get_translations(cid, [item])['items'][0]['text'], '译文')


if __name__ == '__main__':
    unittest.main()
