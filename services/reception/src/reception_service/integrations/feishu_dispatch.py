"""Keep each registration operation on the adapter that created its ledger intent."""
from copy import deepcopy

from .feishu_open_adapter import FeishuOpenAdapter
from .accio_native.vendor.adapters.feishu import FeishuAdapter


class FeishuDispatch:
    def __init__(self, native):
        self.native = native
        cfg = native.settings.get('feishu') or {}
        legacy = cfg.get('legacy_cli_config')
        legacy_settings = deepcopy(native.settings) if isinstance(legacy, dict) else native.settings
        if isinstance(legacy, dict):
            legacy_settings['feishu'] = deepcopy(legacy)
            legacy_settings['feishu']['write_enabled'] = False
        self.legacy = FeishuAdapter(legacy_settings, native.data_dir, native.process, native.ledger)
        self.open = FeishuOpenAdapter(native.settings, native.data_dir, native.process, native.ledger)

    def configure_secret(self, app_id, secret, *, credential_ref=None):
        self.open.configure_secret(app_id, secret, credential_ref=credential_ref)

    def _current(self):
        return self.open if 'app_id' in (self.native.settings.get('feishu') or {}) else self.legacy

    def _owner(self, operation_id):
        old = self.native.ledger.get(operation_id) if operation_id else None
        if old and old[0] == 'register':
            return self.open if old[1].get('adapter') == self.open.OWNER else self.legacy
        return self._current()

    def enabled(self):
        return self._current().enabled()

    def notification_enabled(self):
        return self._current().notification_enabled()

    def notify(self, payload):
        return self._current().notify(payload)

    def check_schema(self):
        return self._current().check_schema()

    def register(self, payload, *, authorization=None):
        return self._owner(payload.get('operation_id')).register(payload, authorization=authorization)

    def reconcile(self, operation_id, request, previous):
        return self._owner(operation_id).reconcile(operation_id, request, previous)
