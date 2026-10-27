"""Extract raw label/value candidates from a text-based PDF with PyMuPDF.

The extractor only finds evidence. It does not decide which metric a figure belongs to,
which column is the current year, or whether a value is correct; that is the job of
the mapper and the reviewer. Every candidate it returns is unverified.

Two methods run on every page, and each candidate records which one produced it:

- Table rows (TABLE_METHOD): words are grouped into visual rows by vertical position,
  then each row is split into a leading text label and a trailing run of number-like
  tokens. Each trailing token becomes one candidate with its column position.
  A page printed as two tables side by side (a landscape spread of two report pages, or
  a two-column page) is first cut into column blocks at the empty gutter between them,
  so a label is only ever joined to numbers of its own block (see _column_blocks).
- Narrative (NARRATIVE_METHOD): inside prose text blocks (sentences, infographic
  captions), every number becomes a candidate together with the words just before
  and after it, e.g. "... network comprising [981] branches, 1,761 ATMs ...".

Pages that are pictures have no text layer. When OCR words for them are supplied (see
src/ocr.py), those words are used instead, and every candidate from such a page is marked
`ocr=True` so the reviewer is warned to check its digits.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field, replace

import pymupdf

TABLE_METHOD = "pymupdf_word_rows"
NARRATIVE_METHOD = "pymupdf_narrative"
EXTRACTOR_VERSION = f"pymupdf_extractor/0.3 (PyMuPDF {pymupdf.__version__})"

# A page with fewer visible characters than this is treated as having no usable text.
MIN_TEXT_CHARS = 20
# Words whose vertical centres are within this fraction of the line height share a row.
ROW_TOLERANCE = 0.5
CONTEXT_LINES_BEFORE = 2
# A text block is prose when at least this share of its tokens contain letters.
NARRATIVE_MIN_WORD_SHARE = 0.6
# Words kept on each side of a narrative number (text_before / text_after).
NARRATIVE_WINDOW = 8
# Column blocks: an empty vertical band at least this wide (points) may separate two tables
# printed side by side. It is looked for in the middle part of the text's width only.
GUTTER_MIN_WIDTH = 18.0
GUTTER_ZONE = (0.2, 0.8)
# Each side of a gutter must hold this many rows of "text label + amount" to be cut apart, so a
# statement's labels are never separated from its own number columns.
MIN_BLOCK_TABLE_ROWS = 3
MAX_BLOCK_SPLITS = 2  # cut at most twice in a row (three or four columns of tables)
MAX_GUTTERS_TRIED = 3  # the widest empty bands only, to keep reading fast

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
# Titles of the primary statements: a short row that starts with the title (after an
# optional bank name ending in "S.C." or "Bank"), not a sentence that mentions one.
_STATEMENT_TITLE = re.compile(
    r"^(?:.{0,40}?\b(?:s\.c\.|bank)\s+)?(?:consolidated\s+|separate\s+)?"
    r"(?:statement\s+of\s+(?:financial\s+position|profit\s+(?:or|and)\s+loss|comprehensive\s+income|income)"
    r"|balance\s+sheet|income\s+statement)\b",
    re.IGNORECASE,
)
STATEMENT_TITLE_MAX_WORDS = 12
# A number that is an amount, not a note reference: grouped digits or at least four digits.
_AMOUNT = re.compile(r"\d{1,3},\d{3}|\d{4,}")
_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"}


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
    year_markers: tuple[tuple[int, float, float, float], ...] = ()  # (year, x centre, y centre, right edge)
    note_headers: tuple[tuple[float, float], ...] = ()  # (x centre, y centre) of "Note(s)"
    ocr: bool = False  # words came from OCR of the page image
    # Tables printed side by side: the x range (x0, x1) of each column block, left to right.
    # Empty when the page is one block. Labels, years and notes belong to their own block.
    column_blocks: tuple[tuple[float, float], ...] = ()
    statement_titles: tuple[tuple[str, float, float], ...] = ()  # (title as printed, x centre, y centre)

    def block_of(self, x: float) -> int:
        """Index of the column block that holds horizontal position x (0 for a one-block page)."""
        blocks = self.column_blocks
        for index in range(len(blocks) - 1):
            if x < (blocks[index][1] + blocks[index + 1][0]) / 2:  # the middle of the gutter
                return index
        return max(0, len(blocks) - 1)


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
    ocr: bool = False  # read by OCR from a picture of the page: digits may be misread


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

    @property
    def ocr_pages(self) -> list[int]:
        return [p.page_index for p in self.pages if p.ocr]

    def count_by_method(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for c in self.candidates:
            counts[c.extraction_method] += 1
        return dict(counts)


def extract_candidates(pdf_bytes: bytes, ocr_words: dict[int, list[list]] | None = None,
                       ocr_engine: str = "") -> ExtractionResult:
    """Extract page text reports and raw candidates from PDF bytes already validated.

    ocr_words: OCR words per zero-based page, used for pages without a text layer.
    """
    ocr_words = ocr_words or {}
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        pages: list[PageReport] = []
        candidates: list[RawCandidate] = []
        for page in doc:
            words = _read_words(page)
            ocr = sum(len(w.text) for w in words) < MIN_TEXT_CHARS and page.number in ocr_words
            if ocr:
                words = _tighten_ocr_boxes([_Word(*w[:8]) for w in ocr_words[page.number]])
            blocks = _column_blocks(words)
            block_rows = [_group_rows(block) for block in blocks]
            report = _page_report(page, words, [row for rows in block_rows for row in rows])
            if len(blocks) > 1:
                report = replace(report, column_blocks=tuple(_x_range(block) for block in blocks))
            pages.append(replace(report, ocr=ocr))
            # Low-text pages are only flagged, never skipped, so no evidence is dropped.
            candidates.extend(_table_candidates(page.number, block_rows, ocr))
            candidates.extend(_narrative_candidates(page.number, words, ocr))
        version = EXTRACTOR_VERSION
        if any(p.ocr for p in pages):
            version += f" + OCR ({ocr_engine or 'tesseract'})"
        result = ExtractionResult(page_count=doc.page_count, pages=pages, candidates=candidates,
                                  extractor_version=version)

    result.messages.extend(_status_messages(result))
    return result


def words_in_area(pdf_bytes: bytes, page_index: int, area: tuple[float, float, float, float],
                  ocr_words: dict[int, list[list]] | None = None) -> list[list[str]]:
    """Words whose centre lies inside area (PDF points), as visual rows top to bottom.

    Uses the OCR words for a page without a text layer, like extract_candidates.
    """
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        words = _read_words(doc[page_index])
    if sum(len(w.text) for w in words) < MIN_TEXT_CHARS and ocr_words and page_index in ocr_words:
        words = _tighten_ocr_boxes([_Word(*w[:8]) for w in ocr_words[page_index]])
    x0, y0, x1, y1 = area
    rows = []
    for block in _column_blocks(words):  # a box across a gutter reads each side's rows separately
        inside = [w for w in block if x0 <= (w.x0 + w.x1) / 2 <= x1 and y0 <= w.y_mid <= y1]
        rows += [[w.text for w in row] for row in _group_rows(inside)]
    return rows


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


def _tighten_ocr_boxes(words: list[_Word]) -> list[_Word]:
    """OCR word boxes start at the right top edge but are often about two lines tall, which
    would merge neighbouring table rows. Cut every box to the page's normal line height,
    estimated from the shortest boxes."""
    heights = sorted(w.height for w in words if len(w.text) >= 2)
    if not heights:
        return words
    shortest = heights[len(heights) // 10]
    line_height = statistics.median(h for h in heights if h <= 1.4 * shortest)
    for w in words:
        w.y1 = w.y0 + min(w.height, line_height)
    return words


def _page_report(page: pymupdf.Page, words: list[_Word], rows: list[list[_Word]]) -> PageReport:
    char_count = sum(len(w.text) for w in words)
    unit_headings: list[str] = []
    titles = []
    for row in rows:
        text = " ".join(w.text for w in row)
        for match in _UNIT_HEADING.finditer(text):
            if match.group(0) not in unit_headings:
                unit_headings.append(match.group(0))
        title = _STATEMENT_TITLE.search(text)
        if title and len(row) <= STATEMENT_TITLE_MAX_WORDS and not any(_is_amount(w.text) for w in row):
            titles.append((title.group(0), (row[0].x0 + row[-1].x1) / 2, row[0].y_mid))
    years = []
    notes = []
    for w in words:
        core = strip_sentence_punctuation(w.text)
        x_mid = (w.x0 + w.x1) / 2
        if _YEAR_TOKEN.match(core):
            years.append((int(core), x_mid, w.y_mid, w.x1))
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
        statement_titles=tuple(titles),
    )


def is_value_token(text: str) -> bool:
    return bool(_NUMBER_TOKEN.match(text) or _DASH_TOKEN.match(text))


def _is_amount(text: str) -> bool:
    """A printed amount such as 1,234 or (12,345): not a note number, not a bare year."""
    core = strip_sentence_punctuation(text)
    return bool(_NUMBER_TOKEN.match(core) and _AMOUNT.search(core)) and not _YEAR_TOKEN.match(core)


# --- column blocks -------------------------------------------------------------

def _column_blocks(words: list[_Word], depth: int = 0) -> list[list[_Word]]:
    """Cut words into side-by-side column blocks, left to right; one block for most pages.

    A cut is made at an empty vertical gutter only when both sides read as tables of their
    own (labels followed by amounts), as on a landscape spread with one statement on each
    half. A statement's own gap between its labels and its number columns never qualifies,
    because the number side has no labels.
    """
    if depth >= MAX_BLOCK_SPLITS or len(words) < 2 * MIN_BLOCK_TABLE_ROWS:
        return [words]
    for gap_start, gap_end in _gutters(words)[:MAX_GUTTERS_TRIED]:
        middle = (gap_start + gap_end) / 2
        right = [w for w in words if (w.x0 + w.x1) / 2 >= middle]
        if _table_rows(right) < MIN_BLOCK_TABLE_ROWS:  # usually only numbers: a statement's own gap
            continue
        left = [w for w in words if (w.x0 + w.x1) / 2 < middle]
        if _table_rows(left) >= MIN_BLOCK_TABLE_ROWS:
            return _column_blocks(left, depth + 1) + _column_blocks(right, depth + 1)
    return [words]


def _gutters(words: list[_Word]) -> list[tuple[float, float]]:
    """Empty vertical bands crossed by no word, widest first, in the middle of the text's width."""
    spans = sorted((w.x0, w.x1) for w in words)
    left, right = spans[0][0], max(x1 for _, x1 in spans)
    low, high = (left + (right - left) * share for share in GUTTER_ZONE)
    gaps = []
    reach = spans[0][1]
    for x0, x1 in spans[1:]:
        if x0 - reach >= GUTTER_MIN_WIDTH and low <= (x0 + reach) / 2 <= high:
            gaps.append((reach, x0))
        reach = max(reach, x1)
    return sorted(gaps, key=lambda gap: gap[0] - gap[1])


