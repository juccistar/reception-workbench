"""Resolve per-category collection settings without changing buyer facts."""
from __future__ import annotations

FIELDS = ('product', 'quantity', 'specifications', 'customization', 'destination',
          'delivery', 'budget', 'packaging', 'contact')


def effective_required_fields(settings, case):
    settings = settings if isinstance(settings, dict) else {}
    case = case if isinstance(case, dict) else {}
    templates = settings.get('required_fields_by_category')
    label = case.get('category_label')
    required = templates.get(label) if isinstance(templates, dict) and isinstance(label, str) else None
    if not isinstance(required, list):
        required = settings.get('required_fields', ['product', 'quantity', 'specifications', 'destination'])
    if not isinstance(required, list):
        required = []
    return list(dict.fromkeys(['product'] + [key for key in required if isinstance(key, str) and key in FIELDS]))


def collection_rules(settings, case):
    settings = settings if isinstance(settings, dict) else {}
    def number(key, upper):
        value = settings.get(key, 2)
        return value if type(value) is int and 1 <= value <= upper else 2
    return {'required_fields': effective_required_fields(settings, case),
            'max_questions_per_reply': number('max_questions_per_reply', 2),
            'max_questions_per_field': number('max_questions_per_field', 10)}
