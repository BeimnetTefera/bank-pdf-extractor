"""Read-only lookups over raw candidates, used by the review screen.

These helpers only find and filter evidence. They do not judge which figure is correct.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal

from src.number_parser import parse_number
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, RawCandidate

CandidateKey = tuple[str, int, int, int]

METHOD_LABELS = {TABLE_METHOD: "Table row", NARRATIVE_METHOD: "Sentence"}


def candidate_key(c: RawCandidate) -> CandidateKey:
    """Stable identity of a candidate within one extraction result."""
    return (c.extraction_method, c.page_index, c.row_index, c.column_index)


def find_by_key(candidates: list[RawCandidate], key: CandidateKey | None) -> RawCandidate | None:
    if key is None:
        return None
    return next((c for c in candidates if candidate_key(c) == key), None)


def display_label(c: RawCandidate) -> str:
    """Short text describing what the number is next to."""
    if c.extraction_method == NARRATIVE_METHOD:
        before = " ".join(c.text_before.split()[-4:])
        after = " ".join(c.text_after.split()[:4])
        return f"…{before} [{c.raw_value_text}] {after}…"
    return c.raw_label or "(no label)"


NO_YEAR = 0  # year filter value for figures without a year column (sentences, unheaded tables)
_NUMBER_SEARCH = re.compile(r"[\d,.\s()-]*\d[\d,.\s()-]*")


def filter_candidates(
    candidates: list[RawCandidate],
    search: str = "",
    method: str | None = None,
    page_index: int | None = None,
    *,
    pages: tuple[int, int] | None = None,
    year: int | None = None,
    year_of: Callable[[RawCandidate], int | None] | None = None,
    min_value: Decimal | None = None,
    max_value: Decimal | None = None,
) -> list[RawCandidate]:
    """Filter by search text (label, nearby words or value), extraction method and page.

    A search made only of digits matches numbers with or without their thousands commas
    ("189512266" finds "189,512,266"). pages is a zero-based inclusive (first, last) range.
    year keeps figures in that year column (NO_YEAR: none found), using year_of. min_value and
    max_value compare the number as printed (no unit scaling); figures without a number are
    left out when either is set."""
    needle = search.strip().lower()
    digits = _compact(needle) if _NUMBER_SEARCH.fullmatch(needle) else ""
    result = []
    for c in candidates:
        if method is not None and c.extraction_method != method:
            continue
        if page_index is not None and c.page_index != page_index:
            continue
        if pages is not None and not pages[0] <= c.page_index <= pages[1]:
            continue
        if year is not None and year_of is not None and (year_of(c) or NO_YEAR) != year:
            continue
        if (min_value is not None or max_value is not None) and not _in_range(c, min_value, max_value):
            continue
        if needle and not _matches(c, needle, digits):
            continue
        result.append(c)
    return result


def _matches(c: RawCandidate, needle: str, digits: str) -> bool:
    haystack = f"{c.raw_label} {c.text_before} {c.text_after} {c.raw_value_text}".lower()
    if needle in haystack:
        return True
    return bool(digits) and digits in _compact(c.raw_value_text)


def _compact(text: str) -> str:
    """Digits and decimal point only: '(189,512,266)' -> '189512266'."""
    return "".join(ch for ch in text if ch.isdigit() or ch == ".")


def _in_range(c: RawCandidate, low: Decimal | None, high: Decimal | None) -> bool:
    value = parse_number(c.raw_value_text).value
    if value is None:
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def context_line(c: RawCandidate) -> str:
    """The whole sentence (with the number in brackets) or the whole table row a figure comes from."""
    if c.extraction_method == NARRATIVE_METHOD:
        return f"{c.text_before} [{c.raw_value_text}] {c.text_after}".strip()
    return c.row_text


def page_of_rows(items: list, page: int, size: int) -> tuple[list, int]:
    """One page (1-based) of a long list and the number of pages; a page past the end shows the last."""
    pages = max(1, -(-len(items) // size))
    page = min(max(1, page), pages)
    return items[(page - 1) * size: page * size], pages


def same_value_elsewhere(candidates: list[RawCandidate], target: RawCandidate) -> list[RawCandidate]:
    """Other places where the identical printed value appears, one per page position."""
    seen = {(target.page_index, target.bbox)}
    matches = []
    for c in candidates:
        location = (c.page_index, c.bbox)
        if c.raw_value_text == target.raw_value_text and location not in seen:
            seen.add(location)  # both methods can catch the same printed number
            matches.append(c)
    return matches


def same_label_elsewhere(candidates: list[RawCandidate], target: RawCandidate) -> list[RawCandidate]:
    """Table rows on other pages with the same label and column position.

    Values may differ (e.g. statement vs. notes); differences are shown, not resolved.
    """
    if target.extraction_method != TABLE_METHOD or not target.raw_label:
        return []
    label = _normalise(target.raw_label)
    return [
        c
        for c in candidates
        if c.extraction_method == TABLE_METHOD
        and c.page_index != target.page_index
        and c.column_index == target.column_index
        and _normalise(c.raw_label) == label
    ]


def _normalise(label: str) -> str:
    return " ".join(label.lower().split())
