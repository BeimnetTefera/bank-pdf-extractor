"""Calculated values with their formula and inputs (spec section 2, D-052)."""

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from src import review_service as review
from src.analysis import analyse
from src.calculated_entries import (
    preview,
    record_input_figure,
    resolve_input,
    save_calculation,
    saved_calculation,
    stale_reasons,
)
from src.config_loader import load_config
from src.database import connect
from src.document_manager import store_document
from src.pdf_extractor import EXTRACTOR_VERSION, extract_candidates
from src.project_service import save_analysis
from src.review_service import ReviewError
from tests.conftest import make_pdf

REVIEWER = "Analyst A"
METRICS = ["profit_after_tax_etb", "total_equity_etb", "total_assets_etb", "roe_percent", "eps_etb"]
ROE = "profit_after_tax_etb / avg(total_equity_etb, total_equity_etb_prev)"


def statement(year: int = 2023, pat: str = "3,872,832") -> list:
    return [[
        ("SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION AND PROFIT OR LOSS", []),
        ("", ["Notes", str(year), str(year - 1)]),
        ("Birr'000", []),
        ("Total assets", ["", "189,512,266", "149,451,430"]),
        ("Total equity", ["", "19,475,296", "14,216,185"]),
        ("Profit after tax", ["", pat, "3,235,824"]),
    ]]


@pytest.fixture
def env(settings):
    config = replace(load_config(), settings=settings)
    conn = connect(settings.database_path)

    def save(period: str = "2022/23", year: int = 2023, bank: str = "Synthetic Bank"):
        pdf = make_pdf(statement(year))
        document = store_document(pdf, f"{period.replace('/', '-')}.pdf", bank, period, settings)
        analysis = analyse(extract_candidates(pdf), config, METRICS, period)
        ids = save_analysis(conn, document, analysis, EXTRACTOR_VERSION)
        return document.id, analysis, ids

    yield conn, config, save
    conn.close()


def approve(conn, analysis, ids, metric_id):
    return review.approve(conn, ids[(metric_id, analysis.metric(metric_id).primary.mapped.key)], REVIEWER)


def test_roe_preview_names_every_figure_used(env):
    conn, config, save = env
    doc, analysis, ids = save()
    for m in ("profit_after_tax_etb", "total_equity_etb"):
        approve(conn, analysis, ids, m)
    p = preview(conn, config, doc, "roe_percent", ROE)
    assert p.ready and p.value == Decimal("0.229900")
    assert p.words == "Profit after tax ÷ average(Total equity, Total equity (previous year))"
    assert p.arithmetic == "3,872,832,000 / avg(19,475,296,000, 14,216,185,000) = 0.2298997779"
    pat, equity, prior = p.inputs
    assert (pat.kind, pat.page, pat.column_year, pat.row_label) == ("metric", 1, 2023, "Profit after tax")
    assert (prior.kind, prior.value, prior.column_year, prior.printed) == ("prior_column", Decimal("14216185000"), 2022, "14,216,185")
    assert prior.box is not None and "2022 column" in prior.where and 'row "Total equity"' in prior.where


def test_unapproved_input_blocks_the_calculation(env):
    conn, config, save = env
    doc, analysis, ids = save()
    approve(conn, analysis, ids, "total_equity_etb")
    p = preview(conn, config, doc, "roe_percent", ROE)
    assert not p.ready and p.problems == ["Approve Profit after tax first (it is not approved)."]
    with pytest.raises(ReviewError, match="cannot be saved yet"):
        save_calculation(conn, config, doc, "roe_percent", ROE, "ROE not printed", REVIEWER)


def test_saved_calculation_is_a_proposal_then_approved_and_kept(env):
    conn, config, save = env
    doc, analysis, ids = save()
    for m in ("profit_after_tax_etb", "total_equity_etb"):
        approve(conn, analysis, ids, m)
    decision_id = save_calculation(conn, config, doc, "roe_percent", ROE, "ROE is not printed; average-equity basis",
                                   REVIEWER)
    state = review.review_state(conn, doc, "roe_percent")
    assert state.status == "corrected" and state.pending.entry_kind == "calculated"
    review.approve_correction(conn, doc, "roe_percent", REVIEWER)
    assert review.review_state(conn, doc, "roe_percent").accepted_value == Decimal("0.229900")
    calc = saved_calculation(conn, decision_id)
    assert calc.expression == ROE and [i.variable for i in calc.inputs] == [
        "profit_after_tax_etb", "total_equity_etb", "total_equity_etb_prev"]
    assert stale_reasons(conn, calc) == []


