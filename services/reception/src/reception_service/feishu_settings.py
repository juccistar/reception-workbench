"""Host-only Feishu application credentials and session write grants."""
from __future__ import annotations

import copy
import os
import re
import threading

from .errors import ServiceError


class FeishuSettingsMixin:
    def _init_feishu_settings(self):
        self._feishu_secrets = {}
        self._feishu_save_local = threading.local()
        self.settings['feishu']['write_enabled'] = False

    def _feishu_owner(self, seller_id, data_root):
        if (not self.account_verified or not self.seller_id or seller_id != self.seller_id
                or not isinstance(data_root, str)
                or os.path.normcase(os.path.realpath(data_root)) != os.path.normcase(str(self.data_root.resolve()))):
            raise ServiceError('source_changed', '商家或数据目录已变化。')

    def _feishu_revision(self):
        value = self.settings['feishu'].get('revision', 0)
        return value if type(value) is int and value >= 0 else 0

    def _feishu_settings_snapshot(self):
        settings = copy.deepcopy(self.settings)
        cfg = settings['feishu']
        cfg['has_secret'] = bool(getattr(self, '_feishu_secrets', {}).get((cfg.get('app_id'), cfg.get('credential_ref'))))
        cfg.pop('legacy_cli_config', None)
        return settings

    def _configure_feishu_integration(self, integration):
        configure = getattr(integration, 'configure_feishu_credential', None)
        if configure is not None:
            for (app_id, credential_ref), secret in getattr(self, '_feishu_secrets', {}).items():
                configure(app_id, secret, credential_ref=credential_ref)

    def _feishu_credential_requests(self, seller_id, data_root):
        with self._lock:
            self._feishu_owner(seller_id, data_root)
            cfg = self.settings['feishu']
            credentials = {(cfg.get('app_id'), cfg.get('credential_ref'))}
            native = getattr(self.integrations, 'native', None)
            ledger = getattr(native, 'ledger', None)
            if ledger is not None:
                for item in self.repo.list('outbox'):
                    if item.get('status') not in ('unknown', 'syncing'):
                        continue
                    try:
                        self._receipt_owner(item, registration=True)
                    except ServiceError:
                        continue
                    record = ledger.get(item['operation_id'])
                    if record and record[0] == 'register' and record[1].get('adapter') == 'feishu_open_v1':
                        target = record[1].get('target') or {}
                        credentials.add((target.get('app_id'), target.get('credential_ref')))
            return {'credentials': [{'app_id': app, 'credential_ref': ref} for app, ref in sorted(credentials, key=lambda item: (str(item[0]), item[1] or ''))
                    if isinstance(app, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app)
                    and (ref is None or isinstance(ref, str) and re.fullmatch(r'[a-f0-9]{64}', ref))]}

    def _configure_feishu_credential(self, seller_id, data_root, app_id, app_secret, credential_ref=None):
        if (not isinstance(app_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app_id)
                or not isinstance(app_secret, str) or not app_secret.strip()
                or len(app_secret) > 4096 or any(c in app_secret for c in ('\0', '\r', '\n'))):
            raise ServiceError('invalid_params', '飞书应用凭据无效。')
        with self._lock:
            self._feishu_owner(seller_id, data_root)
            key = (app_id, credential_ref)
            requested = self._feishu_credential_requests(seller_id, data_root)['credentials']
            if {'app_id': app_id, 'credential_ref': credential_ref} not in requested:
                raise ServiceError('source_changed', '飞书应用配置已变化。')
            configure = getattr(self.integrations, 'configure_feishu_credential', None)
            if configure is None and self.mode != 'demo':
                raise ServiceError('feishu_unavailable', '飞书应用服务尚未就绪。')
            cfg = self.settings['feishu']
            if (cfg.get('app_id'), cfg.get('credential_ref')) == key and self._feishu_secrets.get(key) != app_secret:
                self.settings['feishu']['write_enabled'] = False
                self.checks['feishu'] = {**self.checks['feishu'], 'status': 'not_checked', 'check_id': None,
                                         'checked_at': None, 'message': '飞书应用凭据已更新。'}
            self._feishu_secrets[key] = app_secret
            if configure is not None:
                configure(app_id, app_secret, credential_ref=credential_ref)
        self._changed()
        return {'configured': True}

    def _save_feishu_configuration(self, seller_id, data_root, expected_revision, settings, credential_ref=None):
        from .integrations.feishu_open import parse_table_url, FeishuOpenError
        allowed = {'enabled', 'app_id', 'table_url', 'fields', 'account_option', 'initial_status'}
        if (type(expected_revision) is not int or not isinstance(settings, dict)
                or set(settings) != allowed or type(settings.get('enabled')) is not bool
                or credential_ref is not None and (not isinstance(credential_ref, str) or not re.fullmatch(r'[a-f0-9]{64}', credential_ref))):
            raise ServiceError('invalid_params', '飞书设置格式无效。')
        if settings['enabled']:
            if not isinstance(settings['app_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', settings['app_id']):
                raise ServiceError('invalid_params', '请填写有效的飞书应用 ID。')
            try:
                parse_table_url(settings['table_url'])
            except FeishuOpenError:
                raise ServiceError('invalid_params', '请填写包含数据表 ID 的飞书多维表格链接。') from None
        with self._lock:
            self._feishu_owner(seller_id, data_root)
            if expected_revision != self._feishu_revision():
                raise ServiceError('revision_conflict', '飞书设置已变化，请保留输入并重新读取。')
            self._feishu_save_local.credential_ref = credential_ref
            try:
                self.save_settings(feishu=copy.deepcopy(settings))
            finally:
                del self._feishu_save_local.credential_ref
            revision = self._feishu_revision()
        return {'saved': True, 'revision': revision}

    def _grant_feishu_write(self, seller_id, data_root, expected_revision, enabled):
        if type(expected_revision) is not int or type(enabled) is not bool:
            raise ServiceError('invalid_params', '飞书授权参数无效。')
        with self._lock:
            self._feishu_owner(seller_id, data_root)
            if expected_revision != self._feishu_revision():
                raise ServiceError('revision_conflict', '飞书设置已变化，请重新检查。')
            cfg = self.settings['feishu']
            if enabled and (not cfg.get('enabled') or not self._feishu_secrets.get((cfg.get('app_id'), cfg.get('credential_ref')))
                            or self.checks['feishu'].get('status') not in ('passed', 'demo')):
                raise ServiceError('feishu_blocked', '请先保存飞书配置并检查表格结构。')
            cfg['write_enabled'] = enabled
            # Integrations own a settings copy; update only this ephemeral grant.
            integration_settings = getattr(self.integrations, 'settings', None)
            if isinstance(integration_settings, dict):
                integration_settings.setdefault('feishu', {})['write_enabled'] = enabled
        self._changed()
        return {'enabled': enabled, 'revision': self._feishu_revision()}
