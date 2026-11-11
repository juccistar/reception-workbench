"""Clarifying a buyer's choice and asking a team to verify are not sales promises."""
import unittest
from reception_service.integrations.analysis import reply_grounding_issues, validate_analysis
from reception_service.synthetic_check import synthetic_context


class ClarificationGroundingTests(unittest.TestCase):
    def issues(self, body):
        return reply_grounding_issues(body, ['synthetic-ai-check-message'], synthetic_context('10001'))

    def test_team_confirmation_without_a_promised_term_is_allowed(self):
        for body in (
            "Once I have the details, I'll have our team confirm the applicable lead time for you.",
            "I'll ask the team to check the sample charge.",
            "Our team will confirm the lead time once the details are clear.",
            "Once we have your order details, our team will confirm the lead time.",
        ):
            self.assertEqual(self.issues(body), [], body)

    def test_verification_phrase_does_not_excuse_an_actual_promise(self):
        for body in (
            "Our price is $25. I'll have our team confirm the lead time.",
            "We will ship tomorrow, and I'll ask the team to check the details.",
            "Our lead time is 5 days; our team will confirm it.",
            "Free shipping is available; I'll have our team confirm it.",
            "I guarantee free shipping; I'll have our team confirm the details.",
            "I'll provide free shipping and have our team confirm the details.",
            "I will ship tomorrow and ask the team to check the details.",
        ):
            self.assertTrue(self.issues(body), body)

    def test_capacity_example_in_requirement_clarification_is_allowed(self):
        for body in (
            'It would help to know your preferred capacity and style, for example 11 oz mugs, and any customization.',
            'Could you share your preferred capacity, e.g. 11 oz or 15 oz?',
            'Which size would you prefer, such as 250 ml?',
            'Feel free to mention your preferred capacity, for example 11 oz.',
            # A proposed budget/deadline in a question is not a confirmed offer.
            'Could you share your preferred capacity, for example $11 oz?',
            'Could you share your preferred lead time, for example 11 days?',
        ):
            self.assertEqual(self.issues(body), [], body)

    def test_examples_do_not_authorize_price_stock_delivery_or_asserted_facts(self):
        for body in (
            'Your preferred capacity is, for example, 11 oz.',
            'For example, your capacity is 11 oz.',
            'You confirmed your preferred capacity, for example 11 oz.',
            'We offer your preferred capacity, for example 11 oz.',
            'Your preferred capacity is available, for example 11 oz.',
            'For example, we have 11 oz mugs in stock.',
            'You need 11 oz mugs.',
        ):
            self.assertTrue(self.issues(body), body)

    def test_polite_invitation_is_not_a_free_offer(self):
        for body in (
            'If you already have a preferred size or finish in mind, feel free to mention it.',
            'Feel free to share your preferred shape or color.',
            'Please feel free to send a reference photo.',
        ):
            self.assertEqual(self.issues(body), [], body)
            raw = dict(action='ask', facts={}, question_keys=['quantity'], reply=body,
                       summary='', missing=['quantity'], conflicts=[],
                       procurement_key='synthetic-ai-check', new_case=False,
                       reply_sources=['synthetic-ai-check-message'])
            self.assertEqual(validate_analysis(raw, synthetic_context('10001'))['action'], 'ask')

    def test_polite_invitation_does_not_hide_free_terms(self):
        for body in (
            'Feel free to order; samples are free.',
            'Feel free to order and we provide free shipping.',
            'Feel free to ask for free samples.',
            'Our samples are free, feel free to send your logo.',
            'We provide free shipping and feel free to ask questions.',
            'Feel free to choose your preferred capacity, for example 11 oz; shipping is free.',
        ):
            self.assertTrue(self.issues(body), body)

    def test_example_is_not_accepted_as_a_buyer_fact(self):
        raw = dict(action='ask', facts={'specifications': {'value': '11 oz', 'evidence_ids': ['synthetic-ai-check-message']}},
                   question_keys=['quantity'], reply='Could you tell me the quantity?', summary='', missing=['quantity'],
                   conflicts=[], procurement_key='synthetic-ai-check', new_case=False,
                   reply_sources=['synthetic-ai-check-message'])
        checked = validate_analysis(raw, synthetic_context('10001'))
        self.assertNotIn('specifications', checked['facts'])
        self.assertTrue(checked['fact_issues'])
        self.assertEqual(checked['reply'], raw['reply'])


if __name__ == '__main__':
    unittest.main()
