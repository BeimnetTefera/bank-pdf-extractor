from decimal import Decimal

import pytest

from src.number_parser import parse_number
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, PageReport, RawCandidate
from src.unit_normalizer import UnitEvidence, find_unit, interpret_heading, normalize, rounding_tolerance


def page(*headings: str) -> PageReport:
    return PageReport(0, 100, 0, True, 600, 800, unit_headings=headings)


def sentence(value: str, before: str = "", after: str = "") -> RawCandidate:
    return RawCandidate(0, 0, 0, 1, "", value, "", "", (0, 0, 1, 1), NARRATIVE_METHOD, before, after)


def table_value(value: str) -> RawCandidate:
    return RawCandidate(0, 0, 0, 1, "Total assets", value, "", "", (0, 0, 1, 1), TABLE_METHOD)


@pytest.mark.parametrize(
    "heading,currency,scale",
    [
        ("Birr'000", "ETB", 1_000),
        ("Birr’000", "ETB", 1_000),
        ("Birr�000", "ETB", 1_000),
        ("ETB '000", "ETB", 1_000),
        ("In Thousands of ETB", "ETB", 1_000),
        ("In Millions of Birr", "ETB", 1_000_000),
    ],
)
def test_interpret_heading(heading, currency, scale):
    assert interpret_heading(heading) == (currency, Decimal(scale))


def test_table_value_uses_page_heading():
    unit = find_unit(table_value("381,497,212"), page("Birr'000"))
    result = normalize(parse_number("381,497,212"), unit, "ETB")
    assert result.value == Decimal("381497212000")
    assert result.unit == "ETB"
    assert "Birr'000" in result.basis and "1,000" in result.basis


def test_conflicting_page_headings_are_flagged():
    unit = find_unit(table_value("1"), page("Birr'000", "In millions of Birr"))
    result = normalize(parse_number("1"), unit, "ETB")
    assert result.value is None and "different units" in result.issue


def test_amount_without_any_unit_is_flagged():
    unit = find_unit(table_value("286.23"), page())
    assert "No currency or unit" in normalize(parse_number("286.23"), unit, "ETB").issue


def test_sentence_currency_and_scale():
    c = sentence("318.79", before="increased deposits to Birr", after="billion as of June")
    result = normalize(parse_number("318.79"), find_unit(c, page()), "ETB")
    assert result.value == Decimal("318790000000")
    assert "Birr 318.79 billion" in result.basis


def test_foreign_currency_is_not_converted():
    c = sentence("966.64", before="generated USD", after="million in foreign currency")
    assert "in USD, not Birr" in normalize(parse_number("966.64"), find_unit(c, page()), "ETB").issue


def test_percentage_to_fraction():
    result = normalize(parse_number("29%"), UnitEvidence(), "fraction")
    assert (result.value, result.unit) == (Decimal("0.29"), "fraction")


def test_fraction_metric_without_percent_sign_is_flagged():
    assert "no % sign" in normalize(parse_number("29"), UnitEvidence(), "fraction").issue


def test_percentage_for_an_amount_metric_is_flagged():
    assert "printed as a percentage" in normalize(parse_number("77%"), UnitEvidence(), "ETB").issue


def test_count_as_printed_and_rejects_amounts():
    assert normalize(parse_number("981"), UnitEvidence(), "count").value == Decimal(981)
    assert "amount" in normalize(parse_number("981"), UnitEvidence(currency="ETB"), "count").issue
    assert "whole number" in normalize(parse_number("2.5"), UnitEvidence(), "count").issue


def test_unparseable_value_passes_on_the_parse_issue():
    assert "dash" in normalize(parse_number("-"), UnitEvidence(), "ETB").issue


def test_rounding_tolerance():
    assert rounding_tolerance(parse_number("318.79"), UnitEvidence("ETB", Decimal(10**9))) == Decimal(5_000_000)
    assert rounding_tolerance(parse_number("10,101,095"), UnitEvidence("ETB", Decimal(1000))) == Decimal(500)
    assert rounding_tolerance(parse_number("29%"), UnitEvidence()) == Decimal("0.005")
