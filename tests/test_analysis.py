"""End-to-end Phase 3 checks on a synthetic report: extract -> map -> normalise -> validate."""

from dataclasses import replace
from decimal import Decimal

import pymupdf
import pytest

from src.analysis import analyse, report_end_year
from src.config_loader import LabelMapping, load_config
from tests.conftest import make_pdf

STATEMENT = [
    ("SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION", []),
    ("", ["Notes", "2026", "2025"]),
    ("Birr'000", []),
    ("Total assets", ["", "1,234,567", "987,654"]),
    ("Profit before tax", ["16", "15,849,406", "10,101,095"]),
    ("Total equity", ["", "500,000", "-"]),
]
NOTES_PAGE = [
    ("NOTES TO THE FINANCIAL STATEMENTS", []),
    ("", ["Notes", "2026", "2025"]),
    ("Birr'000", []),
    ("Profit before tax", ["", "15,849,406", "10,101,094"]),
    ("Total equity", ["", "512,000", "400,000"]),
]
CHART_PAGE = [("Total assets", ["286.23", "2025"])]


def report_pdf() -> bytes:
    pdf = pymupdf.open(stream=make_pdf([STATEMENT, NOTES_PAGE, CHART_PAGE, None]), filetype="pdf")
    page = pdf[3]
    page.insert_textbox(
        pymupdf.Rect(72, 72, 520, 300),
        "The Bank now operates 981 branches and 1,761 ATMs. Return on equity has jumped to 29% from 25.3%. "
        "Customer deposits grew to Birr 1.23 billion during the year.",
        fontsize=10,
    )
    data = pdf.tobytes()
    pdf.close()
    return data


MAPPINGS = (
    LabelMapping("total_assets_etb", aliases=("Total assets",)),
    LabelMapping("profit_before_tax_etb", aliases=("Profit before tax",)),
    LabelMapping("total_equity_etb", aliases=("Total equity",)),
    LabelMapping("total_deposits_etb", text_before=("deposits grew to Birr",)),
    LabelMapping("branches", text_after=("branches",)),
    LabelMapping("atms", text_after=("ATMs",)),
    LabelMapping("roe_percent", text_before=("Return on equity",)),
)
METRICS = ["total_assets_etb", "profit_before_tax_etb", "total_equity_etb", "total_deposits_etb",
           "branches", "atms", "roe_percent", "employees"]


@pytest.fixture(scope="module")
def result():
    from src.pdf_extractor import extract_candidates

    config = replace(load_config(), mappings=MAPPINGS)
    return analyse(extract_candidates(report_pdf()), config, METRICS, "2025/26")


def test_statement_value_is_primary_with_unit_and_period(result):
    assets = result.metric("total_assets_etb")
    primary = assets.primary
    assert primary.mapped.candidate.raw_value_text == "1,234,567"
    assert primary.mapped.candidate.page_index == 0
    assert primary.mapped.period == "current" and primary.mapped.period_year == 2026
    assert primary.normalized.value == Decimal("1234567000") and primary.normalized.unit == "ETB"


def test_prior_year_column_is_kept_as_comparison(result):
    prior = [mc for mc in result.metric("total_assets_etb").candidates if mc.mapped.period == "prior"]
    assert [mc.mapped.candidate.raw_value_text for mc in prior] == ["987,654"]


def test_note_numbers_are_not_mapped(result):
    values = [mc.mapped.candidate.raw_value_text for mc in result.metric("profit_before_tax_etb").candidates]
    assert "16" not in values


def test_chart_row_is_kept_but_marked_and_year_not_mapped(result):
    chart = [mc for mc in result.metric("total_assets_etb").candidates if mc.mapped.in_chart_row]
    assert [mc.mapped.candidate.raw_value_text for mc in chart] == ["286.23"]
    assert all(mc.mapped.candidate.raw_value_text != "2025" for mc in result.metric("total_assets_etb").candidates)
    assert result.metric("total_assets_etb").status == "needs_review"  # the chart does not flag the metric


def test_same_value_on_two_pages_is_not_a_conflict(result):
    pbt = result.metric("profit_before_tax_etb")
    assert not [f for f in pbt.findings if f.rule_id == "CONFLICT" and f.severity == "warning"]


def test_prior_year_difference_is_reported_as_info(result):
    pbt = result.metric("profit_before_tax_etb")
    (conflict,) = [f for f in pbt.findings if f.rule_id == "CONFLICT"]
    assert conflict.severity == "info" and "10,101,095" in conflict.message and "10,101,094" in conflict.message


def test_note_figure_that_differs_from_the_statement_is_an_info_note(result):
    # The statement page (title + several metrics) wins; a note printing another figure under the
    # same label is shown, not a conflict (D-062). Conflicts with sentences still flag: test_extraction_rules.
    equity = result.metric("total_equity_etb")
    assert equity.primary.mapped.candidate.raw_value_text == "500,000"
    assert equity.status == "needs_review"
    (note,) = [f for f in equity.findings if f.rule_id == "NOTE-DIFFERS"]
    assert note.severity == "info" and "512,000" in note.message and "500,000" in note.message


def test_dash_is_flagged_not_zero(result):
    dash = [mc for mc in result.metric("total_equity_etb").candidates if mc.mapped.candidate.raw_value_text == "-"]
    assert dash and dash[0].normalized.value is None
    assert any(f.rule_id == "VALUE-PARSE" for f in result.metric("total_equity_etb").findings)


def test_sentence_figures(result):
    assert result.metric("branches").primary.normalized.value == Decimal(981)
    assert result.metric("atms").primary.normalized.value == Decimal(1761)
    roe = result.metric("roe_percent")
    assert [mc.mapped.candidate.raw_value_text for mc in roe.candidates] == ["29%"]  # 25.3% is the prior figure
    assert roe.primary.normalized.value == Decimal("0.29")
    deposits = result.metric("total_deposits_etb").primary
    assert deposits.normalized.value == Decimal("1230000000")
    assert any(f.rule_id == "SENTENCE" for f in result.metric("branches").findings)


def test_metric_not_found_is_not_zero(result):
    employees = result.metric("employees")
    assert employees.status == "not_found" and employees.primary is None
    assert any(f.rule_id == "NOT-FOUND" and "not the same as zero" in f.message for f in employees.findings)


def test_ambiguous_wording_flags_both_metrics():
    from src.pdf_extractor import extract_candidates

    mappings = MAPPINGS + (LabelMapping("total_liabilities_etb", aliases=("Total assets",)),)
    config = replace(load_config(), mappings=mappings)
    result = analyse(extract_candidates(report_pdf()), config, ["total_assets_etb", "total_liabilities_etb"], "2025/26")
    for metric_id in ("total_assets_etb", "total_liabilities_etb"):
        assert result.metric(metric_id).status == "flagged"
        assert any(f.rule_id == "MAP-AMBIGUOUS" for f in result.metric(metric_id).findings)


def test_unknown_report_year_leaves_period_unknown():
    from src.pdf_extractor import extract_candidates

    config = replace(load_config(), mappings=MAPPINGS)
    result = analyse(extract_candidates(report_pdf()), config, ["total_assets_etb"], "")
    assert {mc.mapped.period for mc in result.metric("total_assets_etb").candidates} == {"unknown"}


@pytest.mark.parametrize(
    "text,year",
    [("2025/26", 2026), ("2025-26", 2026), ("2025/2026", 2026), ("FY2026", 2026), ("1999/00", 2000),
     ("2025/27", None), ("", None), ("next year", None)],
)
def test_report_end_year(text, year):
    assert report_end_year(text) == year
