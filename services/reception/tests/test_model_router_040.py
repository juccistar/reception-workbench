import json
import tempfile
import unittest
from pathlib import Path

from reception_service.errors import ServiceError
from reception_service.model_router import ModelRouter


class ModelRouterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.json'
        profile = {'profile_id': 'p', 'name': 'one', 'protocol': 'chat-completions',
                   'base_url': 'https://example.com', 'model': 'example', 'credential_ref': 'c',
                   'has_key': True, 'config_revision': 1, 'credential_revision': 1,
                   'capability_revision': 1, 'capabilities': {'text': {'status': 'passed', 'checked_at': 1}}}
        self.settings = {'format': 'model-settings-v1', 'directory_revision': 1, 'profiles': [profile],
                         'routes': {'reception': {'profile_id': 'p', 'route_revision': 1},
                                    'translation': {'profile_id': None, 'route_revision': 0}}}
        self.write()
        self.router = ModelRouter()
        self.addCleanup(self.router.close)
        self.router.configure({'format': 'model-runtime-v1', 'authority_file': str(self.path),
                               'settings': self.settings, 'credentials': {'p': 'private-key'}})

    def write(self):
        self.path.write_text(json.dumps({**self.settings, 'encrypted_keys': {}}), encoding='utf-8')

    def test_unrelated_directory_change_does_not_invalidate(self):
        snapshot = self.router.resolve('reception', ['text'])
        self.assertNotIn('private-key', repr(snapshot))
        self.settings['directory_revision'] += 1
        self.settings['profiles'][0]['name'] = 'renamed'
        self.write()
        self.router.assert_current(snapshot)

    def test_authority_change_fails_closed(self):
        snapshot = self.router.resolve('reception', ['text'])
        self.settings['profiles'][0]['base_url'] = 'https://other.example'
        self.write()
        with self.assertRaises(ServiceError):
            self.router.assert_current(snapshot)
        self.path.write_text('{', encoding='utf-8')
        with self.assertRaises(ServiceError):
            self.router.assert_current(snapshot)

    def test_capabilities_and_close_fail_closed(self):
        with self.assertRaises(ServiceError):
            self.router.resolve('reception', ['single_image'])
        snapshot = self.router.resolve('reception', ['text'])
        self.router.close()
        with self.assertRaises(ServiceError):
            self.router.assert_current(snapshot)


if __name__ == '__main__':
    unittest.main()
