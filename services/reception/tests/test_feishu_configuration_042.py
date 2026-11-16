import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from reception_service.errors import ServiceError
from reception_service.integrations.feishu_dispatch import FeishuDispatch
from reception_test_support.service import create_synthetic_service


def configuration():
    return {'enabled': True, 'app_id': 'cli_synthetic', 'table_url': 'https://example.feishu.cn/base/baseSynthetic?table=tblSynthetic',
            'fields': {}, 'account_option': '商家A', 'initial_status': '新询盘'}


class FeishuConfigurationTests(unittest.TestCase):
    def test_save_grant_revoke_and_restart_do_not_persist_secrets_or_grants(self):
        with tempfile.TemporaryDirectory() as root:
            service = create_synthetic_service(Path(root))
            try:
                identity = {'seller_id': service.seller_id, 'data_root': root}
                service._save_feishu_configuration(**identity, expected_revision=0, settings=configuration())
                service._configure_feishu_credential(**identity, app_id='cli_synthetic', app_secret='synthetic-secret-never-persist')
                with self.assertRaises(ServiceError):
                    service._grant_feishu_write(**identity, expected_revision=1, enabled=True)
                service.checks['feishu']['status'] = 'demo'
                self.assertTrue(service._grant_feishu_write(**identity, expected_revision=1, enabled=True)['enabled'])
                saved = service._control.get('meta', 'settings')
                self.assertFalse(saved['feishu']['write_enabled'])
                self.assertNotIn('synthetic-secret-never-persist', json.dumps(saved))
                self.assertNotIn('synthetic-secret-never-persist', json.dumps(service.get_snapshot()))
                service.save_settings(merge_seconds=2)
                self.assertFalse(service.settings['feishu']['write_enabled'])
                self.assertFalse(service.integrations.settings['feishu']['write_enabled'])
            finally:
                service.close()
            service = create_synthetic_service(Path(root))
            try:
                self.assertFalse(service.settings['feishu']['write_enabled'])
                self.assertFalse(service.get_snapshot()['settings']['feishu']['has_secret'])
            finally:
                service.close()

    def test_stale_configuration_and_foreign_credential_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            service = create_synthetic_service(Path(root))
            try:
                identity = {'seller_id': service.seller_id, 'data_root': root}
                service._save_feishu_configuration(**identity, expected_revision=0, settings=configuration())
                with self.assertRaises(ServiceError):
                    service._save_feishu_configuration(**identity, expected_revision=0, settings=configuration())
                with self.assertRaises(ServiceError):
                    service._configure_feishu_credential(**identity, app_id='cli_other', app_secret='synthetic')
                with self.assertRaises(ServiceError):
                    service._configure_feishu_credential(seller_id='foreign', data_root=root, app_id='cli_synthetic', app_secret='synthetic')
                with self.assertRaises(ServiceError):
                    service.save_settings(feishu={'write_enabled': True})
            finally:
                service.close()

    def test_dispatch_preserves_original_adapter_before_current_configuration(self):
        records = {
            'legacy': ('register', {'payload': {}}, {'status': 'unknown'}),
            'application': ('register', {'adapter': 'feishu_open_v1', 'target': {'app_id': 'old-app'}}, {'status': 'unknown'}),
        }
        native = SimpleNamespace(settings={'feishu': {'app_id': 'new-app', 'enabled': False}}, data_dir=Path('synthetic'), process=None,
                                 ledger=SimpleNamespace(get=lambda operation: copy.deepcopy(records.get(operation))))
        dispatch = FeishuDispatch(native)
        dispatch.legacy = Mock()
        dispatch.open = Mock(OWNER='feishu_open_v1')
        dispatch.register({'operation_id': 'legacy'})
        dispatch.legacy.register.assert_called_once()
        dispatch.open.register.assert_not_called()
        original = records['application']
        dispatch.reconcile('application', original[1], original[2])
        dispatch.open.reconcile.assert_called_once_with('application', original[1], original[2])
        dispatch.legacy.reconcile.assert_not_called()


if __name__ == '__main__':
    unittest.main()
