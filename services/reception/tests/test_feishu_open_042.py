import unittest

from reception_service.integrations.feishu_open import FeishuOpenError, FeishuOpenReader, parse_table_url, validate_schema


class FeishuOpenTests(unittest.TestCase):
    def test_url_restricts_host_and_table(self):
        self.assertEqual(parse_table_url('https://example.feishu.cn/base/Abc123?table=tbl123&view=vew123'), ('Abc123', 'tbl123'))
        for url in ('https://feishu.cn.evil.test/base/Abc?table=tbl123', 'http://feishu.cn/base/Abc?table=tbl123',
                    'https://example.feishu.cn/base/Abc?table=tbl123&table=tbl456',
                    'https://user@example.feishu.cn/base/Abc?table=tbl123',
                    'https://example.feishu.cn/wiki/Abc?table=tbl123'):
            with self.subTest(url=url), self.assertRaises(FeishuOpenError):
                parse_table_url(url)

    def test_reads_paginated_fields_without_writes(self):
        calls = []
        def transport(method, path, headers, body):
            calls.append((method, path, headers, body))
            if method == 'POST':
                return {'tenant_access_token': 'token'}
            if 'page_token=' not in path:
                return {'data': {'items': [{'field_name': '客户'}], 'has_more': True, 'page_token': 'next'}}
            return {'data': {'items': [{'field_name': '采购需求'}], 'has_more': False}}
        fields = FeishuOpenReader('cli_123', 'secret', transport).fields('https://example.feishu.cn/base/Abc?table=tbl123')
        self.assertEqual(len(fields), 2)
        self.assertEqual([call[0] for call in calls], ['POST', 'GET', 'GET'])
        self.assertTrue(all('secret' not in call[1] for call in calls))

    def test_validates_real_field_types_options_and_unique_names(self):
        mapping = {'buyer_id': {'name': '买家', 'type': 'text'},
                   'account': {'name': '账号', 'type': 'select'},
                   'status': {'name': '状态', 'type': 'select'},
                   'product_name': {'name': '需求', 'type': 'text'},
                   'number': {'name': '编号', 'type': 'auto_number'}}
        config = {'fields': mapping, 'account_option': '账号一', 'initial_status': '新建'}
        fields = [{'field_name': '买家', 'type': 1},
                  {'field_name': '账号', 'type': 3, 'property': {'options': [{'name': '账号一'}]}},
                  {'field_name': '状态', 'type': 3, 'property': {'options': [{'name': '新建'}]}},
                  {'field_name': '需求', 'type': 1}, {'field_name': '编号', 'type': 1005}]
        validate_schema(config, fields)
        with self.assertRaises(FeishuOpenError):
            validate_schema(config, fields + [fields[0]])
        with self.assertRaises(FeishuOpenError):
            validate_schema({**config, 'initial_status': '不存在'}, fields)
        with self.assertRaises(FeishuOpenError):
            validate_schema(config, fields[:-1] + [{'field_name': '编号', 'type': 1}])
        with self.assertRaises(FeishuOpenError):
            validate_schema({**config, 'fields': {k: v for k, v in mapping.items() if k != 'buyer_id'}}, fields)

    def test_repeated_page_is_rejected(self):
        def transport(method, path, headers, body):
            return {'tenant_access_token': 'token'} if method == 'POST' else {'data': {'items': [], 'has_more': True, 'page_token': 'same'}}
        with self.assertRaises(FeishuOpenError):
            FeishuOpenReader('cli_123', 'secret', transport).fields('https://feishu.cn/base/Abc?table=tbl123')
