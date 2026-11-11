import copy
import unittest
from unittest.mock import Mock

from reception_service.errors import ServiceError
from reception_service.integrations.accio_native.gateway import NativeGateway


URL = 'https://clouddisk.alibaba.com/picture.htm?token=secret'
CID = '1-2#chat'
EXPECTED = dict(seller_id='1', buyer_id='2', conversation_id=CID, message_id='m1',
                timestamp_ms=1000, role='buyer', own=False, text='[image]', attachments=[])
ORDINARY = dict(messages=[dict(senderAliId='2', timestamp=1000, content='[image]', messageId='m1')],
                hasMore=False, nextPointTimeStamp=None)
AIMODE = dict(messages=[dict(senderAliId='2', timestamp=1000, content='[image] ' + URL, fileInfo=None)],
              hasMore=False, nextPointTimeStamp=None)


class PhotoSourceTests(unittest.TestCase):
    def setUp(self):
        self.gateway = object.__new__(NativeGateway)
        self.gateway._identity = Mock(return_value=('1', 'sha'))
        self.ordinary = copy.deepcopy(ORDINARY)
        self.aimode = copy.deepcopy(AIMODE)
        self.gateway._read = Mock(side_effect=lambda name, request: copy.deepcopy(
            self.ordinary if name == 'query_conversation_msg' else self.aimode))

    def lookup(self, expected=None):
        return self.gateway.read_photo_source(copy.deepcopy(expected if expected is not None else EXPECTED))

    def test_observed_shape_and_primary_read(self):
        before = self.gateway.read_message_page('1', '2', CID)
        result = self.lookup()
        after = self.gateway.read_message_page('1', '2', CID)
        self.assertEqual(before, after)
        self.assertEqual(before['messages'][0], EXPECTED)
        self.assertEqual(result, dict(resource_url=URL, source='query_conversation_msg_for_aimode',
                                      association='unique_sender_timestamp'))
        self.assertEqual(self.gateway._read.call_args_list[1].args[1], dict(
            conversationId=CID, selfAliId=1, domain='icbu', count=100, limitTimeStamp=1001, forward=False))

    def test_mutated_ordinary(self):
        for key, value in [('content', 'changed'), ('messageId', 'other'), ('senderAliId', '1'),
                           ('timestamp', 999), ('quoteMessage', {'content': 'changed'})]:
            with self.subTest(key=key):
                self.ordinary = copy.deepcopy(ORDINARY)
                self.ordinary['messages'][0][key] = value
                with self.assertRaises(ServiceError):
                    self.lookup()

    def test_identity_mismatch(self):
        for field, value in [('conversationId', '1-3#chat'), ('sellerAliId', '3'), ('contactAliId', '3')]:
            for page in ('ordinary', 'aimode'):
                with self.subTest(field=field, page=page):
                    self.ordinary, self.aimode = copy.deepcopy(ORDINARY), copy.deepcopy(AIMODE)
                    getattr(self, page)['messages'][0][field] = value
                    with self.assertRaises(ServiceError):
                        self.lookup()
        self.gateway._identity.side_effect = ValueError('secret')
        with self.assertRaises(ServiceError) as error:
            self.lookup()
        self.assertNotIn('secret', str(error.exception))

    def test_duplicate_ties(self):
        for page in ('ordinary', 'aimode'):
            for content in ('same', 'other'):
                with self.subTest(page=page, content=content):
                    self.ordinary, self.aimode = copy.deepcopy(ORDINARY), copy.deepcopy(AIMODE)
                    rows = getattr(self, page)['messages']
                    duplicate = copy.deepcopy(rows[0])
                    if content == 'other':
                        duplicate['content'] = 'ordinary text'
                    rows.append(duplicate)
                    with self.assertRaises(ServiceError):
                        self.lookup()

    def test_incomplete_and_malformed_pages(self):
        for page in ('ordinary', 'aimode'):
            for mutation in (dict(hasMore=True, nextPointTimeStamp=1000),
                             dict(hasMore=True, nextPointTimeStamp=999),
                             dict(hasMore=True, nextPointTimeStamp=None),
                             dict(hasMore='false'), dict(nextPointTimeStamp=1001),
                             dict(messages='bad')):
                with self.subTest(page=page, mutation=mutation):
                    self.ordinary, self.aimode = copy.deepcopy(ORDINARY), copy.deepcopy(AIMODE)
                    getattr(self, page).update(mutation)
                    with self.assertRaises(ServiceError):
                        self.lookup()

    def test_invalid_urls(self):
        for url in ('http://clouddisk.alibaba.com/a', 'https://clouddisk.alibaba.com.evil/a',
                    'https://x.alicdn.com.evil/a', 'https://user@clouddisk.alibaba.com/a',
                    'https://127.0.0.1/a', 'https://clouddisk.alibaba.com:444/a',
                    'https://clouddisk.alibaba.com/a#x', 'https://clouddisk.alibaba.com/a%0a',
                    URL + ' https://clouddisk.alibaba.com/b'):
            with self.subTest(url=url):
                self.aimode['messages'][0]['content'] = '[image] ' + url
                with self.assertRaises(ServiceError) as error:
                    self.lookup()
                self.assertNotIn(url, str(error.exception))

    def test_bad_timestamp_and_conflicting_ids(self):
        for page in ('ordinary', 'aimode'):
            for key, value in [('timestamp', True), ('timestamp', 0), ('timestamp', 1000.5),
                               ('messageId', 'different'), ('senderRole', 'seller')]:
                with self.subTest(page=page, key=key, value=value):
                    self.ordinary, self.aimode = copy.deepcopy(ORDINARY), copy.deepcopy(AIMODE)
                    getattr(self, page)['messages'][0][key] = value
                    with self.assertRaises(ServiceError):
                        self.lookup()
        for change in (dict(timestamp_ms=0), dict(conversation_id='1-3#chat'),
                       dict(seller_id='3'), dict(message_id='missing-id-fake')):
            with self.subTest(change=change):
                with self.assertRaises(ServiceError):
                    self.lookup(dict(EXPECTED, **change))

    def test_placeholder_whitespace_preserves_exact_raw_evidence(self):
        self.ordinary['messages'][0]['content'] = ' [image] '
        self.assertEqual(self.lookup(dict(EXPECTED, text=' [image] '))['resource_url'], URL)
        with self.assertRaises(ServiceError):
            self.lookup(EXPECTED)

    def test_terminal_zero_cursor_and_non_promised_page_order(self):
        self.ordinary['nextPointTimeStamp'] = self.aimode['nextPointTimeStamp'] = 0
        self.assertEqual(self.lookup()['resource_url'], URL)
        for page in (self.ordinary, self.aimode):
            page.update(hasMore=True, nextPointTimeStamp=900)
            page['messages'].insert(0, dict(senderAliId='2', timestamp=900, content='earlier', messageId='earlier'))
        self.assertEqual(self.lookup()['resource_url'], URL)

    def test_valid_cdn(self):
        self.aimode['messages'][0]['content'] = '[image]https://img.alicdn.com/path'
        self.assertEqual(self.lookup()['resource_url'], 'https://img.alicdn.com/path')


if __name__ == '__main__':
    unittest.main()
