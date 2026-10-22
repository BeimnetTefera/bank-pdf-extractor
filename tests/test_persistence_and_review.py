"""Phase 4: saving to SQLite, restart persistence, review decisions and approved-only results."""

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from src import review_service as review
from src.analysis import analyse
from src.config_loader import LabelMapping, load_config
from src.database import connect
from src.document_manager import store_document
from src.pdf_extractor import EXTRACTOR_VERSION, extract_candidates
from src.project_service import find_documents_by_hash, list_documents, load_candidates, save_analysis
from src.review_service import ReviewError
from tests.conftest import make_pdf

STATEMENT = [
    ("SYNTHETIC BANK S.C. STATEMENT OF PROFIT OR LOSS AND FINANCIAL POSITION", []),
    ("", ["Notes", "2026", "2025"]),
    ("Birr'000", []),
    ("Total assets", ["", "1,234,567", "987,654"]),
    ("Income tax expense", ["15", "(4,313,629)", "(2,802,590)"]),
    ("Total equity", ["", "500,000", "-"]),
]
NOTES = [
    ("NOTES TO THE FINANCIAL STATEMENTS", []),
    ("", ["Notes", "2026", "2025"]),
    ("Birr'000", []),
    ("Total equity", ["", "512,000", "400,000"]),
]
MAPPINGS = (
    LabelMapping("total_assets_etb", aliases=("Total assets",)),
    LabelMapping("profit_tax_etb", aliases=("Income tax expense",)),
    LabelMapping("total_equity_etb", aliases=("Total equity",)),
)
METRICS = ["total_assets_etb", "profit_tax_etb", "total_equity_etb", "employees"]
REVIEWER = "Analyst A"


@pytest.fixture
def saved(settings):
    """A synthetic report saved to a database file; returns (db path, document id, candidate ids)."""
    pdf = make_pdf([STATEMENT, NOTES])
    config = replace(load_config(), mappings=MAPPINGS)
    document = store_document(pdf, "synthetic.pdf", "Synthetic Bank", "2025/26", settings)
    analysis = analyse(extract_candidates(pdf), config, METRICS, "2025/26")
    conn = connect(settings.database_path)
    ids = save_analysis(conn, document, analysis, EXTRACTOR_VERSION)
    conn.close()
    return settings.database_path, document, analysis, ids


@pytest.fixture
def conn(saved):
    connection = connect(saved[0])
    yield connection
    connection.close()


def primary_id(saved, metric_id: str) -> int:
    _, _, analysis, ids = saved
    return ids[(metric_id, analysis.metric(metric_id).primary.mapped.key)]


def candidate_id_for(saved, metric_id: str, raw_value: str) -> int:
    _, _, analysis, ids = saved
    mc = next(m for m in analysis.metric(metric_id).candidates if m.mapped.candidate.raw_value_text == raw_value)
    return ids[(metric_id, mc.mapped.key)]


# --- persistence ---------------------------------------------------------------------

def test_saved_report_survives_restart(saved):
    db_path, document, _, _ = saved
    conn = connect(db_path)  # a fresh connection, as after restarting the app
    (row,) = list_documents(conn)
    assert (row["bank_name"], row["reporting_period"], row["sha256"]) == ("Synthetic Bank", "2025/26", document.sha256)
    assert row["extractor_version"] == EXTRACTOR_VERSION
    values = {c.raw_value_text for c in load_candidates(conn, document.id, "total_assets_etb")}
    assert {"1,234,567", "987,654"} <= values
    conn.close()


def test_same_pdf_is_detected_as_already_saved(saved, conn):
    _, document, _, _ = saved
    assert [r["id"] for r in find_documents_by_hash(conn, document.sha256)] == [document.id]
    assert find_documents_by_hash(conn, "0" * 64) == []


