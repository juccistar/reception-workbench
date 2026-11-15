import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from reception_service.errors import ServiceError
from reception_service.service import ReceptionService


class IdentityRevocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ReceptionService(Path(self.temp.name))
        self.service.mode = 'live'
        self.service.account_verified = True
        self.service._running = True
        self.service._auto_send = True
        self.service._wa_enabled = True
        self.service._procurement_authorized = True

    def tearDown(self):
        self.service.mode = 'demo'
        self.service.close()
        self.temp.cleanup()

    def test_failed_resolution_revokes_all_grants_without_changing_unknown(self):
        s = self.service
        s.repo.put('operation', 'op_test', {'operation_id': 'op_test', 'status': 'unknown'})
        epoch = s._procurement_epoch
        facade = Mock()
        facade.resolve_identity.side_effect = ServiceError('identity_invalid', 'synthetic', stage='identity')
        with self.assertRaises(ServiceError):
            s._ensure_identity(facade)
        self.assertFalse(s.account_verified)
        self.assertFalse(s._running)
        self.assertFalse(s._auto_send)
        self.assertFalse(s._wa_enabled)
        self.assertFalse(s._procurement_authorized)
        self.assertGreater(s._procurement_epoch, epoch)
        self.assertEqual(s.repo.get('operation', 'op_test')['status'], 'unknown')

    def test_model_configuration_revokes_only_affected_purpose(self):
        s = self.service
        router = s._model_router
        router.configure = Mock(return_value={'translation'})
        translation_generation = s._translation_generation
        self.assertEqual(s.call('_configure_models', {'format': 'test'}), {'affected': ['translation']})
        self.assertEqual(s._translation_generation, translation_generation + 1)
        self.assertTrue(s._running)
        self.assertTrue(s._wa_enabled)
        router.configure.return_value = {'reception'}
        s.call('_configure_models', {'format': 'test'})
        self.assertFalse(s._running)
        self.assertFalse(s._wa_enabled)
        self.assertTrue(s._procurement_authorized)

    def test_model_snapshot_checks_authority_without_notification(self):
        s = self.service
        path = Path(self.temp.name) / 'model-settings.json'
        profile = {'profile_id': 'p', 'name': 'test', 'protocol': 'chat-completions',
                   'base_url': 'https://example.com', 'model': 'test', 'credential_ref': 'c',
                   'has_key': True, 'config_revision': 1, 'credential_revision': 1,
                   'capability_revision': 1, 'capabilities': {'text': {'status': 'passed'},
                                                              'structure': {'status': 'passed'}}}
        settings = {'format': 'model-settings-v1', 'directory_revision': 1, 'profiles': [profile],
                    'routes': {'reception': {'profile_id': 'p', 'route_revision': 1},
                               'translation': {'profile_id': None, 'route_revision': 0}}}
        path.write_text(json.dumps({**settings, 'encrypted_keys': {}}), encoding='utf-8')
        s.call('_configure_models', {'format': 'model-runtime-v1', 'authority_file': str(path),
                                     'settings': settings, 'credentials': {'p': 'synthetic-key'}})
        frozen = s._freeze_reception_model()
        self.assertNotIn('synthetic-key', json.dumps(frozen))
        s._assert_reception_model(frozen)
        settings['routes']['reception']['route_revision'] += 1
        path.write_text(json.dumps({**settings, 'encrypted_keys': {}}), encoding='utf-8')
        with self.assertRaises(ServiceError):
            s._assert_reception_model(frozen)

    def test_cancelled_or_superseded_resolution_keeps_new_grants(self):
        s = self.service
        facade = Mock()
        facade.resolve_identity.side_effect = ServiceError('cancelled', 'cancelled')
        with self.assertRaises(ServiceError):
            s._ensure_identity(facade)
        self.assertTrue(s.account_verified)
        self.assertTrue(s._wa_enabled)
        def late_failure(_):
            s._epoch += 1
            raise ServiceError('identity_invalid', 'late failure')
        facade.resolve_identity.side_effect = late_failure
        with self.assertRaises(ServiceError) as raised:
            s._ensure_identity(facade)
        self.assertEqual(raised.exception.code, 'cancelled')
        self.assertTrue(s.account_verified)
        self.assertTrue(s._procurement_authorized)
