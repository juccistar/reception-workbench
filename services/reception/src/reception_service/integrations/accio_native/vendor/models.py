"""Shared, JSON serializable contracts for the desktop, orchestration and adapters."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any
import time
import uuid


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def identifier(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def _construct(cls, data: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{key: value for key, value in data.items() if key in names})


@dataclass
class Message:
    message_id: str
    seller_id: str
    buyer_id: str
    conversation_id: str
    role: str
    text: str
    timestamp_ms: int
    customer_name: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Message:
        return _construct(cls, data)


@dataclass
class Fact:
    value: str
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Fact:
        return _construct(cls, data)


@dataclass
class Analysis:
    action: str
    facts: dict[str, Fact] = field(default_factory=dict)
    question_keys: list[str] = field(default_factory=list)
    reply: str = ""
    reason: str = ""
    summary: str = ""
    missing: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    procurement_key: str = "main"
    new_case: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Analysis:
        copy = dict(data)
        raw_facts = copy.get("facts", {})
        if not isinstance(raw_facts, dict):
            raise ValueError("facts 必须是对象")
        copy["facts"] = {k: Fact.from_dict(v) for k, v in raw_facts.items() if isinstance(v, dict)}
        return _construct(cls, copy)


@dataclass
class ReceptionCase:
    case_id: str
    seller_id: str
    buyer_id: str
    conversation_id: str
    customer_name: str = ""
    procurement_key: str = "main"
    state: str = "assessing"
    facts: dict[str, Fact] = field(default_factory=dict)
    questions: dict[str, int] = field(default_factory=dict)
    version: int = 1
    context_version: int = 0
    last_processed_id: str = ""
    summary: str = ""
    reason: str = ""
    missing: list[str] = field(default_factory=list)
    registration: dict[str, Any] = field(default_factory=dict)
    handoff_version: int = 0
    stagnant_turns: int = 0
    inquiry_timestamp_ms: int | None = None
    inquiry_source_id: str = ""
    source_message_ids: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    updated_at: int = field(default_factory=now_ms)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ReceptionCase:
        copy = dict(data)
        copy["facts"] = {k: Fact.from_dict(v) for k, v in copy.get("facts", {}).items() if isinstance(v, dict)}
        return _construct(cls, copy)


@dataclass
class Decision:
    case: ReceptionCase
    action: str
    reply: str = ""
    question_keys: list[str] = field(default_factory=list)
    needs_registration: bool = False
    needs_handoff: bool = False
    reason: str = ""


FACT_LABELS = {
    "product": "采购内容", "quantity": "数量与单位", "specifications": "关键规格",
    "customization": "定制要求", "destination": "收货地", "delivery": "交期",
    "budget": "预算原文", "packaging": "包装与认证", "contact": "联系方式",
}

DEFAULT_TEMPLATE = {
    "name": "通用采购需求",
    "required_fields": ["product", "quantity"],
    "optional_fields": ["specifications", "customization", "destination", "delivery", "budget"],
    "max_questions_per_turn": 2,
    "max_asks_per_field": 2,
    "max_stagnant_turns": 3,
}
