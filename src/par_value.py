"""Find the par (nominal) value of the bank's own ordinary shares in a report PDF (D-070).

Ethiopian reports often print EPS as a percentage of the par value ("EPS 39%"), so EPS in Birr
is that percentage times the par value. The par value is read only from explicit wording:

  "Ordinary Shares, with par value of ETB 1,000 each"     (Zemen 2025/26, p18)
  "12,500,000 ordinary shares of Birr 1000 each"          (BoA 2022/23, p113)
  "par value per share Birr 1,000", "par value of Birr 1,000 per share",
  "nominal value of Birr 1,000", "the par value is 1,000 birr per share" (BoA 2025/26, p217)
  "Birr 1,000 per share" shortly after "share capital" or "ordinary shares"

Par values in a table of investments (BoA 2022/23 p102: "Par value (Birr)" of Eth Switch S.Co)
belong to other companies: they have no such wording and their sentence names an investee, so
they are skipped; so is a price, dividend or earnings figure "per share". Each finding keeps its
page and the box of the number, as evidence.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

import pymupdf

Box = tuple[float, float, float, float]
_CUR = r"(?:birr|etb|br\.?)"
# The number, not followed by more digits or a scale word (Birr 1,000 million is not a par value).
_NUM = r"(?P<num>\d{1,3}(?:,\d{3})*|\d+)(?:\.00)?(?![\d,]|\.\d|\s*(?:million|billion|thousand|mn|bn)\b)"
_PER_SHARE = r"(?:\s+(?:each|per\s+share))?"
_WORDINGS = [re.compile(p, re.IGNORECASE) for p in (
    rf"(?:par|nominal)\s+value\s+(?:of|is)\s+{_CUR}\s*{_NUM}{_PER_SHARE}",  # par value of Birr 1,000 (each)
    rf"(?:par|nominal)\s+value\s+per\s+share\s*(?:of|is|:)?\s*{_CUR}\s*{_NUM}",  # par value per share Birr 1,000
    rf"(?:par|nominal)\s+value\s+is\s+{_NUM}\s*{_CUR}\s+per\s+share",  # the par value is 1,000 birr per share
    rf"shares?\s+of\s+{_CUR}\s*{_NUM}\s+each",  # ordinary shares of Birr 1000 each
)]
# "Birr 1,000 per share" alone is a par value only right after share capital wording, and not a
# price, dividend or earnings figure.
_PER_SHARE_ONLY = re.compile(rf"{_CUR}\s*{_NUM}\s+per\s+share", re.IGNORECASE)
_SHARE_CAPITAL = re.compile(r"share\s+capital|ordinary\s+shares?", re.IGNORECASE)
_NOT_PAR = re.compile(r"dividend|earning|eps|price|premium|market|sold|sell|sale|paid\s+to|issued\s+at",
                      re.IGNORECASE)
_NEAR = 100  # characters before "Birr X per share" searched for share capital wording
# Words in the same sentence that show the shares belong to another company (an investment).
_OTHER_COMPANY = re.compile(r"investee|investment|subsidiar|associate|s\.\s?co\b|s\.c\.", re.IGNORECASE)
_LOOK_BACK = 160  # characters before the match searched for those words (within the sentence)


@dataclass(frozen=True)
class ParValue:
    value: Decimal  # Birr per share
    page: int  # 1-based PDF page
    bbox: Box  # the printed number, in PDF points
    printed: str  # the number as printed, e.g. "1,000"
    text: str  # the wording it was read from, e.g. "par value of ETB 1,000 each"


def find_par_values(pdf: bytes) -> list[ParValue]:
    """Every par value of the bank's own shares stated in the PDF, in page order."""
    found: list[ParValue] = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for index, page in enumerate(doc):
            found.extend(page_par_values(page.get_text("words"), index + 1))
    return found


def page_par_values(words: list[tuple], page: int) -> list[ParValue]:
    """Par values on one page; words are pymupdf word tuples (x0, y0, x1, y1, text, ...)."""
    text, spans = _join(words)
    matches = [m for pattern in _WORDINGS for m in pattern.finditer(text)]
    matches += [m for m in _PER_SHARE_ONLY.finditer(text) if _after_share_capital(text, m.start())]
    found, seen = [], set()
    for match in sorted(matches, key=lambda m: m.start("num")):
        start, end = match.span("num")
        if start in seen or _other_company(text, match.start()):
            continue
        seen.add(start)
        boxes = [words[i][:4] for i, (s, e) in enumerate(spans) if s < end and e > start]
        if not boxes:
            continue
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        printed = match.group("num")
        found.append(ParValue(Decimal(printed.replace(",", "")), page, box, printed, " ".join(match.group(0).split())))
    return found


def _other_company(text: str, position: int) -> bool:
    """The sentence before position names an investee: the shares are another company's."""
    sentence_start = max(text.rfind(". ", 0, position) + 1, position - _LOOK_BACK)
    return bool(_OTHER_COMPANY.search(text[sentence_start:position]))


def _after_share_capital(text: str, position: int) -> bool:
    """'Birr 1,000 per share' follows share capital wording, with no price, dividend or earnings words."""
    before = text[max(0, position - _NEAR):position]
    found = list(_SHARE_CAPITAL.finditer(before))
    return bool(found) and not _NOT_PAR.search(before[found[-1].start():])


def best_par_value(found: list[ParValue]) -> ParValue | None:
    """The par value stated most often (the first place it is stated); None when none is found."""
    if not found:
        return None
    most_common, _ = Counter(p.value for p in found).most_common(1)[0]
    return next(p for p in found if p.value == most_common)


def _join(words: list[tuple]) -> tuple[str, list[tuple[int, int]]]:
    """The page's words joined by spaces, and each word's (start, end) in that text."""
    parts, spans, position = [], [], 0
    for word in words:
        token = str(word[4])
        spans.append((position, position + len(token)))
        parts.append(token)
        position += len(token) + 1
    return " ".join(parts), spans
