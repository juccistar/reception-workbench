"""Local receipt watermarks; no platform read operation is performed.

Unknown late history at or before the frozen timestamp frontier is treated as
history. A genuinely new message with such a timestamp cannot be distinguished
from a delayed historical page and is conservatively excluded.
"""
from .errors import ServiceError
from .repository import encoded


def state(repo, cid):
    return repo.get('unread_state', cid, {'conversation_id': cid, 'initialized': False,
                                           'sequence': 0, 'cutoff': None})


def initialize(repo, conversation):
    cid = conversation['conversation_id']
    existing = repo.get('unread_state', cid)
    if existing is not None:
        return existing
    messages = repo.list('message', conversation_id=cid)
    stamps = [m['timestamp_ms'] for m in messages if type(m.get('timestamp_ms')) is int]
    result = {'conversation_id': cid, 'initialized': bool(messages), 'sequence': 0,
              'cutoff': max(stamps) if stamps else None}
    repo.put('unread_state', cid, result)
    if conversation.get('unread_count', 0):
        conversation['unread_count'] = 0
        repo.put('conversation', cid, conversation)
    return result


def ingest(repo, conversation, incoming, *, history=False, changes=None):
    cid = conversation['conversation_id']
    current = initialize(repo, conversation)
    changes = incoming if changes is None else changes
    if not changes:
        return
    baseline = not current['initialized'] or current['cutoff'] is None
    if baseline:
        stamps = [m['timestamp_ms'] for m in incoming if type(m.get('timestamp_ms')) is int]
        current.update(initialized=True, cutoff=max(stamps) if stamps else None)
    # Every changed message has an observation version, including sellers and
    # corrections. An acknowledgement made before a correction cannot silently
    # consume a changed boundary or a message moved outside the visible range.
    for msg in changes:
        key = encoded([cid, msg['message_id']])
        current['sequence'] += 1
        repo.put('message_observation', key, {'conversation_id': cid,
                 'message_id': msg['message_id'], 'sequence': current['sequence']})
        receipt = repo.get('unread_receipt', key)
        if receipt:
            receipt.update(timestamp_ms=msg['timestamp_ms'], sequence=current['sequence'])
            if msg['role'] != 'buyer' and not receipt['read']:
                receipt['read'] = True
                conversation['unread_count'] = max(0, conversation.get('unread_count', 0) - 1)
            repo.put('unread_receipt', key, receipt)
    if not baseline:
        for msg in incoming:
            stamp = msg.get('timestamp_ms')
            if msg['role'] != 'buyer' or type(stamp) is not int or stamp <= current['cutoff']:
                continue
            key = encoded([cid, msg['message_id']])
            observation = repo.get('message_observation', key)
            # Even a history fill can encounter a newer message. Its frozen
            # timestamp frontier, not the request's label, decides eligibility.
            repo.put('unread_receipt', key, {'conversation_id': cid, 'message_id': msg['message_id'],
                     'timestamp_ms': stamp, 'sequence': observation['sequence'], 'read': False})
            conversation['unread_count'] = conversation.get('unread_count', 0) + 1
    repo.put('unread_state', cid, current)


def establish_empty_baseline(repo, cid, cutoff):
    current = state(repo, cid)
    if not current['initialized'] and type(cutoff) is int and cutoff >= 0:
        current.update(initialized=True, cutoff=cutoff)
        repo.put('unread_state', cid, current)


def finish_sync(repo, cid):
    current = state(repo, cid)
    stamps = [m['timestamp_ms'] for m in repo.list('message', conversation_id=cid)
              if type(m.get('timestamp_ms')) is int]
    if stamps:
        current['cutoff'] = max(stamps + ([current['cutoff']] if current['cutoff'] is not None else []))
        repo.put('unread_state', cid, current)


def reading(repo, cid):
    value = state(repo, cid)
    return {'observed_sequence': value['sequence'], 'initialized': value['initialized']}


def acknowledge(repo, conversation, message_id, observed):
    cid = conversation['conversation_id']
    if not isinstance(message_id, str) or not message_id or type(observed) is not int or observed < 0:
        raise ServiceError('invalid_params', '阅读边界无效')
    current = initialize(repo, conversation)
    message = repo.get('message', encoded([cid, message_id]))
    if not message:
        raise ServiceError('invalid_cursor', '阅读边界不属于当前客户')
    if observed > current['sequence']:
        raise ServiceError('invalid_params', '阅读水位超出已接收消息')
    boundary_observation = repo.get('message_observation', encoded([cid, message_id]))
    if boundary_observation and boundary_observation['sequence'] > observed:
        raise ServiceError('invalid_params', '阅读边界已变化，请刷新后确认')
    boundary = (message['timestamp_ms'] if message['timestamp_ms'] is not None else -1, message_id)
    for receipt in repo.list('unread_receipt', conversation_id=cid):
        if (not receipt['read'] and receipt['sequence'] <= observed
                and (receipt['timestamp_ms'] if receipt['timestamp_ms'] is not None else -1, receipt['message_id']) <= boundary):
            receipt['read'] = True
            repo.put('unread_receipt', encoded([cid, receipt['message_id']]), receipt)
    conversation['unread_count'] = sum(not r['read'] for r in repo.list('unread_receipt', conversation_id=cid))
    repo.put('conversation', cid, conversation)
    return {'conversation_id': cid, 'unread_count': conversation['unread_count'],
            'observed_sequence': observed}
