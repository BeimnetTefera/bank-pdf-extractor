"""Cross-checks between figures (D-045): sum checks within a report and last year's figures."""

from decimal import Decimal

import pytest

from src import review_service as review
from src.analysis import analyse
from src.config_loader import load_config
from src.cross_checks import (
    Figure,
    current_figures,
    implied_tolerance,
    prior_year_checks,
    previous_report,
    run_checks,
    sum_checks,
)
from src.database import connect
from src.document_manager import store_document
from src.pdf_extractor import EXTRACTOR_VERSION, extract_candidates
from src.project_service import load_candidates, save_analysis
from tests.conftest import make_pdf

BANK = "Synthetic Bank"
REVIEWER = "Analyst A"
METRICS = ["total_assets_etb", "total_liabilities_etb", "total_equity_etb",
           "profit_before_tax_etb", "profit_tax_etb", "profit_after_tax_etb"]


def statement(equity: str = "500,000", prior_assets: str = "1,200,000") -> list:
    return [[
        ("SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION", []),
        ("", ["Notes", "2026", "2025"]),
        ("Birr'000", []),
        ("Total assets", ["", "1,500,000", prior_assets]),
        ("Total liabilities", ["", "1,000,000", "800,000"]),
        ("Total equity", ["", equity, "400,000"]),
        ("Profit before tax", ["", "100,000", "90,000"]),
        ("Income tax expense", ["15", "(30,000)", "(27,000)"]),
        ("Profit after tax", ["", "70,000", "63,000"]),
    ]]


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def conn(settings):
    connection = connect(settings.database_path)
    yield connection
    connection.close()


def save_report(conn, settings, config, pages, period="2025/26"):
    pdf = make_pdf(pages)
    analysis = analyse(extract_candidates(pdf), config, METRICS, period)
    document = store_document(pdf, f"{period.replace('/', '-')}.pdf", BANK, period, settings)
    ids = save_analysis(conn, document, analysis, EXTRACTOR_VERSION)
    return document, analysis, {key: candidate_id for key, candidate_id in ids.items()}


def states_of(conn, document_id):
    return {m: review.review_state(conn, document_id, m) for m in METRICS}


def by_name(results, name):
    return next(r for r in results if r.name == name)


# --- sum checks ------------------------------------------------------------------------

def test_balanced_statement_passes_with_suggested_figures(config):
    analysis = analyse(extract_candidates(make_pdf(statement())), config, METRICS, "2025/26")
    results = sum_checks(config.checks, current_figures(analysis, {}, {}), analysis)
    balance = by_name(results, "Total assets = total liabilities + total equity")
    assert balance.outcome == "pass"
    assert "1,500,000,000 = Total liabilities 1,000,000,000 + Total equity 500,000,000" in balance.message
    assert "Not yet approved: Total assets" in balance.message
    # Tax is printed in brackets; the size is used, so the profit check passes too.
    assert by_name(results, "Profit before tax = profit after tax + income tax").outcome == "pass"


def test_figures_that_do_not_add_up_fail(config):
    analysis = analyse(extract_candidates(make_pdf(statement(equity="499,000"))), config, METRICS, "2025/26")
    balance = by_name(sum_checks(config.checks, current_figures(analysis, {}, {}), analysis),
                      "Total assets = total liabilities + total equity")
    assert balance.outcome == "fail"
    assert "off by 1,000,000 (rounding allows 1,500)" in balance.message


def test_rounding_of_printed_figures_is_allowed(config):
    analysis = analyse(extract_candidates(make_pdf(statement())), config, METRICS, "2025/26")
    figures = current_figures(analysis, {}, {})
    figures["profit_after_tax_etb"] = Figure(Decimal(70_001_000), Decimal(500), "suggested")  # 1 thousand off
    profit = by_name(sum_checks(config.checks, figures, analysis), "Profit before tax = profit after tax + income tax")
    assert profit.outcome == "pass" and "difference 1,000, within rounding" in profit.message


def test_check_without_all_figures_is_skipped(config):
    analysis = analyse(extract_candidates(make_pdf(statement())), config, METRICS, "2025/26")
    figures = current_figures(analysis, {}, {})
    del figures["total_equity_etb"]
    balance = by_name(sum_checks(config.checks, figures, analysis), "Total assets = total liabilities + total equity")
    assert balance.outcome == "skipped" and "Total equity" in balance.message


