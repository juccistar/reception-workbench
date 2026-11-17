"""Window-only synthetic input driver, excluded from the production backend."""
from pathlib import Path
import os
import sys
import threading

# Isolated Python ignores PYTHONIOENCODING; match the production JSONL entrypoint.
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8', errors='strict')

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/reception/test_support'))
import synthetic_service

create_service = synthetic_service.create_synthetic_service


def create_window_service(*args, **kwargs):
    service = create_service(*args, **kwargs)
    # Scope is fixture-only and contains no real customer or authorization.
    service.set_scope('selected', ['demo-emma'])
    original_call = service.call
    progress_view = {'phase': None, 'job': None, 'batch': None, 'image': None, 'image_stamp': 0}
    direct_ai = os.environ.get('HARNESS_MANUAL_AI_DIRECT') == '1'
    ai_state = {'fail': False, 'checks': 0, 'generation_attempts': 0, 'block': False, 'cancel_observed': False}
    ai_release = threading.Event()
    ai_entered = threading.Event()
    history_release = threading.Event()
    history_entered = threading.Event()
    history_state = {'block': False, 'fail': False}
    original_read_page = service.integrations.read_message_page

    def read_message_page(*args, **kwargs):
        if history_state['block'] and len(args) >= 3 and args[2] == 'demo-emma':
            history_entered.set()
            if not history_release.wait(20):
                raise RuntimeError('synthetic history wait timed out')
        if history_state['fail'] and len(args) >= 3 and args[2] == 'demo-emma':
            raise RuntimeError('合成历史读取失败')
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
        if method == '_fixture_progress' and isinstance(params, dict) and set(params) == {'phase'}:
            from reception_service.repository import now_ms
            phase = params['phase']
            if phase not in ('generating', 'saving', 'prepared', 'sending', 'unknown', 'complete', 'clear'):
                raise ValueError('Unsupported progress projection')
            stamp = now_ms()
            owner = service._require('conversation', 'demo-emma')
            case = service._require('case', owner['case_id'])
            source = service._latest_buyer('demo-emma')
            job = {'job_id': 'window-progress-job', 'kind': 'generate_draft', 'conversation_id': 'demo-emma',
                   'case_id': case['case_id'], 'status': 'running' if phase in ('generating', 'saving', 'prepared') else 'succeeded',
                   'stage': {'generating': 'generate_draft', 'saving': 'save_draft', 'prepared': 'prepare_send'}.get(phase, 'complete'),
                   'message': '回复已保存，正在准备发送' if phase == 'prepared' else '正在生成接待回复', 'automatic': True, 'created_at': stamp,
                   'started_at': stamp, 'completed_at': stamp if phase in ('sending', 'unknown', 'complete') else None,
                   'source_case_revision': case['revision'], 'source_message_id': source['message_id'] if source else None,
                   'result': {'draft_id': 'window-progress-draft'}}
            batch = {'batch_id': 'window-progress-batch', 'status': {'sending': 'sending', 'unknown': 'waiting_receipt', 'complete': 'accepted'}.get(phase, 'prepared'),
                     'origin': 'automatic', 'channel': 'international', 'conversation_id': 'demo-emma',
                     'total': 2, 'accepted': 2 if phase == 'complete' else 0, 'current_index': None if phase == 'complete' else 0,
                     'pending_operation_ids': ['window-pending'] if phase == 'unknown' else [], 'created_at': stamp, 'updated_at': stamp}
            progress_view.update(phase=None if phase == 'clear' else phase, job=job if phase != 'clear' else None,
                                 batch=batch if phase in ('sending', 'unknown', 'complete') else None)
            if phase != 'clear':
                service._emit('job.updated', job)
            else:
                service._emit('job.updated', {**job, 'status': 'cancelled'})
            service._changed('demo-emma')
            return {'phase': phase}
        if method == '_fixture_image_progress' and isinstance(params, dict) and set(params) == {'stage'} and params['stage'] in ('failed', 'product_metadata', 'download', 'ready'):
            from reception_service.repository import now_ms
            progress_view.update(image=params['stage'], image_stamp=now_ms())
            service._changed('demo-emma')
            return {'stage': params['stage']}
        if method == 'request_product_images' and progress_view['image'] is not None:
            progress_view['image'] = 'product_metadata'
            service._changed('demo-emma')
            return {'job_id': 'window-image-job'}
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
        if method == '_fixture_seed_showcase' and not params:
            import io
            from PIL import Image, ImageDraw
            from reception_service.image_assets import decode_image
            from reception_service.repository import now_ms
            stamp = now_ms()
            with service._lock, service.repo.transaction():
                conversation = service._require('conversation', 'demo-emma')
                cid = conversation['conversation_id']
                samples = [
                    ('buyer', 'We need 500 sage green travel mugs, 450 ml, with our logo. Product reference: 1601902417143. Delivery to Hamburg, Germany.'),
                    ('seller', 'Thank you. We can review the logo and sample requirements before preparing a quotation.'),
                    ('buyer', 'Please use our logo on one side. We would like to see a sample first.'),
                    ('seller', 'Could you confirm the packaging and requested delivery date?'),
                    ('buyer', 'Individual boxes, please. We need delivery before December 15. Can we see a sample first?'),
                ]
                messages = [{'message_id': f'window-showcase-{index + 1}', 'seller_id': service.seller_id,
                             'buyer_id': conversation['buyer_id'], 'conversation_id': cid, 'role': role,
                             'text': text, 'timestamp_ms': stamp - 300000 + index * 45000,
                             'attachments': [], 'own': role == 'seller'}
                            for index, (role, text) in enumerate(samples)]
                service._ingest(conversation, messages, has_more=False, full=True, history=True)
                service.integrations.messages.extend(message for message in messages
                    if not any(old.get('message_id') == message['message_id'] for old in service.integrations.messages))
                conversation = service._require('conversation', cid)
                case = service._require('case', conversation['case_id'])
                values = {'product': 'Luma 随行保温杯', 'product_id': '1601902417143',
                          'quantity': '500只', 'specifications': '450 ml，鼠尾草绿，双层不锈钢杯身',
                          'customization': '杯身单面印制客户 Logo，先确认样品', 'destination': '德国汉堡',
                          'delivery': '12月15日前送达', 'packaging': '独立包装盒',
                          'budget': None, 'certifications': None, 'contact': None, 'product_url': None}
                requirements = {key: {'value': value, 'evidence_ids': [
                    'window-showcase-1' if key in ('product', 'product_id', 'quantity', 'specifications', 'destination')
                    else 'window-showcase-3' if key == 'customization' else 'window-showcase-5'] if value else []}
                    for key, value in values.items()}
                requirements['product_images'] = {'value': [], 'evidence_ids': []}
                case.update(requirements=requirements, requirements_format='ai-v1',
                            requirements_revision=case.get('requirements_revision', 0) + 1,
                            revision=case.get('revision', 0) + 1, state='waiting_quote', updated_at=stamp)
                service.repo.put('case', case['case_id'], case, history=True)
                service._sync_requirement_product(case)
                binding = service.repo.get('requirement_product_binding', case['case_id'])
                product_id = binding['local_product_id']
                gallery = []
                for index, color in enumerate(('#a5b398', '#99aa8e', '#aeba9f', '#a0af95', '#b4bea8')):
                    image = Image.new('RGB', (560, 460), '#edf1eb')
                    draw = ImageDraw.Draw(image)
                    draw.ellipse((166, 400, 394, 425), fill='#dce2d6')
                    draw.polygon([(170, 110), (390, 110), (355, 398), (205, 398)], fill=color)
                    draw.ellipse((170, 75, 390, 145), fill='#6f7e62')
                    draw.ellipse((185, 87, 375, 132), fill='#bac5ad')
                    draw.ellipse((202, 94, 358, 125), fill='#7d8d6f')
                    draw.text((262, 246), 'LUMA', fill='#5e7150')
                    stream = io.BytesIO()
                    image.save(stream, 'PNG')
                    asset = decode_image(stream.getvalue(), 'image/png')
                    service.repo.put('image_asset', asset['sha256'], asset)
                    gallery.append({'status': 'ready', 'asset_sha256': asset['sha256']})
                service.repo.put('product_main_image', product_id, {'seller_id': service.seller_id,
                    'local_product_id': product_id, 'product_id': values['product_id'], 'status': 'ready',
                    'updated_at': stamp, 'asset_sha256': gallery[0]['asset_sha256'], 'images': gallery,
                    'image_group_version': 1, 'display_only': True,
                    'primary_verified': False, 'available_count': len(gallery), 'message': '合成演示参考图'})
                service.repo.put('conversation_summary', cid, {'conversation_id': cid, 'seller_id': service.seller_id,
                    'buyer_id': conversation['buyer_id'], 'body': '采购500只鼠尾草绿随行杯，定制Logo；发往德国汉堡，先确认样品与交期。',
                    'revision': 1, 'updated_at': stamp, 'source_fingerprint': 'synthetic-window-showcase'})
                conversation.update(human_owned=True, revision=conversation.get('revision', 0) + 1)
                service.repo.put('conversation', cid, conversation, history=True)
                from copy import deepcopy
                extras = [('oliver', 'Oliver Smith', '英国', 'Please confirm the lead time.'),
                          ('mia', 'Mia Rossi', '意大利', 'Can you share the packaging options?'),
                          ('liam', 'Liam Brown', '澳大利亚', 'Do you have an ivory white version?'),
                          ('hana', 'Hana Kim', '韩国', 'Thank you, I will review the sample details.'),
                          ('alex', 'Alex Morgan', '德国', 'Could you quote for 300 pieces?')]
                for offset, (key, name, country, text) in enumerate(extras, 1):
                    extra_id = 'demo-' + key
                    if service.repo.get('conversation', extra_id):
                        continue
                    extra = deepcopy(conversation)
                    extra.update(conversation_id=extra_id, buyer_id='synthetic-' + key, customer_name=name,
                                 country=country, case_id=None, human_owned=False, revision=1,
                                 last_message=text, last_timestamp=stamp - offset * 60000)
                    service.repo.put('conversation', extra_id, extra, history=True)
                    service._ingest(extra, [{'message_id': 'showcase-' + key, 'seller_id': service.seller_id,
                        'buyer_id': extra['buyer_id'], 'conversation_id': extra_id, 'role': 'buyer', 'text': text,
                        'timestamp_ms': stamp - offset * 60000, 'attachments': [], 'own': False}], has_more=False, full=True, history=True)
            supplement = service._supplement(cid)
            service.save_conversation_supplement(cid, '定制 Logo 需确认图稿与印刷尺寸。\n客户希望先看样品，再确认大货。',
                                                supplement['revision'], f'window-showcase-supplement-{stamp}')
            service._changed(cid)
            return {'conversation_id': cid, 'case_id': case['case_id'], 'local_product_id': product_id}
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
                    if index < 3:
                        case['requirements']['product_id']['value'] = f'定制-{7600001 + index}'
                    service.repo.put('case', case['case_id'], case, history=True)
                    if index < 3:
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
        if method == '_fixture_history_failure' and isinstance(params, dict) and set(params) == {'enabled'} and type(params['enabled']) is bool:
            history_state['fail'] = params['enabled']
            return {'enabled': history_state['fail']}
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
            try:
                result = original_call(method, params or {})
            except Exception:
                if os.environ.get('HARNESS_TEMP'):
                    import traceback
                    (Path(os.environ['HARNESS_TEMP']) / 'backend-failure.txt').write_text(
                        f'{method}\n' + traceback.format_exc(), encoding='utf-8')
                raise
            if direct_ai and method == 'get_snapshot':
                result['app'] = {**result['app'], 'mode': 'live', 'account_verified': True, 'company_name': '澄野生活'}
            if progress_view['phase']:
                if method == 'get_snapshot':
                    result['reception'] = {**result['reception'], 'running': True, 'auto_send': True}
                    result['jobs'].append(progress_view['job'])
                    for row in result['conversations']:
                        if row['conversation_id'] == 'demo-emma':
                            row['human_owned'] = False
                elif method == 'list_jobs':
                    result.append(progress_view['job'])
                elif method == 'get_conversation' and (params or {}).get('conversation_id') == 'demo-emma':
                    result['conversation'] = {**result['conversation'], 'human_owned': False}
                    result['reply_batches'] = [progress_view['batch']] if progress_view['batch'] else []
            if progress_view['image'] is not None:
                state = progress_view['image']
                status = 'loading' if state in ('product_metadata', 'download') else state
                overlay = {'status': status, 'stage': state, 'job_id': 'window-image-job', 'updated_at': progress_view['image_stamp']}
                if method == 'get_product_main_image':
                    result.update(overlay)
                    if status != 'ready':
                        result.pop('data_url', None)
                        result['images'] = []
                elif method == 'get_conversation' and (params or {}).get('conversation_id') == 'demo-emma' and result.get('case'):
                    result['case']['product_main_image'].update(overlay)
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
