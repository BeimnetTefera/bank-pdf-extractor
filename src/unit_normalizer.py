"""Find the unit printed with a figure and convert it to the metric's expected unit.

Conversions happen only on explicit evidence: a unit heading on the page (tables) or
currency/scale words next to the number (sentences). Nothing is assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from src.number_parser import ParsedNumber
from src.pdf_extractor import NARRATIVE_METHOD, PageReport, RawCandidate, strip_sentence_punctuation

_SCALES = {
    "thousand": Decimal(1_000), "thousands": Decimal(1_000),
    "million": Decimal(1_000_000), "millions": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000), "billions": Decimal(1_000_000_000),
}
_CURRENCIES = {"birr": "ETB", "etb": "ETB", "usd": "USD", "us$": "USD", "eur": "EUR", "gbp": "GBP"}
_THOUSANDS_SUFFIX = re.compile(r"000$")


@dataclass(frozen=True)
class UnitEvidence:
    currency: str | None = None  # ISO-like code, e.g. "ETB"
    scale: Decimal = Decimal(1)
    source: str = ""  # where the unit came from, as shown to the reviewer
    issue: str | None = None
    from_heading: bool = False  # read from the page's unit heading (it describes the page's amounts)


@dataclass(frozen=True)
class Normalized:
    value: Decimal | None
    unit: str | None  # "ETB", "count" or "fraction"
    basis: str  # how the conversion was made
    issue: str | None = None


def interpret_heading(heading: str) -> tuple[str | None, Decimal | None]:
    """'Birr'000' -> ('ETB', 1000); 'In millions of ETB' -> ('ETB', 1000000)."""
    words = [w.lower() for w in re.findall(r"[A-Za-z]+|\d+", heading)]
    currency = next((_CURRENCIES[w] for w in words if w in _CURRENCIES), None)
    scale = next((_SCALES[w] for w in words if w in _SCALES), None)
    if scale is None and _THOUSANDS_SUFFIX.search(heading):
        scale = Decimal(1_000)
    return currency, scale


def find_unit(candidate: RawCandidate, page: PageReport) -> UnitEvidence:
    if candidate.extraction_method == NARRATIVE_METHOD:
        return _sentence_unit(candidate)
    return page_unit(page)


def page_unit(page: PageReport) -> UnitEvidence:
    """The unit a table figure on this page is in, from the page's unit heading."""
    if not page.unit_headings:
        return UnitEvidence(source="no unit heading on the page")
    readings = {interpret_heading(h) for h in page.unit_headings}
    if len(readings) > 1:
        headings = ", ".join(f"'{h}'" for h in page.unit_headings)
        return UnitEvidence(source=f"page headings {headings}", issue=f"The page shows different units ({headings}).")
    (currency, scale), = readings
    return UnitEvidence(currency, scale or Decimal(1), f"page heading '{page.unit_headings[0]}'", from_heading=True)


def _sentence_unit(candidate: RawCandidate) -> UnitEvidence:
    before = candidate.text_before.split()
    after = candidate.text_after.split()
    word_before = strip_sentence_punctuation(before[-1]).lower() if before else ""
    word_after = strip_sentence_punctuation(after[0]).lower() if after else ""
    currency = _CURRENCIES.get(word_before)
    scale = _SCALES.get(word_after, Decimal(1))
    if currency is None and scale == 1:
        return UnitEvidence(source="sentence (no unit words next to the number)")
    pieces = ([before[-1]] if currency else []) + [candidate.raw_value_text] + ([after[0]] if scale != 1 else [])
    return UnitEvidence(currency, scale, f"sentence '{' '.join(pieces)}'")


def unit_label(unit: UnitEvidence) -> str:
    """The unit as printed, for messages: "Birr'000" (a page heading), "billion Birr" (a sentence), or ""."""
    heading = re.match(r"^page heading '(.*)'$", unit.source)
    if heading:
        return heading.group(1)
    if unit.from_heading or unit.issue:
        return ""
    words = {Decimal(1_000): "thousand", Decimal(1_000_000): "million", Decimal(1_000_000_000): "billion"}
    scale = words.get(unit.scale, "")
    currency = {"ETB": "Birr"}.get(unit.currency or "", unit.currency or "")
    return " ".join(w for w in (scale, currency) if w)


