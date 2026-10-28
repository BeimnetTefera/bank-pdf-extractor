"""Find a word or a printed number in one report's pages, for the Extract page's search box (D-095).

Text pages are searched with PyMuPDF (case is ignored); picture pages with the words OCR read
from them. A number is found however it was typed: 116546794 also finds 116,546,794.
"""

from __future__ import annotations

import re

import pymupdf

Box = tuple[float, float, float, float]


def _forms(query: str) -> list[str]:
    """The query as typed, plus a number's digits grouped by commas (and ungrouped)."""
    text = query.strip()
    forms = [text]
    digits = text.replace(",", "").replace(" ", "")
    if re.fullmatch(r"\d{4,}", digits):
        grouped = f"{int(digits):,}"
        forms += [f for f in (grouped, digits) if f not in forms]
    return forms


def value_beside(match: Box, numbers: list[tuple[Box, str]]) -> Box | None:
    """The figure printed on the same line as matched words, to their right: the first column, so the
    report year's. Note references such as "24" are passed over when a real amount is on the line."""
    top, bottom = match[1], match[3]
    centre = ((match[0] + match[2]) / 2, (top + bottom) / 2)
    if any(b[0] <= centre[0] <= b[2] and b[1] <= centre[1] <= b[3] for b, _ in numbers):
        return match  # the search was for the number itself
    on_line = [(box, text) for box, text in numbers
               if box[0] >= match[2] - 1 and min(bottom, box[3]) - max(top, box[1]) > 0.4 * (bottom - top)]
    amounts = [(box, text) for box, text in on_line if "," in text or "." in text or len(text.strip("()-")) > 3]
    chosen = amounts or on_line
    return min(chosen, key=lambda item: item[0][0])[0] if chosen else None


def figure_for(matches: list[Box], numbers: list[tuple[Box, str]]) -> Box:
    """The box to show for a page's matches: the figure beside the first match that has one, since a
    page often names a line in a heading or a sentence before its table row. Else the first match."""
    return next((v for m in matches if (v := value_beside(m, numbers)) is not None), matches[0])


def find_in_report(pdf_bytes: bytes, query: str,
                   ocr_words: dict[int, list[list]] | None = None) -> list[tuple[int, list[Box]]]:
    """Each zero-based page that holds the query, once, with the boxes of every match there in reading order."""
    forms = _forms(query)
    if len(forms[0]) < 2:
        return []
    hits: list[tuple[int, list[Box]]] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            boxes = next(([tuple(r) for r in found] for f in forms if (found := page.search_for(f))), [])
            if not boxes and ocr_words and page.number in ocr_words:
                wanted = [f.casefold() for f in forms]
                boxes = [tuple(w[:4]) for w in ocr_words[page.number] if any(f in str(w[4]).casefold() for f in wanted)]
            if boxes:
                hits.append((page.number, boxes))
    return hits
