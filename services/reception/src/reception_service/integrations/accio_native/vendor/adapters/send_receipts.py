"""Read-only workflow receipt recovery; never invokes send-message or guesses an ID.

Native stdout is an envelope, not a flat record. Batch and item statuses may
legitimately differ. Durable receipt references and returned message IDs are kept
before readback. A pre-submit inventory also permits bounded recovery after a
CLI timeout; pre-existing, unchanged receipts are never claimed that way.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .common import AdapterError, target

MAX_RECEIPTS = 2000
MAX_BYTES = 128 * 1024


def _error():
    return AdapterError('发送回执关联未核实，保留未知，禁止重发', code='send_receipt_invalid', stage='send')


def _root(gateway):
    from reception_service.workspace_lock import reject_links
    try:
        data_dir = Path(gateway.data_dir)
        receipt_dir = data_dir / 'workctl-state' / 'chat-analysis' / 'send-receipts'
        reject_links(receipt_dir)
        data_dir, root = data_dir.resolve(), receipt_dir.resolve()
        if not root.is_relative_to(data_dir):
            raise _error()
        return root
    except (OSError, ValueError):
        raise _error()


def _file(gateway, reference):
    if not isinstance(reference, str) or not reference or len(reference) > 2048:
        raise _error()
    root = _root(gateway)
    from reception_service.workspace_backup import _absolute_parts, _receipt_roots, PROVENANCE
    from reception_service.workspace_lock import reject_links
    from reception_service.storage_guard import StorageError
    from pathlib import PureWindowsPath
    path = Path(reference)
    historical = False
    if path.is_absolute() or PureWindowsPath(reference).is_absolute():
        try:
            kind, normalized, original = _absolute_parts(reference)
            current_kind, current_parts, _ = _absolute_parts(str(root))
            if kind == current_kind and normalized[:len(current_parts)] == current_parts:
                relative = original[len(current_parts):]
            else:
                workspace = Path(gateway.data_dir).parent
                matches = []
                for item in _receipt_roots(workspace):
                    root_kind, parts, _ = _absolute_parts(item)
                    if kind == root_kind and normalized[:len(parts)] == parts and len(normalized) > len(parts):
                        matches.append(original[len(parts):])
                if len(matches) != 1:
                    raise _error()
                relative = matches[0]
                historical = True
            if not relative:
                raise _error()
            path = root.joinpath(*relative)
        except (ValueError, OSError, StorageError):
            raise _error()
    else:
        parts = reference.replace('\\', '/').split('/')
        if any(part in ('', '.', '..') or ':' in part for part in parts):
            raise _error()
        path = root.joinpath(*parts)
    try:
        reject_links(path)
        path = path.resolve()
        if not path.is_relative_to(root) or path.suffix != '.json' or not path.is_file():
            raise _error()
        if path.stat().st_size > MAX_BYTES:
            raise _error()
        if historical:
            provenance = Path(gateway.data_dir).parent / PROVENANCE
            reject_links(provenance)
            data = json.loads(provenance.read_text(encoding='utf-8'))
            expected = data.get('receipt_hashes', {}).get(path.relative_to(root).as_posix())
            if not isinstance(expected, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise _error()
    except (OSError, ValueError, TypeError, AttributeError):
        raise _error()
    if path.stat().st_size > MAX_BYTES:
        raise _error()
    return path


def inventory(gateway):
    """Metadata only, confined to this workbench's private workctl state."""
    root = _root(gateway)
    result = {}
    if root.exists():
        for path in root.rglob('*.json'):
            if len(result) >= MAX_RECEIPTS:
                raise _error()
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or not path.is_file():
                raise _error()
            stat = path.stat()
            result[str(path.relative_to(root))] = [stat.st_mtime_ns, stat.st_size]
    return result


