"""Pure tool/approval signal inspection; never returns external values."""
from __future__ import annotations

import json
import re

_TOOL_KEYS = frozenset(('toolCalls', 'tool_calls', 'pendingToolCalls', 'function_call', 'function_calls',
                        'toolResults', 'tool_results', 'requiresApproval', 'requires_approval', 'approvalRequired'))
_APPROVAL = re.compile(r'\b(?:tool approval\s+(?:is\s+)?required|approval required|requires?\s+tool approval|'
                       r'(?:pass|use|requires?)\s+--approve-tools)\b|'
                       r'\b(?:requiresApproval|requires_approval)\s*[:=]\s*(?:true|1)\b', re.I)
_FENCE = re.compile(r'```(?:json)?\s*\n?(.*?)```', re.I | re.S)


def _active(value):
    if isinstance(value, str):
        return value.strip().casefold() not in ('', 'false', '0', 'null', 'none')
    return bool(value)


def _objects(text):
    """Inspect complete JSON, fenced JSON and individual CLI JSON lines."""
    text = text.lstrip('﻿').strip()
    decoder = json.JSONDecoder()
    for candidate in (text, *_FENCE.findall(text)):
        # Decode at line boundaries so banners and multiple output records
        # cannot hide a complete, possibly pretty-printed JSON object.
        for match in re.finditer(r'(?m)^[ \t]*[\[{]', candidate):
            try:
                value, _ = decoder.raw_decode(candidate, match.end() - 1)
            except (ValueError, RecursionError):
                continue
            if isinstance(value, (dict, list)):
                yield value


def has_tool_signals(value):
    """Find active signals recursively, including JSON serialized in answers."""
    pending = [value]
    seen = {}
    while pending:
        node = pending.pop()
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                continue
            seen[id(node)] = node
        if isinstance(node, dict):
            if (node.get('role') in ('tool', 'tool_call', 'tool_result') or node.get('type') in (
                    'tool_use', 'tool_result', 'tool_call', 'function_call', 'server_tool_use')):
                return True
            if any(_active(node.get(key)) for key in _TOOL_KEYS):
                return True
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, bytes):
            pending.append(node.decode('utf-8', errors='replace'))
        elif isinstance(node, str):
            pending.extend(_objects(node))
    return False


def approval_required(*outputs):
    """Require a true structured signal or explicit prose, never a bare key."""
    pending = list(outputs)
    while pending:
        node = pending.pop()
        if isinstance(node, bytes):
            node = node.decode('utf-8', errors='replace')
        if isinstance(node, str):
            objects = list(_objects(node))
            if objects:
                pending.extend(objects)
            if _APPROVAL.search(node):
                return True
        elif isinstance(node, dict):
            if any(_active(node.get(key)) for key in ('requiresApproval', 'requires_approval')):
                return True
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return False


def output_has_tools(*outputs):
    return any(has_tool_signals(value) for value in outputs) or approval_required(*outputs)
