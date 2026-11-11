"""Synthetic workflow envelopes, private durable receipts and exact-ID recovery."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_integrations as fixture
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError
from reception_service.integrations.accio_native.vendor.adapters.send_receipts import inventory, stdout_evidence


class WorkflowReceiptTests(unittest.TestCase):
    setUp = fixture.IntegrationTests.setUp
    live = fixture.IntegrationTests.live

    def setup_operation(self, *, baseline=False):
        facade, process = self.live()
        request = fixture.intent()
        result = dict(status='unknown', message_id='', submission_started=True)
        if baseline:
            result.update(receipt_baseline=inventory(facade.native), receipt_submission_ns=time.time_ns())
        facade.native.ledger.claim(request['operation_id'], 'synthetic-receipt', 'send', request)
        facade.native.ledger.finish(request['operation_id'], result)
        return facade, process, request

    def receipt(self, facade, process, request, *, key='chat-send-v1-synthetic', mid='synthetic-recovered', **changes):
        root = facade.native.data_dir / 'workctl-state/chat-analysis/send-receipts/synthetic/customer'
        root.mkdir(parents=True, exist_ok=True)
        file = root / (key + '.json')
        data = dict(schema='chat_analysis_send_receipt_v1', status='sent', idempotencyKey=key,
            sellerAliId=fixture.SELLER, contactAliId=fixture.BUYER, conversationID=fixture.CID,
            contentSha256=hashlib.sha256(request['reply'].encode('utf-8')).hexdigest(), messageID=mid)
        data.update(changes)
        file.write_text(json.dumps(data), encoding='utf-8')
        # Model a completed post-submit write, independent of test filesystem granularity.
        stamp = time.time_ns() + 10000000
        os.utime(file, ns=(stamp, stamp))
        process.history['messages'].append(dict(messageId=mid, senderAliId=fixture.SELLER,
            timestamp=fixture.NOW + 100, content=request['reply']))
        return file

    def test_batch_status_and_item_status_do_not_conflict(self):
        facade, process = self.live()
        original = process.run
        def run(args, **kwargs):
            result = original(args, **kwargs)
            if args[3] == 'send-message':
                result['status'] = 'partial_success'
                result['results'].append(dict(conversationID='different-conversation', status='send_rejected'))
            return result
        process.run = run
        result = facade.send(fixture.intent(), lambda: True)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['timestamp_ms'], fixture.NOW + 100)
        self.assertEqual(process.send_count, 1)

    def test_timeout_after_native_sent_recovers_without_second_submit(self):
        facade, process = self.live()
        original = process.run
        request = fixture.intent()
        def run(args, **kwargs):
            if args[3] == 'send-message':
                process.send_count += 1
                self.receipt(facade, process, request)
                raise AdapterError('synthetic failure', code='timeout', stage='cli')
            return original(args, **kwargs)
        process.run = run
        result = facade.send(request, lambda: True)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['message_id'], 'synthetic-recovered')
        self.assertEqual(result['timestamp_ms'], fixture.NOW + 100)
        self.assertEqual(facade.send(request, lambda: True)['status'], 'accepted')
        self.assertEqual(process.send_count, 1)

    def test_exact_id_survives_failed_readback_and_later_reconcile(self):
        facade, process = self.live()
        original = process.run
        def run(args, **kwargs):
            if len(args) > 3 and args[2:4] == ['call', 'query_conversation_msg'] and process.send_count:
                raise AdapterError('synthetic read failure')
            return original(args, **kwargs)
        process.run = run
        result = facade.send(fixture.intent(), lambda: True)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['message_id'], 'native-synthetic-sent-1')
        process.run = original
        result = facade.reconcile_operation(fixture.intent()['operation_id'])
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['timestamp_ms'], fixture.NOW + 100)
        self.assertEqual(process.send_count, 1)

    def test_explicit_legacy_receipt_verifies_id_and_preserves_file(self):
        facade, process, request = self.setup_operation()
        file = self.receipt(facade, process, request)
        before = file.read_bytes()
        process.history['messages'].append(dict(messageId='buyer-second', senderAliId=fixture.BUYER,
            timestamp=fixture.NOW + 200, content='Synthetic follow-up'))
        result = facade.native.reconcile(request['operation_id'], receipt_ref=str(file), expected_mid='synthetic-recovered')
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['timestamp_ms'], fixture.NOW + 100)
        self.assertEqual(file.read_bytes(), before)
        self.assertEqual(facade.reconcile_operation(request['operation_id'])['status'], 'accepted')
        self.assertEqual(process.send_count, 0)

    def test_old_same_body_receipt_without_binding_is_never_guessed(self):
        facade, process, request = self.setup_operation()
        self.receipt(facade, process, request)
        self.assertEqual(facade.reconcile_operation(request['operation_id'])['status'], 'unknown')
        self.assertEqual(process.send_count, 0)

    def test_unchanged_receipt_in_baseline_is_never_claimed(self):
        facade, process, request = self.setup_operation()
        self.receipt(facade, process, request)
        result = facade.native.ledger.get(request['operation_id'])[2]
        result.update(receipt_baseline=inventory(facade.native), receipt_submission_ns=time.time_ns())
        facade.native.ledger.finish(request['operation_id'], result)
        self.assertEqual(facade.reconcile_operation(request['operation_id'])['status'], 'unknown')
        self.assertEqual(process.send_count, 0)

    def test_two_new_matching_receipts_remain_ambiguous(self):
        facade, process, request = self.setup_operation(baseline=True)
        self.receipt(facade, process, request)
        self.receipt(facade, process, request, key='chat-send-v1-other', mid='synthetic-other')
        self.assertEqual(facade.reconcile_operation(request['operation_id'])['status'], 'unknown')

    def test_wrong_identity_hash_status_id_or_missing_expected_id_keeps_unknown(self):
        changes = [dict(sellerAliId='99999'), dict(contactAliId='99999'), dict(conversationID='elsewhere'),
                   dict(contentSha256='0' * 64), dict(status='outcome_unknown'), dict(schema='unknown'),
                   dict(idempotencyKey='wrong-file-key')]
        for change in changes:
            with self.subTest(change=change):
                facade, process, request = self.setup_operation()
                file = self.receipt(facade, process, request, **change)
                self.assertEqual(facade.native.reconcile(request['operation_id'], receipt_ref=str(file),
                    expected_mid='synthetic-recovered')['status'], 'unknown')
                self.assertEqual(process.send_count, 0)
        facade, process, request = self.setup_operation()
        file = self.receipt(facade, process, request)
        for expected in ('wrong-id', None):
            self.assertEqual(facade.native.reconcile(request['operation_id'], receipt_ref=str(file),
                expected_mid=expected)['status'], 'unknown')

    def test_wrong_platform_body_or_direction_remains_unknown_with_id(self):
        for change in (dict(content='different body'), dict(senderAliId=fixture.BUYER), dict(timestamp=fixture.NOW - 1)):
            with self.subTest(change=change):
                facade, process, request = self.setup_operation()
                file = self.receipt(facade, process, request)
                process.history['messages'][-1].update(change)
                result = facade.native.reconcile(request['operation_id'], receipt_ref=str(file), expected_mid='synthetic-recovered')
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(result['message_id'], 'synthetic-recovered')
                self.assertEqual(process.send_count, 0)

    def test_receipt_outside_private_root_is_not_read(self):
        facade, process, request = self.setup_operation()
        outside = self.root / 'outside.json'
        outside.write_text('{}', encoding='utf-8')
        self.assertEqual(facade.native.reconcile(request['operation_id'], receipt_ref=str(outside),
            expected_mid='synthetic-recovered')['status'], 'unknown')

    def test_conflicting_returned_ids_are_not_merged(self):
        with self.assertRaises(AdapterError):
            stdout_evidence(dict(status='completed', results=[dict(conversationID=fixture.CID, messageID='one'),
                dict(conversationID=fixture.CID, messageID='two')]), fixture.CID)
        self.assertEqual(stdout_evidence(dict(results=[dict(conversationID='other', messageID='elsewhere'),
            dict(conversationID=fixture.CID, messageID='one')]), fixture.CID), dict(message_id='one'))
