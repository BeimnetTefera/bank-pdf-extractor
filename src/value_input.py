"""Read a value a reviewer types into a value box, in the unit it will be stored in (D-067).

One reader for every value box (Correct, Enter manually, a marked number, an input figure of a
calculation, an external source), so the same text is always read the same way and the screen
can say exactly what will be stored before anything is saved:

  fraction  "22.99", "22.99%", "22.99 percent" and "0.2299" all store 0.2299. A bare value of 1
            or less without % is ambiguous ("0.5": half, or half a percent?); the reviewer
            chooses, and the default is a fraction (0.5 = 50%).
  ETB       commas and an optional scale word: "1,234.5 million" stores 1,234,500,000.
  count     the same; the result must be a whole number.

Brackets and a leading minus are read as negative, as in the reports: "(1,357,194)".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal

from src.number_parser import parse_number

SMALL_RATIO_CHOICES = ("fraction", "percent")  # how a bare ratio of 1 or less is meant
SMALL_RATIO_LABELS = {"fraction": "a fraction ({n} = {p}%)", "percent": "a percentage ({n}%)"}
SIGN_CHOICES = ("positive", "as_printed")  # how a value typed in brackets or with a minus is recorded (D-068)
SIGN_LABELS = {"positive": "Record as positive", "as_printed": "Keep negative"}
SCALE_WORDS = {
    "thousand": Decimal(1_000), "thousands": Decimal(1_000),
    "million": Decimal(1_000_000), "millions": Decimal(1_000_000), "mn": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000), "billions": Decimal(1_000_000_000), "bn": Decimal(1_000_000_000),
}
EXAMPLES = {"ETB": "e.g. 1,234,567,000 or 1,234.6 million", "count": "e.g. 10,627 or 12.5 million",
            "fraction": "e.g. 22.99% or 0.2299"}
HELP = {
    "ETB": "The amount in full Birr. Commas and a scale word are fine: 1,234,567,000 or 1,234.567 million. "
           "Brackets mean negative, as in the report.",
    "count": "A whole number. Commas and a scale word are fine: 12,538,000 or 12.538 million.",
    "fraction": "A percentage: 22.99, 22.99%, 22.99 percent or 0.2299 all store 0.2299 (22.99%).",
}
_CURRENCY_WORDS =("birr", "etb", "br", "br.")
_PERCENT_WORDS = re.compile(r"\s*(?:%|percent|per\s+cent|pct)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class TypedNumber:
    """The pieces of a typed value: the number, a % sign or word, a scale word."""

    number: Decimal | None
    is_percent: bool = False
    scale_word: str | None = None
    problem: str | None = None


@dataclass(frozen=True)
class TypedValue:
    text: str
    unit: str
    value: Decimal | None  # in the unit it will be stored in; None when it cannot be read
    problem: str | None = None
    ambiguous: bool = False  # a bare ratio of 1 or less: the reviewer chooses fraction or percent
    reading: str = ""  # how the text was read, e.g. "22.99% = 0.2299"
    made_positive: bool = False  # typed negative (brackets) and recorded as a positive amount (D-068)

    @property
    def preview(self) -> str:
        """'Will store 0.2299 (22.99%)', 'Will store 1,234,500,000 ETB', 'Will store 10,627'."""
        if self.value is None:
            return ""
        return f"Will store {stored_text(self.value, self.unit)}"


def split_typed(text: str) -> TypedNumber:
    """'Birr 1,234.5 million' -> 1234.5 with scale word 'million'; '22.99 percent' -> 22.99, percent."""
    words = " ".join((text or "").split())
    if not words:
        return TypedNumber(None, problem="Type the value first: the Value box is empty.")
    is_percent = bool(_PERCENT_WORDS.search(words))
    if is_percent:
        words = _PERCENT_WORDS.sub("", words)
    parts = words.split(" ")
    if parts and parts[0].lower() in _CURRENCY_WORDS:
        parts = parts[1:]
    if parts and parts[-1].lower() in _CURRENCY_WORDS:
        parts = parts[:-1]
    scale_word = None
    if len(parts) > 1 and parts[-1].lower() in SCALE_WORDS:
        scale_word = parts.pop().lower()
    if len(parts) != 1:
        return TypedNumber(None, problem=_not_a_number(text))
    parsed = parse_number(parts[0])
    if parsed.value is None:
        return TypedNumber(None, problem=_not_a_number(text))
    return TypedNumber(parsed.value, is_percent or parsed.is_percent, scale_word)


def read_value(text: str, unit: str, small_ratio: str = "fraction", sign: str = "as_printed") -> TypedValue:
    """Read text typed for a value in unit; small_ratio says how a bare ratio of 1 or less is meant.

    sign 'positive' (the metric's setting, D-068): a value typed in brackets or with a minus is
    recorded as a positive amount, and the reading says so."""
    read = _read_signed(text, unit, small_ratio)
    if sign != "positive" or read.value is None or read.value >= 0:
        return read
    note = f"{text.strip()} is negative (brackets) -> recorded as {stored_text(-read.value, unit)} (positive)"
    return replace(read, value=-read.value, made_positive=True,
                   reading=f"{read.reading}; {note}" if read.reading else note)


def _read_signed(text: str, unit: str, small_ratio: str) -> TypedValue:
    typed = split_typed(text)
    if typed.number is None:
        return TypedValue(text, unit, None, typed.problem)
    number = typed.number
    if unit == "fraction":
        return _fraction(text, typed, small_ratio)
    if typed.is_percent:
        kind = "an amount in Birr" if unit == "ETB" else "a count" if unit == "count" else f"in {unit}"
        return TypedValue(text, unit, None, f"'{text.strip()}' is a percentage, but this value is {kind}.")
    if typed.scale_word and unit not in ("ETB", "count"):
        return TypedValue(text, unit, None, f"A {unit} has no scale word: type the number only.")
    scale = SCALE_WORDS.get(typed.scale_word or "", Decimal(1))
    value = exact(number * scale)
    if unit == "count" and value != value.to_integral_value():
        return TypedValue(text, unit, None, f"{value:,f} is not a whole number, so it cannot be a count.")
    reading = f"{number:,f} {typed.scale_word} = {stored_text(value, unit)}" if typed.scale_word else ""
    return TypedValue(text, unit, value, reading=reading)


def _fraction(text: str, typed: TypedNumber, small_ratio: str) -> TypedValue:
    number = typed.number
    if typed.scale_word:
        return TypedValue(text, "fraction", None, "A percentage has no scale word: type e.g. 22.99% or 0.2299.")
    shown = f"{number.normalize():f}"
    if typed.is_percent:
        return TypedValue(text, "fraction", exact(number / 100), reading=f"{shown}% = {exact(number / 100):f}")
    if abs(number) > 1:  # 22.99 can only mean 22.99%: a fraction above 1 would be over 100%
        return TypedValue(text, "fraction", exact(number / 100),
                          reading=f"{shown} read as {shown}% = {exact(number / 100):f}")
    if small_ratio not in SMALL_RATIO_CHOICES:
        raise ValueError(f"small_ratio must be one of {SMALL_RATIO_CHOICES}")
    value = number if small_ratio == "fraction" else exact(number / 100)
    reading = (f"{shown} read as a fraction ({(number * 100).normalize():f}%)" if small_ratio == "fraction"
               else f"{shown} read as {shown}% = {value:f}")
    return TypedValue(text, "fraction", value, ambiguous=True, reading=reading)


def small_ratio_label(choice: str, text: str) -> str:
    """How the choice reads for the number typed: 'a fraction (0.29 = 29%)' / 'a percentage (0.29%)'."""
    typed = split_typed(text)
    n = typed.number if typed.number is not None else Decimal(0)
    return SMALL_RATIO_LABELS[choice].format(n=f"{n.normalize():f}", p=f"{(n * 100).normalize():f}")


def sign_label(choice: str, default: str) -> str:
    """'Record as positive (default)' / 'Keep negative': the default follows the metric's sign setting."""
    return SIGN_LABELS[choice] + (" (default)" if choice == default_sign(default) else "")


def default_sign(metric_sign: str | None) -> str:
    """The choice offered first for a negative value: the metric's setting, positive unless it says as_printed."""
    return "as_printed" if metric_sign == "as_printed" else "positive"


def sign_of(typed: TypedValue) -> str | None:
    """The direction kept with a typed value (review_decisions.sign_choice): 'positive' when a negative
    value is recorded as positive, 'as_printed' when it is kept negative, None when it was not negative."""
    if typed.made_positive:
        return "positive"
    return "as_printed" if typed.value is not None and typed.value < 0 else None


def stored_text(value: Decimal, unit: str | None) -> str:
    """'0.2299 (22.99%)', '1,234,500,000 ETB', '10,627'."""
    if unit == "fraction":
        return f"{value.normalize():f} ({(value * 100).normalize():f}%)"
    shown = f"{exact(value):,f}"
    return f"{shown} ETB" if unit == "ETB" else shown


def input_text(value: Decimal | None, unit: str | None) -> str:
    """How a known value is pre-filled in a value box: fractions as a percentage ('39%'), so the
    box is never ambiguous; amounts and counts in full digits ('1234567000')."""
    if value is None:
        return ""
    if unit == "fraction":
        return f"{(value * 100).normalize():f}%"
    return f"{exact(value):f}"


def stored_on_approval(value: Decimal, sign: str) -> Decimal:
    """What approve() stores for a converted figure: its absolute value when the sign choice is 'positive'."""
    return abs(value) if sign == "positive" else value


def approval_label(printed: str, unit_label: str, value: Decimal, unit: str | None) -> str:
    """The Approve button, saying exactly what is stored (D-067):
    "Approve 189,512,266 (Birr'000) as 189,512,266,000 ETB"."""
    where = f" ({unit_label})" if unit_label else ""
    return f"Approve {printed.strip()}{where} as {stored_text(value, unit)}"


def sign_note(printed: str, value: Decimal, unit: str | None, sign: str) -> str:
    """How a figure printed in brackets is recorded (D-068), or "" when it is not negative:
    "(1,357,194) printed in brackets -> recorded as 1,357,194,000 ETB (positive)"."""
    if value >= 0:
        return ""
    how = "brackets" if printed.strip().startswith("(") else "with a minus sign"
    if sign == "positive":
        return f"{printed.strip()} printed in {how} -> recorded as {stored_text(abs(value), unit)} (positive)"
    return f"{printed.strip()} printed in {how} -> recorded as {stored_text(value, unit)} (negative, as printed)"


def exact(value: Decimal) -> Decimal:
    """Whole numbers without decimals (1234500000, not 1234500000.0); others without trailing zeros."""
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize()


def _not_a_number(text: str) -> str:
    return (f"'{(text or '').strip()}' is not a number. Use digits with optional commas, a decimal point, a minus "
            "sign or brackets, and optionally % or a scale word (thousand, million, billion).")
