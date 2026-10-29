"""The report year in its one saved form, YYYY/YY (D-017, D-085), and the year a PDF states.

canonical_period turns what a reviewer typed ("2026", "FY2026", "2025-2026") into the Shega
fiscal_year form ("2025/26": July 2025 to June 2026). suggested_period reads the first pages of a
PDF for a period end such as "30 June 2026" and gives that fiscal year as a suggestion only: the
reviewer can change it, and nothing is saved until they press Save.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import pymupdf

from src.analysis import report_end_year

SUGGESTION_PAGES = 5  # the period end is stated on the cover or in the first statements

_ORDINAL = r"(?:st|nd|rd|th)?"
# "30 June 2026", "30th June, 2026", "June 30, 2026", "June 30th 2026" (any case, any spacing)
_PERIOD_END = re.compile(
    rf"\b(?:30{_ORDINAL}\s+(?:of\s+)?June,?\s+((?:19|20)\d\d)|June\s+30{_ORDINAL},?\s+((?:19|20)\d\d))\b",
    re.IGNORECASE,
)


def canonical_period(text: str | None) -> str | None:
    """'2026', 'FY2026', '2025-2026', '2025/26' -> '2025/26'; None when no fiscal year can be read."""
    end_year = report_end_year(text or "")
    if end_year is None:
        return None
    from src.excel_exporter import fiscal_year_label  # local: the exporter imports modules that import this one

    return fiscal_year_label(end_year)


def fiscal_year(period: str) -> str:
    """A saved report period as shown everywhere, YYYY/YY; the period as saved when no year can be read."""
    return canonical_period(period) or period


def period_from_text(pages: Iterable[str]) -> str | None:
    """The fiscal year of the latest 30 June period end stated in the text, or None.

    The latest one is this year's: statements print last year's period end next to it."""
    years = [int(a or b) for text in pages for a, b in _PERIOD_END.findall(text or "")]
    return canonical_period(str(max(years))) if years else None


def suggested_period(pdf_bytes: bytes, pages: int = SUGGESTION_PAGES) -> str | None:
    """The fiscal year stated on the first pages of a PDF, or None (also for a PDF that cannot be read)."""
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            texts = [doc[i].get_text() for i in range(min(pages, doc.page_count))]
    except Exception:  # a suggestion only: an unreadable PDF is reported when it is validated
        return None
    return period_from_text(texts)
