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


def find_in_report(pdf_bytes: bytes, query: str, ocr_words: dict[int, list[list]] | None = None) -> list[tuple[int, Box]]:
    """Each zero-based page that holds the query, once, with the box of its first match there."""
    forms = _forms(query)
    if len(forms[0]) < 2:
        return []
    hits: list[tuple[int, Box]] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            box = next((tuple(r) for f in forms for r in page.search_for(f)[:1]), None)
            if box is None and ocr_words and page.number in ocr_words:
                wanted = [f.casefold() for f in forms]
                box = next((tuple(w[:4]) for w in ocr_words[page.number]
                            if any(f in str(w[4]).casefold() for f in wanted)), None)
            if box is not None:
                hits.append((page.number, box))
    return hits
