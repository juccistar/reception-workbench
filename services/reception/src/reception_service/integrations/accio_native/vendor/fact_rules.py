"""Pure business completeness rules shared by planning and analysis validation."""
from __future__ import annotations

import math
import re


_PLACEHOLDERS = {
    "", "unknown", "unspecified", "not sure", "i don't know", "tbd", "n/a", "null",
    "none", "not decided", "undecided", "not specified", "to be confirmed",
    "待确认", "未知", "不确定", "不知道", "未指定", "未提供", "待定",
}
_UNIT = re.compile(
    r"\b(?:pieces?|pcs?|units?|sets?|pairs?|cartons?|boxes|box|dozens?|kg|kilograms?|"
    r"g|grams?|tons?|tonnes?|lit(?:er|re)s?|ml|met(?:er|re)s?|m|rolls?|packs?|bags?|bottles?)\b"
    r"|件|个|套|双|箱|打|千克|公斤|克|吨|升|米|卷|包|袋|瓶", re.I,
)


def is_valid_value(value: str, key: str = "") -> bool:
    if not isinstance(value, str) or value.strip().casefold() in _PLACEHOLDERS:
        return False
    if key == "quantity":
        if re.search(r"\b(?:nan|inf(?:inity)?|zero)\b", value, re.I):
            return False
        number = re.search(r"(?<!\w)[+-]?(?:\d+(?:[.,]\d+)*|\.\d+)(?:e[+-]?\d+)?", value, re.I)
        if number:
            try:
                numeric = float(number.group().replace(",", ""))
            except ValueError:
                return False
            if not math.isfinite(numeric) or numeric <= 0:
                return False
    return True


def is_complete(key: str, value: str) -> bool:
    if not is_valid_value(value, key):
        return False
    if key != "quantity":
        return True
    # Keep original text: completeness never converts or writes a numeric field.
    if not _UNIT.search(value):
        return False
    tokens = re.findall(r"(?<![\w.])-?\d+(?:[.,]\d+)*", value)
    try:
        numbers = [float(token.replace(",", "")) for token in tokens]
    except ValueError:
        return False
    return bool(numbers) and all(math.isfinite(number) and number > 0 for number in numbers)