def test_decisions_survive_restart(saved):
    db_path, document, _, _ = saved
    conn = connect(db_path)
    review.approve(conn, primary_id(saved, "total_assets_etb"), REVIEWER)
    conn.close()
    conn = connect(db_path)
    state = review.review_state(conn, document.id, "total_assets_etb")
    assert state.status == "approved" and state.accepted_value == Decimal("1234567000")
    conn.close()


# --- approve -------------------------------------------------------------------------

def test_approve_records_reviewer_time_and_value(saved, conn):
    _, document, _, _ = saved
    review.approve(conn, primary_id(saved, "total_assets_etb"), REVIEWER)
    (decision,) = review.decisions(conn, document.id, "total_assets_etb")
    assert decision.decision == "approved" and decision.reviewer == REVIEWER
    assert decision.created_at.endswith("+00:00")
    assert (decision.reviewed_value, decision.reviewed_unit, decision.sign_choice) == (Decimal("1234567000"), "ETB", "as_printed")


def test_reviewer_chooses_the_sign(saved, conn):
    _, document, _, _ = saved
    tax = primary_id(saved, "profit_tax_etb")
    review.approve(conn, tax, REVIEWER, sign="positive")
    assert review.review_state(conn, document.id, "profit_tax_etb").accepted_value == Decimal("4313629000")
    review.approve(conn, tax, REVIEWER, sign="as_printed")
    assert review.review_state(conn, document.id, "profit_tax_etb").accepted_value == Decimal("-4313629000")
    with pytest.raises(ReviewError, match="Sign choice"):
        review.approve(conn, tax, REVIEWER, sign="flip")


def test_approve_requires_a_reviewer(saved, conn):
    with pytest.raises(ReviewError, match="reviewer name"):
        review.approve(conn, primary_id(saved, "total_assets_etb"), "  ")


def test_warnings_must_be_acknowledged(saved, conn):
    _, document, analysis, _ = saved
    assert analysis.metric("total_equity_etb").status == "flagged"  # 500,000 vs 512,000
    equity = primary_id(saved, "total_equity_etb")
    with pytest.raises(ReviewError, match="warning"):
        review.approve(conn, equity, REVIEWER)
    review.approve(conn, equity, REVIEWER, acknowledge_warnings=True)
    (decision,) = review.decisions(conn, document.id, "total_equity_etb")
    assert decision.warnings_acknowledged


def test_unconverted_value_cannot_be_approved(saved, conn):
    dash = candidate_id_for(saved, "total_equity_etb", "-")
    with pytest.raises(ReviewError, match="Use Correct"):
        review.approve(conn, dash, REVIEWER, acknowledge_warnings=True)


# --- reject / reopen -------------------------------------------------------------------

def test_reject_requires_a_reason_and_keeps_the_candidate(saved, conn):
    _, document, _, _ = saved
    assets = primary_id(saved, "total_assets_etb")
    with pytest.raises(ReviewError, match="reason"):
        review.reject(conn, assets, REVIEWER, "")
    review.reject(conn, assets, REVIEWER, "Wrong column")
    assert assets in review.review_state(conn, document.id, "total_assets_etb").rejected_candidate_ids
    assert any(c.id == assets for c in load_candidates(conn, document.id, "total_assets_etb"))
    with pytest.raises(ReviewError, match="rejected"):
        review.approve(conn, assets, REVIEWER)


def test_rejecting_the_approved_candidate_reopens_the_metric(saved, conn):
    _, document, _, _ = saved
    assets = primary_id(saved, "total_assets_etb")
    review.approve(conn, assets, REVIEWER)
    review.reject(conn, assets, REVIEWER, "Approved by mistake")
    assert review.review_state(conn, document.id, "total_assets_etb").status == "open"
    assert review.approved_results(conn, document.id) == []


