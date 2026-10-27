"""Parse printed number text into an exact Decimal, only when the format is unambiguous.

The original text is never changed; when parsing is unsafe the value is None and the
reason is returned so the candidate can be flagged for review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal

_DASHES = {"-", "–", "—"}
_GROUPED = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")  # 1,234,567.89
_PLAIN = re.compile(r"^\d+(\.\d+)?$")  # 1234567.89


@dataclass(frozen=True)
class ParsedNumber:
    value: Decimal | None
    is_percent: bool = False
    decimals: int = 0  # digits printed after the decimal point
    issue: str | None = None


def parse_number(text: str) -> ParsedNumber:
    t = text.strip()
    if t in _DASHES:
        return ParsedNumber(None, issue="Printed as a dash, usually meaning nil or not applicable; not read as zero.")

    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative, t = True, t[1:-1]
    elif t.startswith("(") or t.endswith(")"):
        return ParsedNumber(None, issue=f"Unbalanced brackets in '{text}'.")
    if t[:1] in _DASHES:
        negative, t = True, t[1:]

    is_percent = t.endswith("%")
    if is_percent:
        t = t[:-1]

    if "," in t and not _GROUPED.match(t):
        return ParsedNumber(None, is_percent, issue=f"Unusual comma grouping in '{text}'; not read automatically.")
    if not (_GROUPED.match(t) or _PLAIN.match(t)):
        return ParsedNumber(None, is_percent, issue=f"'{text}' is not a recognised number format.")

    digits = t.replace(",", "")
    value = Decimal(digits)
    decimals = len(digits.split(".")[1]) if "." in digits else 0
    return ParsedNumber(-value if negative else value, is_percent, decimals)


_PERCENT_WORDS = re.compile(r"^(?:percent|per\s+cent|pct)\b", re.IGNORECASE)


def with_percent_word(parsed: ParsedNumber, text_after: str) -> ParsedNumber:
    """A sentence number followed by the word "percent" ("6.6 percent") is a percentage."""
    if parsed.value is None or parsed.is_percent or not _PERCENT_WORDS.match(text_after.strip()):
        return parsed
    return replace(parsed, is_percent=True)
