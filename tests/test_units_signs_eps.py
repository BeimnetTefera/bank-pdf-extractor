"""Counts printed in thousands (D-069), sign settings (D-068) and EPS from the par value (D-070)."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from src import review_service as review
from src.analysis import analyse
from src.calculated_entries import (
    FoundFigure,
    convert_eps_from_par,
    eps_conversion_text,
    eps_cross_check,
    saved_calculation,
)
from src.config_loader import ConfigError, load_config, load_metrics
from src.database import connect
from src.document_manager import store_document
from src.number_parser import parse_number
from src.pdf_extractor import EXTRACTOR_VERSION, extract_candidates
from src.project_service import load_candidates, save_analysis
from src.unit_normalizer import UnitEvidence, normalize
from tests.conftest import make_pdf

REVIEWER = "Analyst A"
REPORTS = Path.home() / "OneDrive" / "Desktop" / "Bank Reports"
THOUSANDS = UnitEvidence("ETB", Decimal(1000), "page heading 'ETB'000'", from_heading=True)


# --- counts printed in thousands -------------------------------------------------------------

def test_a_share_count_under_a_thousands_heading_is_multiplied_with_a_note():
    result = normalize(parse_number("12,538"), THOUSANDS, "count", count_in_thousands_below=Decimal(1_000_000))
    assert result.value == Decimal(12_538_000) and result.unit == "count"
    assert "12,538 × 1,000 = 12,538,000" in result.basis


def test_a_full_share_count_and_other_counts_are_taken_as_printed():
    full = normalize(parse_number("9,907,298"), THOUSANDS, "count", count_in_thousands_below=Decimal(1_000_000))
    assert full.value == Decimal(9_907_298)  # BoA prints the full count under Birr'000
    branches = normalize(parse_number("250"), THOUSANDS, "count")  # no setting: not scaled
    assert branches.value == Decimal(250)


def test_the_metric_settings_are_read_and_checked(tmp_path):
    config = load_config()
    assert config.metric("weighted_average_shares").count_in_thousands_below == Decimal(1_000_000)
    assert config.metric("profit_tax_etb").sign == "positive"  # brackets recorded as a positive amount
    assert config.metric("profit_after_tax_etb").sign == "as_printed"  # can really be negative
    bad = tmp_path / "metrics.yaml"
    bad.write_text("metrics:\n  - {id: x_etb, display_name: X, definition: D, expected_unit: ETB, active: true, "
                   "sign: upside_down}\n  - {id: y, display_name: Y, definition: D, expected_unit: ETB, "
                   "active: true, count_in_thousands_below: 10}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_metrics(bad)
    assert "sign 'upside_down' is not supported" in str(error.value)
    assert "count_in_thousands_below must be a positive whole number, for a count metric" in str(error.value)


def test_real_zemen_weighted_average_shares():
    path = REPORTS / "Zemen_Bank_2025_26.pdf"
    if not path.is_file():
        pytest.skip("Zemen_Bank_2025_26.pdf is not on this computer")
    config = load_config()
    analysis = analyse(extract_candidates(path.read_bytes()), config, ["weighted_average_shares"], "2025/26")
    primary = analysis.metric("weighted_average_shares").primary
    assert primary.mapped.candidate.page_index + 1 == 63 and primary.mapped.candidate.raw_value_text == "12,538"
    assert primary.normalized.value == Decimal(12_538_000) and "× 1,000" in primary.normalized.basis


# --- saved reports: signs and EPS --------------------------------------------------------------

def statement() -> list:
    return [[
        ("SYNTHETIC BANK S.C. STATEMENT OF PROFIT OR LOSS", []),
        ("", ["Notes", "2023", "2022"]),
        ("Birr'000", []),
        ("Income tax expense", ["", "(1,357,194)", "(1,000,000)"]),
        ("Profit after tax", ["", "3,869,598", "3,235,824"]),
        ("Weighted average number of ordinary shares in issue", ["", "9,907,298", "6,190,260"]),
        ("Earnings per share", ["", "39%", "57%"]),
    ]]


@pytest.fixture
def saved(settings):
    config = replace(load_config(), settings=settings)
    conn = connect(settings.database_path)
    pdf = make_pdf(statement())
    document = store_document(pdf, "s.pdf", "Synthetic Bank", "2022/23", settings)
    metrics = ["profit_tax_etb", "profit_after_tax_etb", "weighted_average_shares", "eps_etb"]
    analysis = analyse(extract_candidates(pdf), config, metrics, "2022/23")
    save_analysis(conn, document, analysis, EXTRACTOR_VERSION)
    yield conn, config, document.id
    conn.close()


def current(conn, document_id: str, metric_id: str):
    return next(c for c in load_candidates(conn, document_id, metric_id) if c.period == "current")


def test_a_bracketed_amount_approved_as_positive_keeps_the_printed_direction(saved):
    conn, _, doc = saved
    tax = current(conn, doc, "profit_tax_etb")
    assert tax.normalized_value == Decimal(-1_357_194_000) and tax.raw_value_text == "(1,357,194)"
    review.approve(conn, tax.id, REVIEWER, sign="positive")
    state = review.review_state(conn, doc, "profit_tax_etb")
    assert state.accepted_value == Decimal(1_357_194_000) and state.accepted_decision.sign_choice == "positive"


def test_a_correction_typed_in_brackets_keeps_the_sign_choice_through_approval(saved):
    conn, _, doc = saved
    tax = current(conn, doc, "profit_tax_etb")
    review.correct(conn, tax.id, REVIEWER, Decimal(1_357_195_000), "ETB", "last digit", sign="positive")
    review.approve_correction(conn, doc, "profit_tax_etb", REVIEWER)
    assert review.review_state(conn, doc, "profit_tax_etb").accepted_decision.sign_choice == "positive"
    with pytest.raises(review.ReviewError, match="Sign choice"):
        review.correct(conn, tax.id, REVIEWER, "1", "ETB", "x", sign="sideways")


def test_eps_from_par_value_is_a_calculation_with_both_boxes(saved):
    conn, config, doc = saved
    eps_box = current(conn, doc, "eps_etb")
    eps = FoundFigure(Decimal("0.39"), eps_box.page_index + 1, eps_box.bbox, eps_box.raw_value_text)
    par = FoundFigure(Decimal(1000), 1, (170.0, 180.0, 195.0, 194.0), "1000")
    assert eps_conversion_text(eps.value, par.value, 113) == "Convert: 39% x par value Birr 1,000 (p113) = 390 Birr"
    assert eps_conversion_text(Decimal("0.539"), Decimal(1000), 18).endswith("= 539 Birr")
    decision_id = convert_eps_from_par(conn, config, doc, eps, par, REVIEWER)
    state = review.review_state(conn, doc, "eps_etb")
    assert state.status == "corrected" and state.pending.entry_kind == "calculated"
    assert state.pending.reviewed_value == Decimal("390.00")
    calc = saved_calculation(conn, decision_id)
    assert calc.expression == "eps_percent * par_value"
    assert [(i.variable, i.page, i.printed) for i in calc.inputs] == [("eps_percent", 1, "39%"), ("par_value", 1, "1000")]
    review.approve_correction(conn, doc, "eps_etb", REVIEWER)  # approved like any calculation
    assert review.review_state(conn, doc, "eps_etb").accepted_value == Decimal("390.00")


def test_eps_from_par_value_records_nothing_when_the_calculation_is_refused(saved, monkeypatch):
    """All or nothing: the two input figures are not kept when saving the calculation fails."""
    from src import calculated_entries

    conn, config, doc = saved
    eps = FoundFigure(Decimal("0.39"), 1, (10.0, 10.0, 30.0, 20.0), "39%")
    par = FoundFigure(Decimal(1000), 1, (170.0, 180.0, 195.0, 194.0), "1000")

    def refuse(*args, **kwargs):
        raise review.ReviewError("refused")

    monkeypatch.setattr(calculated_entries, "insert_decision", refuse)
    with pytest.raises(review.ReviewError, match="refused"):
        convert_eps_from_par(conn, config, doc, eps, par, REVIEWER)
    assert conn.execute("SELECT COUNT(*) FROM input_figures").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM calculations").fetchone()[0] == 0
    with pytest.raises(review.ReviewError, match="reviewer"):  # checked before anything is written
        convert_eps_from_par(conn, config, doc, eps, par, " ")
    assert conn.execute("SELECT COUNT(*) FROM input_figures").fetchone()[0] == 0


def test_eps_cross_check_needs_both_figures_approved(saved):
    conn, _, doc = saved
    assert eps_cross_check(conn, doc) is None
    review.approve(conn, current(conn, doc, "profit_after_tax_etb").id, REVIEWER)
    assert eps_cross_check(conn, doc) is None
    review.approve(conn, current(conn, doc, "weighted_average_shares").id, REVIEWER, acknowledge_warnings=True)
    profit, shares, eps = eps_cross_check(conn, doc)
    assert (profit, shares, round(eps, 2)) == (Decimal(3_869_598_000), Decimal(9_907_298), Decimal("390.58"))
