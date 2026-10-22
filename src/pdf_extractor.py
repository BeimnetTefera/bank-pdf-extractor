"""Extract raw label/value candidates from a text-based PDF with PyMuPDF.

The extractor only finds evidence. It does not decide which metric a row belongs to,
which column is the current year, or whether a value is correct; that is the job of
the mapper and the reviewer. Every candidate it returns is unverified.

Method: words on a page are grouped into visual rows by vertical position, then each
row is split into a leading text label and a trailing run of number-like tokens.
Each trailing token becomes one candidate, keeping its column position in the row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

EXTRACTION_METHOD = "pymupdf_word_rows"
EXTRACTOR_VERSION = f"{EXTRACTION_METHOD}/0.1 (PyMuPDF {pymupdf.__version__})"

# A page with fewer visible characters than this is treated as having no usable text.
MIN_TEXT_CHARS = 20
# Words whose vertical centres are within this fraction of the line height share a row.
ROW_TOLERANCE = 0.5
CONTEXT_LINES_BEFORE = 2

_NUMBER_TOKEN = re.compile(r"^\(?[-–—]?\d[\d,]*(\.\d+)?\)?%?$")
_DASH_TOKEN = re.compile(r"^[-–—]$")


@dataclass(frozen=True)
class PageReport:
    page_index: int  # zero-based
    char_count: int
    image_count: int
    has_text: bool


@dataclass(frozen=True)
class RawCandidate:
    page_index: int  # zero-based PDF page index
    row_index: int  # visual row on the page, top to bottom
    column_index: int  # position among the row's value tokens, left to right
    row_value_count: int
    raw_label: str  # empty when the row has numbers only (e.g. a year header)
    raw_value_text: str  # exactly as printed
    row_text: str
    context_text: str  # preceding rows + this row
    bbox: tuple[float, float, float, float]  # value position on the page, in points
    extraction_method: str = EXTRACTION_METHOD
    printed_page_number: int | None = None  # not detected yet; never guessed


@dataclass
class ExtractionResult:
    page_count: int
    pages: list[PageReport]
    candidates: list[RawCandidate]
    extractor_version: str = EXTRACTOR_VERSION
    messages: list[str] = field(default_factory=list)

    @property
    def text_status(self) -> str:
        """'ok' (all pages have text), 'partial_text' or 'no_text'."""
        with_text = sum(p.has_text for p in self.pages)
        if with_text == 0:
            return "no_text"
        return "ok" if with_text == len(self.pages) else "partial_text"

    @property
    def pages_without_text(self) -> list[int]:
        return [p.page_index for p in self.pages if not p.has_text]


def extract_candidates(pdf_bytes: bytes) -> ExtractionResult:
    """Extract page text reports and raw candidates from PDF bytes already validated."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        pages: list[PageReport] = []
        candidates: list[RawCandidate] = []
        for page in doc:
            report, rows = _read_page(page)
            pages.append(report)
            # Low-text pages are only flagged, never skipped, so no evidence is dropped.
            candidates.extend(_candidates_from_rows(page.number, rows))
        result = ExtractionResult(page_count=doc.page_count, pages=pages, candidates=candidates)

    result.messages.extend(_status_messages(result))
    return result


# --- page reading ------------------------------------------------------------

@dataclass
class _Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def _read_page(page: pymupdf.Page) -> tuple[PageReport, list[list[_Word]]]:
    words = [_Word(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]
    char_count = sum(len(w.text) for w in words)
    report = PageReport(
        page_index=page.number,
        char_count=char_count,
        image_count=len(page.get_images(full=False)),
        has_text=char_count >= MIN_TEXT_CHARS,
    )
    return report, _group_rows(words)


def _group_rows(words: list[_Word]) -> list[list[_Word]]:
    """Group words into visual rows (top to bottom), each sorted left to right."""
    rows: list[list[_Word]] = []
    for word in sorted(words, key=lambda w: (w.y_mid, w.x0)):
        if rows:
            row = rows[-1]
            row_mid = sum(w.y_mid for w in row) / len(row)
            tolerance = ROW_TOLERANCE * max(word.height, row[0].height)
            if abs(word.y_mid - row_mid) <= tolerance:
                row.append(word)
                continue
        rows.append([word])
    return [sorted(row, key=lambda w: w.x0) for row in rows]


# --- candidate building ------------------------------------------------------

def is_value_token(text: str) -> bool:
    return bool(_NUMBER_TOKEN.match(text) or _DASH_TOKEN.match(text))


def _candidates_from_rows(page_index: int, rows: list[list[_Word]]) -> list[RawCandidate]:
    row_texts = [" ".join(w.text for w in row) for row in rows]
    candidates: list[RawCandidate] = []

    for row_index, row in enumerate(rows):
        split_at = len(row)
        while split_at > 0 and is_value_token(row[split_at - 1].text):
            split_at -= 1
        values = row[split_at:]
        if not values:
            continue

        label = " ".join(w.text for w in row[:split_at])
        context_start = max(0, row_index - CONTEXT_LINES_BEFORE)
        context = "\n".join(row_texts[context_start : row_index + 1])
        for column_index, word in enumerate(values):
            candidates.append(
                RawCandidate(
                    page_index=page_index,
                    row_index=row_index,
                    column_index=column_index,
                    row_value_count=len(values),
                    raw_label=label,
                    raw_value_text=word.text,
                    row_text=row_texts[row_index],
                    context_text=context,
                    bbox=(word.x0, word.y0, word.x1, word.y1),
                )
            )
    return candidates


def _status_messages(result: ExtractionResult) -> list[str]:
    messages = []
    if result.text_status == "no_text":
        messages.append(
            "No extractable text was found. The PDF may be scanned images; "
            "OCR is not implemented, so no candidates were produced."
        )
    elif result.text_status == "partial_text":
        pages = ", ".join(str(i + 1) for i in result.pages_without_text)
        messages.append(
            f"{len(result.pages_without_text)} page(s) have little or no extractable text "
            f"(PDF pages {pages}). They may be scanned images or pictures; OCR is not implemented."
        )
    messages.append(
        f"{len(result.candidates)} raw candidates found. These are unverified extraction "
        "results, not approved figures."
    )
    return messages
