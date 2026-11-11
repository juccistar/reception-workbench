"""Conversation stays automatic; actual seller terms still require authority."""
import unittest

from reception_service.errors import ServiceError
from reception_service.integrations.analysis import (
    has_seller_commitment, reply_grounding_issues, validate_analysis,
)
from reception_service.synthetic_check import synthetic_context


SOURCE = 'synthetic-ai-check-message'


def answer(body, sources=None):
    return dict(action='ask', reply=body, facts={}, question_keys=[], summary='',
                missing=[], conflicts=[], procurement_key='synthetic-ai-check',
                new_case=False, reply_sources=[] if sources is None else sources)


def policy(content, **changes):
    return dict(policy_id='public-policy', content=content, confirmed=True, public=True,
                product_scope=[], valid_until=None, **changes)


class ReplyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.context = synthetic_context('10001')

    def issues(self, body, sources=None):
        return reply_grounding_issues(body, [SOURCE] if sources is None else sources, self.context)

    def test_greetings_and_general_product_conversation_are_not_promises(self):
        for body in (
            'Hello! How are you today?', 'Thanks for your message. Happy holidays!',
            'Good morning! What can I help you with?',
            'We can discuss the product, sample and logo requirements together.',
            'We can provide samples and help with your logo design.',
            'Tell me what you are looking for and we can discuss shipping options.',
            'Price and availability depend on the final specifications.',
            'We can discuss a discount once the order details are clear.',
            '您好，感谢联系！欢迎告诉我您的需求。',
            '我们可以沟通样品和 Logo，报价与交期待团队核实。',
        ):
            with self.subTest(body=body):
                self.assertFalse(has_seller_commitment(body))
                self.assertEqual(self.issues(body), [])

    def test_chat_time_years_and_question_markers_need_no_procurement_evidence(self):
        for body in (
            'Happy 2027! We are here to chat 24/7.',
            'It is 10:30 here. How is your day?',
            'I am here 24/7 to help you discuss your requirements.',
            '1. Which color?\n2. What logo would you like?',
            'Please share: 1) your size, and 2) your preferred finish.',
            '现在是 2026 年，祝您新年快乐！',
        ):
            with self.subTest(body=body):
                self.assertEqual(self.issues(body), [])

    def test_discussing_unconfirmed_terms_is_not_asserting_them(self):
        for body in (
            'We can discuss free samples and shipping options.',
            'We can explore delivery within 5 days with our team.',
            'We can discuss a price of USD 25 once we know the requirements.',
            'I can discuss ISO 9001 certified product options with you.',
            'Please share your preferred lead time, for example 11 days.',
            '我们可以讨论免费样品和运费。',
        ):
            with self.subTest(body=body):
                self.assertFalse(has_seller_commitment(body))
                self.assertEqual(self.issues(body), [])
        for body in (
            'We can discuss shipping, but we provide free samples.',
            'We can discuss the order; delivery takes 5 days.',
            'We can discuss details and our factory is ISO 9001 certified.',
        ):
            with self.subTest(body=body):
                self.assertTrue(has_seller_commitment(body))
                self.assertTrue(self.issues(body))

    def test_numeric_questions_and_explicit_options_are_not_confirmed_facts(self):
        for body in (
            'Do you need 500 pieces?',
            'Would you prefer 11 oz or 15 oz mugs?',
            'For the capacity, you could consider 250 ml or 350 ml.',
            'Could you share your budget, for example USD 20?',
            'Would delivery within 5 days meet your requirements?',
            'Can we discuss whether free samples are needed?',
            'Are these mugs in stock?',
            '是否需要 500 件？',
            '您需要 500 件吗？',
            '样品免费吗？',
        ):
            with self.subTest(body=body):
                self.assertFalse(has_seller_commitment(body))
                self.assertEqual(self.issues(body), [])

    def test_actual_numeric_product_or_buyer_assertions_still_need_evidence(self):
        for body in (
            'You need 500 pieces.', 'Your budget is USD 20.',
            'Our mugs have a capacity of 350 ml.',
            'We offer 11 oz mugs, for example.',
            'You confirmed 15 oz mugs, for example.',
            'Your preferred capacity is 11 oz.',
            'Your order was placed in 2025.',
            '您的数量为 500 件。',
        ):
            with self.subTest(body=body):
                self.assertTrue(any('数值' in issue for issue in self.issues(body)))

    def test_asserted_quantities_are_allowed_when_the_cited_buyer_supplies_them(self):
        self.context['recent_messages'][0]['text'] = 'I need 500 pieces of 11 oz mugs.'
        self.assertEqual(self.issues('You need 500 pieces of 11 oz mugs.'), [])

    def test_hard_commitments_are_detected_without_keyword_only_blocking(self):
        for body in (
            'Our price is USD 25 per piece.', 'USD 25 per piece.',
            'Shipping costs $20.', 'We offer a 12% discount.',
            'We offer free samples.', 'Samples are free.',
            'We provide free shipping.', 'There is no shipping fee.',
            'We have 500 pieces in stock.', 'Ready to ship now.',
            'We will ship tomorrow.', 'Delivery takes 5 days.',
            'Our lead time is 2 weeks.', 'We can match your budget.',
            'We guarantee the quality.', 'Our factory is ISO 9001 certified.',
            '我们的报价为每件 25 美元。', '样品免费。', '有 500 件现货。',
            '我们在 5 天内发货。', '我们保证产品质量。',
        ):
            with self.subTest(body=body):
                self.assertTrue(has_seller_commitment(body))
                self.assertTrue(any('卖家资料' in issue for issue in self.issues(body)))

    def test_deferred_terms_do_not_become_promises(self):
        for body in (
            'Our team will check shipping costs and sample fees.',
            'Let me verify stock and delivery time with the team.',
            'I will ask our team whether delivery within 5 days is possible.',
            'We will check whether a price of USD 25 is possible.',
            'Free samples need to be confirmed by our team.',
            'We will confirm whether these items are in stock.',
            '样品是否免费和运费待核实。',
        ):
            with self.subTest(body=body):
                self.assertFalse(has_seller_commitment(body))
                self.assertEqual(self.issues(body), [])

    def test_disclaimers_and_buyer_budget_never_hide_a_positive_offer(self):
        for body in (
            'Our price is $25, pending confirmation.',
            'Our lead time is 5 days, subject to confirmation.',
            'We have 500 pieces in stock, but our team will verify.',
            'We provide free shipping, subject to confirmation.',
            'Your budget is $25 and we can match your budget.',
            'Our team will check the details, and we will ship tomorrow.',
            'We will check the fees but samples are free.',
            'In stock, pending confirmation.',
            'Our products are ISO 9001 certified, pending confirmation.',
            'Our stock is 500 units, subject to confirmation.',
            'Samples are complimentary, pending confirmation.',
            'We will check whether a price of USD 12 is possible but shipping is free.',
            'We will check whether stock is available; samples are free.',
        ):
            with self.subTest(body=body):
                self.assertTrue(has_seller_commitment(body))
                self.assertTrue(any('卖家资料' in issue for issue in self.issues(body)))

    def test_natural_policy_phrasing_and_courtesy_preserve_body(self):
        for content, body in (
            ('Our price is USD 12 per piece.', 'Thanks for asking, our quotation is $12 per unit for you.'),
            ('Free samples are supported.', 'Of course, we can provide free samples for you.'),
            ('Shipping is free.', 'Certainly, we offer free shipping for you.'),
            ('Our sample charge is USD 12.', 'Our sample fee is $12.'),
            ('We ship within 5 days.', 'We dispatch within 5 days.'),
            ('Free samples are supported.', 'We can provide complimentary samples.'),
            ('Free samples are supported.', 'We can provide samples at no charge.'),
            ('Our price is USD 12 per piece.', 'Thanks! The unit price is $12.00.'),
        ):
            with self.subTest(content=content, body=body):
                self.context['knowledge'] = [policy(content)]
                self.assertEqual(self.issues(body, ['public-policy']), [])
                result = validate_analysis(answer(body, ['public-policy']), self.context)
                self.assertEqual(result['action'], 'ask')
                self.assertEqual(result['reply'], body)

    def test_same_amount_cannot_change_subject_currency_unit_or_conditions(self):
        for content in (
            'Packaging costs USD 12 per carton.',
            'Our sample fee is USD 12 per piece.',
            'Our price is EUR 12 per piece.',
            'Our price is USD 12 per carton.',
            'Only for orders of 1000 units, our price is USD 12 per piece.',
            'Our price is USD 12 per piece for orders of 1000 units.',
            'Our price is USD 12 per piece, subject to confirmation.',
            'Our price is not USD 12 per piece.',
            'Our EXW price is USD 12 per piece.',
        ):
            with self.subTest(content=content):
                self.context['knowledge'] = [policy(content)]
                self.assertTrue(self.issues('Our price is USD 12 per piece.', ['public-policy']))

    def test_amounts_cannot_be_swapped_between_sample_and_shipping_fees(self):
        self.context['knowledge'] = [policy('Our sample fee is USD 12 and shipping costs USD 25.')]
        self.assertTrue(self.issues('Our sample fee is USD 25 and shipping costs USD 12.', ['public-policy']))

    def test_a_question_or_option_does_not_hide_a_later_assertion(self):
        for body in (
            'Which size do you prefer? We will ship tomorrow.',
            'Would you prefer 11 oz? Our price is USD 12.',
            'For example, we offer free samples.',
            'We will check whether a price of USD 12 is possible but we offer free shipping.',
            'Could you confirm your requirements, and our price is USD 12.',
        ):
            with self.subTest(body=body):
                self.assertTrue(has_seller_commitment(body))
                self.assertTrue(self.issues(body))

    def test_decimal_formatting_does_not_change_the_authorized_amount(self):
        self.context['knowledge'] = [policy('Our price is USD 12.50 per piece.')]
        self.assertEqual(self.issues('Our price is USD 12.5 per piece.', ['public-policy']), [])
        self.assertTrue(self.issues('Our price is USD 1250 per piece.', ['public-policy']))

    def test_only_cited_confirmed_public_applicable_policies_authorize_promises(self):
        body = 'Free samples are supported.'
        row = policy(body)
        self.context['knowledge'] = [row]
        self.assertEqual(self.issues(body, ['public-policy']), [])
        self.assertTrue(self.issues(body))
        for changes in (dict(confirmed=False), dict(public=False), dict(valid_until=1),
                        dict(product_scope=['unrelated product'])):
            with self.subTest(changes=changes):
                self.context['knowledge'] = [dict(row, **changes)]
                self.assertTrue(self.issues(body, ['public-policy']))

    def test_empty_sources_for_chat_bind_only_to_latest_current_buyer(self):
        first = self.context['recent_messages'][0]
        newer = dict(first, message_id='latest-buyer', text='Hello again!', timestamp_ms=1)
        seller = dict(first, message_id='latest-seller', role='seller', text='Hello!', timestamp_ms=2)
        self.context['recent_messages'].extend([newer, seller])
        self.context['historical_evidence'] = [dict(first, message_id='older-evidence')]
        raw = answer('Good morning! Happy 2027!')
        result = validate_analysis(raw, self.context)
        self.assertEqual(result['action'], 'ask')
        self.assertEqual(result['reply_sources'], ['latest-buyer'])
        self.assertEqual(raw['reply_sources'], [])
        self.assertEqual(self.issues(raw['reply'], []), [])

    def test_no_buyer_message_means_no_source_can_be_invented(self):
        self.context['recent_messages'][0]['role'] = 'seller'
        result = validate_analysis(answer('Hello!'), self.context)
        self.assertEqual(result['reply_sources'], [])
        self.assertTrue(result['warnings'])

    def test_supplied_invalid_or_foreign_sources_are_never_replaced(self):
        for ids in (['missing-id'], [SOURCE, 'other-customer-message']):
            with self.subTest(ids=ids):
                result = validate_analysis(answer('Hello!', ids), self.context)
                self.assertTrue(result['warnings'])
                self.assertNotIn('other-customer-message', result['reply_sources'])
                self.assertNotIn('missing-id', result['reply_sources'])
                self.assertTrue(self.issues('Hello!', ids))

    def test_cross_customer_context_cannot_supply_a_fallback_source(self):
        self.context['recent_messages'][0]['buyer_id'] = 'another-buyer'
        with self.assertRaises(ServiceError) as caught:
            validate_analysis(answer('Hello!'), self.context)
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        with self.assertRaises(ServiceError):
            self.issues('Hello!', [])

    def test_commercial_reply_without_sources_is_still_blocked(self):
        for body in ('Our price is USD 25.', 'We offer free shipping.', 'We will ship tomorrow.'):
            with self.subTest(body=body):
                result = validate_analysis(answer(body), self.context)
                self.assertTrue(result['warnings'])
                self.assertTrue(any('来源' in issue for issue in self.issues(body, [])))

    def test_fact_evidence_stays_verbatim_and_buyer_only(self):
        for fact in (
            dict(value='500 pieces', evidence_ids=[SOURCE]),
            dict(value='ceramic mugs', evidence_ids=['seller-message']),
            dict(value='ceramic mugs', evidence_ids=['other-customer-message']),
        ):
            with self.subTest(fact=fact):
                seller = dict(self.context['recent_messages'][0], message_id='seller-message', role='seller')
                self.context['historical_evidence'] = [seller]
                raw = answer('Hello!')
                raw['facts'] = dict(product=fact)
                result = validate_analysis(raw, self.context)
                self.assertNotIn('product', result['facts'])
                self.assertTrue(result['fact_issues'])

    def test_tool_approval_signals_and_schema_are_not_relaxed_for_chat(self):
        for update, code in (
            (dict(tool_calls=[dict(name='send')]), 'tool_approval'),
            (dict(reply_sources='missing-list'), 'schema_lists'),
            (dict(new_case='false'), 'schema_procurement'),
        ):
            with self.subTest(update=update):
                raw = dict(answer('Hello!'), **update)
                with self.assertRaises(ServiceError) as caught:
                    validate_analysis(raw, self.context)
                self.assertEqual(caught.exception.code, code)


if __name__ == '__main__':
    unittest.main()
