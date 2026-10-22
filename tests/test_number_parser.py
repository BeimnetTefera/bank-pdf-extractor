from decimal import Decimal

import pytest

from src.number_parser import parse_number


@pytest.mark.parametrize(
    "text,value,is_percent,decimals",
    [
        ("381,497,212", Decimal("381497212"), False, 0),
        ("(18,576)", Decimal("-18576"), False, 0),
        ("-12", Decimal("-12"), False, 0),
        ("286.23", Decimal("286.23"), False, 2),
        ("29%", Decimal("29"), True, 0),
        ("25.3%", Decimal("25.3"), True, 1),
        ("1,234.50", Decimal("1234.50"), False, 2),
        ("981", Decimal("981"), False, 0),
    ],
)
def test_supported_formats(text, value, is_percent, decimals):
    parsed = parse_number(text)
    assert (parsed.value, parsed.is_percent, parsed.decimals, parsed.issue) == (value, is_percent, decimals, None)


def test_precision_is_kept_exactly():
    assert parse_number("0.10").value == Decimal("0.10")
    assert str(parse_number("1,234,567.891").value) == "1234567.891"


@pytest.mark.parametrize("dash", ["-", "–", "—"])
def test_dash_is_not_read_as_zero(dash):
    parsed = parse_number(dash)
    assert parsed.value is None
    assert "not read as zero" in parsed.issue


@pytest.mark.parametrize("text", ["1,23,456", "12,34", "1,2345"])
def test_unusual_comma_grouping_is_flagged(text):
    parsed = parse_number(text)
    assert parsed.value is None and "comma grouping" in parsed.issue


@pytest.mark.parametrize("text", ["(123", "123)"])
def test_unbalanced_brackets_are_flagged(text):
    assert "Unbalanced" in parse_number(text).issue
