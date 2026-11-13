"""Targeted request budgets, safe visual diagnostics and translation controls."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from reception_service.errors import ServiceError, safe_diagnostics
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError
from reception_service.integrations.generation_metadata import checked_metadata
from reception_service.integrations.live import boundary
from reception_service.model_payload import model_json, model_request
from reception_service.repository import encoded
from reception_service.service import ReceptionService


class ProcessingTests(unittest.TestCase):
    def test_model_budget_counts_projection_and_complete_request_without_truncation(self):
        context = {'task_mode': 'polish', 'internal_diagnostic': 'x' * 150000,
                   'unsent_reply': {'body': 'Keep the complete sentence.'}, 'recent_messages': []}
        self.assertIn('Keep the complete sentence.', model_json(context))
        self.assertNotIn('internal_diagnostic', model_json(context))
        with self.assertRaises(ServiceError) as caught:
            model_request('instructions', context, 'x' * 140000)
        self.assertEqual(caught.exception.code, 'context_too_long')
        context['unsent_reply']['body'] = 'x' * 120001
        with self.assertRaises(ServiceError):
            model_json(context)

    def test_valid_sources_beyond_one_hundred_and_notes_beyond_ten_are_preserved(self):
        mids = ['source-' + str(index) for index in range(125)]
        context = {'recent_messages': [{'message_id': mid} for mid in mids]}
        raw = {'reply_sources': mids,
               'quality_notes': [{'code': 'style_review', 'detail': 'note-' + str(index)} for index in range(12)],
               'assistance_requests': [{'kind': 'verify_followup', 'detail': 'task-' + field,
                                        'fact_keys': [field], 'source_message_ids': [mids[0]]}
                                       for field in ('product', 'quantity', 'specifications', 'customization', 'destination', 'delivery')]}
        result = checked_metadata(raw, context)
        self.assertEqual(result['reply_sources'], mids)
        self.assertEqual(len(result['quality_notes']), 12)
        self.assertEqual(len(result['assistance_requests']), 6)
        raw['assistance_requests'].append({'kind': 'verify_followup', 'detail': 'Additional quantity detail',
                                          'fact_keys': ['quantity'], 'source_message_ids': [mids[1]]})
        grouped = checked_metadata(raw, context)['assistance_requests']
        quantity = next(row for row in grouped if row['fact_keys'] == ['quantity'])
        self.assertEqual(quantity['source_message_ids'], mids[:2])
        self.assertIn('Additional quantity detail', quantity['detail'])
        with self.assertRaises(ServiceError) as caught:
            checked_metadata({'quality_notes': ['x' * 2000] * 40}, context)
        self.assertEqual(caught.exception.code, 'metadata_too_large')

    def test_visual_boundary_preserves_only_fixed_classification(self):
        @boundary('analysis')
        def fail():
            error = AdapterError('private-path-and-prompt', code='unverified')
            error.visual_stage, error.visual_status = 'read_trace', 'not_found'
            raise error
        with self.assertRaises(ServiceError) as caught:
            fail()
        self.assertEqual(caught.exception.diagnostics['visual_stage'], 'read_trace')
        self.assertEqual(caught.exception.diagnostics['visual_status'], 'not_found')
        self.assertNotIn('private', str(caught.exception.to_dict()))
        self.assertEqual(safe_diagnostics({'visual_stage': 'C:/private', 'visual_status': 'private prompt'}), {})

    def test_translation_context_respects_saved_message_and_utf8_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReceptionService(Path(directory))
            try:
                cid = service.repo.list('conversation')[0]['conversation_id']
                owner = service.repo.get('conversation', cid)
                for index in range(4):
                    row = {**{key: owner[key] for key in ('conversation_id', 'buyer_id', 'seller_id')},
                           'message_id': 'context-' + str(index), 'role': 'buyer', 'own': False,
                           'text': '中文上下文' + str(index), 'attachments': [], 'timestamp_ms': 2000000000000 + index}
                    service.repo.put('message', encoded([cid, row['message_id']]), row)
                source = [{'kind': 'message', 'source_id': 'context-3'}]
                service.save_settings(translation_context_messages=3, translation_context_bytes=13)
                payload = service._translation_sources(cid, source)[0][2]
                self.assertLessEqual(sum(len(row['text'].encode('utf-8')) for row in payload['context']), 13)
                self.assertEqual(payload['text'], '中文上下文3')
                service.save_settings(translation_context_messages=0)
                self.assertEqual(service._translation_sources(cid, source)[0][2]['context'], [])
                with self.assertRaises(ServiceError):
                    service.save_settings(required_fields_by_category={'帽子': ['unrecognized']})
                service.save_settings(required_fields_by_category={'帽子': ['quantity']})
                self.assertEqual(service.settings['required_fields_by_category']['帽子'], ['product', 'quantity'])
            finally:
                service.close()

    def test_saved_translation_parallelism_changes_the_actual_http_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReceptionService(Path(directory))
            release = threading.Event()
            entered = [threading.Event(), threading.Event()]
            try:
                cid = service.repo.list('conversation')[0]['conversation_id']
                owner = service.repo.get('conversation', cid)
                for index in range(2):
                    row = {**{key: owner[key] for key in ('conversation_id', 'buyer_id', 'seller_id')},
                           'message_id': 'slot-' + str(index), 'role': 'buyer', 'own': False,
                           'text': 'Synthetic translation ' + str(index), 'attachments': [], 'timestamp_ms': 2000000000000 + index}
                    service.repo.put('message', encoded([cid, row['message_id']]), row)
                class Provider:
                    def translate(self, items, request_id, *, cancel_event=None):
                        index = int(items[0]['text'][-1])
                        entered[index].set()
                        release.wait(3)
                        return {'items': [{'key': item['key'], 'text': '合成译文'} for item in items]}
                service._new_translation_provider = Provider
                service.save_settings(translation_parallelism=1)
                for index in range(2):
                    service.request_translations(cid, [{'kind': 'message', 'source_id': 'slot-' + str(index)}], 'request-' + str(index))
                    if index == 0:
                        self.assertTrue(entered[0].wait(1))
                self.assertFalse(entered[1].wait(.15))
                service.save_settings(translation_parallelism=2)
                self.assertTrue(entered[1].wait(1), 'Saved parallelism did not change the translation worker')
            finally:
                release.set()
                service.close()


if __name__ == '__main__':
    unittest.main()
