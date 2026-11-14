"""Normalization and stable fingerprints for ordered AI reply messages."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def reply_display_body(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(message["body"] for message in messages)


def reply_messages_hash(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_reply_output(
    output: dict[str, Any], *, allow_legacy: bool = False, allow_empty: bool = False, max_chars: int = 1200
) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ValueError("reply output must be an object")
    if "reply_messages" in output:
        raw = output["reply_messages"]
    elif allow_legacy and "reply" in output:
        raw = [{"body": output["reply"], "reply_sources": output.get("reply_sources", [])}]
    else:
        raise ValueError("reply_messages is required")
    if not isinstance(raw, list) or len(raw) > 3 or (not raw and not allow_empty):
        raise ValueError("reply_messages must contain 1 to 3 messages")
    messages = []
    sources: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("body"), str) or not item["body"].strip():
            raise ValueError("each reply message must have a nonempty body")
        item_sources = item.get("reply_sources")
        if not isinstance(item_sources, list) or any(not isinstance(source, str) for source in item_sources):
            raise ValueError("reply_sources must be an array of strings")
        messages.append({"body": item["body"], "reply_sources": list(item_sources)})
        for source in item_sources:
            if source not in sources:
                sources.append(source)
    if sum(len(message["body"]) for message in messages) > max_chars:
        raise ValueError("reply messages exceed character budget")
    result = dict(output)
    result.update(reply_messages=messages, reply=reply_display_body(messages), reply_sources=sources)
    return result
