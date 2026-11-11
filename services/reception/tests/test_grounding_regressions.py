"""Budget/quotation and inquiry-time regressions using synthetic protocols only."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reception_service.context import validate_reply
from reception_service.errors import ServiceError
from reception_service.integrations import build_integrations
from reception_service.service import ReceptionService

SELLER, BUYER = '10001', '20002'
CID = SELLER + '-' + BUYER + '#icbu'
ORIGINAL_TIME, LATER_TIME = 1700000000123, 1700000500456


def buyer_message(mid='budget-message', text='My budget is USD 12 per piece for ceramic mugs.', timestamp=LATER_TIME):
    return dict(message_id=mid, seller_id=SELLER, buyer_id=BUYER, conversation_id=CID,
                role='buyer', own=False, text=text, timestamp_ms=timestamp, attachments=[])


def context():
    return dict(case=dict(case_id='synthetic-case', seller_id=SELLER, buyer_id=BUYER,
                          conversation_id=CID, revision=1, state='clarifying', procurement_key='mugs',
                          facts={'product': dict(value='ceramic mugs', evidence_ids=['budget-message'], status='confirmed')},
                          questions={}), recent_messages=[buyer_message()], historical_evidence=[],
                knowledge=[], required_fields=['product'])


def answer(body, sources=None):
    return dict(action='ask', reply=body, facts={}, question_keys=[], summary='', missing=[], conflicts=[],
                procurement_key='mugs', new_case=False, reply_sources=sources or ['budget-message'])


def policy(content, **changes):
    row = dict(policy_id='seller-policy', content=content, confirmed=True, public=True,
               product_scope=['ceramic mugs'], valid_until=None)
    row.update(changes)
    return row


def feishu_settings(root):
    kinds = dict(buyer_id='text', account='select', status='select',
                 product_name='text', number='auto_number', inquiry_time='datetime')
    return dict(cli_path=str(root / 'synthetic-lark.exe'), base_token='synthetic-base',
                table_id='synthetic-table', form_share_token='synthetic-form',
                fields={name: dict(name=name, type=kind) for name, kind in kinds.items()},
                account_option='synthetic-account', initial_status='pending', write_enabled=True)


class MockProcess:
    """Implements only the exact local test protocols, including Lark readback."""
    def __init__(self, config):
        self.config = config
        self.calls, self.rows, self.submissions = [], [], []
        self.answer = answer('Your budget is USD 12 per piece. Our team will confirm the quotation.')
        self.history = [buyer_message('original-message', 'I am looking for ceramic mugs.', ORIGINAL_TIME), buyer_message()]

    def run(self, args, *, timeout=40, input=None, inherit_runtime_config=False):
        args = [str(value) for value in args]
        self.calls.append(args)
        if args[1:3] == ['desktop', 'query']:
            return {'answer': deepcopy(self.answer)}
        if args[1:4] == ['workflow', 'chat-analysis', 'resolve-id']:
            Path(args[args.index('--output-file') + 1]).write_text(
                json.dumps({'CURRENT_SELF_ALIID': SELLER}), encoding='utf-8')
            return dict(identitySha256='synthetic-sha')
        if len(args) > 3 and args[2:4] == ['call', 'query_conversation_msg']:
            return dict(success=True, data=dict(conversationId=CID, sellerAliId=SELLER, contactAliId=BUYER,
                messages=[dict(messageId=m['message_id'], senderAliId=BUYER if m['role'] == 'buyer' else SELLER,
                               timestamp=m['timestamp_ms'], content=m['text']) for m in self.history], hasMore=False))
        if args[1] != 'base':
            raise AssertionError('Unexpected command; no real process may run')
        command = args[2]
        cfg = self.config
        mapping = cfg['fields']
        if command == '+field-list':
            data = [dict(meta, options=[{'name': cfg['account_option']}]) if role == 'account' else dict(meta)
                    for role, meta in mapping.items()]
        elif command == '+form-detail':
            questions = [dict(title=meta['name'], type=meta['type'], required=True,
                              **({'options': [{'name': cfg['account_option'] if role == 'account' else cfg['initial_status']}]}
                                 if role in ('account', 'status') else {}))
                         for role, meta in mapping.items() if role != 'number']
            data = dict(base_token=cfg['base_token'], table_id=cfg['table_id'], questions=questions)
        elif command in ('+record-list', '+record-get'):
            rows = deepcopy(self.rows)
            if command == '+record-get':
                rid = args[args.index('--record-id') + 1]
                rows = [row for row in rows if row['record_id'] == rid]
            path = Path(args[args.index('--output') + 1])
            path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
            path.with_suffix('.manifest.json').write_text(json.dumps(dict(base_token=cfg['base_token'],
                table_id=cfg['table_id'], has_more=False, rev=1, records_count=len(rows))), encoding='utf-8')
            data = {}
        elif command in ('+form-submit', '+record-batch-update'):
            payload = json.loads(args[args.index('--json') + 1])
            if '--dry-run' not in args:
                self.submissions.append((command, deepcopy(payload)))
                if command == '+form-submit':
                    self.rows.append(dict(payload['fields'], record_id='recSynthetic', number='SYNTHETIC-001'))
                else:
                    for rid, fields in payload['update_records'].items():
                        next(row for row in self.rows if row['record_id'] == rid).update(fields)
            data = {}
        else:
            raise AssertionError('Unexpected synthetic Lark command: ' + command)
        return dict(ok=True, identity='user', data=data)


class GroundingRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='grounding-regression-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ('subprocess.run', 'subprocess.Popen', 'socket.create_connection'):
            guard = patch(target, side_effect=AssertionError('Real external execution forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        account = self.root / 'accounts' / 'synthetic'
        account.mkdir(parents=True)
        node, script, cli = self.root / 'synthetic-node.exe', account / 'synthetic-mcp.js', account / 'workctl.exe'
        for path in (node, script, cli):
            path.touch()
        os.environ.update(ACCIO_GATEWAY_TOKEN='synthetic-gateway-token', ACCIO_NODE_BIN=str(node), ACCIO_MCP_SCRIPT=str(script))
        config = feishu_settings(self.root)
        self.live = build_integrations('live', self.root, dict(workctl_path=str(cli), seller_id=SELLER,
                                       ai={'agent_id': 'synthetic-agent'}, feishu=config))
        self.process = MockProcess(config)
        self.live.native.process = self.live.native.feishu.process = self.process
        # These cases validate grounding through the delegated synthetic process.
        # Account binding is real, but this fixture has no desktop history store.
        records = patch('reception_service.integrations.accio_native.vendor.adapters.desktop_query._OwnRecords')
        records.start().return_value.folder = None
        self.addCleanup(records.stop)

    def assert_blocked_in_both_layers(self, body, ctx=None, sources=None):
        ctx = context() if ctx is None else ctx
        raw = answer(body, sources)
        self.process.answer = raw
        result = self.live.generate(ctx, 'synthetic-generation')
        self.assertEqual(result['action'], 'ask', body)
        self.assertEqual(result['reply'], body)
        self.assertTrue(result['warnings'], body)
        validation = validate_reply(body, raw['reply_sources'], ctx)
        self.assertTrue(validation['ready'], body)
        self.assertTrue(validation['warnings'], body)

    def assert_allowed_in_both_layers(self, body, ctx=None, sources=None):
        ctx = context() if ctx is None else ctx
        raw = answer(body, sources)
        self.process.answer = raw
        result = self.live.generate(ctx, 'synthetic-generation')
        self.assertEqual(result['action'], 'ask', body)
        self.assertEqual(result['reply'], body)
        _, validation = ReceptionService._validate_ai_output(None, raw, ctx)
        self.assertTrue(validation['ready'], validation['issues'])

    def test_buyer_budget_is_not_english_or_chinese_seller_quotation(self):
        for body in ('Our price is USD 12 per piece.', '我们的报价是每件 USD 12。', 'USD 12 per piece.'):
            with self.subTest(body=body):
                self.assert_blocked_in_both_layers(body)

    def test_budget_acknowledgement_and_unresolved_terms_remain_sendable(self):
        for body in (
            'Your budget is USD 12 per piece. Our team will confirm the quotation and delivery time.',
            "I've noted your USD 12 budget. The final price is pending confirmation.",
            'Your budget is USD 12 per piece, and our quotation is pending confirmation.',
            'We will confirm the sample charge, availability and delivery time.',
            'We will check whether USD 12 per piece is possible.',
            "I'll confirm the sample charge and preparation time with our team.",
            "I'll pass your requirements to our team to confirm the quotation.",
            'Could you let me know the delivery destination?',
            '请再告诉我交期。',
            '您的预算为每件 USD 12。我们的报价待核实，费用和交期待确认。',
            '您的预算是每件 USD 12，我们的报价待核实。',
        ):
            with self.subTest(body=body):
                self.assert_allowed_in_both_layers(body)

    def test_budget_or_deferment_does_not_excuse_a_later_quotation(self):
        for body in (
            'Your budget is USD 12 per piece, and our price is USD 12 per piece.',
            'Your budget is USD 12 per piece and our price is USD 12 per piece.',
            'Our price is USD 12 per piece, subject to confirmation.',
            'We have 12 pieces in stock, pending confirmation.',
            'Free samples are available, pending confirmation.',
            "We'll match your budget of USD 12 per piece.",
            'We can meet your budget.',
            'Our team will confirm delivery. Our price is USD 12 per piece.',
            '您的预算是 USD 12，我们的报价是 USD 12。',
            '我们的报价是 USD 12，交期待核实。',
        ):
            with self.subTest(body=body):
                self.assert_blocked_in_both_layers(body)

    def test_buyer_requested_terms_never_authorize_seller_commitments(self):
        ctx = context()
        ctx['recent_messages'][0]['text'] += ' Can you offer free samples, a 12% discount, 12 pieces in stock and delivery within 12 days?'
        for body in ('Our sample fee is USD 12.', 'We offer a 12% discount.', 'We have 12 pieces in stock.',
                     'We ship within 12 days.', 'We offer free samples.', '我们的样品收费为 USD 12。',
                     '折扣是 12%。', '有 12 件现货。', '我们在 12 天内发货。'):
            with self.subTest(body=body):
                self.assert_blocked_in_both_layers(body, ctx)

    def test_only_cited_applicable_confirmed_public_seller_material_authorizes_quote(self):
        body = 'Our price is USD 12 per piece.'
        ctx = context()
        ctx['knowledge'] = [policy(body)]
        self.assert_allowed_in_both_layers(body, ctx, ['budget-message', 'seller-policy'])
        self.assert_blocked_in_both_layers(body, ctx, ['budget-message'])
        for changes in (dict(confirmed=False), dict(public=False), dict(valid_until=1), dict(product_scope=['steel bolts'])):
            with self.subTest(changes=changes):
                ctx['knowledge'] = [policy(body, **changes)]
                self.assert_blocked_in_both_layers(body, ctx)
                validation = validate_reply(body, ['budget-message', 'seller-policy'], ctx)
                self.assertTrue(validation['ready'])
                self.assertTrue(validation['warnings'])

    def test_same_amount_in_unrelated_policy_or_old_seller_message_is_insufficient(self):
        body = 'Our price is USD 12 per piece.'
        ctx = context()
        for content in ('Packaging costs USD 12 per carton.',
                        'Only for orders of 12000 units, our price is USD 12 per piece.',
                        'Our price is USD 12 per piece, subject to confirmation.'):
            ctx['knowledge'] = [policy(content)]
            self.assert_blocked_in_both_layers(body, ctx, ['budget-message', 'seller-policy'])
        seller = dict(buyer_message('old-seller', body), role='seller', own=True)
        ctx['historical_evidence'] = [seller]
        self.assert_blocked_in_both_layers(body, ctx, ['budget-message', 'old-seller'])

    def test_approved_free_sample_and_decimal_quote_preserve_natural_body(self):
        for body in ('Free samples are supported.', 'Our price is USD 12.50 per piece.'):
            with self.subTest(body=body):
                ctx = context()
                ctx['knowledge'] = [policy(body)]
                self.assert_allowed_in_both_layers(body, ctx, ['budget-message', 'seller-policy'])

    def test_budget_restatement_does_not_allow_uncited_numbers(self):
        self.assert_blocked_in_both_layers('Your budget is USD 13 per piece.')

    def registration_case(self, *, source='original-message'):
        # Case keeps its public contract; the core supplies a private immutable ID.
        case = deepcopy(context()['case'])
        case['customer_name'] = ''
        if source is not None:
            case['_inquiry_source_id'] = source
        return case

    def test_required_inquiry_time_uses_original_buyer_platform_time(self):
        self.assertEqual(self.live.check_registration()['status'], 'ready')
        case = self.registration_case()
        case['inquiry_timestamp_ms'] = LATER_TIME  # caller's local/latest clock is ignored
        original = deepcopy(case)
        result = self.live.register(case, {}, 'synthetic-create')
        self.assertEqual(result['status'], 'confirmed', result)
        self.assertEqual(case, original)
        self.assertEqual(self.process.rows[0]['inquiry_time'], '2023-11-15 06:13:20')
        self.assertEqual(len(self.process.submissions), 1)
        record = self.live.native.ledger.get('synthetic-create')
        saved_case = record[1]['payload']['case']
        self.assertEqual(saved_case['inquiry_source_id'], 'original-message')
        self.assertEqual(saved_case['inquiry_timestamp_ms'], ORIGINAL_TIME)
        self.assertIn('original-message', saved_case['source_message_ids'])
        self.assertNotIn('_inquiry_source_id', saved_case)

    def test_same_registration_operation_replays_without_history_or_resubmission(self):
        case = self.registration_case()
        first = self.live.register(case, {}, 'synthetic-replay')
        self.assertEqual(first['status'], 'confirmed')
        self.process.history = []
        calls_before = len(self.process.calls)
        self.assertEqual(self.live.register(case, {}, 'synthetic-replay'), first)
        self.assertEqual(len(self.process.submissions), 1)
        self.assertFalse(any(args[2:4] == ['call', 'query_conversation_msg'] for args in self.process.calls[calls_before:]))
        case['_inquiry_source_id'] = 'budget-message'
        self.assertNotEqual(self.live.register(case, {}, 'synthetic-replay')['status'], 'confirmed')
        self.assertEqual(len(self.process.submissions), 1)

    def test_existing_registered_inquiry_time_is_immutable_on_update(self):
        first = self.live.register(self.registration_case(), {}, 'synthetic-first')
        self.assertEqual(first['status'], 'confirmed')
        case = self.registration_case(source='budget-message')
        case['revision'] = 2
        case['facts']['budget'] = dict(value='USD 12 per piece', evidence_ids=['budget-message'], status='confirmed')
        previous = dict(record_id=first['record_id'], number=first['number'], case_id=case['case_id'])
        result = self.live.register(case, previous, 'synthetic-update')
        self.assertEqual(result['status'], 'confirmed', result)
        self.assertEqual(self.process.rows[0]['inquiry_time'], '2023-11-15 06:13:20')
        self.assertEqual(len(self.process.submissions), 2)
        self.assertNotIn('inquiry_time', self.process.submissions[-1][1]['update_records']['recSynthetic'])

    def test_missing_or_wrong_original_source_is_not_guessed_from_facts(self):
        for source in (None, 'missing-message'):
            with self.subTest(source=source):
                result = self.live.register(self.registration_case(source=source), {}, 'synthetic-missing-' + str(source))
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(self.process.submissions, [])
        self.process.history[0]['role'] = 'seller'
        result = self.live.register(self.registration_case(), {}, 'synthetic-wrong-role')
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.process.submissions, [])

    def test_invalid_platform_time_is_never_replaced_with_local_time(self):
        self.process.history[0]['timestamp_ms'] = None
        result = self.live.register(self.registration_case(), {}, 'synthetic-missing-time')
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.process.submissions, [])


if __name__ == '__main__':
    unittest.main()
