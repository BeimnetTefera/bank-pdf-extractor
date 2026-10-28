"""Finishing the extraction review and entering calculated columns (synthetic report)."""

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from src import calculation_service as calc
from src import review_service as review
from src.config_loader import load_config
from src.excel_exporter import EVIDENCE_SHEET, METADATA_SHEET, build_workbook
from src.review_service import ReviewError
from tests.test_excel_exporter import approved_rows, evidence_rows
from tests.test_persistence_and_review import REVIEWER, conn, primary_id, saved  # noqa: F401 (fixtures)

LTD = "Loan-to-Deposit Ratio"


@pytest.fixture
def config(settings):
    return replace(load_config(), settings=settings)


@pytest.fixture
def finished(saved, conn):
    """The synthetic report with total assets approved and the extraction review finished."""
    _, document, _, _ = saved
    review.approve(conn, primary_id(saved, "total_assets_etb"), REVIEWER)
    review.finish_extraction(conn, document.id, REVIEWER, "done")
    return document.id


# --- finish / reopen ---------------------------------------------------------------------

def test_finish_locks_the_figures_until_reopened(saved, conn, finished):
    status = review.extraction_status(conn, finished)
    assert (status.finished, status.reviewer, status.reason) == (True, REVIEWER, "done")
    with pytest.raises(ReviewError, match="locked"):
        review.approve(conn, primary_id(saved, "profit_tax_etb"), REVIEWER)
    with pytest.raises(ReviewError, match="already finished"):
        review.finish_extraction(conn, finished, REVIEWER)

    with pytest.raises(ReviewError, match="reason"):
        review.reopen_extraction(conn, finished, REVIEWER, " ")
    review.reopen_extraction(conn, finished, REVIEWER, "Tax figure missed")
    assert not review.extraction_status(conn, finished).finished
    review.approve(conn, primary_id(saved, "profit_tax_etb"), REVIEWER)  # unlocked again


def test_not_approved_metrics_are_listed_before_finishing(saved, conn):
    _, document, _, _ = saved
    review.approve(conn, primary_id(saved, "total_assets_etb"), REVIEWER)
    assert review.not_approved_metrics(conn, document.id) == ["profit_tax_etb", "total_equity_etb", "employees"]


def test_report_events_are_append_only(conn, finished):
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM report_events")


# --- calculated values -------------------------------------------------------------------

def test_calculated_values_need_a_finished_extraction(saved, conn, config):
    _, document, _, _ = saved
    with pytest.raises(ReviewError, match="Finish the extraction review"):
        calc.enter_value(conn, config.export_template, document.id, LTD, "0.85", "Loans / Deposits", REVIEWER)


def test_enter_check_and_replace_calculated_values(conn, config, finished):
    template = config.export_template
    with pytest.raises(ReviewError, match="not a calculated column"):
        calc.enter_value(conn, template, finished, "Total Assets", "1", "x", REVIEWER)
    with pytest.raises(ReviewError, match="how 'Loan-to-Deposit Ratio' was calculated"):
        calc.enter_value(conn, template, finished, LTD, "0.85", "", REVIEWER)
    with pytest.raises(ReviewError, match="not a number"):
        calc.enter_value(conn, template, finished, LTD, "85%", "Loans / Deposits", REVIEWER)

    calc.enter_value(conn, template, finished, LTD, "0.85", "Total Loans / Total Deposits", REVIEWER)
    calc.enter_value(conn, template, finished, LTD, "0.8512", "Total Loans / Total Deposits, 4 dp", "Analyst B")
    (current,) = calc.current_values(conn, finished).values()
    assert (current.value, current.reviewer) == (Decimal("0.8512"), "Analyst B")
    calc.enter_value(conn, template, finished, LTD, "", "", REVIEWER)  # clear
    assert calc.current_values(conn, finished) == {}
    assert len(calc.history(conn, finished)) == 3  # nothing is overwritten
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE calculated_values SET value = '1'")


