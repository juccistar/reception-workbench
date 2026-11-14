import unittest

from reception_service.reply_messages import normalize_reply_output, reply_display_body, reply_messages_hash


class ReplyMessagesTests(unittest.TestCase):
    def test_order_sources_and_hash(self):
        messages = [{'body': 'First', 'reply_sources': ['m1', 'm2']},
                    {'body': 'Second', 'reply_sources': ['m2', 'm3']}]
        result = normalize_reply_output({'reply_messages': messages, 'other': 3})
        self.assertEqual(result['reply'], 'First\n\nSecond')
        self.assertEqual(result['reply_sources'], ['m1', 'm2', 'm3'])
        self.assertEqual(reply_display_body(messages), result['reply'])
        self.assertNotEqual(reply_messages_hash(messages), reply_messages_hash(list(reversed(messages))))
        self.assertEqual(result['other'], 3)
        self.assertIsNot(result['reply_messages'][0]['reply_sources'], messages[0]['reply_sources'])

    def test_invalid_arrays_never_fall_back(self):
        for messages in ([], [None], [{'body': ' ', 'reply_sources': []}],
                         [{'body': 'x', 'reply_sources': [1]}],
                         [{'body': 'x'}], [{'body': 'x', 'reply_sources': []}] * 4):
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                normalize_reply_output({'reply_messages': messages, 'reply': 'old'}, allow_legacy=True)
        with self.assertRaises(ValueError):
            normalize_reply_output({'reply': 'old'})

    def test_empty_legacy_and_budget(self):
        self.assertEqual(normalize_reply_output({'reply_messages': []}, allow_empty=True)['reply'], '')
        self.assertEqual(normalize_reply_output({'reply': 'old', 'reply_sources': ['m']}, allow_legacy=True)['reply_messages'][0]['body'], 'old')
        self.assertEqual(len(normalize_reply_output({'reply_messages': [{'body': 'x' * 1200, 'reply_sources': []}]})['reply']), 1200)
        with self.assertRaises(ValueError):
            normalize_reply_output({'reply_messages': [{'body': 'x' * 600, 'reply_sources': []}, {'body': 'x' * 601, 'reply_sources': []}]})


if __name__ == '__main__':
    unittest.main()
