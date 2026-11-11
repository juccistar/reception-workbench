"""Ready snapshot time contracts using synthetic messages and no external execution."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reception_service.integrations import build_integrations
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError
from test_integrations import BUYER, CID, NOW, SELLER, MockProcess, intent, message


class SnapshotProcess(MockProcess):
    def __init__(self):
        super().__init__()
        self.snapshot = dict(conversationId=CID, messages=[dict(
            messageId='buyer-1', senderRole='buyer', content=message()['text'], time=NOW)])
        self.snapshot_before = None
        self.snapshot_at_seal = None
        self.identity_file = None

    def run(self, args, **kwargs):
        if list(args[1:4]) == ['workflow', 'chat-analysis', 'seal-reply-preview']:
            # The public CLI contract requires this delivery-relative location.
            request = Path(args[args.index('--request-file') + 1])
            if request.relative_to(self.root).as_posix() != '.runtime/chat-analysis/params/seal-reply-preview.json':
                raise AdapterError('Synthetic fixed request path mismatch', code='args', stage='cli')
        if list(args[1:3]) == ['workflow', 'chat-analysis'] and args[3] in ('seal-reply-preview', 'send-message'):
            if self.identity_file is None or not self.identity_file.is_file():
                raise AdapterError('Synthetic identity file no longer exists', code='identity_invalid', stage='identity')
        result = super().run(args, **kwargs)
        if list(args[1:3]) == ['workflow', 'chat-analysis']:
            if args[3] == 'resolve-id':
                self.identity_file = Path(args[args.index('--output-file') + 1])
            if args[3] == 'run':
                self.snapshot_before = json.dumps(dict(conversations=[self.snapshot]), ensure_ascii=False).encode('utf-8')
                (self.root / 'messages_ready.json').write_bytes(self.snapshot_before)
            elif args[3] == 'seal-reply-preview':
                self.snapshot_at_seal = (self.root / 'messages_ready.json').read_bytes()
        return result


class SnapshotTimeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='native-snapshot-time-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ('subprocess.run', 'subprocess.Popen', 'socket.create_connection', 'socket.socket'):
            guard = patch(target, side_effect=AssertionError('Real external execution forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        node = self.root / 'synthetic-node.exe'
        account = self.root / 'accounts' / 'synthetic'
        account.mkdir(parents=True)
        script, cli = account / 'synthetic-mcp.js', account / 'workctl.exe'
        for file in (node, script, cli):
            file.touch()
        os.environ.update(ACCIO_GATEWAY_TOKEN='synthetic-session-only', ACCIO_NODE_BIN=str(node), ACCIO_MCP_SCRIPT=str(script))
        self.settings = dict(workctl_path=str(cli), seller_id=SELLER)
        self.counter = 0

    def live(self):
        self.counter += 1
        facade = build_integrations('live', self.root / str(self.counter), self.settings)
        process = SnapshotProcess()
        facade.native.process = facade.native.feishu.process = process
        return facade, process

    def normalized(self, facade, process, history=None):
        if history is None:
            history = facade.native._messages(process.history, SELLER, BUYER, CID)
        return facade.native._snapshot_messages(process.snapshot, SELLER, BUYER, CID, history)

    def commands(self, process):
        return [call['args'][3] for call in process.calls if call['args'][1:3] == ['workflow', 'chat-analysis']]

    def rejected(self, facade, process, code):
        result = facade.send(intent(), lambda: True)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertIs(result['submission_started'], False)
        self.assertEqual(result['message_id'], '')
        self.assertEqual(result['error_code'], code)
        self.assertEqual(result['stage'], 'snapshot')
        self.assertEqual(result['diagnostics']['code'], code)
        self.assertEqual(result['diagnostics']['stage'], 'snapshot')
        self.assertEqual(process.send_count, 0)
        self.assertNotIn('seal-reply-preview', self.commands(process))
        self.assertNotIn('send-message', self.commands(process))
        stored = facade.native.ledger.get(intent()['operation_id'])[2]
        self.assertEqual(stored['error_code'], code)
        self.assertIs(stored['submission_started'], False)
        return result

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusive file-handle cleanup contract')
    def test_windows_cleanup_lock_does_not_override_a_successful_preview(self):
        facade, process = self.live()
        original = process.run
        def run(args, **kwargs):
            result = original(args, **kwargs)
            if list(args[1:4]) == ['workflow', 'chat-analysis', 'seal-reply-preview']:
                held = (process.root / 'messages_ready.json').open('rb')
                self.addCleanup(held.close)
            return result
        process.run = run
        result = facade.preview_send(intent())
        self.assertEqual(result['status'], 'passed', result)
        self.assertFalse(result['submission_started'])
        self.assertEqual(process.send_count, 0)

    def test_seal_cli_failure_keeps_exit_diagnostics_without_exposing_output(self):
        from reception_service.integrations.accio_native.vendor.adapters.common import Process
        from subprocess import CompletedProcess
        facade, process = self.live()
        original = process.run
        def run(args, **kwargs):
            if list(args[1:4]) == ['workflow', 'chat-analysis', 'seal-reply-preview']:
                fake = CompletedProcess(args, 2, '{"error":{"code":"invalid_request"}}', 'invalid argument PRIVATE_BODY')
                with patch('subprocess.run', return_value=fake):
                    return Process(facade.native.data_dir).run(args)
            return original(args, **kwargs)
        process.run = run
        result = facade.preview_send(intent())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'args')
        self.assertEqual(result['stage'], 'cli')
        self.assertEqual(result['diagnostics']['exit_code'], 2)
        self.assertTrue(result['diagnostics']['structured_error'])
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(process.send_count, 0)

    def test_numeric_ms_z_and_explicit_offsets_preserve_platform_instant(self):
        for millis in (NOW, NOW + 123):
            instant = datetime.fromtimestamp(millis / 1000, timezone.utc)
            values = (millis, float(millis), instant.isoformat().replace('+00:00', 'Z'),
                      instant.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat(),
                      instant.astimezone(timezone(timedelta(hours=-4))).isoformat())
            for value in values:
                with self.subTest(time=value):
                    facade, process = self.live()
                    process.history['messages'][0]['timestamp'] = millis
                    process.snapshot['messages'][0]['time'] = value
                    self.assertEqual(self.normalized(facade, process)[0].timestamp_ms, millis)
                    result = facade.send(intent(), lambda: True)
                    self.assertEqual(result['status'], 'accepted', result)
                    self.assertEqual(process.send_count, 1)
                    self.assertEqual(process.snapshot_before, process.snapshot_at_seal)

    def test_naive_latest_uses_exact_platform_id_without_guessing_its_zone(self):
        facade, process = self.live()
        instant = datetime.fromtimestamp(NOW / 1000, timezone(timedelta(hours=5, minutes=30)))
        process.snapshot['messages'][0]['time'] = instant.replace(tzinfo=None).isoformat()
        self.assertEqual(self.normalized(facade, process)[0].timestamp_ms, NOW)
        automatic = intent(send_mode='automatic', source_timestamp_ms=NOW, budget_reserved=True,
                           frozen_scope=dict(mode='incoming', seller_id=SELLER, started_at_ms=NOW))
        result = facade.send(automatic, lambda: True)
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(process.send_count, 1)
        self.assertEqual(process.snapshot_before, process.snapshot_at_seal)
        reads = [call for call in process.calls if call['args'][2:4] == ['call', 'query_conversation_msg']]
        self.assertEqual(len(reads), 3)  # Initial/final source checks plus exact sent-ID readback.

    def test_naive_historical_row_gets_its_own_platform_time(self):
        facade, process = self.live()
        historical_time = NOW - 3600123
        process.history['messages'].insert(0, dict(messageId='seller-old', senderAliId=SELLER,
                                                 timestamp=historical_time, content='Synthetic earlier reply'))
        process.snapshot['messages'].insert(0, dict(messageId='seller-old', senderRole='seller',
                                                   time='2026-11-06T07:00:00', content='Synthetic earlier reply'))
        normalized = self.normalized(facade, process)
        self.assertEqual([row.timestamp_ms for row in normalized], [historical_time, NOW])
        self.assertEqual(facade.send(intent(), lambda: True)['status'], 'accepted')
        self.assertEqual(process.send_count, 1)

    def test_from_only_latest_and_historical_messages_use_platform_times(self):
        facade, process = self.live()
        process.snapshot['messages'][0].pop('senderRole')
        process.snapshot['messages'][0].update({'from': 'buyer', 'time': '2026-11-06T08:00:00'})
        process.history['messages'].insert(0, dict(messageId='old', senderAliId=SELLER,
                                                 timestamp=NOW - 5000, content='Earlier'))
        process.snapshot['messages'].insert(0, {'messageId': 'old', 'from': 'seller',
                                                'time': '2026-11-06T07:00:00', 'content': 'Earlier'})
        self.assertEqual([m.timestamp_ms for m in self.normalized(facade, process)], [NOW - 5000, NOW])
        self.assertEqual(facade.send(intent(), lambda: True)['status'], 'accepted')

    def test_image_card_projection_for_historical_and_latest_preview_and_automatic(self):
        for latest in (False, True):
            for preview in (False, True):
                with self.subTest(latest=latest, preview=preview):
                    facade, process = self.live()
                    if latest:
                        process.history['messages'][0]['content'] = '[image]'
                        process.snapshot['messages'][0].update(content='[Card]', time='2026-11-06T08:00:00')
                    else:
                        process.history['messages'].insert(0, dict(messageId='image-old', senderAliId=SELLER,
                                                                  timestamp=NOW - 5000, content='[image]'))
                        process.snapshot['messages'].insert(0, dict(messageId='image-old', senderRole='seller',
                            content='[Card]', time='2026-11-06T07:00:00'))
                    self.assertEqual(self.normalized(facade, process)[0].text, '[image]')
                    automatic = intent(send_mode='automatic', source_timestamp_ms=NOW, budget_reserved=True,
                                       frozen_scope=dict(mode='incoming', seller_id=SELLER, started_at_ms=NOW))
                    result = facade.preview_send(automatic) if preview else facade.send(automatic, lambda: True)
                    self.assertEqual(result['status'], 'passed' if preview else 'accepted', result)
                    self.assertEqual(process.send_count, 0 if preview else 1)
                    self.assertEqual(process.snapshot_before, process.snapshot_at_seal)
                    self.assertIn(b'[Card]', process.snapshot_at_seal)

    def test_image_card_projection_rejects_other_content_and_identity(self):
        cases = [('[Card]', 'ordinary'), ('[Card]', '[Image]'), ('[Card]', ' [image]'),
                 ('[card]', '[image]'), (' [Card]', '[image]'), ('[Card] ', '[image]'),
                 ('[image]', '[Card]'), ({'type': 'Card'}, '[image]'),
                 (['[Card]'], '[image]')]
        for ready, platform in cases:
            with self.subTest(ready=ready, platform=platform):
                facade, process = self.live()
                process.history['messages'][0]['content'] = platform
                process.snapshot['messages'][0]['content'] = ready
                self.rejected(facade, process, 'snapshot_message_mismatch')
        for changes, code in (({'senderRole': 'seller'}, 'snapshot_message_mismatch'),
                              ({'from': 'seller'}, 'snapshot_message_mismatch'),
                              ({'senderAliId': SELLER}, 'snapshot_identity_mismatch'),
                              ({'sellerAliId': '30003'}, 'snapshot_identity_mismatch'),
                              ({'contactAliId': '30003'}, 'snapshot_identity_mismatch'),
                              ({'conversationId': SELLER + '-30003#icbu'}, 'snapshot_identity_mismatch'),
                              ({'messageId': 'other-id', 'time': '2026-11-06T08:00:00'}, 'snapshot_time_unverified')):
            with self.subTest(changes=changes):
                facade, process = self.live()
                process.history['messages'][0]['content'] = '[image]'
                process.snapshot['messages'][0].update(content='[Card]', **changes)
                self.rejected(facade, process, code)
        facade, process = self.live()
        process.history['messages'][0]['content'] = '[image]'
        process.snapshot['messages'][0]['content'] = '[Card]'
        original = facade.native._messages(process.history, SELLER, BUYER, CID)[0]
        for changes in ({'seller_id': '30003'}, {'buyer_id': '30003'},
                        {'conversation_id': SELLER + '-30003#icbu'}):
            with self.subTest(authority=changes):
                projected = self.normalized(facade, process, [replace(original, **changes)])
                self.assertEqual(projected[0].text, '[Card]')
        with self.assertRaises(AdapterError) as failure:
            self.normalized(facade, process, [original, original])
        self.assertEqual(failure.exception.code, 'snapshot_message_ambiguous')
        with self.assertRaises(AdapterError) as failure:
            self.normalized(facade, process, [replace(original, attachments=[dict(kind='native-content', content={})])])
        self.assertEqual(failure.exception.code, 'snapshot_message_mismatch')
        process.snapshot['messages'][0]['messageId'] = 'other-id'
        self.assertEqual(self.normalized(facade, process)[0].text, '[Card]')

    def test_image_card_projection_keeps_final_source_check(self):
        facade, process = self.live()
        process.history['messages'][0]['content'] = '[image]'
        process.snapshot['messages'][0]['content'] = '[Card]'
        def hook(stage):
            if stage == 'seal-reply-preview':
                process.history['messages'][0]['content'] = 'changed image'
        process.stage_hook = hook
        automatic = intent(send_mode='automatic', source_timestamp_ms=NOW, budget_reserved=True,
                           frozen_scope=dict(mode='incoming', seller_id=SELLER, started_at_ms=NOW))
        result = facade.send(automatic, lambda: True)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertFalse(result['submission_started'])
        self.assertEqual(process.send_count, 0)
        self.assertEqual(process.snapshot_before, process.snapshot_at_seal)

    def test_role_fields_fail_closed_before_preview_or_automatic_submission(self):
        cases = ({'from': 'seller', 'senderRole': 'buyer'}, {'from': 'unknown'},
                 {'from': 'INVALID_PRIVATE_ROLE'}, {'senderRole': 'unknown'},
                 {'senderRole': None}, {}, {'from': 'buyer', 'senderRole': 'INVALID_PRIVATE_ROLE'})
        for fields in cases:
            with self.subTest(fields=fields):
                facade, process = self.live()
                row = process.snapshot['messages'][0]
                row.pop('senderRole')
                row.update(fields)
                result = facade.preview_send(intent())
                self.assertEqual(result['error_code'], 'snapshot_message_mismatch')
                self.assertIs(result['submission_started'], False)
                self.assertNotIn('seal-reply-preview', self.commands(process))
                automatic = intent(send_mode='automatic', source_timestamp_ms=NOW, budget_reserved=True,
                                   frozen_scope=dict(mode='incoming', seller_id=SELLER, started_at_ms=NOW))
                result = facade.send(automatic, lambda: True)
                self.assertEqual(result['error_code'], 'snapshot_message_mismatch')
                self.assertIs(result['submission_started'], False)
                self.assertEqual(process.send_count, 0)
                self.assertNotIn('INVALID_PRIVATE_ROLE', json.dumps(result))

    def test_snapshot_diagnostics_are_filtered_through_service_and_job_filter(self):
        from reception_service.errors import ServiceError, safe_diagnostics
        from reception_service.integrations.accio_native.vendor.diagnostics import process_diagnostics
        facade, process = self.live()
        row = process.snapshot['messages'][0]
        row.update(time='2026-11-06T08:00:00', content='SECRET_BODY https://secret.invalid/token')
        result = self.rejected(facade, process, 'snapshot_message_mismatch')
        facts = result['diagnostics']
        self.assertEqual(facts['snapshot_difference'], 'content')
        self.assertTrue(facts['snapshot_role_equal'])
        self.assertFalse(facts['snapshot_content_equal'])
        self.assertEqual(facts['snapshot_message_id'], 'redacted')
        self.assertEqual(ServiceError('x', 'x', diagnostics=facts).diagnostics, facts)
        self.assertEqual(safe_diagnostics(facts), facts)
        private = 'SECRET_BODY https://secret.invalid/token'
        self.assertNotIn(private, json.dumps(result))
        forged = dict(snapshot_from=private, snapshot_difference=private, snapshot_content_length=True,
                      snapshot_role_equal=1, snapshot_message_id=private, extra=private)
        self.assertEqual(process_diagnostics(forged), {})
        self.assertEqual(safe_diagnostics(forged), {})

    def test_numeric_platform_id_and_both_lengths_survive_safe_diagnostic_filters(self):
        from reception_service.errors import safe_diagnostics
        from reception_service.integrations.accio_native.vendor.diagnostics import process_diagnostics
        facade, process = self.live()
        mid = '4397577601655.PNM'
        process.snapshot['messages'][0].update(messageId=mid, time='2026-11-06T08:00:00', content='Different synthetic body')
        history = facade.native._messages(process.history, SELLER, BUYER, CID)
        history = [replace(history[0], message_id=mid)]
        with self.assertRaises(AdapterError) as raised:
            self.normalized(facade, process, history)
        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics['snapshot_message_id'], mid)
        self.assertEqual(diagnostics['snapshot_platform_content_length'], len(message()['text']))
        self.assertEqual(diagnostics['snapshot_content_length'], len('Different synthetic body'))
        self.assertEqual(safe_diagnostics(diagnostics), diagnostics)
        self.assertEqual(process_diagnostics(diagnostics), diagnostics)
        for value in ('https://private.invalid/token', '123.PNM\nPRIVATE', '0', '1' * 33, True, 123):
            self.assertNotIn('snapshot_message_id', safe_diagnostics({'snapshot_message_id': value}))
        for value in (-1, 1000001, True, '24'):
            self.assertNotIn('snapshot_platform_content_length', safe_diagnostics({'snapshot_platform_content_length': value}))

    def test_naive_time_without_matching_authoritative_id_is_rejected(self):
        facade, process = self.live()
        process.snapshot['messages'][0].update(messageId='unverified-id', time='2026-11-06T08:00:00')
        result = self.rejected(facade, process, 'snapshot_time_unverified')
        self.assertIn('无法按消息编号核实平台时间', result['message'])

    def test_unmatched_naive_historical_row_is_not_assigned_latest_source_time(self):
        facade, process = self.live()
        process.snapshot['messages'].insert(0, dict(messageId='unknown-history', senderRole='seller',
                                                   content='Synthetic historical row', time='2026-11-01T08:00:00'))
        self.rejected(facade, process, 'snapshot_time_unverified')

    def test_same_id_from_another_seller_buyer_or_cid_cannot_supply_a_time(self):
        for changes in ({'seller_id': '30003'}, {'buyer_id': '30003'}, {'conversation_id': SELLER + '-30003#icbu'}):
            with self.subTest(changes=changes):
                facade, process = self.live()
                process.snapshot['messages'][0]['time'] = '2026-11-06T08:00:00'
                original = facade.native._messages(process.history, SELLER, BUYER, CID)[0]
                with self.assertRaises(AdapterError) as failure:
                    self.normalized(facade, process, [replace(original, **changes)])
                self.assertEqual(failure.exception.code, 'snapshot_time_unverified')

    def test_explicit_snapshot_identity_mismatch_is_rejected_even_when_id_matches(self):
        for location, key, value in (
                ('conversation', 'sellerAliId', '30003'), ('conversation', 'contactAliId', '30003'),
                ('row', 'conversationId', SELLER + '-30003#icbu'), ('row', 'sellerAliId', '30003'),
                ('row', 'contactAliId', '30003'), ('row', 'senderAliId', SELLER)):
            with self.subTest(location=location, key=key):
                facade, process = self.live()
                row = process.snapshot['messages'][0]
                row['time'] = '2026-11-06T08:00:00'
                (row if location == 'row' else process.snapshot)[key] = value
                self.rejected(facade, process, 'snapshot_identity_mismatch')

    def test_naive_time_fallback_requires_matching_direction_and_body(self):
        for changes in ({'senderRole': 'seller'}, {'senderRole': 'unknown'}, {'content': 'SYNTHETIC_PRIVATE_MISMATCH'}):
            with self.subTest(changes=changes):
                facade, process = self.live()
                process.snapshot['messages'][0].update(time='2026-11-06T08:00:00', **changes)
                result = self.rejected(facade, process, 'snapshot_message_mismatch')
                self.assertNotIn('SYNTHETIC_PRIVATE_MISMATCH', json.dumps(result))

    def test_explicit_time_historical_message_requires_matching_role_and_body(self):
        historical_time = NOW - 5000
        values = (historical_time, datetime.fromtimestamp(historical_time / 1000, timezone.utc).isoformat())
        for value in values:
            for changes, difference in (({'from': 'buyer'}, 'role'),
                                        ({'content': 'different'}, 'content'),
                                        ({'from': 'buyer', 'content': 'different'}, 'role_and_content')):
                with self.subTest(time=value, difference=difference):
                    facade, process = self.live()
                    process.history['messages'].insert(0, dict(messageId='old', senderAliId=SELLER,
                                                             timestamp=historical_time, content='actual'))
                    row = {'messageId': 'old', 'from': 'seller', 'time': value, 'content': 'actual'}
                    row.update(changes)
                    process.snapshot['messages'].insert(0, row)
                    result = self.rejected(facade, process, 'snapshot_message_mismatch')
                    self.assertEqual(result['diagnostics']['snapshot_difference'], difference)

    def test_duplicate_snapshot_or_authoritative_ids_are_ambiguous(self):
        facade, process = self.live()
        process.snapshot['messages'].append(deepcopy(process.snapshot['messages'][0]))
        self.rejected(facade, process, 'snapshot_message_ambiguous')
        facade, process = self.live()
        original = facade.native._messages(process.history, SELLER, BUYER, CID)[0]
        with self.assertRaises(AdapterError) as failure:
            self.normalized(facade, process, [original, replace(original, timestamp_ms=NOW - 1)])
        self.assertEqual(failure.exception.code, 'snapshot_message_ambiguous')

    def test_explicit_time_mismatch_is_not_overwritten_by_authoritative_lookup(self):
        for value in (NOW + 1, datetime.fromtimestamp((NOW + 1) / 1000, timezone.utc).isoformat()):
            with self.subTest(time=value):
                facade, process = self.live()
                process.snapshot['messages'][0]['time'] = value
                self.rejected(facade, process, 'snapshot_source_mismatch')

    def test_invalid_time_or_message_structure_fails_with_fixed_diagnostics(self):
        for value in (None, True, 0, -1, float('nan'), float('inf'), [], 'SYNTHETIC_PRIVATE_INVALID_TIME',
                      '2026-11-06T00:00:00.000001Z'):
            with self.subTest(time=value):
                facade, process = self.live()
                process.snapshot['messages'][0]['time'] = value
                result = self.rejected(facade, process, 'snapshot_time_invalid')
                self.assertNotIn('SYNTHETIC_PRIVATE_INVALID_TIME', json.dumps(result))
        for rows in (None, [None], [dict(time=NOW, content='Synthetic row')]):
            with self.subTest(rows=rows):
                facade, process = self.live()
                process.snapshot['messages'] = rows
                self.rejected(facade, process, 'snapshot_schema_invalid')

    def test_naive_preview_still_seals_without_claiming_or_submitting(self):
        facade, process = self.live()
        process.snapshot['messages'][0]['time'] = '2026-11-06T08:00:00'
        result = facade.preview_send(intent())
        self.assertEqual(result['status'], 'passed', result)
        self.assertIn('seal-reply-preview', self.commands(process))
        self.assertEqual(process.send_count, 0)
        self.assertIsNone(facade.native.ledger.get(intent()['operation_id']))
        self.assertEqual(process.snapshot_before, process.snapshot_at_seal)

    def test_preview_rejection_preserves_fixed_reason_without_a_claim(self):
        facade, process = self.live()
        process.snapshot['messages'][0].update(messageId='unknown', time='2026-11-06T08:00:00')
        result = facade.preview_send(intent())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'snapshot_time_unverified')
        self.assertEqual(result['stage'], 'snapshot')
        self.assertIn('无法按消息编号核实平台时间', result['message'])
        self.assertIsNone(facade.native.ledger.get(intent()['operation_id']))
        self.assertNotIn('seal-reply-preview', self.commands(process))

    def test_naive_fallback_does_not_bypass_final_source_identity_or_authorization_checks(self):
        for change in ('body', 'timestamp', 'new-message', 'identity', 'authorization'):
            with self.subTest(change=change):
                facade, process = self.live()
                process.snapshot['messages'][0]['time'] = '2026-11-06T08:00:00'
                def hook(stage):
                    if stage != 'seal-reply-preview':
                        return
                    if change == 'body':
                        process.history['messages'][0]['content'] = 'Synthetic edited source'
                    elif change == 'timestamp':
                        process.history['messages'][0]['timestamp'] += 1
                    elif change == 'new-message':
                        process.history['messages'].append(dict(messageId='buyer-new', senderAliId=BUYER,
                                                               timestamp=NOW + 1, content='Synthetic newer source'))
                    elif change == 'identity':
                        process.seller = '30003'
                    else:
                        facade.revoke()
                process.stage_hook = hook
                result = facade.send(intent(), lambda: True)
                self.assertEqual(result['status'], 'rejected', result)
                self.assertIs(result['submission_started'], False)
                self.assertIn('seal-reply-preview', self.commands(process))
                self.assertEqual(process.send_count, 0)
                if change == 'authorization':
                    self.assertEqual(result['error_code'], 'auth')
                    self.assertEqual(result['stage'], 'send')

    def test_process_failure_retains_only_allowlisted_diagnostics_and_unknown_is_not_retried(self):
        for failure_stage in ('run', 'send-message'):
            with self.subTest(stage=failure_stage):
                facade, process = self.live()
                private = 'SYNTHETIC_PRIVATE_OUTPUT_AND_TOKEN'
                def hook(stage):
                    if stage == failure_stage:
                        raise AdapterError(private, code='timeout', stage='cli', returncode=17,
                                           diagnostics=dict(elapsed_ms=23, stdout_present=True, stderr_present=True,
                                                            structured_error=False, native_error_category='io_timeout',
                                                            stderr=private, reply=private, token=private))
                process.stage_hook = hook
                result = facade.send(intent(), lambda: True)
                expected_status = 'unknown' if failure_stage == 'send-message' else 'rejected'
                self.assertEqual(result['status'], expected_status, result)
                self.assertIs(result['submission_started'], failure_stage == 'send-message')
                self.assertEqual(result['error_code'], 'timeout')
                self.assertEqual(result['stage'], 'cli')
                self.assertEqual(result['diagnostics'], dict(code='timeout', stage='cli', elapsed_ms=23,
                                 stdout_present=True, stderr_present=True, structured_error=False,
                                 native_error_category='io_timeout', exit_code=17))
                stored = facade.native.ledger.get(intent()['operation_id'])[2]
                self.assertNotIn(private, json.dumps(result))
                self.assertNotIn(private, json.dumps(stored))
                calls = len(process.calls)
                self.assertEqual(facade.send(intent(), lambda: True), result)
                self.assertEqual(len(process.calls), calls)

    def test_facade_does_not_forward_unrecognized_codes_or_free_text_diagnostics(self):
        facade, _ = self.live()
        private = 'SYNTHETIC_PRIVATE_NATIVE_TEXT'
        for code in (private, {'bad': private}, 'snapshot_time_unverified'):
            with self.subTest(code=code):
                receipt = facade._send_result(dict(status='rejected', submission_started=False, message=private,
                        detail=private, error_code=code, stage=private,
                        diagnostics=dict(stage=private, code=private, stderr=private, elapsed_ms=1)))
                self.assertNotIn(private, json.dumps(receipt))
                if code == 'snapshot_time_unverified':
                    self.assertEqual(receipt['stage'], 'adapter')
                    self.assertEqual(receipt['diagnostics'], dict(code=code, stage='adapter', elapsed_ms=1))


if __name__ == '__main__':
    unittest.main()
