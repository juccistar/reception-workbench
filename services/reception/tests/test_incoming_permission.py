"""Synthetic native incoming authorization checks; no real processes or accounts."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reception_service.integrations import build_integrations
from test_integrations import BUYER, CID, NOW, SELLER, MockProcess, intent


def incoming(**changes):
    value = intent(send_mode='automatic', source_timestamp_ms=NOW, budget_reserved=True,
                   frozen_scope=dict(mode='incoming', conversation_ids=[], seller_id=SELLER,
                                     started_at_ms=NOW))
    value.update(changes)
    return value


class IncomingPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='reception-incoming-permission-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('run', 'Popen'):
            guard = patch('subprocess.' + name, side_effect=AssertionError('Real subprocess forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        account = self.root / 'accounts' / 'synthetic'
        account.mkdir(parents=True)
        node, script, cli = self.root / 'synthetic-node.exe', account / 'synthetic-mcp.js', account / 'workctl.exe'
        for file in (node, script, cli):
            file.touch()
        os.environ.update(ACCIO_GATEWAY_TOKEN='synthetic-test-token', ACCIO_NODE_BIN=str(node), ACCIO_MCP_SCRIPT=str(script))
        self.settings = dict(workctl_path=str(cli), seller_id=SELLER)
        self.counter = 0

    def live(self):
        self.counter += 1
        facade = build_integrations('live', self.root / str(self.counter), self.settings)
        process = MockProcess()
        facade.native.process = process
        facade.native.feishu.process = process
        return facade, process

    def reject(self, value, authorization=lambda: True):
        facade, process = self.live()
        result = facade.send(value, authorization)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertFalse(result['submission_started'])
        self.assertEqual(process.send_count, 0)

    def test_correct_incoming_includes_a_customer_outside_the_frozen_id_list(self):
        facade, process = self.live()
        value = incoming()
        self.assertNotIn(CID, value['frozen_scope']['conversation_ids'])
        result = facade.send(value, lambda: True)
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(process.send_count, 1)
        commands = [call['args'][3] for call in process.calls if call['args'][1:3] == ['workflow', 'chat-analysis']]
        self.assertIn('resolve-id', commands)
        self.assertIn('seal-reply-preview', commands)
        self.assertEqual(commands.count('send-message'), 1)
        self.assertEqual(commands[-1], 'resolve-id')  # Receipt readback rechecks identity.

    def test_message_earlier_than_authorization_start_is_rejected(self):
        value = incoming()
        value['frozen_scope']['started_at_ms'] = NOW + 1
        self.reject(value)

    def test_start_time_must_be_present_and_a_positive_integer(self):
        for invalid in (None, 0, -1, True, str(NOW), float(NOW), 9007199254740992):
            with self.subTest(started_at_ms=invalid):
                value = incoming()
                value['frozen_scope']['started_at_ms'] = invalid
                self.reject(value)
        value = incoming()
        del value['frozen_scope']['started_at_ms']
        self.reject(value)

    def test_source_time_must_be_present_and_a_valid_integer(self):
        for invalid in (None, 0, -1, True, str(NOW), float(NOW), 9007199254740992):
            with self.subTest(source_timestamp_ms=invalid):
                self.reject(incoming(source_timestamp_ms=invalid))
        value = incoming()
        del value['source_timestamp_ms']
        self.reject(value)

    def test_incoming_scope_must_bind_the_intent_seller(self):
        for invalid in ('30003', None, ''):
            with self.subTest(seller_id=invalid):
                value = incoming()
                value['frozen_scope']['seller_id'] = invalid
                self.reject(value)
        value = incoming()
        del value['frozen_scope']['seller_id']
        self.reject(value)
        self.reject(incoming(seller_id=None))

    def test_incoming_requires_reserved_budget_and_affirmative_service_callback(self):
        self.reject(incoming(budget_reserved=False))
        self.reject(incoming(budget_reserved=1))
        self.reject(incoming(), lambda: False)
        self.reject(incoming(), lambda: 1)
        def failed_callback():
            raise RuntimeError('synthetic authorization failure')
        self.reject(incoming(), failed_callback)

    def test_pause_after_native_sealing_still_prevents_submission(self):
        facade, process = self.live()
        process.stage_hook = lambda command: facade.revoke() if command == 'seal-reply-preview' else None
        result = facade.send(incoming(), lambda: True)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertFalse(result['submission_started'])
        self.assertEqual(process.send_count, 0)

    def test_revocation_during_callback_cannot_restore_permission(self):
        facade, process = self.live()
        def authorize():
            facade.revoke()
            return True
        result = facade.send(incoming(), authorize)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(process.send_count, 0)

    def test_incoming_rechecks_callback_at_the_native_submission_boundary(self):
        facade, process = self.live()
        checks = []
        def authorize():
            checks.append(True)
            return len(checks) < 4
        result = facade.send(incoming(), authorize)
        self.assertGreaterEqual(len(checks), 4)
        self.assertEqual(process.send_count, 0)
        self.assertIn(result['status'], ('rejected', 'unknown'))

    def test_claimed_source_time_must_match_native_message_time(self):
        value = incoming(source_timestamp_ms=NOW + 100)
        value['frozen_scope']['started_at_ms'] = NOW + 1
        self.reject(value)

    def test_native_identity_and_latest_buyer_checks_remain_required(self):
        facade, process = self.live()
        process.seller = '30003'
        result = facade.send(incoming(), lambda: True)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(process.send_count, 0)
        facade, process = self.live()
        process.history['messages'].append(dict(messageId='buyer-later', senderAliId=BUYER,
                                               timestamp=NOW + 1, content='A newer buyer question'))
        result = facade.send(incoming(), lambda: True)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(process.send_count, 0)
        facade, process = self.live()
        process.history['messages'].append(dict(messageId='seller-later', senderAliId=SELLER,
                                               timestamp=NOW + 1, content='A newer seller reply'))
        result = facade.send(incoming(), lambda: True)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(process.send_count, 0)

    def test_selected_and_all_guards_are_not_relaxed_by_incoming(self):
        for mode in ('selected', 'all'):
            value = incoming(frozen_scope=dict(mode=mode, conversation_ids=[CID]))
            self.reject(deepcopy(value), lambda: False)
            self.reject(dict(value, budget_reserved=False))
            facade, process = self.live()
            result = facade.send(value, lambda: True)
            self.assertEqual(result['status'], 'accepted', result)
            self.assertEqual(process.send_count, 1)
        for ids in ([], ['other-customer'], None, CID):
            self.reject(incoming(frozen_scope=dict(mode='selected', conversation_ids=ids)))
        self.reject(incoming(frozen_scope=dict(mode='unsupported', conversation_ids=[CID])))


if __name__ == '__main__':
    unittest.main()
