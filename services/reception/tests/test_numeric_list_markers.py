"""Ordered questions are formatting; amounts inside them still require evidence."""
import unittest
from reception_service.integrations.analysis import reply_grounding_issues
from reception_service.synthetic_check import synthetic_context


class NumericListMarkersTests(unittest.TestCase):
    def check(self, reply):
        return reply_grounding_issues(reply, ['synthetic-ai-check-message'], synthetic_context('10001'))

    def test_numbered_questions_do_not_invent_quantities(self):
        self.assertEqual(self.check('Happy to help with ceramic mugs.\n1. How many pieces do you need?\n2. Which size do you prefer?'), [])
        self.assertEqual(self.check('(1) Which size?\n(2) Which color?'), [])
        self.assertEqual(self.check('Please confirm: 1) the quantity you need, and 2) your preferred size?'), [])
        self.assertTrue(self.check('Please confirm: 1) your quantity of 500 pieces, and 2) your preferred size?'))

    def test_number_inside_list_item_still_requires_source(self):
        self.assertTrue(any('500' in issue for issue in self.check('1. You need 500 pieces.')))
        self.assertTrue(any('25' in issue for issue in self.check('1. Our price is $25.')))
        self.assertTrue(any('卖家资料' in issue for issue in self.check('1. Our price is $25.')))

    def test_decimals_and_inline_quantities_are_not_list_markers(self):
        for reply in ('1.5 kg of ceramic mugs.', 'You need 2 pieces.', '1.2 USD per piece.', 'We have 1. carton available.'):
            self.assertTrue(self.check(reply), reply)

    def test_numbered_shipping_promise_still_requires_seller_material(self):
        self.assertTrue(self.check('1. We will ship tomorrow.\n2. Free shipping is available.'))


if __name__ == '__main__':
    unittest.main()
