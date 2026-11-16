import unittest

from reception_service.errors import ServiceError, safe_diagnostics
from reception_service.requirement_changes import validate_changes


class RequirementFailureDiagnosticsTests(unittest.TestCase):
    def test_rejection_names_the_field_without_exporting_model_values_or_excerpts(self):
        owner = {'seller_id': 'seller', 'buyer_id': 'buyer', 'conversation_id': 'conversation'}
        context = {'case': owner, 'case_catalog': [], 'recent_messages': [
            {**owner, 'message_id': 'm1', 'role': 'buyer', 'text': 'Please quote for blue cups.'}]}
        output = {'requirement_changes': [{'action': 'create', 'case_ref': 'new:1',
            'trigger_evidence_ids': ['m1'], 'set_fields': {'product': {'value': '蓝色杯子',
                'evidence_ids': ['m1'], 'source_excerpts': {'m1': 'sensitive-model-fragment'}}}}]}
        with self.assertRaises(ServiceError) as caught:
            validate_changes(output, context)
        self.assertEqual(caught.exception.code, 'invalid_requirement_change')
        self.assertEqual(caught.exception.diagnostics, {'requirement_field': 'product'})
        self.assertIn('产品', str(caught.exception))
        self.assertNotIn('sensitive-model-fragment', str(caught.exception.to_dict()))
        self.assertNotIn('蓝色杯子', str(caught.exception.to_dict()))

    def test_only_known_field_names_are_admitted_to_diagnostics(self):
        self.assertEqual(safe_diagnostics({'requirement_field': 'product_images', 'output': 'private'}),
                         {'requirement_field': 'product_images'})
        self.assertEqual(safe_diagnostics({'requirement_field': 'private-customer-value'}), {})


if __name__ == '__main__':
    unittest.main()
