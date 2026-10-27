"""A box the reviewer draws around the right number, saved as the evidence (synthetic PDFs)."""

from dataclasses import replace

import pytest

from src import page_marks
from src import review_service as review
from src.config_loader import load_config
from src.excel_exporter import build_workbook
from src.pdf_extractor import extract_candidates
from src.review_service import ReviewError
from tests.conftest import make_pdf
from tests.test_excel_exporter import evidence_rows
from tests.test_ocr import FAKE_WORDS, STATEMENT, photographed
from tests.test_persistence_and_review import REVIEWER, conn, primary_id, saved  # noqa: F401 (fixtures)

# make_pdf writes the 2026 values at x=300 and the 2025 values at x=400, row 4 at y=160.
TOTAL_ASSETS_2026 = (295, 150, 360, 165)
BOTH_YEARS = (295, 150, 460, 165)


def test_mark_reads_the_number_and_the_page_unit():
    pdf = make_pdf([STATEMENT])
    page = extract_candidates(pdf).pages[0]
    mark = page_marks.read_mark(pdf, page, TOTAL_ASSETS_2026)
    assert (mark.page_index, mark.printed_value) == (0, "1,234,567")
    suggestion = page_marks.suggested_value(mark, "ETB")
    assert suggestion.value == 1_234_567_000  # Birr'000 heading on the page


def test_marked_share_count_uses_the_metrics_count_in_thousands_setting():
    """D-069: 12,538 under a Birr'000 heading is 12,538,000 shares for a metric that says so."""
    from decimal import Decimal

    from tests.test_units_signs_eps import THOUSANDS

    mark = page_marks.Mark(0, TOTAL_ASSETS_2026, "12,538", "12,538", THOUSANDS)
    assert page_marks.suggested_value(mark, "count").value == Decimal(12_538)
    in_thousands = page_marks.suggested_value(mark, "count", Decimal(1_000_000))
    assert in_thousands.value == Decimal(12_538_000)
    assert page_marks.suggested_value(replace(mark, printed_value="9,907,298"), "count",
                                      Decimal(1_000_000)).value == Decimal(9_907_298)


def test_mark_around_two_numbers_suggests_nothing():
    pdf = make_pdf([STATEMENT])
    mark = page_marks.read_mark(pdf, extract_candidates(pdf).pages[0], BOTH_YEARS)
    assert mark.printed_value is None and "1,234,567" in mark.text and "987,654" in mark.text
    assert page_marks.suggested_value(mark, "ETB") is None


def test_mark_on_a_picture_page_uses_the_ocr_words():
    pdf = photographed(make_pdf([STATEMENT]))
    page = extract_candidates(pdf, FAKE_WORDS).pages[0]
    mark = page_marks.read_mark(pdf, page, (295, 150, 350, 165), FAKE_WORDS)
    assert mark.printed_value == "1,234,567"


# --- the box is saved and exported as the evidence ------------------------------------------

def test_correction_with_a_marked_box_becomes_the_evidence(saved, conn, settings):
    _, document, _, _ = saved
    assets = primary_id(saved, "total_assets_etb")
    with pytest.raises(ReviewError, match="page and its position"):
        review.correct(conn, assets, REVIEWER, "1234568000", "ETB", "Wrong row", source_page=1)
    review.correct(conn, assets, REVIEWER, "1234568000", "ETB", "Wrong row picked",
                   source_page=1, source_bbox=(295.0, 150.0, 360.0, 165.0))
    review.approve_correction(conn, document.id, "total_assets_etb", "Analyst B")

    (result,) = review.approved_results(conn, document.id)
    assert result.box_marked_by_reviewer and result.evidence_box == (295.0, 150.0, 360.0, 165.0)
    config = replace(load_config(), settings=settings)
    (row,) = evidence_rows(build_workbook(conn, config, [document.id], exported_by=REVIEWER))
    assert (row["box_on_page"], row["box_marked_by_reviewer"], row["pdf_page"]) == (
        "295.0, 150.0, 360.0, 165.0", "yes", 1)


def test_approved_extracted_figure_keeps_its_own_position(saved, conn):
    _, document, _, _ = saved
    review.approve(conn, primary_id(saved, "total_assets_etb"), REVIEWER)
    (result,) = review.approved_results(conn, document.id)
    assert not result.box_marked_by_reviewer
    assert result.evidence_box == result.candidate.bbox


def test_manual_entry_saves_its_box(saved, conn):
    _, document, _, _ = saved
    review.manual_entry(conn, document.id, "employees", REVIEWER, "10627", "count", 2, "HR section",
                        source_bbox=(100.0, 200.0, 140.0, 212.0))
    review.approve_correction(conn, document.id, "employees", REVIEWER)
    (result,) = review.approved_results(conn, document.id)
    assert (result.source_page, result.evidence_box) == (2, (100.0, 200.0, 140.0, 212.0))
