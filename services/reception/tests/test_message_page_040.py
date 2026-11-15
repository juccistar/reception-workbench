import unittest
from unittest.mock import patch

from reception_service import repository


class MessagePageTests(unittest.TestCase):
    def setUp(self):
        self.repo = repository.Repository(None, 'seller')
        for i in range(103):
            row = {'conversation_id': 'c', 'message_id': f'm{i:03}', 'timestamp_ms': None if i < 3 else i // 4}
            self.repo.put('message', row['message_id'], row)
        self.repo.put('message', 'other', {'conversation_id': 'other', 'message_id': 'other', 'timestamp_ms': 99})

    def tearDown(self):
        self.repo.close()

    def test_pages_match_existing_order_and_bound_decodes(self):
        baseline = sorted(self.repo.list('message', conversation_id='c'), key=lambda m: (m['timestamp_ms'] if m['timestamp_ms'] is not None else -1, m['message_id']))
        pages = []
        cursor = None
        decode = repository.json.loads
        with patch.object(repository.json, 'loads', wraps=decode) as loads:
            while True:
                page = self.repo.message_page('c', 11, cursor)
                if not page:
                    break
                pages[0:0] = page
                first = page[0]
                cursor = (first['timestamp_ms'] if first['timestamp_ms'] is not None else -1, first['message_id'])
            self.assertEqual(loads.call_count, 103)
        self.assertEqual(pages, baseline)
        with patch.object(repository.json, 'loads', wraps=decode) as loads:
            self.assertEqual(len(self.repo.message_page('c', 11)), 11)
            self.assertEqual(loads.call_count, 11)

    def test_reject_invalid_arguments(self):
        for conversation, limit, cursor in [('', 1, None), ('c', 0, None), ('c', 501, None), ('c', 1, (True, 'x')), ('c', 1, [1, 'x'])]:
            with self.subTest(conversation=conversation, limit=limit, cursor=cursor), self.assertRaises(ValueError):
                self.repo.message_page(conversation, limit, cursor)


if __name__ == '__main__':
    unittest.main()
