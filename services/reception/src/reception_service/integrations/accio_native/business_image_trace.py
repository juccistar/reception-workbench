"""Verify one business answer and only its exact, owned image-read trace."""
from __future__ import annotations

import re


def validate_business_trace(rows, cid, agent, prompt, images):
    from .visual_query import _ID, _unsafe, validate_read_progress
    if (not isinstance(rows, list) or not 4 <= len(rows) <= 2 * len(images) + 2 or _unsafe(rows)
            or not images or len(set(map(str, images))) != len(images)):
        raise ValueError('Invalid image trace')
    if any(not isinstance(row, dict) or row.get('conversationId') != cid
           or row.get('agentId') != agent for row in rows):
        raise ValueError('Foreign trace')
    if any('seq' in row for row in rows):
        sequence = [row.get('seq') for row in rows]
        if any(type(value) is not int for value in sequence) or any(a >= b for a, b in zip(sequence, sequence[1:])):
            raise ValueError('Invalid sequence')
    request, final = rows[0], rows[-1]
    rid, parent = request.get('requestId'), request.get('messageId')
    if (request.get('role') != 'req' or request.get('status') != 'finished'
            or request.get('content', '').replace('\r\n', '\n') != prompt.replace('\r\n', '\n')
            or request.get('tool_calls') or request.get('tool_progress')
            or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in (rid, parent))):
        raise ValueError('Request mismatch')
    expected = set(map(str, images))
    opened, call_ids = set(), set()
    index = 1
    while index < len(rows) - 1:
        call = rows[index]
        index += 1
        mid = call.get('messageId')
        calls = call.get('tool_calls')
        if (call.get('role') != 'res' or call.get('branchParentId') != parent
                or call.get('clientRequestId') != rid or not isinstance(mid, str)
                or not _ID.fullmatch(mid) or mid == request['messageId']
                or not isinstance(calls, list) or not 1 <= len(calls) <= len(expected) - len(opened)):
            raise ValueError('Tool call mismatch')
        progress = call.get('tool_progress')
        entries = {}
        for entry in calls:
            if not isinstance(entry, dict):
                raise ValueError('Invalid read')
            key, arguments = entry.get('id'), entry.get('arguments')
            if (not isinstance(key, str) or not _ID.fullmatch(key) or key in call_ids
                    or entry.get('name') != 'read' or not isinstance(arguments, dict)
                    or set(arguments) != {'file_path', 'image_prompt'}
                    or arguments.get('file_path') not in expected
                    or arguments['file_path'] in opened
                    or not isinstance(arguments.get('image_prompt'), str)
                    or not 0 < len(arguments['image_prompt'].strip()) <= 4096):
                raise ValueError('Unexpected tool or image')
            if not isinstance(progress, dict) or key not in progress or not validate_read_progress({key: progress[key]}, key):
                raise ValueError('Image read not completed')
            call_ids.add(key)
            opened.add(arguments['file_path'])
            entries[key] = arguments['file_path']
        if set(progress) != set(entries):
            raise ValueError('Unexpected progress')
        for _ in range(len(entries)):
            if index >= len(rows) - 1:
                raise ValueError('Missing image result')
            tool = rows[index]
            index += 1
            key = tool.get('tool_call_id')
            image = entries.pop(key, None)
            if (image is None or tool.get('role') != 'tool' or tool.get('name') != 'read'
                    or tool.get('status') != 'finished' or tool.get('messageId') != mid
                    or tool.get('parentMessageId') != mid or tool.get('branchParentId') != mid
                    or tool.get('tool_calls') or tool.get('tool_progress')
                    or not isinstance(tool.get('content'), str)
                    or not re.fullmatch(r'Loaded 1 image:\r?\n  ' + re.escape(image)
                        + r' → https://[A-Za-z0-9-]+\.alicdn\.com/[^\s]+', tool['content'].strip())):
                raise ValueError('Unverified image result')
        parent = mid
    answer = final.get('content')
    if (opened != expected or final.get('role') != 'res' or final.get('status') != 'finished'
            or final.get('clientRequestId') != rid or final.get('branchParentId') != parent
            or not isinstance(final.get('messageId'), str) or not _ID.fullmatch(final['messageId'])
            or final.get('tool_calls') or final.get('tool_progress')
            or not isinstance(answer, str) or not answer.strip()):
        raise ValueError('Business answer mismatch')
    return answer, rid