def _id(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256 and all(ord(c) >= 32 for c in value)


def stdout_evidence(value, conversation_id):
    """Collect atomic references/IDs, without merging statuses across JSON levels.

    These are lookup hints only. Acceptance always requires an exact platform
    readback. Conflicting conversation IDs disqualify that subtree; more than one
    candidate remains ambiguous. Neither businessID nor a body search supplies ID.
    """
    mids, refs, keys = set(), set(), set()
    pending = [(value, False)]
    nodes = 0
    while pending:
        node, excluded = pending.pop()
        nodes += 1
        if nodes > 10000:
            raise _error()
        if isinstance(node, list):
            pending.extend((item, excluded) for item in node)
        elif isinstance(node, dict):
            cids = [node[key] for key in ('conversationID', 'conversationId', 'conversation_id') if key in node]
            excluded = excluded or any(cid != conversation_id for cid in cids)
            if not excluded:
                for key in ('messageID', 'messageId', 'message_id'):
                    if _id(node.get(key)):
                        mids.add(node[key])
                if isinstance(node.get('receiptFile'), str):
                    refs.add(node['receiptFile'])
                if _id(node.get('idempotencyKey')):
                    keys.add(node['idempotencyKey'])
            pending.extend((child, excluded) for child in node.values() if isinstance(child, (dict, list)))
    if len(mids) > 1 or len(refs) > 1 or len(keys) > 1:
        raise _error()
    result = {}
    if mids:
        result['message_id'] = next(iter(mids))
    if refs:
        result['native_receipt_ref'] = next(iter(refs))
    if keys:
        result['native_idempotency_key'] = next(iter(keys))
    return result


def read_native_receipt(gateway, intent, reference, *, expected_mid=None, expected_key=None):
    path = _file(gateway, reference)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise _error()
    key, mid = data.get('idempotencyKey'), data.get('messageID')
    if (data.get('schema') != 'chat_analysis_send_receipt_v1' or data.get('status') != 'sent'
            or data.get('sellerAliId') != intent['seller_id'] or data.get('contactAliId') != intent['buyer_id']
            or data.get('conversationID') != intent['conversation_id']
            or data.get('contentSha256') != hashlib.sha256(intent['reply'].encode('utf-8')).hexdigest()
            or not _id(mid) or not _id(key) or path.stem != key
            or expected_mid is not None and mid != expected_mid
            or expected_key is not None and key != expected_key):
        raise _error()
    return dict(message_id=mid, native_receipt_ref=str(path.relative_to(_root(gateway))), native_idempotency_key=key)


def _new_receipt(gateway, intent, result):
    baseline, started = result.get('receipt_baseline'), result.get('receipt_submission_ns')
    if not isinstance(baseline, dict) or type(started) is not int or started <= 0:
        return {}
    current = inventory(gateway)
    candidates = []
    for reference, metadata in current.items():
        if metadata == baseline.get(reference) or metadata[0] < started:
            continue
        try:
            candidate = read_native_receipt(gateway, intent, reference,
                expected_mid=result.get('message_id') or None,
                expected_key=result.get('native_idempotency_key'))
        except (AdapterError, OSError, ValueError, TypeError, KeyError):
            continue
        candidates.append(candidate)
    if len(candidates) != 1:
        return {}
    return candidates[0]


def reconcile_receipt(gateway, intent, result, *, receipt_ref=None, expected_mid=None):
    """Reconcile a bound ID/ref, or a unique new receipt from this submit window.

    receipt_ref + expected_mid is an explicit repair reference for old operations
    without a submit journal; both are independently known, never inferred from a
    matching old body. The CLI-owned receipt is read, never changed or deleted.
    """
    seller, buyer, cid = intent['seller_id'], intent['buyer_id'], intent['conversation_id']
    target(seller, buyer, cid)
    pending = dict(result, status='unknown', submission_started=True)
    reference = receipt_ref if receipt_ref is not None else result.get('native_receipt_ref')
    if receipt_ref is not None and not _id(expected_mid):
        raise _error()
    if reference:
        evidence = read_native_receipt(gateway, intent, reference,
            expected_mid=expected_mid or result.get('message_id') or None,
            expected_key=result.get('native_idempotency_key'))
    else:
        evidence = _new_receipt(gateway, intent, result)
    if evidence:
        pending.update(evidence)
        # A later readback failure cannot erase this exact native message ID.
        gateway.ledger.finish(intent['operation_id'], pending)
    mid = pending.get('message_id')
    if not _id(mid):
        return pending
    gateway._identity(seller)
    history = gateway._history(seller, buyer, cid)
    matches = [row for row in history if row.message_id == mid]
    sources = [row for row in history if row.message_id == intent.get('source_message_id')]
    if (len(matches) == 1 and len(sources) == 1 and sources[0].role == 'buyer'
            and matches[0].role == 'seller' and matches[0].text == intent['reply']
            and matches[0].conversation_id == cid and matches[0].timestamp_ms > sources[0].timestamp_ms
            and (intent.get('source_timestamp_ms') is None or sources[0].timestamp_ms == intent['source_timestamp_ms'])):
        return dict(pending, status='accepted', timestamp_ms=matches[0].timestamp_ms,
                    detail='已按原生回执和确切平台消息编号核实发送')
    return pending
