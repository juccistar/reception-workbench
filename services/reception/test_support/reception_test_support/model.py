"""Temporary model authority for synthetic live facades in source tests."""
import json
from dataclasses import asdict


def configure_synthetic_model(service, authority_file, *, activate=True):
    capabilities = {name: {'status': 'passed', 'checked_at': 1} for name in
                    ('text', 'structure', 'single_image', 'multi_image', 'combined', 'translation')}
    profile = {'profile_id': 'fixture', 'protocol': 'chat-completions',
               'base_url': 'https://example.invalid', 'model': 'fixture', 'has_key': True,
               'config_revision': 1, 'credential_revision': 1, 'capability_revision': 1,
               'capabilities': capabilities}
    settings = {'format': 'model-settings-v1', 'directory_revision': 1, 'profiles': [profile],
                'routes': {'reception': {'profile_id': 'fixture', 'route_revision': 1},
                           'translation': {'profile_id': None, 'route_revision': 0}}}
    authority_file.write_text(json.dumps({**settings, 'encrypted_keys': {}}), encoding='utf-8')
    packet = {'format': 'model-runtime-v1', 'authority_file': str(authority_file),
              'settings': settings, 'credentials': {'fixture': 'synthetic'}}
    if activate:
        service._configure_models(**packet)
    else:
        service._model_router.configure(packet)


def synthetic_snapshot(router):
    return asdict(router.resolve('reception', ('text', 'structure')))
