"""A catalogue title is not evidence of a buyer's logo request."""
import unittest

from reception_service.integrations.analysis import prepare_context, validate_analysis
from reception_service.synthetic_check import synthetic_context


class ProductAnalysisTests(unittest.TestCase):
    def analyze(self, note):
        context = synthetic_context('974310521')
        message = context['recent_messages'][0]
        message['text'] = ("This is a inquiry card. Inquiry id is SYNTHETIC; Product id is 123456789; "
                           "Product Name is Custom Logo Bracelet; The note of the buyer's inquiry is " + note + ';')
        context, _, _ = prepare_context(context)
        return validate_analysis({'action': 'ask', 'facts': {}, 'question_keys': ['customization'],
            'reply': 'Do you need a custom logo on the bracelets?', 'reply_sources': [message['message_id']]}, context)

    def test_title_does_not_mark_a_logo_question_as_repeated(self):
        result = self.analyze('What is the shipping cost?')
        self.assertNotIn('客户已提出 Logo 需求，请避免重复询问。', [row['message'] for row in result['warnings']])

    def test_actual_buyer_logo_request_still_marks_a_repeated_question(self):
        result = self.analyze('I need a custom logo')
        self.assertIn('客户已提出 Logo 需求，请避免重复询问。', [row['message'] for row in result['warnings']])


if __name__ == '__main__':
    unittest.main()
