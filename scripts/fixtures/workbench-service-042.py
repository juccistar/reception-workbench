"""Window-only synthetic input driver, excluded from the production backend."""
from pathlib import Path
import os
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/reception/test_support'))
import synthetic_service

create_service = synthetic_service.create_synthetic_service


def create_window_service(*args, **kwargs):
    service = create_service(*args, **kwargs)
    # Scope is fixture-only and contains no real customer or authorization.
    service.set_scope('selected', ['demo-emma'])
    original_call = service.call
    direct_ai = os.environ.get('HARNESS_MANUAL_AI_DIRECT') == '1'
    ai_state = {'fail': False, 'checks': 0, 'generation_attempts': 0, 'block': False, 'cancel_observed': False}
    ai_release = threading.Event()
    ai_entered = threading.Event()
    history_release = threading.Event()
    history_entered = threading.Event()
    history_state = {'block': False}
    original_read_page = service.integrations.read_message_page

    def read_message_page(*args, **kwargs):
        if history_state['block'] and len(args) >= 3 and args[2] == 'demo-emma':
            history_entered.set()
            if not history_release.wait(20):
                raise RuntimeError('synthetic history wait timed out')
        return original_read_page(*args, **kwargs)

    service.integrations.read_message_page = read_message_page
    send_state = {'mode': 'accepted', 'attempts': 0}
    send_entered, send_release = threading.Event(), threading.Event()
    original_send = service.integrations.send_chat

    def send_chat(intent, authorization):
        send_state['attempts'] += 1
        if send_state['mode'] != 'unknown':
            return original_send(intent, authorization)
        from reception_service.integrations.accio_native.chat import authorized
        authorized(authorization)
        ledger = service.integrations.ledger
        prior = ledger.claim(intent['operation_id'], 'window-unknown:' + intent['client_request_id'],
                             'send', intent, conversation_fence=True)
        if prior:
            return prior
        send_entered.set()
        if not send_release.wait(20):
            raise RuntimeError('synthetic send wait timed out')
        return ledger.finish(intent['operation_id'], {'status': 'unknown', 'message_id': '',
                              'submission_started': True, 'simulated': True, 'message': '合成发送结果待确认'})

    service.integrations.send_chat = send_chat
    if direct_ai:
        from reception_service.errors import ServiceError
        original_generate = service.integrations.generate_manual

        def generate_manual(context, request_id, **options):
            ai_state['generation_attempts'] += 1
            if ai_state['block']:
                ai_entered.set()
                if not ai_release.wait(20):
                    raise ServiceError('model_network_error', '合成模型等待超时')
                cancel = options.get('cancel_event')
                ai_state['cancel_observed'] = bool(cancel and cancel.is_set())
                raise ServiceError('model_network_error', '合成模型迟到失败')
            if ai_state['fail']:
                raise ServiceError('model_network_error', '合成模型请求失败')
            return original_generate(context, request_id, **options)

        service.integrations.generate_manual = generate_manual

    def call(method, params=None):
        if method == '_fixture_send_mode' and isinstance(params, dict) and set(params) == {'mode'} and params['mode'] in ('accepted', 'unknown'):
            send_state['mode'] = params['mode']
            send_entered.clear()
            send_release.clear()
            return {'mode': send_state['mode']}
        if method == '_fixture_send_release' and not params:
            send_release.set()
            return {'released': True}
        if method == '_fixture_send_stats' and not params:
            return {**send_state, 'entered': send_entered.is_set()}
        if method == '_fixture_seed_quotes' and not params:
            from copy import deepcopy
            from reception_service.repository import now_ms
            with service._lock, service.repo.transaction():
                conversation = service._require('conversation', 'demo-emma')
                base = service._require('case', conversation['case_id'])
                stamp = now_ms()
                supplier = service.save_supplier('合成供货商', 'other', None, None, '隔离窗口联系信息',
                                                 '仅用于本地界面验收的合成资料', 'window-supplier')
                for index in range(28):
                    case = deepcopy(base)
                    case.update(case_id=f'window-quote-{index}', state='waiting_quote', revision=1,
                                requirements_format='ai-v1', requirements_revision=1, updated_at=stamp - index)
                    case['requirements'] = {key: {'value': [] if key == 'product_images' else None, 'evidence_ids': []}
                        for key in ('product', 'product_id', 'product_url', 'product_images', 'quantity', 'specifications',
                                    'customization', 'destination', 'delivery', 'budget', 'packaging', 'certifications', 'contact')}
                    case['requirements']['product']['value'] = f'合成报价产品 {index + 1}'
                    case['requirements']['quantity']['value'] = '100件'
                    case['requirements']['specifications']['value'] = '仅用于隔离窗口滚动验收的合成规格。' * 90
                    service.repo.put('case', case['case_id'], case, history=True)
                    if index < 3:
                        case['requirements']['product_id']['value'] = f'定制-{7600001 + index}'
                        service._sync_requirement_product(case)
                        binding = service.repo.get('requirement_product_binding', case['case_id'])
                        service.save_product_offer(binding['local_product_id'], supplier['supplier_id'],
                            {'specifications': '合成供货规格', 'moq': '100', 'unit': '件', 'unit_price': '4.20',
                             'currency': 'CNY', 'source_type': 'page_reference', 'enabled': True, 'reply_enabled': False,
                             'source_note': '仅用于隔离窗口的合成参考，不是供应商确认报价'}, f'window-offer-{index}')
            service._changed('demo-emma')
            return {'case_ids': [f'window-quote-{index}' for index in range(28)]}
        if method == '_fixture_history_block' and isinstance(params, dict) and set(params) == {'enabled'} and type(params['enabled']) is bool:
            history_state['block'] = params['enabled']
            if params['enabled']:
                history_entered.clear()
                history_release.clear()
            else:
                history_release.set()
            return {'enabled': history_state['block']}
        if method == '_fixture_history_state' and not params:
            return {'entered': history_entered.is_set()}
        if direct_ai:
            if method == '_fixture_ai_block' and isinstance(params, dict) and set(params) == {'enabled'} and type(params['enabled']) is bool:
                ai_state['block'] = params['enabled']
                if params['enabled']:
                    ai_entered.clear()
                    ai_release.clear()
                    ai_state['cancel_observed'] = False
                else:
                    ai_release.set()
                return {'enabled': ai_state['block']}
            if method == '_fixture_ai_block_state' and not params:
                with service._lock:
                    jobs = [{key: job.get(key) for key in ('job_id', 'kind', 'status')}
                            for job in service.repo.list('job') if job.get('kind') == 'manual_suggestion']
                return {'entered': ai_entered.is_set(), 'cancel_observed': ai_state['cancel_observed'], 'jobs': jobs}
            if method == 'get_translations' and isinstance(params, dict):
                result = original_call(method, params)
                for item in result.get('items', []):
                    if item.get('kind') == 'manual_suggestion':
                        item.update(status='ready', text='合成建议中文译文', error=None)
                return result
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
