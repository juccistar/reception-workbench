"""Window-only synthetic input driver, excluded from the production backend."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/reception/test_support'))
import synthetic_service

create_service = synthetic_service.create_synthetic_service


def create_window_service(*args, **kwargs):
    service = create_service(*args, **kwargs)
    original_call = service.call
    direct_ai = os.environ.get('HARNESS_MANUAL_AI_DIRECT') == '1'
    ai_state = {'fail': False, 'checks': 0, 'generation_attempts': 0}
    if direct_ai:
        from reception_service.errors import ServiceError
        original_generate = service.integrations.generate_manual

        def generate_manual(context, request_id, **options):
            ai_state['generation_attempts'] += 1
            if ai_state['fail']:
                raise ServiceError('model_network_error', '合成模型请求失败')
            return original_generate(context, request_id, **options)

        service.integrations.generate_manual = generate_manual

    def call(method, params=None):
        if direct_ai:
            if method == '_fixture_ai_failure' and isinstance(params, dict) and set(params) == {'enabled'} and type(params['enabled']) is bool:
                ai_state['fail'] = params['enabled']
                return {'enabled': ai_state['fail']}
            if method == '_fixture_ai_stats' and not params:
                return {'checks': ai_state['checks'], 'generation_attempts': ai_state['generation_attempts'], 'check_status': service.checks['ai']['status']}
            if method == '_fixture_repeat_ai_failure' and not params:
                failed = [job for job in service.repo.list('job') if job.get('kind') == 'manual_suggestion' and job.get('status') == 'failed']
                if not failed:
                    raise ValueError('No synthetic failure to repeat')
                service._emit('job.updated', failed[-1])
                return {'repeated': True}
            if method == 'check_ai':
                ai_state['checks'] += 1
                raise ValueError('Manual AI window fixture does not permit capability prechecks')
        if method != '_fixture_inject':
            result = original_call(method, params or {})
            if direct_ai and method == 'get_snapshot':
                result['app'] = {**result['app'], 'mode': 'live', 'account_verified': True}
            return result
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
