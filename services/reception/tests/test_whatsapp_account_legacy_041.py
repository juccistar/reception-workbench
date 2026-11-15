import unittest

from reception_service.whatsapp_account_legacy import LegacyBlocked, backup_rehearsal, rehearse_import, restore_rehearsal


class LegacyRehearsalTest(unittest.TestCase):
    def setUp(self):
        self.op = {'source': 'ledger', 'account_id': 'a', 'chat_id': 'c', 'operation_id': 'o',
                   'status': 'unknown', 'body': 'original', 'message_id': None}

    def make(self):
        return rehearse_import(account_id='a', declared_sources=['ledger'], source_bytes={'ledger': b'ledger\x00bytes\n'}, operations=[self.op])

    def test_round_trip_preserves_exact_bytes_and_unknown(self):
        original = self.make()
        restored = restore_rehearsal(backup_rehearsal(original), target_empty=True)
        self.assertEqual(restored, original)
        self.assertFalse(restored['reception_enabled'])
        self.assertEqual(restored['operations'][0]['status'], 'unknown')

    def test_missing_and_conflicting_sources_block(self):
        with self.assertRaises(LegacyBlocked):
            rehearse_import(account_id='a', declared_sources=['ledger', 'missing'], source_bytes={'ledger': b'x'}, operations=[])
        with self.assertRaises(LegacyBlocked):
            rehearse_import(account_id='a', declared_sources=['ledger'], source_bytes={'ledger': b'x'},
                           operations=[self.op, dict(self.op, body='different')])

    def test_tampering_and_nonempty_restore_block(self):
        backup = backup_rehearsal(self.make())
        with self.assertRaises(LegacyBlocked):
            restore_rehearsal(backup, target_empty=False)
        backup['operations'][0]['status'] = 'accepted'
        with self.assertRaises(LegacyBlocked):
            restore_rehearsal(backup, target_empty=True)


if __name__ == '__main__':
    unittest.main()
