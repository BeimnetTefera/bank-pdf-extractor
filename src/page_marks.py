"""A box the reviewer draws on the page around the right number.

When the suggested figure is wrong, the reviewer marks the real one on the page. The box
becomes the evidence saved with the correction or manual entry, so anyone checking later
sees exactly which number was used. The words inside the box (text layer or OCR) give a
suggested value; the reviewer always confirms or fixes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.number_parser import parse_number
from src.pdf_extractor import PageReport, RawCandidate, is_value_token, words_in_area
from src.unit_normalizer import Normalized, UnitEvidence, find_unit, normalize, page_unit

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class Mark:
    page_index: int  # zero-based
    bbox: Box  # in PDF points
    text: str  # words inside the box, as read
    printed_value: str | None  # the one number in the box, if there is exactly one
    unit: UnitEvidence  # from the page's unit heading
    from_figure: bool = False  # picked from All Figures rather than drawn on the page


def read_mark(pdf_bytes: bytes, page: PageReport, bbox: Box, ocr_words: dict[int, list[list]] | None = None) -> Mark:
    rows = words_in_area(pdf_bytes, page.page_index, bbox, ocr_words)
    numbers = [w for row in rows for w in row if is_value_token(w)]
    text = "  ".join(" ".join(row) for row in rows)
    return Mark(page.page_index, bbox, text, numbers[0] if len(numbers) == 1 else None, page_unit(page))


def mark_from_figure(candidate: RawCandidate, page: PageReport) -> Mark:
    """Use a figure found in the PDF (All Figures) as the evidence: its own page and position."""
    return Mark(candidate.page_index, candidate.bbox, candidate.row_text, candidate.raw_value_text,
                find_unit(candidate, page), from_figure=True)


def suggested_value(mark: Mark, expected_unit: str,
                    count_in_thousands_below: Decimal | None = None) -> Normalized | None:
    """The marked number in the metric's unit (e.g. 341,985,031 under Birr'000 -> 341,985,031,000 ETB).

    count_in_thousands_below is the metric's setting (D-069): a share count printed under a
    Birr'000 heading and below this number is read in thousands."""
    if mark.printed_value is None:
        return None
    return normalize(parse_number(mark.printed_value), mark.unit, expected_unit,
                     count_in_thousands_below=count_in_thousands_below)
