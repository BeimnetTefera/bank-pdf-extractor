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
    return _table_unit(page)


def _table_unit(page: PageReport) -> UnitEvidence:
    if not page.unit_headings:
        return UnitEvidence(source="no unit heading on the page")
    readings = {interpret_heading(h) for h in page.unit_headings}
    if len(readings) > 1:
        headings = ", ".join(f"'{h}'" for h in page.unit_headings)
        return UnitEvidence(source=f"page headings {headings}", issue=f"The page shows different units ({headings}).")
    (currency, scale), = readings
    return UnitEvidence(currency, scale or Decimal(1), f"page heading '{page.unit_headings[0]}'")


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


def normalize(parsed: ParsedNumber, unit: UnitEvidence, expected_unit: str) -> Normalized:
    """Convert to the metric's expected unit, or explain why that is not safe."""
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
