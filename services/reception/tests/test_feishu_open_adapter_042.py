import json
import hashlib
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reception_service.integrations.feishu_open_adapter import FeishuOpenAdapter
from reception_service.integrations.accio_native.vendor.adapters.common import Ledger
from reception_service.integrations.accio_native.vendor.models import FACT_LABELS, Message
from reception_service.integrations.accio_native.gateway import NativeGateway
from reception_service.context import REQUIREMENT_FIELDS


class Transport:
    def __init__(self):
        self.rows = {}
        self.posts = 0
        self.drop = False
        self.fail_read = False
        self.calls = []
        self.fields = [dict(field_name=name, type=kind, **({'property': {'options': [{'name': option}]}} if option else {})) for name, kind, option in [
            ('Buyer', 1, None), ('Account', 3, 'A'), ('Status', 3, 'New'), ('Product', 1, None), ('Number', 1005, None)]]

    def __call__(self, method, path, headers, body):
        self.calls.append((method, path))
        if '/auth/' in path:
            return {'code': 0, 'tenant_access_token': 'token'}
        if '/fields?' in path:
            return {'code': 0, 'data': {'items': self.fields, 'has_more': False}}
        if method == 'GET' and '?' in path:
            return {'code': 0, 'data': {'items': list(self.rows.values()), 'has_more': False}}
        if method == 'GET':
            if self.fail_read:
                raise TimeoutError()
            return {'code': 0, 'data': {'record': self.rows[path.rsplit('/', 1)[-1]]}}
        if '/medias/upload_all' in path:
            return {'code': 0, 'data': {'file_token': 'file123'}}
        if method in ('POST', 'PUT'):
            self.posts += 1
            rid = 'rec' + str(self.posts) if method == 'POST' else path.rsplit('/', 1)[-1]
            fields = json.loads(body)['fields']
            fields['Number'] = 'N' + rid
            self.rows[rid] = {'record_id': rid, 'fields': fields}
            if self.drop:
                raise TimeoutError()
            return {'code': 0, 'data': {'record': self.rows[rid]}}
        raise AssertionError(path)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(Path(self.temp.name) / 'ledger.sqlite3')
        self.transport = Transport()
        self.settings = {'seller_id': '100', 'feishu': {'enabled': True, 'write_enabled': True, 'app_id': 'cli_A', 'table_url': 'https://example.feishu.cn/base/Base1?table=tbl1', 'account_option': 'A', 'initial_status': 'New', 'fields': {role: {'name': name, 'type': kind} for role, name, kind in [('buyer_id', 'Buyer', 'text'), ('account', 'Account', 'select'), ('status', 'Status', 'select'), ('product_name', 'Product', 'text'), ('number', 'Number', 'auto_number')]}}}
        self.adapter = FeishuOpenAdapter(self.settings, self.temp.name, None, self.ledger, transport=self.transport)
        self.adapter.configure_secret('cli_A', 'SECRET_VALUE')
        self.payload = {'operation_id': 'op1', 'case': {'seller_id': '100', 'buyer_id': '200', 'case_id': 'case1', 'version': 1, 'facts': {'product': {'value': 'widget', 'evidence_ids': ['m1']}}}}

    def test_create_readback_and_no_secret_in_ledger(self):
        result = self.adapter.register(self.payload)
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['record_id'], 'rec1')
        self.assertEqual(self.adapter.register(self.payload), result)
        self.assertEqual(self.transport.posts, 1)
        self.assertNotIn(b'SECRET_VALUE', self.ledger.path.read_bytes())

    def test_lost_create_response_never_searches_or_creates_again(self):
        self.transport.drop = True
        result = self.adapter.register(self.payload)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.adapter.register(self.payload)['status'], 'unknown')
        old = self.ledger.get('op1')
        before = len(self.transport.calls)
        self.assertEqual(self.adapter.reconcile('op1', old[1], old[2])['status'], 'unknown')
        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual(self.transport.posts, 1)

    def test_original_target_after_switch_and_exact_readback(self):
        self.transport.fail_read = True
        result = self.adapter.register(self.payload)
        self.assertEqual(result['record_id'], 'rec1')
        self.settings['feishu']['table_url'] = 'https://example.feishu.cn/base/Base2?table=tbl2'
        self.settings['feishu']['app_id'] = 'cli_B'
        self.transport.fail_read = False
        old = self.ledger.get('op1')
        result = self.adapter.reconcile('op1', old[1], old[2])
        self.assertEqual(result['status'], 'confirmed')
        self.assertIn('/apps/Base1/tables/tbl1/records/rec1', self.transport.calls[-1][1])

    def test_wrong_buyer_or_account_does_not_block_distinct_record(self):
        self.transport.rows['recOld'] = {'record_id': 'recOld', 'fields': {'Buyer': '999', 'Account': 'A', 'Status': 'New', 'Product': FACT_LABELS['product'] + '：widget', 'Number': 'old'}}
        self.transport.rows['recOther'] = {'record_id': 'recOther', 'fields': {'Buyer': '200', 'Account': 'B', 'Status': 'New', 'Product': FACT_LABELS['product'] + '：widget', 'Number': 'old2'}}
        self.assertEqual(self.adapter.register(self.payload)['status'], 'confirmed')
        self.assertEqual(self.transport.posts, 1)

    def test_legacy_unknown_never_routed_or_replaced(self):
        request = {'payload': self.payload}
        self.ledger.claim('op1', 'legacy', 'register', request)
        self.assertEqual(self.adapter.register(self.payload)['status'], 'unknown')
        old = self.ledger.get('op1')
        self.assertEqual(self.adapter.reconcile('op1', old[1], old[2])['status'], 'unknown')
        self.assertEqual(self.transport.posts, 0)
        self.assertEqual(self.ledger.get('op1'), old)

    def test_exact_readback_rejects_wrong_account(self):
        self.transport.fail_read = True
        self.adapter.register(self.payload)
        self.transport.fail_read = False
        self.transport.rows['rec1']['fields']['Account'] = 'other'
        old = self.ledger.get('op1')
        self.assertEqual(self.adapter.reconcile('op1', old[1], old[2])['status'], 'unknown')
        self.assertEqual(self.ledger.get('op1')[2]['status'], 'unknown')

    def test_evidenced_attachment_upload_and_readback(self):
        self.settings['feishu']['fields']['main_image'] = {'name': 'Image', 'type': 'attachment'}
        self.settings['feishu']['registration'] = {'allowed_image_hosts': ['example.com']}
        self.transport.fields.append({'field_name': 'Image', 'type': 17})
        image = {'kind': 'main_image', 'source_message_id': 'm1', 'url': 'https://example.com/image.png'}
        case = self.payload['case']
        case.update(conversation_id='conv', source_message_ids=['m1'], attachments=[image])
        self.payload['verified_messages'] = [{'message_id': 'm1', 'role': 'buyer', 'seller_id': '100', 'buyer_id': '200', 'conversation_id': 'conv', 'text': 'widget', 'attachments': [image]}]
        with patch('reception_service.integrations.feishu_open_adapter.download_image', return_value=b'\x89PNG\r\n\x1a\nimage'):
            result = self.adapter.register(self.payload)
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(self.transport.rows['rec1']['fields']['Image'], [{'file_token': 'file123'}])
        self.assertTrue(any('/medias/upload_all' in path for _, path in self.transport.calls))

    def ai_case(self):
        text = 'I need 20 pieces of blue widgets.'
        digest = hashlib.sha256(text.encode()).hexdigest()
        requirements = {key: {'value': [] if key == 'product_images' else None, 'evidence_ids': []} for key in REQUIREMENT_FIELDS}
        requirements['product'] = {'value': '蓝色部件', 'evidence_ids': ['m1'], 'source_excerpts': {'m1': 'blue widgets'}, 'evidence_versions': {'m1': digest}}
        requirements['quantity'] = {'value': '20件', 'evidence_ids': ['m1'], 'source_excerpts': {'m1': '20 pieces'}, 'evidence_versions': {'m1': digest}}
        self.payload['case'].update(requirements_format='ai-v1', requirements=requirements, facts={}, conversation_id='conv', inquiry_source_id='m1')
        message = Message('m1', '100', '200', 'conv', 'buyer', text, 1700000000000)
        gateway = NativeGateway.__new__(NativeGateway)
        gateway._permission_lock, gateway._permission_epoch = threading.Lock(), 0
        gateway.lock = threading.RLock()
        gateway.ledger, gateway.feishu = self.ledger, self.adapter
        gateway.history = lambda *args: [message]
        return gateway, message

    def test_ai_v1_gateway_confirmed_with_evidenced_quantity(self):
        gateway, _ = self.ai_case()
        self.settings['feishu']['fields']['quantity'] = {'name': 'Quantity', 'type': 'number'}
        self.transport.fields.append({'field_name': 'Quantity', 'type': 2})
        result = gateway.register(self.payload)
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(self.transport.rows['rec1']['fields']['Quantity'], 20)
        self.assertIn('蓝色部件', self.transport.rows['rec1']['fields']['Product'])

    def test_ai_v1_gateway_create_and_unknown_replay(self):
        gateway, _ = self.ai_case()
        self.transport.drop = True
        result = gateway.register(self.payload)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.transport.posts, 1)
        self.assertIn('蓝色部件', self.transport.rows['rec1']['fields']['Product'])
        self.assertIn('20件', self.transport.rows['rec1']['fields']['Product'])
        gateway.history = lambda *args: self.fail('replay must not fetch history')
        self.assertEqual(gateway.register(self.payload)['status'], 'unknown')
        self.assertEqual(self.transport.posts, 1)

    def test_ai_v1_changed_source_or_wrong_buyer_rejected(self):
        gateway, message = self.ai_case()
        message.text += ' changed'
        self.assertEqual(gateway.register(self.payload)['status'], 'rejected')
        self.assertEqual(self.transport.posts, 0)
        message.text = 'I need 20 pieces of blue widgets.'
        message.buyer_id = '999'
        self.assertEqual(gateway.register(self.payload)['status'], 'rejected')
        self.assertEqual(self.transport.posts, 0)

    def test_credential_reference_survives_restart_and_never_falls_back(self):
        self.settings['feishu']['credential_ref'] = 'ref-old'
        self.adapter.configure_secret('cli_A', 'OLD_SECRET', credential_ref='ref-old')
        self.transport.fail_read = True
        self.assertEqual(self.adapter.register(self.payload)['status'], 'unknown')
        old = self.ledger.get('op1')
        self.assertEqual(old[1]['target']['credential_ref'], 'ref-old')
        restarted = FeishuOpenAdapter(self.settings, self.temp.name, None, self.ledger, transport=self.transport)
        restarted.configure_secret('cli_A', 'NEW_SECRET', credential_ref='ref-new')
        self.transport.fail_read = False
        self.assertEqual(restarted.reconcile('op1', old[1], old[2])['status'], 'unknown')
        restarted.configure_secret('cli_A', 'OLD_SECRET', credential_ref='ref-old')
        self.assertEqual(restarted.reconcile('op1', old[1], old[2])['status'], 'confirmed')
        self.assertNotIn(b'OLD_SECRET', self.ledger.path.read_bytes())

    def test_types_and_options_block_submission(self):
        self.assertTrue(self.adapter.check_schema()['ok'])
        self.transport.fields[1]['property']['options'] = [{'name': 'wrong'}]
        self.assertFalse(self.adapter.check_schema()['ok'])
        self.assertEqual(self.adapter.register(self.payload)['status'], 'rejected')
        self.transport.fields[1]['property']['options'] = [{'name': 'A'}]
        self.transport.fields[0]['type'] = 2
        self.assertFalse(self.adapter.check_schema()['ok'])
        self.assertEqual(self.transport.posts, 0)


if __name__ == '__main__':
    unittest.main()