def test_calculated_values_are_exported_with_their_formula(conn, config, finished):
    calc.enter_value(conn, config.export_template, finished, LTD, "0.85", "Total Loans / Total Deposits", REVIEWER)
    wb = build_workbook(conn, config, [finished], exported_by=REVIEWER)

    (row,) = approved_rows(wb)
    assert (row["Total Assets"], row[LTD]) == (1_234_567_000, 0.85)
    assert wb["Approved Data"].cell(2, list(row).index(LTD) + 1).number_format == "General"  # not shown as %
    calculated = next(e for e in evidence_rows(wb) if e["export_column"] == LTD)
    assert (calculated["value_origin"], calculated["approval_note"], calculated["approved_by"]) == (
        "calculated by analyst", "Total Loans / Total Deposits", REVIEWER)
    metadata = dict(wb[METADATA_SHEET].iter_rows(min_row=2, values_only=True))
    assert (metadata["Approved values"], metadata["Calculated values"]) == (1, 1)
    assert EVIDENCE_SHEET in wb.sheetnames


def test_export_warns_when_extraction_was_reopened_after_calculating(conn, config, finished):
    calc.enter_value(conn, config.export_template, finished, LTD, "0.85", "Total Loans / Total Deposits", REVIEWER)
    review.reopen_extraction(conn, finished, REVIEWER, "Recheck loans")
    metadata = list(build_workbook(conn, config, [finished], exported_by=REVIEWER)[METADATA_SHEET].iter_rows(
        values_only=True))
    assert any(k == "Warning" and "reopened after calculated values" in v for k, v in metadata)


# --- auto values from the draft formulas (D-088) -------------------------------------------

EQUITY_TO_ASSET = "Equity-to-Asset Ratio"


@pytest.fixture
def finished_with_equity(saved, conn):
    _, document, _, _ = saved
    for metric_id in ("total_assets_etb", "total_equity_etb"):
        review.approve(conn, primary_id(saved, metric_id), REVIEWER, acknowledge_warnings=True)
    review.finish_extraction(conn, document.id, REVIEWER, "done")
    return document.id


def test_auto_value_is_worked_out_from_approved_figures(conn, config, finished_with_equity):
    autos = calc.auto_values(conn, config, finished_with_equity)
    assert set(autos) == set(config.export_template.calculated)  # every column has a draft formula
    assets = review.review_state(conn, finished_with_equity, "total_assets_etb").accepted_value
    equity = review.review_state(conn, finished_with_equity, "total_equity_etb").accepted_value
    auto = autos[EQUITY_TO_ASSET]
    assert auto.value == (equity / assets).quantize(Decimal("0.000001"))
    assert auto.words == "Total equity ÷ Total assets"
    assert auto.arithmetic == "500,000,000 / 1,234,567,000 = 0.4050002957"
    assert auto.problems == [] and auto.note.startswith("Auto (draft formula): Total equity ÷ Total assets")


def test_auto_value_names_the_missing_inputs(conn, config, finished_with_equity):
    auto = calc.auto_values(conn, config, finished_with_equity)[LTD]
    assert auto.value is None
    assert [i.label for i in auto.inputs if i.kind == "missing"] == ["Loans and advances (net)", "Total deposits"]
    assert any("Approve" in p for p in auto.problems)


def test_accept_auto_values_fills_only_empty_columns(conn, config, finished_with_equity):
    template = config.export_template
    calc.enter_value(conn, template, finished_with_equity, "Leverage Ratio", "7.4", "typed", REVIEWER)
    saved_columns = calc.accept_auto_values(conn, config, finished_with_equity, REVIEWER)
    assert EQUITY_TO_ASSET in saved_columns and "Leverage Ratio" not in saved_columns and LTD not in saved_columns
    current = calc.current_values(conn, finished_with_equity)
    assert current["Leverage Ratio"].value == Decimal("7.4")  # the analyst's value is kept
    assert current[EQUITY_TO_ASSET].formula_note.startswith("Auto (draft formula)")
    assert calc.accept_auto_values(conn, config, finished_with_equity, REVIEWER) == []  # nothing left to fill


def test_difference_is_flagged_above_half_a_percent():
    assert calc.difference(Decimal("0.851"), Decimal("0.85")) == (Decimal("0.001"), False)  # 0.12%: rounding
    assert calc.difference(Decimal("0.86"), Decimal("0.85")) == (Decimal("0.01"), True)  # 1.2%
    assert calc.difference(None, Decimal("0.85")) is None
    assert calc.difference(Decimal("1"), Decimal("0")) == (Decimal("1"), True)