def test_checks_for_metrics_not_in_the_report_are_left_out(config):
    analysis = analyse(extract_candidates(make_pdf(statement())), config, ["total_assets_etb"], "2025/26")
    assert sum_checks(config.checks, current_figures(analysis, {}, {}), analysis) == []


def test_approved_and_pending_values_are_used_before_suggestions(conn, settings, config):
    document, analysis, ids = save_report(conn, settings, config, statement(equity="499,000"))
    equity = ids[("total_equity_etb", analysis.metric("total_equity_etb").primary.mapped.key)]
    review.correct(conn, equity, REVIEWER, "500000000", "ETB", "Misprint; note 30 shows 500,000")
    assets = ids[("total_assets_etb", analysis.metric("total_assets_etb").primary.mapped.key)]
    review.approve(conn, assets, REVIEWER)

    figures = current_figures(analysis, states_of(conn, document.id), ids)
    assert figures["total_equity_etb"] == Figure(Decimal(500_000_000), Decimal(500_000), "awaiting approval")
    assert figures["total_assets_etb"].source == "approved" and figures["total_assets_etb"].tolerance == 500
    balance = by_name(sum_checks(config.checks, figures, analysis), "Total assets = total liabilities + total equity")
    assert balance.outcome == "pass"


def test_rejected_suggestion_is_not_used(conn, settings, config):
    document, analysis, ids = save_report(conn, settings, config, statement())
    equity = ids[("total_equity_etb", analysis.metric("total_equity_etb").primary.mapped.key)]
    review.reject(conn, equity, REVIEWER, "Wrong row")
    assert "total_equity_etb" not in current_figures(analysis, states_of(conn, document.id), ids)


# --- last year's figures ---------------------------------------------------------------------

def approve_previous_assets(conn, settings, config, value: str) -> None:
    document, analysis, ids = save_report(conn, settings, config, statement(), period="2024/25")
    assets = ids[("total_assets_etb", analysis.metric("total_assets_etb").primary.mapped.key)]
    review.correct(conn, assets, REVIEWER, value, "ETB", "Set for the test")
    review.approve_correction(conn, document.id, "total_assets_etb", REVIEWER)


def test_prior_year_column_matches_last_years_approved_figure(conn, settings, config):
    approve_previous_assets(conn, settings, config, "1200000000")
    document, analysis, ids = save_report(conn, settings, config, statement())
    results = run_checks(conn, config.checks, analysis, states_of(conn, document.id), ids, BANK, "2025/26")
    check = by_name(results, "Total assets: last year")
    assert check.outcome == "pass" and "Synthetic Bank 2024/25: 1,200,000,000" in check.message


def test_prior_year_column_that_differs_fails(conn, settings, config):
    approve_previous_assets(conn, settings, config, "1150000000")
    document, analysis, ids = save_report(conn, settings, config, statement())
    check = by_name(run_checks(conn, config.checks, analysis, {}, ids, BANK, "2025/26"), "Total assets: last year")
    assert check.outcome == "fail" and "off by 50,000,000" in check.message and "restated" in check.message


def test_without_last_years_report_the_comparison_is_skipped(conn, settings, config):
    document, analysis, ids = save_report(conn, settings, config, statement())
    check = by_name(run_checks(conn, config.checks, analysis, {}, ids, BANK, "2025/26"), "Last year's figures")
    assert check.outcome == "skipped" and "No 2024/25 report" in check.message
    assert previous_report(conn, "Other Bank", 2026) is None


def test_prior_year_check_without_prior_figures_says_nothing(config):
    pages = [[("STATEMENT", []), ("", ["2026"]), ("Birr'000", []), ("Total assets", ["1,500,000"])]]
    analysis = analyse(extract_candidates(make_pdf(pages)), config, METRICS, "2025/26")
    assert prior_year_checks(analysis, None, "2024/25") == []


# --- rounding of typed figures ------------------------------------------------------------------

@pytest.mark.parametrize("value, tolerance", [
    ("286226976000", "500"), ("10627", "0.5"), ("0.29", "0.005"), ("318790000000", "500000"), ("0", "0.5"),
])
def test_implied_tolerance(value, tolerance):
    assert implied_tolerance(Decimal(value)) == Decimal(tolerance)


def test_saved_candidates_are_unchanged_by_checks(conn, settings, config):
    document, analysis, ids = save_report(conn, settings, config, statement())
    before = load_candidates(conn, document.id)
    run_checks(conn, config.checks, analysis, states_of(conn, document.id), ids, BANK, "2025/26")
    assert load_candidates(conn, document.id) == before
    assert review.decisions(conn, document.id, "total_assets_etb") == []