def test_reopen_requires_reason_and_withdraws_approval(saved, conn):
    _, document, _, _ = saved
    review.approve(conn, primary_id(saved, "total_assets_etb"), REVIEWER)
    with pytest.raises(ReviewError, match="reason"):
        review.reopen(conn, document.id, "total_assets_etb", REVIEWER, " ")
    review.reopen(conn, document.id, "total_assets_etb", REVIEWER, "Recheck units")
    assert review.review_state(conn, document.id, "total_assets_etb").status == "open"
    assert len(review.decisions(conn, document.id, "total_assets_etb")) == 2  # history kept


# --- correct / manual entry -------------------------------------------------------------

def test_correction_keeps_the_original_and_needs_approval(saved, conn):
    _, document, _, _ = saved
    assets = primary_id(saved, "total_assets_etb")
    before = next(c for c in load_candidates(conn, document.id, "total_assets_etb") if c.id == assets)

    with pytest.raises(ReviewError, match="reason"):
        review.correct(conn, assets, REVIEWER, "1234568000", "ETB", "")
    with pytest.raises(ReviewError, match="not a number"):
        review.correct(conn, assets, REVIEWER, "about 1.2bn", "ETB", "typo")
    with pytest.raises(ReviewError, match="Unit"):
        review.correct(conn, assets, REVIEWER, "1", "USD", "typo")

    review.correct(conn, assets, REVIEWER, "1,234,568,000", "ETB", "Last digit misread")
    state = review.review_state(conn, document.id, "total_assets_etb")
    assert state.status == "corrected" and state.pending.reviewed_value == Decimal("1234568000")
    assert review.approved_results(conn, document.id) == []  # not until the correction is approved

    review.approve_correction(conn, document.id, "total_assets_etb", REVIEWER)
    (result,) = review.approved_results(conn, document.id)
    assert result.value == Decimal("1234568000") and result.human_entered
    after = next(c for c in load_candidates(conn, document.id, "total_assets_etb") if c.id == assets)
    assert after == before  # the extracted candidate is unchanged


def test_manual_entry_for_a_metric_not_found(saved, conn):
    _, document, analysis, _ = saved
    assert analysis.metric("employees").status == "not_found"
    with pytest.raises(ReviewError, match="PDF page"):
        review.manual_entry(conn, document.id, "employees", REVIEWER, "10627", "count", 99, "From HR section")
    review.manual_entry(conn, document.id, "employees", REVIEWER, "10627", "count", 2, "Read from page 2")
    review.approve_correction(conn, document.id, "employees", REVIEWER)
    (result,) = review.approved_results(conn, document.id)
    assert (result.metric_id, result.value, result.unit, result.source_page) == ("employees", Decimal(10627), "count", 2)
    assert result.candidate is None and result.human_entered


def test_approve_correction_needs_a_pending_correction(saved, conn):
    _, document, _, _ = saved
    with pytest.raises(ReviewError, match="no correction"):
        review.approve_correction(conn, document.id, "total_assets_etb", REVIEWER)


# --- audit guarantees --------------------------------------------------------------------

def test_decisions_cannot_be_edited_or_deleted(saved, conn):
    review.approve(conn, primary_id(saved, "total_assets_etb"), REVIEWER)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE review_decisions SET reviewed_value = '1'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM review_decisions")


def test_candidates_cannot_be_changed(conn):
    with pytest.raises(sqlite3.DatabaseError, match="cannot be changed"):
        conn.execute("UPDATE candidates SET raw_value_text = '999'")


def test_only_approved_metrics_reach_approved_results(saved, conn):
    _, document, _, _ = saved
    review.approve(conn, primary_id(saved, "total_assets_etb"), REVIEWER)  # approved
    review.reject(conn, primary_id(saved, "profit_tax_etb"), REVIEWER, "Wrong row")  # rejected
    review.correct(conn, primary_id(saved, "total_equity_etb"), REVIEWER, "510000000", "ETB", "Check")  # pending
    # employees: never reviewed
    results = review.approved_results(conn, document.id)
    assert [r.metric_id for r in results] == ["total_assets_etb"]
    assert results[0].source_page == 1 and results[0].candidate.raw_value_text == "1,234,567"
