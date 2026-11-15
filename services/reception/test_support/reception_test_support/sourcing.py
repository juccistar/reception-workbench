"""Synthetic sourcing fixtures, excluded from production."""
import time
from reception_service.integrations.sourcing import _error, _prompt, _hash, _INQUIRY_REASON

def demo_source_product(context, request_id, *, cancel_event=None, on_call_start=None, on_request_created=None):
    if cancel_event is not None and cancel_event.is_set():
        _error('cancelled')
    prompt = _prompt(context)
    if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        _error('args')
    record = dict(conversation_name='reception-source-' + _hash(request_id)[:32], agent_id='demo',
                  prompt_sha256=_hash(prompt), prompt=prompt, started_at=time.time(), request_id=request_id)
    if on_request_created is not None:
        on_request_created(record.copy())
    if cancel_event is not None and cancel_event.is_set():
        _error('cancelled')
    if on_call_start is not None:
        on_call_start()
    return _demo_result(context, request_id)


def demo_reconcile_source_product(record, context, *, cancel_event=None):
    """Recreate only the deterministic Demo result for the persisted request."""
    if cancel_event is not None and cancel_event.is_set():
        _error('cancelled')
    try:
        request_id, prompt = record['request_id'], record['prompt']
        if (not isinstance(request_id, str) or not request_id or len(request_id) > 200
                or record['agent_id'] != 'demo'
                or record['conversation_name'] != 'reception-source-' + _hash(request_id)[:32]
                or record['prompt_sha256'] != _hash(prompt) or prompt not in (_prompt(context), _prompt(context, legacy=True), _prompt(context, legacy='url'))):
            _error('args')
    except (KeyError, TypeError, AttributeError):
        _error('args')
    return _demo_result(context, request_id, prompt=prompt)


def _demo_result(context, request_id, *, prompt=None):
    offer = dict(supplier_product_id='1000000000001', product_url='https://www.alibaba.com/product-detail/demo_1000000000001.html', specifications='', moq='100', unit='pieces', unit_price='1.00', currency='USD', lead_time='', packaging='', freight='', tax_included=None, valid_until=None, source_type='page_reference', source_note='演示数据', customer_wording='', enabled=True, reply_enabled=False)
    supplier = dict(platform_supplier_id=None, name='演示供应商', product_url=None, contact='', offers=[offer])
    return dict(product_id=str(context['product_id']), product_url=None, product_images=[], suppliers=[supplier], execution=dict(conversation_name='reception-source-' + _hash(request_id)[:32], conversation_id=None, request_sha256=_hash(prompt if prompt is not None else _prompt(context)), request_id=request_id, tool_names=[], search_calls=0, result_source='demo'), inquiry_status='blocked', inquiry_reason=_INQUIRY_REASON)