def count_in_thousands(parsed: ParsedNumber, unit: UnitEvidence, below: Decimal | None) -> bool:
    """True when a count printed under a "'000" heading is itself in thousands (D-069).

    Only for a metric that says so in metrics.yaml (count_in_thousands_below, e.g. share counts):
    Ethiopian banks have millions of shares, so a share count under a '000 heading that is below
    that number (Zemen prints 12,538 for 12.5 million) is in thousands, while one above it is in
    full (BoA prints 9,907,298 under the same kind of heading)."""
    return (below is not None and unit.from_heading and unit.scale == 1_000 and parsed.value is not None
            and not parsed.is_percent and abs(parsed.value) < below)


def normalize(parsed: ParsedNumber, unit: UnitEvidence, expected_unit: str, *,
              count_in_thousands_below: Decimal | None = None) -> Normalized:
    """Convert to the metric's expected unit, or explain why that is not safe.

    count_in_thousands_below: the metric's setting from metrics.yaml (see count_in_thousands)."""
    if parsed.value is None:
        return Normalized(None, None, "", parsed.issue)
    if unit.issue:
        return Normalized(None, None, "", unit.issue)

    if expected_unit == "fraction":
        if not parsed.is_percent:
            return Normalized(None, None, "", "Expected a percentage, but no % sign is printed.")
        return Normalized(parsed.value / 100, "fraction", "printed as a percentage, divided by 100")

    if parsed.is_percent:
        kind = "an amount in Birr" if expected_unit == "ETB" else "a count"
        return Normalized(None, None, "", f"Expected {kind}, but the figure is printed as a percentage.")

    if expected_unit == "count":
        if count_in_thousands(parsed, unit, count_in_thousands_below):
            value = parsed.value * unit.scale
            if value != value.to_integral_value():
                return Normalized(None, None, "", "Expected a whole number for a count.")
            return Normalized(value, "count", f"printed in thousands under the {unit.source}: "
                                              f"{parsed.value:,} × 1,000 = {value:,} (a count below "
                                              f"{count_in_thousands_below:,} under a '000 heading is in thousands)")
        if unit.from_heading:  # e.g. a share count in a Birr'000 table: the heading is for the amounts
            if parsed.value != parsed.value.to_integral_value():
                return Normalized(None, None, "", "Expected a whole number for a count.")
            return Normalized(parsed.value, "count", f"as printed: a count is not scaled by the {unit.source}")
        if unit.currency:
            return Normalized(None, None, "", f"Expected a count, but the figure is a {unit.currency} amount.")
        value = parsed.value * unit.scale
        if value != value.to_integral_value():
            return Normalized(None, None, "", "Expected a whole number for a count.")
        basis = "as printed" if unit.scale == 1 else f"{unit.source}: × {unit.scale:,}"
        return Normalized(value, "count", basis)

    if expected_unit == "ETB":
        if unit.currency and unit.currency != "ETB":
            return Normalized(None, None, "", f"The amount is in {unit.currency}, not Birr.")
        if unit.currency is None and unit.scale == 1:
            return Normalized(None, None, "", f"No currency or unit is stated for this figure ({unit.source}).")
        basis = f"{unit.source}: × {unit.scale:,}" if unit.scale != 1 else f"{unit.source}: as printed"
        return Normalized(parsed.value * unit.scale, "ETB", basis)

    return Normalized(None, None, "", f"Unit '{expected_unit}' is not converted automatically.")


def rounding_tolerance(parsed: ParsedNumber, unit: UnitEvidence) -> Decimal:
    """Half of the last printed digit, in normalized units: 318.79 billion -> 5,000,000."""
    step = Decimal(1).scaleb(-parsed.decimals) * unit.scale
    if parsed.is_percent:
        step = step / 100
    return step / 2
