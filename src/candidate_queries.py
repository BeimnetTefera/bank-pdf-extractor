"""Read-only lookups over raw candidates, used by the review screen.

These helpers only find and filter evidence. They do not judge which figure is correct.
"""

from __future__ import annotations

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


def filter_candidates(
    candidates: list[RawCandidate],
    search: str = "",
    method: str | None = None,
    page_index: int | None = None,
) -> list[RawCandidate]:
    """Filter by search text (label, nearby words or value), extraction method and page."""
    needle = search.strip().lower()
    result = []
    for c in candidates:
        if method is not None and c.extraction_method != method:
            continue
        if page_index is not None and c.page_index != page_index:
            continue
        if needle:
            haystack = f"{c.raw_label} {c.text_before} {c.text_after} {c.raw_value_text}".lower()
            if needle not in haystack:
                continue
        result.append(c)
    return result


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
