"""Approve all verified (D-072) and the work left on a saved report (D-077), on the synthetic report."""

from dataclasses import replace
from decimal import Decimal

import pytest

from src import bulk_review
from src import review_service as review
from src.config_loader import load_config
from src.review_service import ReviewError
from tests.test_persistence_and_review import REVIEWER, conn, primary_id, saved  # noqa: F401 (fixtures)


def listed(saved, conn):
    _, document, analysis, ids = saved
    return bulk_review.verified_figures(conn, document.id, analysis, ids)


def test_only_clean_report_year_figures_are_listed(saved, conn):
    ready, left = listed(saved, conn)
    assert [(f.metric_id, f.printed, f.value, f.unit, f.page) for f in ready] == [
        ("total_assets_etb", "1,234,567", Decimal("1234567000"), "ETB", 1)]
    reasons = {x.metric_id: x.reason for x in left}
    assert "sign" in reasons["profit_tax_etb"]  # negative: the reviewer chooses the sign (D-028)
    assert "warning" in reasons["total_equity_etb"]  # 500,000 vs 512,000 conflict
    assert "No figure" in reasons["employees"]


def test_approve_verified_records_each_with_the_reviewer(saved, conn):
    _, document, _, _ = saved
    ready, _ = listed(saved, conn)
    result = bulk_review.approve_verified(conn, document.id, ready, REVIEWER)
    assert result.approved == ["total_assets_etb"] and result.skipped == []
    (decision,) = review.decisions(conn, document.id, "total_assets_etb")
    assert (decision.decision, decision.reviewer, decision.reviewed_value) == ("approved", REVIEWER, Decimal("1234567000"))
    assert not decision.warnings_acknowledged and "Approve all verified" in decision.reason
    for metric_id in ("profit_tax_etb", "total_equity_etb", "employees"):
        assert review.review_state(conn, document.id, metric_id).status == "open"  # left for a person
    assert listed(saved, conn)[0] == []  # reviewed metrics are not listed again


def test_a_metric_reviewed_meanwhile_is_skipped(saved, conn):
    _, document, _, _ = saved
    ready, _ = listed(saved, conn)
    review.reject(conn, primary_id(saved, "total_assets_etb"), REVIEWER, "Wrong column")
    review.reopen(conn, document.id, "total_assets_etb", REVIEWER, "Look again")
    review.approve(conn, primary_id(saved, "total_assets_etb"), "Analyst B")
    result = bulk_review.approve_verified(conn, document.id, ready, REVIEWER)
    assert result.approved == [] and "meanwhile" in result.skipped[0].reason


def test_a_listed_value_that_differs_from_the_saved_one_is_not_approved(saved, conn):
    _, document, _, _ = saved
    (figure,), _ = listed(saved, conn)
    result = bulk_review.approve_verified(conn, document.id, [replace(figure, value=Decimal("1"))], REVIEWER)
    assert result.approved == [] and "not the one listed" in result.skipped[0].reason
    assert review.review_state(conn, document.id, "total_assets_etb").status == "open"


def test_approve_verified_needs_a_reviewer_and_an_open_extraction(saved, conn):
    _, document, _, _ = saved
    ready, _ = listed(saved, conn)
    with pytest.raises(ReviewError, match="reviewer name"):
        bulk_review.approve_verified(conn, document.id, ready, " ")
    review.finish_extraction(conn, document.id, REVIEWER)
    assert listed(saved, conn) == ([], [])


def test_outstanding_work_lists_pending_and_unreviewed_metrics(saved, conn, settings):
    _, document, _, _ = saved
    config = replace(load_config(), settings=settings)
    review.approve(conn, primary_id(saved, "total_assets_etb"), REVIEWER)
    review.correct(conn, primary_id(saved, "total_equity_etb"), REVIEWER, "512000000", "ETB", "Restated figure")
    work = bulk_review.outstanding_work(conn, document.id, config)
    assert work.corrections == ["total_equity_etb"] and work.calculations == []
    assert work.not_reviewed == ["profit_tax_etb", "employees"] and work.count == 3
