import json
import threading
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.model_payload import project_model_payload
from reception_service.reception_context import dialogue_window
from reception_service.integrations.live import LiveIntegration


class ModelPayloadTests(unittest.TestCase):
    def test_window_and_modes_preserve_sources_without_duplicate_timeline(self):
        messages = [dict(message_id=f'm{i}', role='buyer' if i % 2 else 'seller',
                         text=f'text {i}', timestamp_ms=i) for i in range(60)]
        messages.insert(30, dict(message_id='image', role='buyer', text='[image]', timestamp_ms=29))
        messages.insert(31, dict(message_id='card', role='buyer', text='[card] address', timestamp_ms=29))
        recent, older = dialogue_window(messages, {'facts': {'product': {'evidence_ids': ['m0', 'm20']}}},
                                         {'source_message_id': 'm1'})
        self.assertEqual([m['message_id'] for m in older], ['m0', 'm1'])
        self.assertIn('image', [m['message_id'] for m in recent])
        self.assertIn('card', [m['message_id'] for m in recent])
        self.assertEqual(sum(m['message_id'].startswith('m') for m in recent), 50)
        context = {'task_mode': 'automatic', 'recent_messages': recent, 'historical_evidence': older,
                   'case': {'facts': {'product': {'value': 'text 0', 'evidence_ids': ['m0']}},
                            'seller_id': 'secret', 'procurement_key': 'one'},
                   'knowledge': [{'source_id': 'policy', 'policy_id': 'policy', 'content': 'Limit applies'}],
                   'supplement': {'body': 'latest note', 'revision': 5},
                   'budget': {'remaining': 99}, 'fingerprint': 'hash',
                   'unsent_reply': {'body': 'Please confirm', 'revision': 1}}
        automatic = project_model_payload(context)
        self.assertEqual(len(automatic['message_view']), 54)
        self.assertNotIn('recent_messages', automatic)
        self.assertNotIn('budget', automatic)
        self.assertEqual(automatic['facts']['product']['evidence_ids'], ['m0'])
        self.assertEqual(automatic['knowledge'][0]['content'], 'Limit applies')
        polish = project_model_payload(context, 'polish')
        self.assertNotIn('facts', polish)
        self.assertNotIn('case', polish)
        self.assertEqual(polish['supplement']['body'], 'latest note')
        self.assertEqual(polish['unsent_reply']['body'], 'Please confirm')

    def test_leading_images_and_cards_are_kept_when_text_fits_window(self):
        for count in (0, 1, 50):
            with self.subTest(text_count=count):
                messages = [dict(message_id='image', role='seller', text='[image]', timestamp_ms=0),
                            dict(message_id='card', role='buyer', text='[card] address', timestamp_ms=1)]
                messages.extend(dict(message_id=f'm{i}', role='buyer', text=f'text {i}', timestamp_ms=i + 2)
                                for i in range(count))
                recent, historical = dialogue_window(messages, {})
                self.assertEqual(recent, messages)
                self.assertEqual(historical, [])

    def test_query_passes_cancel_event_and_projects_input(self):
        facade = object.__new__(LiveIntegration)
        facade._query_state = threading.local()
        facade.native = type('Native', (), {'data_dir': '.', 'process': None, 'cli': 'fake'})()
        facade._agent_args = lambda: ['--agent-id', 'test']
        facade._own_result_recovery_enabled = lambda: False
        event = threading.Event()
        calls = {}
        class Runner:
            last_request = {}
            last_diagnostics = {}
            def __init__(self, *args, **kwargs): pass
            def run(self, args, **kwargs):
                calls.update(kwargs)
                return {'answer': '{}'}
        with patch('reception_service.integrations.live.DesktopQueryRunner', Runner):
            facade._business_query('prompt', {'task_mode': 'polish', 'unsent_reply': {'body': 'x'},
                                              'photo_context': {'direct_images': [{'message_id': 'x'}]}},
                                   [], cancel_event=event)
        self.assertIs(calls['cancel_event'], event)
        self.assertNotIn('photo_context', calls['input'])
        self.assertEqual(json.loads(calls['input'][len('prompt'):])['unsent_reply']['body'], 'x')
        event.set()
        with self.assertRaises(ServiceError) as error:
            facade._business_query('prompt', {'task_mode': 'polish'}, [], cancel_event=event,
                                   on_call_start=lambda: self.fail('cancelled request started'))
        self.assertEqual(error.exception.code, 'cancelled')

    def test_diagnostics_are_thread_local(self):
        facade = object.__new__(LiveIntegration)
        facade._query_state = threading.local()
        facade.last_ai_diagnostics = {'value': 'main'}
        observed = []
        def worker():
            facade.last_ai_diagnostics = {'value': 'worker'}
            observed.append(facade.last_ai_diagnostics['value'])
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(observed, ['worker'])
        self.assertEqual(facade.last_ai_diagnostics['value'], 'main')


if __name__ == '__main__':
    unittest.main()
