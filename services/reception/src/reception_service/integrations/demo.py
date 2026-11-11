"""Local synthetic adapter. Nothing here starts a process or contacts an account."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import threading
import time
import uuid

from ..errors import ServiceError
from ..conversation_style import social_intent, demo_social_reply
from .analysis import FIELDS, current_message, prepare_context, validate_analysis
from .accio_native.vendor.fact_rules import is_complete
from .accio_native.vendor.models import FACT_LABELS
from .accio_native.vendor.adapters.common import AdapterError, Ledger, digest

PATTERNS = {
    'quantity': r'(?<![\w.])\d[\d,]*(?:\.\d+)?\s*(?:pieces?|pcs|units?|sets?|pairs?|件|个|套|双|箱)(?![a-z])',
    'specifications': r'(?:size|colou?r|material|specifications?|尺寸|颜色|材质|规格)\s*[:：=]?\s*[^\n;；。!?]+',
    'customization': r'(?:custom\w*|logo|print\w*|engrave\w*|定制|印刷|刻字)[^\n;；。!?]*',
    'destination': r'(?:ship(?:ping)? to|deliver to|destination\s*[:：]?|收货地\s*[:：]?|寄到|发往|运到)[^\n;；。!?]+',
    'delivery': r'(?:deadline|need (?:it|them) by|交期\s*[:：]|交货\s*[:：]|到货\s*[:：])[^\n;；。!?]+',
    'budget': r'(?:budget|target price|预算|目标价)[^\n;；。!?]+',
    'packaging': r'(?:packag\w*|packing|certification|包装|认证)[^\n;；。!?]+',
    'contact': r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}',
}


def natural_demo_analysis(context):
    context, messages, recent = prepare_context(context)
    case = context['case']
    latest = current_message(context, recent)
    continuing = 'continuation' in context
    result = dict(action='skip', facts={}, question_keys=[], reply='', summary='', missing=[], conflicts=[],
                  procurement_key=case.get('procurement_key') or 'main', new_case=False, reply_sources=[])
    if latest['role'] != 'buyer' or case.get('state') in ('human_owned', 'closed'):
        return result
    if re.search(r'unsubscribe|stop contacting|do not contact|不要联系|停止联系|退订', latest['text'], re.I):
        return result
    intent = social_intent(latest['text']) if not latest.get('attachments') else None
    if intent:
        if not continuing and case.get('state') == 'waiting_quote' and intent in ('thanks', 'ack', 'farewell'):
            return result
        result.update(action='ask', reply=demo_social_reply(latest['text'], intent),
                      summary=case.get('summary', ''), missing=list(case.get('missing') or []),
                      reply_sources=[latest['message_id']])
        return validate_analysis(result, context)
    photo_rows = (context.get('photo_context') or {}).get('observations') or []
    photo = next((row for row in photo_rows if row.get('message_id') == latest['message_id']), None)
    if photo and latest['text'].strip() == '[image]':
        result.update(action='ask', facts={}, question_keys=[],
                      reply='Thanks for the photo. The image appears to show a green tote bag. Is this the design you have in mind?',
                      summary=case.get('summary', ''), missing=list(case.get('missing') or []),
                      reply_sources=[latest['message_id'], photo['observation_id']])
        return validate_analysis(result, context)
    buyers = [row for row in messages if row['role'] == 'buyer']
    boundary = re.compile(r'new (?:order|inquiry)|another (?:order|product)|separate order|新(?:采购|订单|需求)|另(?:一|外).*采购', re.I)
    boundaries = [index for index, row in enumerate(buyers) if boundary.search(row['text'])]
    if boundaries:
        result['new_case'] = buyers[boundaries[-1]]['message_id'] == latest['message_id']
        buyers = buyers[boundaries[-1]:]
        if result['new_case']:
            result['procurement_key'] = 'demo-' + latest['message_id']
    facts = {}
    for row in buyers:
        text = row['text']
        found = {}
        product = re.search(r'(?:product\s*[:：]|产品\s*[:：]|采购\s*[:：]|looking for|interested in|'
                            r'(?:i |we )?(?:need|want)|需要|想要|采购)\s*([^\n;；。.!?]+)', text, re.I)
        if product:
            value = re.sub(r'^\d[\d,]*(?:\.\d+)?\s*(?:(?:pieces?|pcs|units?|sets?|件|个|套)(?![a-z])\s*)?',
                           '', product[1], flags=re.I)
            value = re.split(r'\s+(?:with|ship|shipping)\b|，|,\s*(?:qty|quantity)|数量', value, maxsplit=1, flags=re.I)[0].strip(' ,，')
            if value and not re.fullmatch(r'[\d\s,.]+|pieces?|pcs|units?|samples?|a sample|一个样品', value, re.I):
                found['product'] = value
        for key, pattern in PATTERNS.items():
            match = re.search(pattern, text, re.I)
            if match:
                found[key] = match[0].strip()
        facts.update({key: {'value': value, 'evidence_ids': [row['message_id']]} for key, value in found.items()})
    existing = {} if result['new_case'] else case.get('facts') or {}
    values = {key: fact.get('value', '') for key, fact in existing.items()}
    values.update({key: fact['value'] for key, fact in facts.items()})
    required = list(dict.fromkeys(['product', *context.get('required_fields', ['product', 'quantity'])]))
    missing = [key for key in required if not is_complete(key, values.get(key, ''))]
    refused = bool(re.search(r'don.t know|not sure|prefer not|不知道|不确定|不提供|未确定', latest['text'], re.I))
    questions = [] if refused or continuing else [key for key in missing if (case.get('questions') or {}).get(key, 0) < 2][:2]
    human = bool(re.search(r'\bhuman\b|real person|refund|complaint|bank account|人工|业务员|退款|投诉|收款账户', latest['text'], re.I))
    action = 'human' if human or (missing and not questions and not continuing) else 'ask' if missing else 'wait_quote'
    if not continuing and case.get('state') == 'waiting_quote' and not facts and not result['new_case']:
        return result
    chinese = bool(re.search(r'[一-鿿]', latest['text']))
    sample = bool(re.search(r'samples?|样品', latest['text'], re.I))
    logo = bool(re.search(r'logo|标志|印标', latest['text'] + values.get('customization', ''), re.I))
    if human:
        reply = '我会把您的问题和现有需求交给业务员核实。' if chinese else "I'll pass your question and requirements to our team for review."
    elif sample:
        if chinese:
            reply = '已记下您的样品' + ('及 Logo 定制需求。' if logo else '需求。') + '样品费用和准备时间需要业务员核实。'
        else:
            reply = "I've noted your sample request" + (' with your logo. ' if logo else '. ') + "I'll confirm the sample charge and preparation time with our team."
    elif logo:
        reply = '已记下您的 Logo 定制需求，具体费用和制作安排需要业务员核实。' if chinese else "I've noted your logo customization request. Our team will confirm the charge and production details."
    elif action == 'wait_quote':
        reply = '需求已经记下，我会交给业务员核实适用报价。' if chinese else "Thanks for the details. I'll pass your requirements to our team to confirm the quotation."
    else:
        reply = ('您的问题需要业务员进一步核实，我会跟进。' if chinese else
                 "I'll check your question with our team and follow up.") if continuing else (
                 '谢谢您提供的信息。' if chinese else 'Thanks for sharing the details. ')
    en = {'product': 'which product you have in mind', 'quantity': 'the quantity and unit you need',
          'specifications': 'the key specifications', 'customization': 'your customization requirements',
          'destination': 'the delivery destination', 'delivery': 'your requested delivery date',
          'budget': 'your target budget', 'packaging': 'your packaging requirements', 'contact': 'your preferred contact details'}
    if action == 'ask' and questions:
        reply += ('请再告诉我' + '和'.join(FACT_LABELS[key] for key in questions) + '。') if chinese else (
            ' Could you let me know ' + ' and '.join(en[key] for key in questions) + '?')
    result.update(action=action, facts=facts, question_keys=questions if action == 'ask' else [], reply=reply,
                  summary='；'.join(FACT_LABELS[key] + '：' + value for key, value in values.items() if key in FIELDS),
                  missing=missing, reply_sources=list(dict.fromkeys([latest['message_id'],
                      *(mid for fact in facts.values() for mid in fact['evidence_ids'])])))
    return validate_analysis(result, context)


class DemoIntegration:
    def __init__(self, data_dir: Path, settings: dict):
        self.settings = settings
        self.seller_id = str(settings.get('seller_id') or 'demo-seller')
        self.messages = deepcopy(settings.get('demo_messages') or [])
        self.ledger = Ledger(Path(data_dir) / 'integrations-demo.sqlite3')
        self.lock = threading.RLock()
        self.permission_epoch = 0

    def capabilities(self):
        return dict(mode='demo', simulated=True, demo=True, local_only=True, verified=False,
                    read=True, ai=True, send=True, registration_enabled=False,
                    message='演示：本地合成数据与规则回复，未连接真实 Accio 或飞书')

    def runtime_status(self):
        return dict(ok=True, missing_variables=[], simulated=True, message='演示使用本地规则，无真实账号连接')

    def resolve_identity(self, expected_seller_id=''):
        if expected_seller_id and expected_seller_id != self.seller_id:
            raise ServiceError('identity_mismatch', '演示卖家身份不匹配', stage='identity')
        return self.seller_id

    def read_recent_conversations(self, seller_id, before_ms=None, count=20):
        self.resolve_identity(seller_id)
        rows = {}
        for message in self.messages:
            if message['seller_id'] != seller_id:
                raise ServiceError('identity_mismatch', '演示消息不属于当前卖家', stage='read')
            cid = message['conversation_id']
            previous = rows.get(cid)
            if previous is None or message['timestamp_ms'] > previous['latest_timestamp']:
                rows[cid] = dict(conversation_id=cid, buyer_id=message['buyer_id'],
                                 customer_name=message.get('customer_name', '演示客户'), latest_timestamp=message['timestamp_ms'])
        items = sorted(rows.values(), key=lambda row: row['latest_timestamp'], reverse=True)
        return self._page(items, 'latest_timestamp', before_ms, count, 'items')

    @staticmethod
    def _page(rows, timestamp_key, before_ms, count, result_key):
        if type(count) is not int or not 1 <= count <= 100 or (before_ms is not None and (type(before_ms) is not int or before_ms <= 0)):
            raise ServiceError('invalid_page', '分页数量或游标无效', stage='read')
        rows = [row for row in rows if before_ms is None or row[timestamp_key] < before_ms]
        page = rows[:count]
        # Keep timestamp ties on one local page so a numeric cursor loses no row.
        while len(page) < len(rows) and page and rows[len(page)][timestamp_key] == page[-1][timestamp_key]:
            page.append(rows[len(page)])
        has_more = len(rows) > len(page)
        return {result_key: deepcopy(page), 'has_more': has_more,
                'next_cursor': page[-1][timestamp_key] if has_more else None}

    def read_message_page(self, seller_id, buyer_id, conversation_id, before_ms=None, count=100):
        self.resolve_identity(seller_id)
        rows = [row for row in self.messages if row['conversation_id'] == conversation_id]
        if any(row['buyer_id'] != buyer_id for row in rows):
            raise ServiceError('identity_mismatch', '演示会话买家身份不匹配', stage='read')
        rows.sort(key=lambda row: row['timestamp_ms'], reverse=True)
        return self._page(rows, 'timestamp_ms', before_ms, count, 'messages')

    def load_photo(self, message):
        from io import BytesIO
        from PIL import Image, ImageDraw
        from ..image_assets import decode_image
        self.resolve_identity(message.get('seller_id'))
        if message.get('role') != 'buyer' or message.get('text', '').strip() != '[image]':
            raise ServiceError('image_not_found', '该演示消息没有图片')
        image = Image.new('RGB', (480, 360), '#f4f3ef')
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((150, 100, 330, 280), radius=18, fill='#668b83')
        draw.arc((182, 50, 298, 155), 180, 360, fill='#668b83', width=15)
        draw.text((16, 330), 'DEMO / SYNTHETIC IMAGE', fill='#44504c')
        output = BytesIO()
        image.save(output, 'PNG')
        return {**decode_image(output.getvalue(), 'image/png'), 'association': 'synthetic'}

    def observe_photo(self, message, asset, request_id, *, on_start):
        # Deterministic synthetic fixture only; never opens an external image.
        expected = self.load_photo(message)
        if asset.get('sha256') != expected['sha256']:
            raise ServiceError('photo_vision_unavailable', '演示图片不匹配', stage='analysis')
        on_start()
        return dict(description='演示合成图片：浅色背景上的绿色手提袋示意图。',
                    visible_text=['DEMO / SYNTHETIC IMAGE'],
                    uncertainties=['这是合成演示图，不能确认实物材质、尺寸或采购要求。'],
                    request_id='demo-vision-' + uuid.uuid4().hex, image_sha256=asset['sha256'])

    def generate(self, context, request_id, *, on_call_start=None):
        prepare_context(context)
        self.resolve_identity(context['case']['seller_id'])
        if not isinstance(request_id, str) or not request_id.strip():
            raise ServiceError('invalid_request', '生成请求缺少任务标识', stage='context')
        if on_call_start is not None:
            on_call_start()
        return natural_demo_analysis(context)

    def generate_manual(self, context, request_id, *, on_call_start=None, cancel_event=None):
        self.resolve_identity(context.get('seller_id'))
        if not isinstance(request_id, str) or not request_id.strip():
            raise ServiceError('invalid_request', '生成请求缺少任务标识', stage='context')
        if cancel_event is not None and cancel_event.is_set():
            raise ServiceError('cancelled', '请求已取消', stage='analysis')
        if on_call_start is not None:
            on_call_start()
        messages = context.get('recent_messages') or []
        buyer = next((m.get('text', '') for m in reversed(messages) if m.get('role') == 'buyer'), '')
        supplement = context.get('supplement') or ''
        if isinstance(supplement, dict):
            supplement = supplement.get('body', '')
        if re.search(r'[一-鿿]', buyer):
            reply = '感谢您的耐心等待。' + (f'关于您的询价，{supplement}。' if supplement else '我们已记录您的需求，会继续跟进。')
        else:
            reply = 'Thank you for your patience. ' + (f'Regarding your inquiry: {supplement}' if supplement else 'We have noted your request and will follow up.')
        return {'reply': reply[:1200]}

    def translate(self, items, request_id, *, on_call_start=None, cancel_event=None):
        if not isinstance(request_id, str) or not request_id.strip() or not isinstance(items, list) or not items:
            raise ServiceError('invalid_request', '翻译请求无效', stage='context')
        if any(not isinstance(row, dict) or not isinstance(row.get('key'), str) or not row['key'] or
               not isinstance(row.get('text'), str) or not row['text'] for row in items):
            raise ServiceError('invalid_request', '翻译来源无效', stage='context')
        if len({row['key'] for row in items}) != len(items):
            raise ServiceError('invalid_request', '翻译来源重复', stage='context')
        if cancel_event is not None and cancel_event.is_set():
            raise ServiceError('cancelled', '请求已取消', stage='analysis')
        if on_call_start is not None:
            on_call_start()
        fixtures = {'Hello': '你好', 'Thank you': '谢谢', 'How much?': '多少钱？',
                    'I need 100 pieces': '我需要100件', 'Bonjour': '你好'}
        return {'items': [{'key': row['key'], 'text': fixtures.get(row['text'], row['text'])} for row in items]}

    def preview_send(self, intent):
        valid = bool(isinstance(intent.get('reply'), str) and intent['reply'].strip() and len(intent['reply']) <= 1200)
        return dict(status='passed' if valid else 'blocked', submission_started=False, simulated=True,
                    message='演示：仅本地检查正文，不代表原生预演通过' if valid else '演示草稿正文为空')

    def _authorized(self, authorization, epoch):
        try:
            return callable(authorization) and authorization() is True and epoch == self.permission_epoch
        except Exception:
            return False

    def send(self, intent, authorization):
        with self.lock:
            epoch = self.permission_epoch
            try:
                self.resolve_identity(intent.get('seller_id'))
                if not self._authorized(authorization, epoch):
                    raise ServiceError('authorization_revoked', '发送临时权限已撤销', stage='send')
                if (self.preview_send(intent)['status'] != 'passed' or intent.get('send_mode') not in ('manual', 'automatic')
                        or any(not isinstance(intent.get(key), str) or not intent[key].strip() for key in (
                            'buyer_id', 'conversation_id', 'source_message_id', 'draft_id', 'case_id', 'operation_id'))):
                    raise ServiceError('invalid_intent', '发送正文或来源无效', stage='send')
                if intent.get('send_mode') == 'automatic':
                    scope = intent.get('frozen_scope') or {}
                    if (intent.get('budget_reserved') is not True or scope.get('mode') not in ('selected', 'all', 'incoming')
                            or (scope.get('mode') == 'selected' and intent.get('conversation_id') not in scope.get('conversation_ids', []))):
                        raise ServiceError('authorization_revoked', '自动发送范围或额度未就绪', stage='send')
                    if scope.get('mode') == 'incoming':
                        started, source_time = scope.get('started_at_ms'), intent.get('source_timestamp_ms')
                        if (scope.get('seller_id') != intent.get('seller_id') or type(started) is not int or started <= 0
                                or type(source_time) is not int or source_time < started):
                            raise ServiceError('authorization_revoked', '新消息范围、卖家或来源时间未通过', stage='send')
                operation = intent.get('operation_id')
                prior = self.ledger.claim(operation, 'send:' + digest([
                    self.seller_id, intent.get('conversation_id'), intent.get('source_message_id')]), 'send', intent,
                    allow_rejected_send=True, conversation_fence=True)
                if prior:
                    return dict(prior, simulated=True)
                if not self._authorized(authorization, epoch):
                    return self.ledger.finish(operation, dict(status='rejected', message_id='', submission_started=False,
                                                             simulated=True, message='演示发送权限已撤销'))
                mid, when = 'demo-send-' + uuid.uuid4().hex, time.time_ns() // 1_000_000
                self.messages.append(dict(message_id=mid, seller_id=self.seller_id, buyer_id=intent['buyer_id'],
                    conversation_id=intent['conversation_id'], role='seller', text=intent['reply'],
                    timestamp_ms=when, attachments=[], own=True))
                return self.ledger.finish(operation, dict(status='accepted', message_id=mid, timestamp_ms=when,
                    submission_started=True, simulated=True, message='演示：已在本地模拟发送，未联系客户'))
            except (ServiceError, AdapterError, OSError, TypeError, ValueError, KeyError):
                return dict(status='rejected', message_id='', submission_started=False, simulated=True,
                            message='演示发送的身份、内容或临时权限未通过')

    def _verify_continuity(self, intent, authorization, epoch):
        import hashlib
        import json
        from contextlib import closing
        from .accio_native.chat import authorized
        authorized(authorization)
        if self.permission_epoch != epoch or intent.get('budget_reserved') is not True:
            raise ServiceError('cancelled', '演示接待权限已撤销')
        scope = intent.get('frozen_scope', {})
        if (scope.get('seller_id') != self.seller_id or scope.get('mode') not in ('selected', 'all', 'incoming')
                or scope['mode'] != 'incoming' and intent['conversation_id'] not in scope.get('conversation_ids', [])):
            raise ServiceError('scope_required', '演示接待范围不符')
        rows = sorted((m for m in self.messages if m['conversation_id'] == intent['conversation_id']),
                      key=lambda m: (m['timestamp_ms'], m['message_id']))
        source = next((m for m in reversed(rows) if m['role'] == 'buyer'), None)
        latest = rows[-1] if rows else None
        sha = lambda text: hashlib.sha256(text.encode('utf-8')).hexdigest()
        if (not source or not latest or source['message_id'] != intent.get('source_message_id')
                or source['timestamp_ms'] != intent.get('source_timestamp_ms')
                or sha(source['text']) != intent.get('source_text_sha256')
                or latest['message_id'] != intent.get('latest_message_id')
                or latest['role'] != intent.get('latest_message_role') or sha(latest['text']) != intent.get('latest_text_sha256')
                or scope['mode'] == 'incoming' and source['timestamp_ms'] < scope.get('started_at_ms', 9007199254740991)):
            raise ServiceError('source_changed', '演示接待来源已变化')
        phase = intent.get('continuity_phase')
        if phase not in ('ack', 'final', 'assistance') or phase == 'ack' and latest['role'] != 'buyer':
            raise ServiceError('invalid_intent', '演示接待阶段无效')
        if phase == 'final' and latest['role'] != 'seller':
            raise ServiceError('invalid_intent', '演示正式答复缺少先前确认')
        if latest['role'] == 'seller':
            with self.ledger.lock, closing(self.ledger._db()) as db:
                records = db.execute("SELECT request,result FROM operations WHERE kind='send'").fetchall()
            matches = [request for raw, raw_result in records
                       for request, result in [(json.loads(raw), json.loads(raw_result))]
                       if result.get('status') == 'accepted' and result.get('message_id') == latest['message_id']
                       and request.get('reply') == latest['text']
                       and all(request.get(k) == intent[k] for k in ('seller_id', 'buyer_id', 'conversation_id'))]
            if len(matches) != 1 or phase == 'final' and not (
                    matches[0].get('purpose') == 'continuity' and matches[0].get('continuity_phase') == 'ack'
                    and matches[0].get('source_message_id') == source['message_id']):
                raise ServiceError('source_changed', '演示最新卖家消息缺少已受理依据')

    def send_chat(self, intent, authorization):
        from .accio_native.chat import authorized, chat_text, _claim_continuity
        with self.lock:
            claimed = attempted = False
            epoch = self.permission_epoch
            operation = intent.get('operation_id')
            try:
                self.resolve_identity(intent.get('seller_id'))
                continuity = intent.get('purpose') == 'continuity' and intent.get('send_mode') == 'automatic'
                if not continuity and (intent.get('purpose') != 'chat' or intent.get('send_mode') != 'manual'):
                    raise ServiceError('invalid_intent', '演示人工发送意图无效')
                if any(not isinstance(intent.get(key), str) or not intent[key].strip() for key in
                       ('buyer_id', 'conversation_id', 'client_request_id', 'draft_id', 'operation_id')):
                    raise ServiceError('invalid_intent', '演示人工发送归属不完整')
                rows = [m for m in self.messages if m['conversation_id'] == intent['conversation_id']]
                if rows and any(m['buyer_id'] != intent['buyer_id'] or m['seller_id'] != self.seller_id for m in rows):
                    raise ServiceError('identity_mismatch', '演示会话归属不符')
                chat_text(intent.get('reply'))
                authorized(authorization)
                if continuity:
                    previous = self.ledger.get(operation)
                    if previous:
                        if previous[1] != intent:
                            raise ServiceError('invalid_intent', '操作已绑定不同正文')
                        return previous[2]
                    self._verify_continuity(intent, authorization, epoch)
                    _claim_continuity(self, intent)
                else:
                    prior = self.ledger.claim(operation, 'chat:' + digest([
                        self.seller_id, intent['conversation_id'], intent['client_request_id']]), 'send', intent,
                        conversation_fence=True)
                    if prior:
                        return prior
                claimed = True
                authorized(authorization)
                if continuity:
                    self._verify_continuity(intent, authorization, epoch)
                attempted = True
                mid, when = 'demo-send-' + uuid.uuid4().hex, time.time_ns() // 1_000_000
                self.messages.append(dict(message_id=mid, seller_id=self.seller_id, buyer_id=intent['buyer_id'],
                    conversation_id=intent['conversation_id'], role='seller', text=intent['reply'],
                    timestamp_ms=when, attachments=[], own=True))
                return self.ledger.finish(operation, dict(status='accepted', message_id=mid, timestamp_ms=when,
                    submission_started=True, simulated=True, message='演示：已在本地模拟发送，未联系客户'))
            except Exception:
                result = dict(status='unknown' if attempted else 'rejected', message_id='', submission_started=attempted, simulated=True)
                return self.ledger.finish(operation, result) if claimed else result

    def register(self, case, previous, operation_id, *, authorization=None):
        return dict(status='disabled', record_id=previous.get('record_id', ''), number=previous.get('number', ''),
                    simulated=True, message='演示模式未写入飞书；需求保存在本机')

    def check_registration(self):
        return dict(status='disabled', simulated=True, message='演示模式不连接飞书')

    def revoke(self):
        # Deliberately independent of the potentially busy send lock.
        self.permission_epoch += 1

    def reconcile_operation(self, operation_id):
        record = self.ledger.get(operation_id)
        return dict(record[2]) if record else dict(status='unknown', message='没有该演示操作的回执')

    def inject_demo(self, conversation_id, text, buyer_id='', customer_name='演示客户'):
        if not isinstance(text, str) or not text.strip():
            raise ServiceError('invalid_message', '演示消息不能为空')
        message = dict(message_id='demo-' + uuid.uuid4().hex, seller_id=self.seller_id,
                       buyer_id=buyer_id or 'demo-buyer-' + digest(conversation_id)[:12], conversation_id=conversation_id,
                       role='buyer', own=False, text=text, timestamp_ms=time.time_ns() // 1_000_000,
                       attachments=[], customer_name=customer_name)
        self.messages.append(message)
        return deepcopy(message)