def _table_rows(words: list[_Word]) -> int:
    """How many rows read as a table line: a text label followed by at least one amount."""
    count = 0
    for row in _group_rows(words):
        split_at = len(row)
        while split_at > 0 and is_value_token(row[split_at - 1].text):
            split_at -= 1
        has_label = any(
            len(core) >= 3 and core.isalpha() and core.lower() not in _MONTHS
            for core in (strip_sentence_punctuation(w.text) for w in row[:split_at])
        )
        if has_label and any(_is_amount(w.text) for w in row[split_at:]):
            count += 1
    return count


def _x_range(words: list[_Word]) -> tuple[float, float]:
    return (min(w.x0 for w in words), max(w.x1 for w in words))


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


def _table_candidates(page_index: int, block_rows: list[list[list[_Word]]], ocr: bool = False) -> list[RawCandidate]:
    """Candidates from each block's rows. Rows are numbered down the page, block after block,
    so a one-block page keeps the row numbers it always had."""
    candidates: list[RawCandidate] = []
    first_row = 0
    for rows in block_rows:
        candidates += _block_candidates(page_index, rows, first_row, ocr)
        first_row += len(rows)
    return candidates


def _block_candidates(page_index: int, rows: list[list[_Word]], first_row: int, ocr: bool) -> list[RawCandidate]:
    row_texts = [" ".join(w.text for w in row) for row in rows]
    candidates: list[RawCandidate] = []

    for index, row in enumerate(rows):
        split_at = len(row)
        while split_at > 0 and is_value_token(row[split_at - 1].text):
            split_at -= 1
        values = row[split_at:]
        if not values:
            continue

        label = " ".join(w.text for w in row[:split_at])
        context_start = max(0, index - CONTEXT_LINES_BEFORE)
        context = "\n".join(row_texts[context_start : index + 1])
        for column_index, word in enumerate(values):
            candidates.append(
                RawCandidate(
                    page_index=page_index,
                    row_index=first_row + index,
                    column_index=column_index,
                    row_value_count=len(values),
                    raw_label=label,
                    raw_value_text=word.text,
                    row_text=row_texts[index],
                    context_text=context,
                    bbox=word.bbox,
                    ocr=ocr,
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


def _narrative_candidates(page_index: int, words: list[_Word], ocr: bool = False) -> list[RawCandidate]:
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
                    ocr=ocr,
                )
            )
    return candidates


