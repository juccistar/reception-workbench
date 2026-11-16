"""Host-only Feishu application credentials and session write grants."""
from __future__ import annotations

import copy
import os
import re

from .errors import ServiceError


class FeishuSettingsMixin:
    def _init_feishu_settings(self):
        self._feishu_secrets = {}
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
        cfg['has_secret'] = bool(getattr(self, '_feishu_secrets', {}).get(cfg.get('app_id')))
        cfg.pop('legacy_cli_config', None)
        return settings

    def _configure_feishu_integration(self, integration):
        configure = getattr(integration, 'configure_feishu_credential', None)
        if configure is not None:
            for app_id, secret in getattr(self, '_feishu_secrets', {}).items():
                configure(app_id, secret)

    def _feishu_credential_requests(self, seller_id, data_root):
        with self._lock:
            self._feishu_owner(seller_id, data_root)
            apps = {self.settings['feishu'].get('app_id')}
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
                        apps.add((record[1].get('target') or {}).get('app_id'))
            return {'app_ids': sorted(app for app in apps if isinstance(app, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app))}

    def _configure_feishu_credential(self, seller_id, data_root, app_id, app_secret):
        if (not isinstance(app_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', app_id)
                or not isinstance(app_secret, str) or not app_secret.strip()
                or len(app_secret) > 4096 or any(c in app_secret for c in ('\0', '\r', '\n'))):
            raise ServiceError('invalid_params', '飞书应用凭据无效。')
        with self._lock:
            self._feishu_owner(seller_id, data_root)
            if app_id not in self._feishu_credential_requests(seller_id, data_root)['app_ids']:
                raise ServiceError('source_changed', '飞书应用配置已变化。')
            configure = getattr(self.integrations, 'configure_feishu_credential', None)
            if configure is None and self.mode != 'demo':
                raise ServiceError('feishu_unavailable', '飞书应用服务尚未就绪。')
            if self.settings['feishu'].get('app_id') == app_id and self._feishu_secrets.get(app_id) != app_secret:
                self.settings['feishu']['write_enabled'] = False
                self.checks['feishu'] = {**self.checks['feishu'], 'status': 'not_checked', 'check_id': None,
                                         'checked_at': None, 'message': '飞书应用凭据已更新。'}
            self._feishu_secrets[app_id] = app_secret
            if configure is not None:
                configure(app_id, app_secret)
        self._changed()
        return {'configured': True}

    def _save_feishu_configuration(self, seller_id, data_root, expected_revision, settings):
        from .integrations.feishu_open import parse_table_url, FeishuOpenError
        allowed = {'enabled', 'app_id', 'table_url', 'fields', 'account_option', 'initial_status'}
        if (type(expected_revision) is not int or not isinstance(settings, dict)
                or set(settings) != allowed or type(settings.get('enabled')) is not bool):
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
            self.save_settings(feishu=copy.deepcopy(settings))
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
            if enabled and (not cfg.get('enabled') or not self._feishu_secrets.get(cfg.get('app_id'))
                            or self.checks['feishu'].get('status') not in ('passed', 'demo')):
                raise ServiceError('feishu_blocked', '请先保存飞书配置并检查表格结构。')
            cfg['write_enabled'] = enabled
            # Integrations own a settings copy; update only this ephemeral grant.
            integration_settings = getattr(self.integrations, 'settings', None)
            if isinstance(integration_settings, dict):
                integration_settings.setdefault('feishu', {})['write_enabled'] = enabled
        self._changed()
        return {'enabled': enabled, 'revision': self._feishu_revision()}
