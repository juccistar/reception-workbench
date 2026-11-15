import unittest
from unittest.mock import Mock, patch

from reception_service.errors import ServiceError
from reception_service.model_router import ModelSnapshot
from reception_service.service import ReceptionService


class ModelWiringTests(unittest.TestCase):
    def setUp(self):
        self.service = ReceptionService.__new__(ReceptionService)
        self.service.mode = 'live'
        self.service._model_config_valid = True
        self.service._model_router = Mock(spec=['assert_current'])
        self.snapshot = {
            'purpose': 'reception', 'profile_id': 'p', 'route_revision': 1,
            'config_revision': 2, 'credential_revision': 3,
            'capability_revision': 4, 'required_capabilities': ['text', 'structure'],
        }

    def test_result_requires_same_route_and_current_actual_capabilities(self):
        actual = dict(self.snapshot, required_capabilities=['text', 'structure', 'single_image', 'combined'])
        self.assertEqual(self.service._verify_model_result(self.snapshot, {'model_snapshot': actual}), actual)
        self.assertEqual(self.service._model_router.assert_current.call_count, 2)
        checked = self.service._model_router.assert_current.call_args.args[0]
        self.assertIsInstance(checked, ModelSnapshot)
        self.assertIn('single_image', checked.required_capabilities)
        with self.assertRaises(ServiceError):
            self.service._verify_model_result(self.snapshot, {'model_snapshot': dict(actual, route_revision=9)})
        with self.assertRaises(ServiceError):
            self.service._verify_model_result(self.snapshot, {})

    def test_failed_authority_check_prevents_result_application(self):
        self.service._model_router.assert_current.side_effect = ServiceError('model_configuration_stale', 'stale')
        with self.assertRaises(ServiceError):
            self.service._verify_model_result(self.snapshot, {'model_snapshot': self.snapshot})

    def test_message_page_uses_composite_cursor_and_legacy_key(self):
        import threading
        from reception_service.repository import Repository, encoded
        service = ReceptionService.__new__(ReceptionService)
        service._lock = threading.RLock()
        service.repo = Repository(None)
        service.repo.put('conversation', 'c', {'conversation_id': 'c'})
        for key, mid, stamp in [('legacy', 'a', None), (encoded(['c', 'b']), 'b', 10),
                                (encoded(['c', 'c']), 'c', 10)]:
            service.repo.put('message', key, {'conversation_id': 'c', 'message_id': mid, 'timestamp_ms': stamp})
        service._photo_projection = lambda row: row
        try:
            with patch('reception_service.service.unread.reading', return_value={}):
                first = service.get_messages('c', limit=2)
                self.assertEqual([row['message_id'] for row in first['items']], ['b', 'c'])
                self.assertTrue(first['has_more'])
                self.assertEqual([row['message_id'] for row in service.get_messages('c', before_id='b')['items']], ['a'])
                self.assertEqual(service.get_messages('c', before_id='a')['items'], [])
                with self.assertRaises(ServiceError):
                    service.get_messages('c', before_id='other')
        finally:
            service.repo.close()

    def test_checker_dispatch_does_not_construct_service(self):
        from reception_service import __main__
        with patch('sys.argv', ['reception', '--model-check']), patch(
                'reception_service.model_providers.checker.run_check_from_stdin', return_value=17) as check:
            self.assertEqual(__main__.main(), 17)
        check.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