def _status_messages(result: ExtractionResult) -> list[str]:
    messages = []
    if result.ocr_pages:
        messages.append(
            f"{len(result.ocr_pages)} page(s) are pictures and were read with OCR. OCR can misread digits, "
            "so every figure from these pages carries a warning to check it against the page."
        )
    if result.text_status == "no_text":
        messages.append(
            "No extractable text was found. The PDF may be scanned images; read them with OCR "
            "(Extract & Review offers it when Tesseract is installed)."
        )
    elif result.text_status == "partial_text":
        pages = ", ".join(str(i + 1) for i in result.pages_without_text)
        messages.append(
            f"{len(result.pages_without_text)} page(s) have little or no extractable text "
            f"(PDF pages {pages}). They may be scanned images or pictures; read them with OCR."
        )
    split = [p.page_index + 1 for p in result.pages if p.column_blocks]
    if split:
        messages.append(
            f"{len(split)} page(s) hold tables side by side (PDF pages {', '.join(map(str, split))}); "
            "each side was read on its own, so labels are only matched with numbers on their own side."
        )
    by_method = result.count_by_method()
    breakdown = ", ".join(f"{n} {m}" for m, n in sorted(by_method.items()))
    messages.append(
        f"{len(result.candidates)} raw candidates found ({breakdown or 'none'}). These are "
        "unverified extraction results, not approved figures."
    )
    return messages
