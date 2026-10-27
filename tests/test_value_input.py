"""The one reader for typed values, and what Approve says it stores (D-067, D-068)."""

from decimal import Decimal

import pytest

from src.value_input import (
    approval_label,
    input_text,
    read_value,
    sign_note,
    small_ratio_label,
    stored_on_approval,
)


@pytest.mark.parametrize("text", ["22.99", "22.99%", "22.99 percent", "22.99 per cent", "0.2299"])
def test_every_way_of_typing_a_percentage_stores_the_same_fraction(text):
    typed = read_value(text, "fraction")
    assert typed.value == Decimal("0.2299")
    assert typed.preview == "Will store 0.2299 (22.99%)"


def test_a_bare_ratio_of_one_or_less_is_ambiguous_and_the_reviewer_chooses():
    as_fraction = read_value("0.5", "fraction")
    assert as_fraction.ambiguous and as_fraction.value == Decimal("0.5")  # default: a fraction (50%)
    as_percent = read_value("0.5", "fraction", "percent")
    assert as_percent.value == Decimal("0.005") and as_percent.preview == "Will store 0.005 (0.5%)"
    assert small_ratio_label("fraction", "0.5") == "a fraction (0.5 = 50%)"
    assert small_ratio_label("percent", "0.5") == "a percentage (0.5%)"
    assert not read_value("0.5%", "fraction").ambiguous  # a % sign settles it


@pytest.mark.parametrize("text,value", [
    ("1,234,567,000", "1234567000"), ("1234567000", "1234567000"), ("1,234.5 million", "1234500000"),
    ("Birr 2.5 billion", "2500000000"), ("12 thousand", "12000"), ("(1,357,194)", "-1357194"),
])
def test_amounts_accept_commas_and_a_scale_word(text, value):
    assert read_value(text, "ETB").value == Decimal(value)


def test_the_full_value_is_shown():
    typed = read_value("1,234.5 million", "ETB")
    assert typed.preview == "Will store 1,234,500,000 ETB"
    assert typed.reading == "1,234.5 million = 1,234,500,000 ETB"


@pytest.mark.parametrize("text,unit,problem", [
    ("", "ETB", "empty"), ("1,2a", "ETB", "not a number"), ("12%", "ETB", "percentage"),
    ("12.5", "count", "whole number"), ("2 million", "fraction", "scale word"),
])
def test_values_that_cannot_be_stored_are_explained(text, unit, problem):
    typed = read_value(text, unit)
    assert typed.value is None and problem in typed.problem


def test_counts_accept_a_scale_word():
    assert read_value("12.538 million", "count").value == Decimal(12_538_000)


def test_brackets_become_positive_when_the_metric_says_so():
    typed = read_value("(1,357,194,000)", "ETB", sign="positive")
    assert typed.value == Decimal(1_357_194_000) and typed.made_positive
    assert "recorded as 1,357,194,000 ETB (positive)" in typed.reading
    as_printed = read_value("(1,357,194,000)", "ETB", sign="as_printed")
    assert as_printed.value == Decimal(-1_357_194_000) and not as_printed.made_positive


def test_approve_says_exactly_what_is_stored():
    assert approval_label("189,512,266", "Birr'000", Decimal(189_512_266_000), "ETB") == (
        "Approve 189,512,266 (Birr'000) as 189,512,266,000 ETB")
    assert approval_label("39%", "", Decimal("0.39"), "fraction") == "Approve 39% as 0.39 (39%)"


def test_sign_note_shows_the_conversion():
    value = Decimal(-1_357_194_000)
    assert stored_on_approval(value, "positive") == Decimal(1_357_194_000)
    assert sign_note("(1,357,194)", value, "ETB", "positive") == (
        "(1,357,194) printed in brackets -> recorded as 1,357,194,000 ETB (positive)")
    assert "(negative, as printed)" in sign_note("(1,357,194)", value, "ETB", "as_printed")
    assert sign_note("1,357,194", -value, "ETB", "positive") == ""


def test_pre_filled_values_are_never_ambiguous():
    assert input_text(Decimal("0.39"), "fraction") == "39%"
    assert input_text(Decimal("1234567000.0"), "ETB") == "1234567000"
    assert read_value(input_text(Decimal("0.005"), "fraction"), "fraction").value == Decimal("0.005")
