"""Local reply semantics: ordinary conversation versus verifiable seller terms.

These checks deliberately look for relationships (an amount, a stock assertion,
a deadline or a guarantee), rather than treating commercial nouns as promises.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re


MONEY = re.compile(
    r'\b(?:USD|EUR|GBP|CNY|RMB|AUD|CAD|JPY)\s*\d|[$€£¥￥]\s*\d|'
    r'\d\s*(?:美元|欧元|英镑|人民币|元)|\d+(?:\.\d+)?\s*(?:USD|EUR|GBP|CNY|RMB|AUD|CAD|JPY)\b|'
    r'\d+(?:\.\d+)?\s*(?:/|per\s+)(?:piece|unit|pc|set)', re.I)
NUMBER = re.compile(r'\d+(?:[.,]\d+)*')
DEFERRED = re.compile(
    r'\b(?:check\w*|confirm\w*|verif\w*|pending|unconfirmed|unknown|subject to|'
    r'not (?:yet )?(?:confirmed|verified)|to be determined|still (?:checking|confirming)(?: with (?:our|the) team)?)\b|待(?:核实|确认|核价|定)|'
    r'(?:核实|核价|尚不确定|尚未确认)', re.I)
_QUESTION = re.compile(
    r'^\s*(?:(?:\d{1,2}[.)、]|\(\d{1,2}\))\s*)?'
    r'(?:can|could|would|do|does|did|will|are|is|have|has|what|which|when|where|how)\b|'
    r'^\s*(?:请问|能否|是否|您是否|你是否)|'
    r'^\s*(?:您|你).{0,20}(?:需要|想要|希望|想了解).{0,50}[吗呢]\s*$|'
    r'^\s*(?:样品|运费|交期|库存).{0,25}吗\s*$', re.I)
_BUYER = re.compile(
    r'\b(?:your|buyer[’\x27]?s|customer[’\x27]?s)\b.{0,60}\b(?:budget|target price|requested|desired|required|preferred)\b|'
    r'\byou\s+(?:mentioned|requested|asked|need|specified|want)\b|'
    r'(?:您|你|买家|客户)(?:的|提出的|要求的|希望的)?.{0,30}(?:预算|目标价|期望价|要求|需求)', re.I)
_DISCUSSION = re.compile(
    r'\b(?:discuss|explore|talk about|ask about|questions? about|requirements? for)\b|'
    r'讨论|沟通|想了解|请(?:告诉|说明)|是否', re.I)
_SELLER = r'(?:(?:we|i)(?:\s+(?:can|will|shall))?|we[’\x27]ll|i[’\x27]ll)'
_AMOUNT = r'(?:\d|USD|EUR|GBP|CNY|RMB|[$€£¥￥])'
# An unresolved question may contain amounts; a price/offer actually asserted
# alongside a disclaimer must remain visible.
_BOUND_TERM = re.compile(
    rf'\b(?:our\s+)?(?:price|quote|quotation|cost|charge|fee|discount|stock|inventory|lead[ -]?time|delivery)\s+'
    rf'(?:is|are|will be|can be|:|为)\s*(?:only\s+)?{_AMOUNT}|'
    rf'\bour\s+(?:price|quote|quotation)\s+of\s+{_AMOUNT}|'
    r'\b(?:shipping|delivery|lead[ -]?time|turnaround)\s+(?:takes?|is|will be)\s+\d|'
    rf'\b{_SELLER}\s+(?:offer|quote|charge|provide|give|match|meet|guarantee|ship|deliver|have|hold)\b|'
    r'\b(?:samples?|shipping|delivery|freight)\s+(?:is|are|will be)\s+(?:free|complimentary|at no (?:charge|cost))\b|'
    r'\b(?:free\s+(?:samples?|shipping)|in stock)\s+(?:is|are|available|supported)\b|'
    r'\b(?:is|are|will be|remain)\s+(?:(?:ISO\s*\d+|CE|FDA)\s+)?(?:in stock|certified|guaranteed|accredited)\b|'
    r'^\s*(?:in stock|ready to ship|guaranteed)\b|'
    r'(?:报价|价格|单价|收费|费用|折扣|交期)(?:是|为|定为|只需|仅需)|'
    r'(?:我们|我方|本公司)(?:可以|会|将)?(?:报价|收费|保证|发货|交付)|'
    r'(?:样品|运费)(?:是|为)?免费|(?:有|剩余)\s*\d+\s*(?:件|个|套).{0,8}现货', re.I)
_FREE = re.compile(
    r'\b(?:free|complimentary)\s+(?:samples?|shipping|delivery|freight)\b|'
    r'\b(?:samples?|shipping|delivery|freight|sample\s+(?:fee|charge))\s+(?:is|are|will be|comes?)\s+(?:free|complimentary)\b|'
    r'\b(?:samples?|shipping|delivery|freight)\b.{0,20}\bat no (?:charge|cost)\b|'
    r'\b(?:no|zero)\s+(?:sample|shipping|delivery|freight)\s+(?:fee|charge|cost)\b|'
    r'免费(?:样品|寄样|运费|运输|配送)|(?:样品|运费|寄样)(?:是|为)?免费|免(?:样品费|运费)', re.I)
_STOCK = re.compile(
    r'\bin stock\b|\b(?:ready|available)\s+(?:now|immediately|to ship|for immediate)\b|'
    r'\b(?:have|hold)\b.{0,35}\b(?:stock|inventory)\b|'
    r'\b(?:stock|inventory)\s+(?:is|are|stands at)\s+(?:available|\d)|'
    r'\b(?:have|hold)\s+\d+.{0,35}\bavailable\b|'
    r'现货|(?:有|剩余|当前|现在).{0,10}库存|(?:立即|马上|随时)(?:发货|出货)', re.I)
_DEADLINE = re.compile(
    r'\b(?:ship\w*|deliver\w*|dispatch\w*|lead[ -]?time|turnaround|arriv\w*)\b.{0,60}'
    r'\b(?:\d+\s*(?:business\s+|working\s+)?(?:hours?|days?|weeks?|months?)|'
    r'today|tomorrow|tonight|immediately|within|by\s+\w+)\b|'
    r'\b(?:ship|deliver|dispatch)\s+(?:it\s+)?(?:now|right away)\b|'
    r'(?:交期|发货|交付|送达|出货).{0,20}(?:\d+\s*[天日周月小时]|今天|明天|立即)|'
    r'(?:\d+\s*[天日周月]|今天|明天|立即).{0,12}(?:发货|交付|送达|出货)', re.I)
_ASSURANCE = re.compile(
    rf'\b{_SELLER}\s+(?:guarantee|promise|ensure)\b|\b(?:guaranteed|certified|accredited)\b|'
    r'\b(?:CE|FDA|ISO\s*\d+)\s+(?:approved|compliant)\b|'
    r'(?:我们|我方|本公司).{0,8}(?:保证|承诺|确保)|已.{0,10}认证|通过.{0,12}认证|保证.{0,10}(?:质量|交付|送达)', re.I)
_PRICE = re.compile(
    r'\b(?:price|quote|quotation|cost|charge|fee|discount)\b.{0,30}(?:\d|free)|'
    r'\d+(?:\.\d+)?\s*%\s*(?:discount|off)\b|'
    rf'\b{_SELLER}\s+(?:meet|match)\s+(?:your\s+)?(?:budget|target price)\b|'
    r'(?:报价|价格|单价|收费|费用|折扣|优惠).{0,15}(?:\d|免费)|'
    r'(?:满足|匹配|接受).{0,8}(?:预算|目标价)', re.I)


def reply_clauses(body, *, split_seller=True):
    """Split independent statements without splitting decimal prices or e.g."""
    layout = re.sub(r'\be\.g\.', lambda match: ' ' * len(match[0]), body, flags=re.I)
    boundary = re.compile(
        r'\.(?=\s|$)|[!?。！？;；\r\n]|(?:[,，]|\b(?:and|but|however)\b)\s*'
        r'(?=(?:(?:and|but)\s+)?(?:our\b|we\b|i\b|the\b|我们|我方|本公司|报价|价格|费用|交期))', re.I)
    start = 0
    for match in boundary.finditer(layout):
        if not split_seller and not re.fullmatch(r'\.(?=\s|$)|[!?。！？;；\r\n]', match[0]):
            continue
        if body[start:match.start()].strip():
            yield body[start:match.start()]
        start = match.end()
    if body[start:].strip():
        yield body[start:]


def _is_commitment(clause):
    if _QUESTION.search(clause):
        return False
    # A verification question has no asserted answer. Limit the removed span
    # to that complement, so a later "but we offer ..." remains enforceable.
    clause = re.sub(
        r'\b(?:check\w*|confirm\w*|verify|ask|consult)\b.{0,35}?\b(?:whether|if)\b.*?'
        r'(?=\b(?:but|however)\b|\band\s+(?:we|i|our|the)\b|$)', '', clause, flags=re.I)
    bound = _BOUND_TERM.search(clause)
    if not bound and (_BUYER.search(clause) or DEFERRED.search(clause) or _DISCUSSION.search(clause)):
        return False
    # Conditional possession of order details does not assert current inventory.
    statement = re.sub(r'\b(?:once|when|after|as soon as)\s+(?:we|i)\s+have\b', '', clause, flags=re.I)
    return bool(MONEY.search(statement) or _FREE.search(statement) or _STOCK.search(statement)
                or _DEADLINE.search(statement) or _ASSURANCE.search(statement) or _PRICE.search(statement))


def seller_commitments(body):
    """Return the actual seller claim clauses, retaining their scope/conditions."""
    return [clause for clause in reply_clauses(body) if _is_commitment(clause)]


def has_seller_commitment(body):
    """Whether the reply contains an actual seller term needing seller evidence."""
    return any(_is_commitment(clause) for clause in reply_clauses(body))


def _claim_signature(clause):
    """Normalize phrasing, never the commercial subject, unit or conditions.

    Equality of the remaining terms is intentional: a policy with an MOQ,
    destination, incoterm, caveat or different fee basis cannot authorize an
    unconditional offer merely because its price happens to be the same.
    """
    text = clause.casefold().replace('’', "'")
    text = re.sub(r'^(?:\s*(?:thanks?(?: you)?(?: for (?:asking|your (?:message|question)))?|'
                  r'of course|certainly|sure|happy to help|yes)[!,，.\s]+)+', '', text)
    text = re.sub(r'\b(?:for you|as requested|please let (?:me|us) know)\b[!.\s]*$', '', text)
    for pattern, replacement in (
        (r'\bcomplimentary\b|\bat no (?:charge|cost)\b', ' free '),
        (r'\bus dollars?\b|\bdollars?\b|\bus\$|\$', ' usd '),
        (r'€|\beuros?\b', ' eur '), (r'£', ' gbp '),
        (r'人民币|\brmb\b|[¥￥]', ' cny '), (r'美元', ' usd '),
        (r'\b(?:pieces?|pcs?|units?)\b', ' unit '),
        (r'\b(?:quotations?|quotes?|prices?)\b', ' price '),
        (r'\b(?:costs?|charges?|fees?)\b', ' fee '),
        (r'\b(?:freight|shipping|delivery)\b(?=.{0,15}\bfree\b)|\bfreight\b', ' shipping '),
        (r'\b(?:dispatch|ships?|shipping)\b', ' ship '),
        (r'\b(?:samples?)\b', ' sample '),
        (r'\bno\s+(sample|ship)\s+fee\b', r'free \1'),
        (r'\b(?:we|i)(?:\s+(?:can|will|shall))?\s+(?:offer|provide|give)\b', ' '),
        (r'\b(?:our|the|a|an|is|are|be|will|can|of|at|per|for|to|you|we|i|supported|available)\b', ' '),
    ):
        text = re.sub(pattern, replacement, text)
    # A decimal's presentation is irrelevant; the unit and currency are not.
    text = NUMBER.sub(lambda match: numeric_value(match[0]), text)
    text = re.sub(r'\b(sample|ship)\s+free\b', r'free \1', text)
    text = re.sub(r'\bunit\s+price\s+((?:usd|eur|gbp|cny)\s+\d+(?:\.\d+)?)\b',
                  r'price \1 unit', text)
    # Preserve order: swapping the sample/shipping amounts changes the offer.
    return tuple(re.findall(r'[a-z]+|\d+(?:\.\d+)?|%|[一-鿿]+', text))


def numeric_value(number):
    if ',' in number:
        if re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?', number):
            number = number.replace(',', '')
        elif re.fullmatch(r'\d+,\d{1,2}', number):
            number = number.replace(',', '.')
        else:
            return number
    try:
        return format(Decimal(number).normalize(), 'f')
    except InvalidOperation:
        # Version-like strings are not valid decimal amounts.
        return number


def commitment_supported(clause, policy_contents):
    signature = _claim_signature(clause)
    return any(signature == _claim_signature(policy_clause)
               for content in policy_contents for policy_clause in reply_clauses(content, split_seller=False)
               if _is_commitment(policy_clause) and not DEFERRED.search(policy_clause))


_FACT = re.compile(
    r'\byou\s+(?:(?:have|had)\s+)?(?:need|want|requested|confirmed|specified|ordered|selected|chose|mentioned)\b|'
    r'\byour\b.{0,45}\b(?:is|are|was|were|of)\b|'
    r'\b(?:capacity|size|quantity|volume|weight|dimensions?|budget)\s+(?:is|are|equals?)\b|'
    r'\b(?:we|i)\s+have\s+\d|'
    r'(?:您|你|客户|买家).{0,12}(?:需要|已确认|选择|订购)|(?:容量|数量|尺寸|重量|预算)(?:是|为)', re.I)
_MEASURE = re.compile(
    r'\d[\d.,]*\s*(?:oz|ounces?|ml|millilit(?:er|re)s?|lit(?:er|re)s?|mm|cm|inches?|'
    r'kg|kilograms?|g|grams?|pieces?|pcs?|units?|sets?|cartons?|boxes|box|%|件|个|套|毫升|厘米|千克)\b', re.I)
_OPTIONS = re.compile(
    r'\b(?:for example|such as|e\.g\.|options?|consider|choose|prefer|would you like)\b|'
    r'例如|比如|可选|您可以选择|你可以选择', re.I)


def numbers_needing_evidence(body):
    """Numbers in assertions need evidence; chat and suggested choices do not."""
    markers = {match.span(1) for match in re.finditer(r'(?m)^[ \t]*\(?(\d{1,2})[.)、](?=[ \t]+\S)', body)}
    inline = list(re.finditer(r'(?<![\w.])(\d{1,2})\)[ \t]+(?=\S)', body))
    if len(inline) >= 2 and [int(match[1]) for match in inline] == list(range(1, len(inline) + 1)):
        markers.update(match.span(1) for match in inline)
    offset = 0
    for clause in reply_clauses(body):
        start = body.index(clause, offset)
        offset = start + len(clause)
        commitment = _is_commitment(clause)
        asserted = bool(_FACT.search(clause) or (
            _MEASURE.search(clause) and re.search(
                rf'\b{_SELLER}\s+(?:offer|have|provide)\b', clause, re.I)))
        # Questions/options do not establish a quantity, price or delivery term.
        # An explicit "you confirmed" or "your capacity is" still asserts a fact.
        if not commitment and _QUESTION.search(clause):
            continue
        if not commitment and not asserted and (
                _OPTIONS.search(clause) or _DISCUSSION.search(clause) or re.search(
                    r'\b(?:check\w*|confirm\w*|verify|ask|consult)\b.{0,35}\b(?:whether|if)\b', clause, re.I)):
            continue
        if commitment or asserted or _MEASURE.search(clause) or MONEY.search(clause):
            for match in NUMBER.finditer(clause):
                if (start + match.start(), start + match.end()) not in markers:
                    yield match[0]
