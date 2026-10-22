"""Extract raw label/value candidates from a text-based PDF with PyMuPDF.

The extractor only finds evidence. It does not decide which metric a figure belongs to,
which column is the current year, or whether a value is correct; that is the job of
the mapper and the reviewer. Every candidate it returns is unverified.

Two methods run on every page, and each candidate records which one produced it:

- Table rows (TABLE_METHOD): words are grouped into visual rows by vertical position,
  then each row is split into a leading text label and a trailing run of number-like
  tokens. Each trailing token becomes one candidate with its column position.
- Narrative (NARRATIVE_METHOD): inside prose text blocks (sentences, infographic
  captions), every number becomes a candidate together with the words just before
  and after it, e.g. "... network comprising [981] branches, 1,761 ATMs ...".
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

import pymupdf

TABLE_METHOD = "pymupdf_word_rows"
NARRATIVE_METHOD = "pymupdf_narrative"
EXTRACTOR_VERSION = f"pymupdf_extractor/0.2 (PyMuPDF {pymupdf.__version__})"

# A page with fewer visible characters than this is treated as having no usable text.
MIN_TEXT_CHARS = 20
# Words whose vertical centres are within this fraction of the line height share a row.
ROW_TOLERANCE = 0.5
CONTEXT_LINES_BEFORE = 2
# A text block is prose when at least this share of its tokens contain letters.
NARRATIVE_MIN_WORD_SHARE = 0.6
# Words kept on each side of a narrative number (text_before / text_after).
NARRATIVE_WINDOW = 8

_NUMBER_TOKEN = re.compile(r"^\(?[-–—]?\d[\d,]*(\.\d+)?\)?%?$")
_DASH_TOKEN = re.compile(r"^[-–—]$")
_YEAR_TOKEN = re.compile(r"^(19|20)\d\d$")
# Unit headings such as "Birr'000", "ETB '000", "Birr in thousands", "in millions of Birr".
# The apostrophe varies (' ’ ‘ or U+FFFD when the glyph has no character), so any single
# punctuation mark is accepted there; a digit is not ("Birr 1000" is an amount).
_UNIT_HEADING = re.compile(
    r"\b(?:birr|etb)\s?[^\w\s]?\s?000\b"
    r"|\b(?:birr|etb)\s+(?:in\s+)?(?:thousands?|millions?|billions?)\b"
    r"|\bin\s+(?:thousands|millions|billions)\s+of\s+(?:birr|etb)\b",
    re.IGNORECASE,
)
_SENTENCE_PUNCTUATION = ",;:.!?\"'“”‘’"


@dataclass(frozen=True)
class PageReport:
    page_index: int  # zero-based
    char_count: int
    image_count: int
    has_text: bool
    width: float  # page size in points, for locating candidate boxes on the page
    height: float
    # Raw layout evidence used later for units and table columns (not interpreted here):
    unit_headings: tuple[str, ...] = ()  # e.g. "Birr'000", as printed
    year_markers: tuple[tuple[int, float, float], ...] = ()  # (year, x centre, y centre)
    note_headers: tuple[tuple[float, float], ...] = ()  # (x centre, y centre) of "Note(s)"


@dataclass(frozen=True)
class RawCandidate:
    """One number found in the PDF, with its evidence.

    For TABLE_METHOD, row_index is the visual row on the page and column_index the
    position among that row's values. For NARRATIVE_METHOD, row_index is the PyMuPDF
    text block and column_index the number's position among that block's numbers.
    """

    page_index: int  # zero-based PDF page index
    row_index: int
    column_index: int
    row_value_count: int
    raw_label: str  # table label; empty for narrative or numbers-only rows
    raw_value_text: str  # as printed (narrative: without surrounding sentence punctuation)
    row_text: str  # the visual row (table) or the text line (narrative)
    context_text: str  # limited surrounding text
    bbox: tuple[float, float, float, float]  # value position on the page, in points
    extraction_method: str = TABLE_METHOD
    text_before: str = ""  # narrative only: words just before the number
    text_after: str = ""  # narrative only: words just after the number
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

    def count_by_method(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for c in self.candidates:
            counts[c.extraction_method] += 1
        return dict(counts)


def extract_candidates(pdf_bytes: bytes) -> ExtractionResult:
    """Extract page text reports and raw candidates from PDF bytes already validated."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        pages: list[PageReport] = []
        candidates: list[RawCandidate] = []
        for page in doc:
            words = _read_words(page)
            rows = _group_rows(words)
            pages.append(_page_report(page, words, rows))
            # Low-text pages are only flagged, never skipped, so no evidence is dropped.
            candidates.extend(_table_candidates(page.number, rows))
            candidates.extend(_narrative_candidates(page.number, words))
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
    block: int
    line: int
    word_no: int

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


