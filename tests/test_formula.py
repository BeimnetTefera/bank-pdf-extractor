"""The formula language for calculated values: parse, work out, write back (no eval)."""

from decimal import Decimal

import pytest

from src.formula import FormulaError, parse

ROE = "profit_after_tax_etb / avg(total_equity_etb, total_equity_etb_prev)"


def test_parse_names_and_target():
    target, f = parse("roe_percent = " + ROE)
    assert target == "roe_percent"
    assert f.names == ["profit_after_tax_etb", "total_equity_etb", "total_equity_etb_prev"]
    assert parse(ROE)[0] is None


def test_evaluate_exactly():
    _, f = parse(ROE)
    values = {"profit_after_tax_etb": Decimal("3872832000"), "total_equity_etb": Decimal("19475296000"),
              "total_equity_etb_prev": Decimal("14216185000")}
    assert round(f.evaluate(values), 4) == Decimal("0.2299")  # BoA 2022/23 on average equity
    assert parse("eps_percent * par_value")[1].evaluate({"eps_percent": Decimal("0.39"), "par_value": Decimal(1000)}) \
        == Decimal("390.00")


def test_precedence_brackets_and_functions():
    _, f = parse("a - (b + c) * 2 / sum(d, 1)")
    assert f.evaluate({"a": Decimal(10), "b": Decimal(1), "c": Decimal(2), "d": Decimal(2)}) == Decimal(8)
    assert parse("-a + abs(b)")[1].evaluate({"a": Decimal(1), "b": Decimal(-3)}) == Decimal(2)
    assert parse("max(a, b) - min(a, b)")[1].evaluate({"a": Decimal(5), "b": Decimal(2)}) == Decimal(3)


def test_written_back_in_words_and_as_arithmetic():
    _, f = parse(ROE)
    labels = {"profit_after_tax_etb": "Profit after tax", "total_equity_etb": "Total equity 2023",
              "total_equity_etb_prev": "Total equity 2022"}
    assert f.render(labels.get, words=True) == "Profit after tax ÷ average(Total equity 2023, Total equity 2022)"
    numbers = {"profit_after_tax_etb": "3,872,832,000", "total_equity_etb": "19,475,296,000",
               "total_equity_etb_prev": "14,216,185,000"}
    assert f.render(numbers.get) == "3,872,832,000 / avg(19,475,296,000, 14,216,185,000)"
    _, g = parse("a - (b + c)")
    assert g.render(str) == "a - (b + c)"  # the brackets that change the result are kept


@pytest.mark.parametrize("text, message", [
    ("", "empty"), ("a +", "ends too early"), ("a ** b", "Unexpected"), ("foo(a)", "Unknown function"),
    ("a; b", "cannot be used"), ("(a + b", "ends too early"), ("a b", "after a complete formula"),
])
def test_unreadable_formulas_are_explained(text, message):
    with pytest.raises(FormulaError, match=message):
        parse(text)


def test_missing_value_and_division_by_zero():
    _, f = parse("a / b")
    with pytest.raises(FormulaError, match="No value for 'b'"):
        f.evaluate({"a": Decimal(1)})
    with pytest.raises(FormulaError, match="divides by zero"):
        f.evaluate({"a": Decimal(1), "b": Decimal(0)})


def test_nothing_is_executed():
    with pytest.raises(FormulaError):
        parse("__import__('os').system('echo hi')")
