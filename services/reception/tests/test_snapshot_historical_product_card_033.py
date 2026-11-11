"""Synthetic historical product-card projection and send safety contracts."""
from copy import deepcopy
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3
import unittest

import test_native_snapshot_time as fixtures
from test_integrations import BUYER, CID, NOW, SELLER, intent
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError

CARD = 'This is a single product card. Product name is 1234567890123'
LABEL = '[Card] Synthetic display label (invented for test)'


class HistoricalCardTests(unittest.TestCase):
    setUp = fixtures.SnapshotTimeTests.setUp
    live = fixtures.SnapshotTimeTests.live
    commands = fixtures.SnapshotTimeTests.commands

    def setup_history(self):
        facade, process = self.live()
        process.history['messages'].insert(0, dict(messageId='old-card', senderAliId=BUYER,
                                                  timestamp=NOW - 1000, content=CARD))
        process.snapshot['messages'].insert(0, dict(messageId='old-card', senderRole='buyer',
                                                   time=NOW - 1000, content=LABEL))
        return facade, process

    def reject(self, facade, process, code='snapshot_message_mismatch'):
        result = facade.send(intent(), lambda: True)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(result['error_code'], code, result)
        self.assertFalse(result['submission_started'])
        self.assertEqual(process.send_count, 0)

    def test_two_rounds_use_distinct_ledger_keys_and_preserve_ready(self):
        facade, process = self.live()
        process.history['messages'][0]['content'] = CARD
        process.snapshot['messages'][0]['content'] = CARD
        self.assertEqual(facade.send(intent(), lambda: True)['status'], 'accepted')
        sent = process.history['messages'][-1]
        process.history['messages'].append(dict(messageId='buyer-2', senderAliId=BUYER,
                                                timestamp=NOW + 200, content='Synthetic follow-up'))
        process.snapshot['messages'] = [dict(messageId='buyer-1', senderRole='buyer', content=LABEL, time=NOW),
            dict(messageId=sent['messageId'], senderRole='seller', content=sent['content'], time=sent['timestamp']),
            dict(messageId='buyer-2', senderRole='buyer', content='Synthetic follow-up', time=NOW + 200)]
        before = deepcopy(process.snapshot)
        original_run = process.run
        def unique_second_receipt(args, **kwargs):
            result = original_run(args, **kwargs)
            if list(args[1:4]) == ['workflow', 'chat-analysis', 'send-message']:
                process.history['messages'][-1]['messageId'] = 'synthetic-sent-2'
                result['results'][0]['messageID'] = 'synthetic-sent-2'
            return result
        process.run = unique_second_receipt
        result = facade.send(intent('send-synthetic-2', source_message_id='buyer-2'), lambda: True)
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(process.send_count, 2)
        self.assertEqual(process.snapshot, before)
        self.assertEqual(process.snapshot_before, process.snapshot_at_seal)
        with closing(sqlite3.connect(facade.native.ledger.path)) as db:
            keys = db.execute('SELECT logical_key FROM operations ORDER BY operation_id').fetchall()
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0][0], keys[1][0])

    def test_unrecognized_or_unsupported_content_is_rejected(self):
        for text, label, quote in [(CARD + ' ', LABEL, None), (CARD.replace('1234567890123', '0123456789012'), LABEL, None),
                                   (CARD.replace('1234567890123', '123456789012A'), LABEL, None),
                                   ('Ordinary historical text', LABEL, None), (CARD, '', None), (CARD, ' \r\n ', None),
                                   (CARD, {'display': LABEL}, None), (CARD, LABEL, 'quoted attachment'),
                                   ('[card]', LABEL, None), ('[card]   ', LABEL, None),
                                   ('[Card] Unexpected label', LABEL, None),
                                   ('This is a product card. The product is ', LABEL, None),
                                   (CARD, 'I accept your offer of $1', None),
                                   (CARD, 'Synthetic display label (invented for test)', None),
                                   (CARD, '[Card]   ', None), (CARD, '[Card]Unexpected label', None),
                                   (CARD, CARD.replace('1234567890123', '2234567890123'), None),
                                   ('[card] Synthetic title', '[card] Different title', None),
                                   ('This is a product card. The product is Synthetic title',
                                    'This is a product card. The product is Different title', None)]:
            with self.subTest(text=text, label=label, quote=quote):
                facade, process = self.setup_history()
                process.history['messages'][0]['content'] = text
                if quote:
                    process.history['messages'][0]['quoteMessage'] = quote
                process.snapshot['messages'][0]['content'] = label
                self.reject(facade, process)

    def test_explicit_card_marker_and_equal_descriptor_views(self):
        pairs = ((CARD, '[Card]'),
                 (CARD, '[card] 1234567890123'),
                 ('[card] Synthetic title', 'This is a product card. The product is Synthetic title'),
                 ('This is a product card. The product is Synthetic title', '[card] Synthetic title'))
        for canonical, display in pairs:
            with self.subTest(canonical=canonical, display=display):
                facade, process = self.setup_history()
                process.history['messages'][0]['content'] = canonical
                process.snapshot['messages'][0]['content'] = display
                before = deepcopy(process.snapshot)
                result = facade.send(intent(), lambda: True)
                self.assertEqual(result['status'], 'accepted', result)
                self.assertEqual(process.send_count, 1)
                self.assertEqual(process.snapshot, before)
                self.assertEqual(process.snapshot_before, process.snapshot_at_seal)

    def test_source_and_other_history_remain_strict(self):
        for scenario in ('source-text', 'source-card', 'source-legacy-card', 'source-catalog-card', 'old-text'):
            with self.subTest(scenario=scenario):
                facade, process = self.setup_history()
                if scenario == 'source-text':
                    process.snapshot['messages'][-1]['content'] = 'Changed source'
                elif scenario in ('source-card', 'source-legacy-card', 'source-catalog-card'):
                    descriptions = {'source-card': CARD, 'source-legacy-card': '[card] Synthetic transport card',
                                    'source-catalog-card': 'This is a product card. The product is Synthetic catalog title'}
                    process.history['messages'][-1]['content'] = descriptions[scenario]
                    process.snapshot['messages'][-1]['content'] = LABEL
                else:
                    process.history['messages'][0]['content'] = 'Ordinary text'
                self.reject(facade, process)

    def test_identity_role_duplicate_and_time_guards(self):
        for changes, code in [({'senderRole': 'seller'}, 'snapshot_message_mismatch'),
                              ({'from': 'seller'}, 'snapshot_message_mismatch'),
                              ({'senderAliId': SELLER}, 'snapshot_identity_mismatch'),
                              ({'sellerAliId': '30003'}, 'snapshot_identity_mismatch'),
                              ({'contactAliId': '30003'}, 'snapshot_identity_mismatch'),
                              ({'conversationId': 'wrong'}, 'snapshot_identity_mismatch'),
                              ({'time': 'invalid'}, 'snapshot_time_invalid')]:
            with self.subTest(changes=changes):
                facade, process = self.setup_history()
                process.snapshot['messages'][0].update(changes)
                self.reject(facade, process, code)
        facade, process = self.setup_history()
        process.snapshot['messages'].append(deepcopy(process.snapshot['messages'][0]))
        self.reject(facade, process, 'snapshot_message_ambiguous')

    def test_historical_projection_rejects_valid_but_wrong_time(self):
        wrong = NOW - 500000
        for value in (wrong, datetime.fromtimestamp(wrong / 1000, timezone.utc).isoformat()):
            with self.subTest(time=value):
                facade, process = self.setup_history()
                process.snapshot['messages'][0]['time'] = value
                self.reject(facade, process)

    def test_historical_projection_accepts_equivalent_aware_time(self):
        facade, process = self.setup_history()
        process.snapshot['messages'][0]['time'] = datetime.fromtimestamp((NOW - 1000) / 1000, timezone.utc).isoformat()
        before = deepcopy(process.snapshot)
        self.assertEqual(facade.send(intent(), lambda: True)['status'], 'accepted')
        self.assertEqual(process.send_count, 1)
        self.assertEqual(process.snapshot, before)
        self.assertEqual(process.snapshot_before, process.snapshot_at_seal)

    @staticmethod
    def display_time(timestamp, offset_minutes, precision='milliseconds'):
        value = datetime.fromtimestamp(timestamp / 1000, timezone.utc) + timedelta(minutes=offset_minutes)
        return value.replace(tzinfo=None).isoformat(timespec=precision)

    def test_naive_projected_time_is_bound_to_verified_source_clock(self):
        for offset in (0, 345, 480, 840, -720):
            for precision in ('seconds', 'milliseconds'):
                with self.subTest(offset=offset, precision=precision):
                    facade, process = self.setup_history()
                    source_time, card_time = NOW + 321, NOW - 1234
                    process.history['messages'][-1]['timestamp'] = source_time
                    process.history['messages'][0]['timestamp'] = card_time
                    process.snapshot['messages'][-1]['time'] = self.display_time(source_time, offset, precision)
                    process.snapshot['messages'][0]['time'] = self.display_time(card_time, offset, precision)
                    before = deepcopy(process.snapshot)
                    result = facade.send(intent(source_timestamp_ms=source_time), lambda: True)
                    self.assertEqual(result['status'], 'accepted', result)
                    self.assertEqual(process.send_count, 1)
                    self.assertEqual(process.snapshot, before)

    def test_inconsistent_naive_card_or_source_clock_is_rejected(self):
        for scenario in ('card-year', 'card-minute', 'card-millisecond', 'card-zone', 'source-year', 'source-range', 'source-absolute'):
            with self.subTest(scenario=scenario):
                facade, process = self.setup_history()
                process.snapshot['messages'][-1]['time'] = self.display_time(NOW, 480)
                process.snapshot['messages'][0]['time'] = self.display_time(NOW - 1000, 480)
                if scenario == 'card-year':
                    process.snapshot['messages'][0]['time'] = '2000-01-01T00:00:00'
                elif scenario == 'card-minute':
                    process.snapshot['messages'][0]['time'] = self.display_time(NOW - 1000 + 60000, 480)
                elif scenario == 'card-millisecond':
                    process.snapshot['messages'][0]['time'] = self.display_time(NOW - 999, 480)
                elif scenario == 'card-zone':
                    process.snapshot['messages'][0]['time'] = self.display_time(NOW - 1000, 420)
                elif scenario == 'source-year':
                    process.snapshot['messages'][-1]['time'] = '2000-01-01T00:00:00'
                    process.snapshot['messages'][0]['time'] = NOW - 1000
                elif scenario == 'source-range':
                    process.snapshot['messages'][-1]['time'] = self.display_time(NOW, 900)
                    process.snapshot['messages'][0]['time'] = self.display_time(NOW - 1000, 900)
                else:
                    process.snapshot['messages'][-1]['time'] = NOW
                self.reject(facade, process)

    def test_observed_card_formats_support_buyer_and_seller_history(self):
        descriptions = (CARD, '[card] Synthetic transport card',
                        'This is a product card. The product is Synthetic catalog title')
        for description in descriptions:
            for sender, role in ((BUYER, 'buyer'), (SELLER, 'seller')):
                with self.subTest(description=description, role=role):
                    facade, process = self.setup_history()
                    process.history['messages'][0].update(senderAliId=sender, content=description)
                    process.snapshot['messages'][0]['senderRole'] = role
                    before = deepcopy(process.snapshot)
                    result = facade.send(intent(), lambda: True)
                    self.assertEqual(result['status'], 'accepted', result)
                    self.assertEqual(process.send_count, 1)
                    self.assertEqual(process.snapshot, before)
                    self.assertEqual(process.snapshot_before, process.snapshot_at_seal)

    def test_projected_card_canonical_record_is_rechecked_before_submission(self):
        for change in ('body', 'time', 'role', 'attachment', 'removed'):
            with self.subTest(change=change):
                facade, process = self.setup_history()
                def mutate(command):
                    if command != 'seal-reply-preview':
                        return
                    row = process.history['messages'][0]
                    if change == 'body':
                        row['content'] = CARD.replace('1234567890123', '2234567890123')
                    elif change == 'time':
                        row['timestamp'] = NOW - 2000
                    elif change == 'role':
                        row['senderAliId'] = SELLER
                    elif change == 'attachment':
                        row['quoteMessage'] = 'Synthetic attachment changed after sealing'
                    else:
                        process.history['messages'].pop(0)
                process.stage_hook = mutate
                self.reject(facade, process, 'latest_source_changed')

    def test_private_default_closed_and_ambiguous_authority(self):
        facade, process = self.setup_history()
        history = facade.native._messages(process.history, SELLER, BUYER, CID)
        with self.assertRaises(AdapterError):
            facade.native._snapshot_messages(process.snapshot, SELLER, BUYER, CID, history)
        for modified in ([history[0], history[0], history[1]],
                         [replace(history[0], attachments=None), history[1]]):
            with self.assertRaises(AdapterError):
                facade.native._snapshot_messages(process.snapshot, SELLER, BUYER, CID, modified, source=history[1])

    def test_changed_final_source_and_unknown_fence(self):
        facade, process = self.setup_history()
        def change_source(command):
            if command == 'seal-reply-preview':
                process.history['messages'][-1]['content'] = 'Changed after snapshot'
        process.stage_hook = change_source
        self.reject(facade, process, 'latest_source_changed')
        facade, process = self.setup_history()
        process.send_status = 'timeout'
        self.assertEqual(facade.send(intent(), lambda: True)['status'], 'unknown')
        process.history['messages'].append(dict(messageId='buyer-2', senderAliId=BUYER,
                                                timestamp=NOW + 200, content='Follow-up'))
        result = facade.send(intent('send-synthetic-2', source_message_id='buyer-2'), lambda: True)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertEqual(process.send_count, 1)