def test_changed_input_makes_the_calculation_stale(env):
    conn, config, save = env
    doc, analysis, ids = save()
    pat_approval = approve(conn, analysis, ids, "profit_after_tax_etb")
    approve(conn, analysis, ids, "total_equity_etb")
    decision_id = save_calculation(conn, config, doc, "roe_percent", ROE, "note", REVIEWER)
    review.reopen(conn, doc, "profit_after_tax_etb", REVIEWER, "Check the figure")
    assert stale_reasons(conn, saved_calculation(conn, decision_id)) == ["Profit after tax is no longer approved."]
    review.correct(conn, ids[("profit_after_tax_etb", analysis.metric("profit_after_tax_etb").primary.mapped.key)],
                   REVIEWER, "3872833000", "ETB", "Rounding")
    review.approve_correction(conn, doc, "profit_after_tax_etb", REVIEWER)
    (reason,) = stale_reasons(conn, saved_calculation(conn, decision_id))
    assert reason == "Profit after tax changed from 3,872,832,000 to 3,872,833,000."
    assert pat_approval  # the original approval stays in the history


def test_eps_from_marked_figures(env):
    conn, config, save = env
    doc, _, _ = save()
    p = preview(conn, config, doc, "eps_etb", "eps_percent * par_value")
    assert [i.problem for i in p.inputs] == ["Mark EPS as printed (percent of par value) on the page.",
                                             "Mark Par value per share on the page."]
    record_input_figure(conn, doc, "eps_percent", "EPS as printed (percent of par value)", "0.39", "fraction", 1,
                        (300, 100, 330, 110), "39%", "Income statement, EPS row", REVIEWER)
    record_input_figure(conn, doc, "par_value", "Par value per share", "1000", "ETB", 1, (300, 120, 330, 130),
                        "1000", "Note 28: ordinary shares of Birr 1000 each", REVIEWER)
    p = preview(conn, config, doc, "eps_etb", "eps_percent * par_value")
    assert p.ready and p.value == Decimal("390.00") and p.arithmetic == "0.39 * 1,000 = 390"
    decision_id = save_calculation(conn, config, doc, "eps_etb", "eps_percent * par_value", "39% of par", REVIEWER)
    record_input_figure(conn, doc, "par_value", "Par value per share", "500", "ETB", 1, (300, 120, 330, 130),
                        "500", "Re-marked", REVIEWER)
    assert stale_reasons(conn, saved_calculation(conn, decision_id)) == [
        "Par value per share was marked again: 1,000 → 500."]


def test_previous_year_from_the_banks_earlier_report(env):
    conn, config, save = env
    old_doc, old_analysis, old_ids = save("2021/22", 2022)
    approve(conn, old_analysis, old_ids, "total_assets_etb")
    doc, _, _ = save()
    found = resolve_input(conn, config, doc, "total_assets_etb_prev")
    assert found.kind == "prior_column"  # the report's own prior-year column comes first
    conn.execute("INSERT INTO report_deletions (document_id, bank_name, reporting_period, original_filename, sha256, "
                 "approved_values, reason, reviewer, deleted_at) VALUES ('x','x','x','x','x',0,'x','x','x')")
    # Without a prior-year column (a metric only the old report approved), the old report is used.
    found = resolve_input(conn, config, doc, "roe_percent_prev")
    assert found.kind == "missing"
    review.manual_entry(conn, old_doc, "roe_percent", REVIEWER, "0.25", "fraction", 1, "printed in highlights")
    review.approve_correction(conn, old_doc, "roe_percent", REVIEWER)
    found = resolve_input(conn, config, doc, "roe_percent_prev")
    assert (found.kind, found.value, found.source_document_id) == ("previous_report", Decimal("0.25"), old_doc)


def test_typed_formula_problems(env):
    conn, config, save = env
    doc, _, _ = save()
    assert preview(conn, config, doc, "roe_percent", "roa_percent = a / b").problems[0].startswith("The formula is for")
    assert "uses the value it is meant to work out" in preview(conn, config, doc, "roe_percent", "roe_percent * 2").problems[0]
    assert preview(conn, config, doc, "roe_percent", "a +").problems == ["The formula ends too early: something is missing at the end."]


def test_calculations_are_append_only_and_go_with_the_report(env, settings):
    conn, config, save = env
    doc, analysis, ids = save()
    for m in ("profit_after_tax_etb", "total_equity_etb"):
        approve(conn, analysis, ids, m)
    save_calculation(conn, config, doc, "roe_percent", ROE, "note", REVIEWER)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE calculations SET expression = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="delete the whole report"):
        conn.execute("DELETE FROM calculation_inputs")
    review.delete_report(conn, doc, REVIEWER, "Demo", settings.data_dir)
    assert conn.execute("SELECT count(*) FROM calculations").fetchone()[0] == 0
