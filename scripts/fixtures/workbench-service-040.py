"""Window-only synthetic input driver, excluded from the production backend."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/reception/test_support'))
import synthetic_service

create_service = synthetic_service.create_synthetic_service


def create_window_service(*args, **kwargs):
    service = create_service(*args, **kwargs)
    original_call = service.call

    def call(method, params=None):
        if method != '_fixture_inject':
            return original_call(method, params or {})
        if params != {'conversation_id': 'demo-emma', 'text': 'I need 100 pieces'}:
            raise ValueError('Unsupported window fixture input')
        with service._lock:
            conversation = service._require('conversation', 'demo-emma')
            message = service.integrations.inject_demo(
                'demo-emma', params['text'], buyer_id=conversation['buyer_id'],
                customer_name=conversation['customer_name'])
            service._ingest(conversation, [message], has_more=False, full=False)
        service._emit('state.changed', {})
        return {'message_id': message['message_id']}

    service.call = call
    return service


synthetic_service.create_synthetic_service = create_window_service
synthetic_service.main()