def _read_words(page: pymupdf.Page) -> list[_Word]:
    return [_Word(*w[:8]) for w in page.get_text("words")]


def _page_report(page: pymupdf.Page, words: list[_Word], rows: list[list[_Word]]) -> PageReport:
    char_count = sum(len(w.text) for w in words)
    unit_headings: list[str] = []
    for row in rows:
        for match in _UNIT_HEADING.finditer(" ".join(w.text for w in row)):
            if match.group(0) not in unit_headings:
                unit_headings.append(match.group(0))
    years = []
    notes = []
    for w in words:
        core = strip_sentence_punctuation(w.text)
        x_mid = (w.x0 + w.x1) / 2
        if _YEAR_TOKEN.match(core):
            years.append((int(core), x_mid, w.y_mid))
        elif core.lower() in ("note", "notes"):
            notes.append((x_mid, w.y_mid))
    return PageReport(
        page_index=page.number,
        char_count=char_count,
        image_count=len(page.get_images(full=False)),
        has_text=char_count >= MIN_TEXT_CHARS,
        width=page.rect.width,
        height=page.rect.height,
        unit_headings=tuple(unit_headings),
        year_markers=tuple(years),
        note_headers=tuple(notes),
    )


def is_value_token(text: str) -> bool:
    return bool(_NUMBER_TOKEN.match(text) or _DASH_TOKEN.match(text))


# --- table rows ----------------------------------------------------------------

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


def _table_candidates(page_index: int, rows: list[list[_Word]]) -> list[RawCandidate]:
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
                    bbox=word.bbox,
                )
            )
    return candidates


# --- narrative -----------------------------------------------------------------

def strip_sentence_punctuation(token: str) -> str:
    """Remove punctuation that belongs to the sentence, not the number: '29%,' -> '29%'."""
    core = token.strip(_SENTENCE_PUNCTUATION)
    if core.startswith("(") and ")" not in core:
        core = core[1:]
    if core.endswith(")") and "(" not in core:
        core = core[:-1]
    return core


def is_narrative_number(token: str) -> bool:
    """A number worth capturing in prose: not a dash, and not a bare year like 2026."""
    return bool(_NUMBER_TOKEN.match(token)) and not _YEAR_TOKEN.match(token)


def is_prose(tokens: list[str]) -> bool:
    if not tokens:
        return False
    wordy = sum(1 for t in tokens if any(ch.isalpha() for ch in t))
    return wordy / len(tokens) >= NARRATIVE_MIN_WORD_SHARE


def _narrative_candidates(page_index: int, words: list[_Word]) -> list[RawCandidate]:
    blocks: dict[int, list[_Word]] = defaultdict(list)
    for word in words:
        blocks[word.block].append(word)

    candidates: list[RawCandidate] = []
    for block_no in sorted(blocks):
        block = sorted(blocks[block_no], key=lambda w: (w.line, w.word_no))
        if not is_prose([w.text for w in block]):
            continue  # numeric blocks are tables; the table method covers them
        texts = [w.text for w in block]
        cores = [strip_sentence_punctuation(t) for t in texts]
        positions = [i for i, core in enumerate(cores) if is_narrative_number(core)]

        for column_index, i in enumerate(positions):
            word = block[i]
            line_text = " ".join(w.text for w in block if w.line == word.line)
            candidates.append(
                RawCandidate(
                    page_index=page_index,
                    row_index=block_no,
                    column_index=column_index,
                    row_value_count=len(positions),
                    raw_label="",
                    raw_value_text=cores[i],
                    row_text=line_text,
                    context_text=" ".join(texts[max(0, i - 2 * NARRATIVE_WINDOW) : i + 1 + 2 * NARRATIVE_WINDOW]),
                    bbox=word.bbox,
                    extraction_method=NARRATIVE_METHOD,
                    text_before=" ".join(texts[max(0, i - NARRATIVE_WINDOW) : i]),
                    text_after=" ".join(texts[i + 1 : i + 1 + NARRATIVE_WINDOW]),
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
    by_method = result.count_by_method()
    breakdown = ", ".join(f"{n} {m}" for m, n in sorted(by_method.items()))
    messages.append(
        f"{len(result.candidates)} raw candidates found ({breakdown or 'none'}). These are "
        "unverified extraction results, not approved figures."
    )
    return messages
