"""Learning wording from reviews (D-043) and adding new figures to a saved report (D-044).

Synthetic PDF only. The repository configuration is copied to a temporary folder, so
learned wording is never written to the real config/learned_aliases.yaml.
"""

import shutil
import sqlite3
from dataclasses import replace

import pytest

from src import review_service as review
from src.alias_learning import LearnError, add_learned_alias, label_in_box, suggest_aliases
from src.analysis import analyse
from src.config_loader import CONFIG_DIR, PROJECT_ROOT, LabelMapping, load_config
from src.database import connect
from src.document_manager import store_document
from src.pdf_extractor import EXTRACTOR_VERSION, TABLE_METHOD, RawCandidate, extract_candidates
from src.project_service import analysis_runs, load_candidates, new_figures, save_analysis, saved_candidate_ids
from src.review_service import ReviewError
from tests.conftest import make_pdf

STATEMENT = [
    ("SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION", []),
    ("", ["Notes", "2026", "2025"]),
    ("Birr'000", []),
    ("Total assets", ["", "1,234,567", "987,654"]),
    ("Customer deposit balances", ["21", "800,000", "700,000"]),
]
METRICS = ["total_assets_etb", "total_deposits_etb"]
REVIEWER = "Analyst A"
BANK = "Synthetic Bank"


@pytest.fixture
def config_dir(tmp_path):
    return shutil.copytree(CONFIG_DIR, tmp_path / "config")


@pytest.fixture
def report(settings, config_dir):
    """A saved synthetic report; deposits are not recognised by the repository wording."""
    pdf = make_pdf([STATEMENT])
    config = load_config(config_dir, PROJECT_ROOT)
    extraction = extract_candidates(pdf)
    analysis = analyse(extraction, config, METRICS, "2025/26")
    document = store_document(pdf, "synthetic.pdf", "Synthetic Bank", "2025/26", settings)
    conn = connect(settings.database_path)
    save_analysis(conn, document, analysis, EXTRACTOR_VERSION)
    yield conn, document, config, extraction, analysis
    conn.close()


def deposits_figure(extraction) -> RawCandidate:
    return next(c for c in extraction.candidates if c.raw_label == "Customer deposit balances" and c.raw_value_text == "800,000")


def enter_deposits_from_the_page(conn, document, extraction) -> None:
    """What a reviewer does with "Use this figure for a metric": the figure's own box is the evidence."""
    figure = deposits_figure(extraction)
    review.manual_entry(conn, document.id, "total_deposits_etb", REVIEWER, "800000000", "ETB", 1,
                        "Picked from All Figures on PDF page 1", source_bbox=figure.bbox)
    review.approve_correction(conn, document.id, "total_deposits_etb", REVIEWER)


def table_figure(label: str, bbox: tuple) -> RawCandidate:
    return RawCandidate(0, 0, 0, 1, label, "1,000", label, label, bbox)


# --- finding the label ----------------------------------------------------------------

def test_label_in_box_drops_the_note_reference_and_keeps_the_spelling():
    figures = [table_figure("Total Assets 16 (c)", (300, 100, 340, 110))]
    assert label_in_box(figures, 0, (290, 95, 350, 115)) == "Total Assets"


def test_label_in_box_needs_exactly_one_row():
    figures = [table_figure("Total assets", (300, 100, 340, 110)), table_figure("Total equity", (300, 120, 340, 130))]
    assert label_in_box(figures, 0, (290, 95, 350, 135)) is None  # two different rows
    assert label_in_box(figures, 0, (0, 0, 10, 10)) is None  # nothing in the box
    assert label_in_box(figures, 1, (290, 95, 350, 115)) is None  # other page


# --- suggestions -------------------------------------------------------------------------

def test_value_entered_from_an_unrecognised_row_suggests_its_label(report):
    conn, document, config, extraction, analysis = report
    assert analysis.metric("total_deposits_etb").status == "not_found"
    enter_deposits_from_the_page(conn, document, extraction)
    (suggestion,) = suggest_aliases(review.approved_results(conn, document.id), extraction.candidates, config.mappings)
    assert (suggestion.metric_id, suggestion.label, suggestion.page) == ("total_deposits_etb", "Customer deposit balances", 1)
    assert suggestion.can_add


def test_approved_extracted_figures_suggest_nothing(report):
    conn, document, config, extraction, analysis = report
    assets = next(c for c in load_candidates(conn, document.id, "total_assets_etb") if c.raw_value_text == "1,234,567")
    review.approve(conn, assets.id, REVIEWER)
    assert suggest_aliases(review.approved_results(conn, document.id), extraction.candidates, config.mappings) == []


def test_label_that_means_another_metric_is_not_offered(report):
    conn, document, config, extraction, _ = report
    assets = next(c for c in extraction.candidates if c.raw_label == "Total assets" and c.raw_value_text == "1,234,567")
    review.manual_entry(conn, document.id, "total_deposits_etb", REVIEWER, "1", "ETB", 1, "wrong row", source_bbox=assets.bbox)
    review.approve_correction(conn, document.id, "total_deposits_etb", REVIEWER)
    (suggestion,) = suggest_aliases(review.approved_results(conn, document.id), extraction.candidates, config.mappings)
    assert suggestion.already_used_by == ("total_assets_etb",) and not suggestion.can_add
    with pytest.raises(LearnError, match="ambiguous"):
        add_learned_alias(config.learned_aliases_path, config.mappings_for(BANK), "total_deposits_etb", "Total assets",
                          "x", REVIEWER, BANK)


# --- learning ------------------------------------------------------------------------------

def learn_deposits(config, bank: str = BANK) -> None:
    add_learned_alias(config.learned_aliases_path, config.mappings_for(bank), "total_deposits_etb",
                      "Customer deposit balances", f"{bank} 2025/26, PDF page 1", REVIEWER, bank)


def test_learned_label_is_saved_with_bank_who_and_where(report, config_dir):
    conn, document, config, extraction, _ = report
    learn_deposits(config)
    reloaded = load_config(config_dir, PROJECT_ROOT)
    # The copied config may already hold labels learned for other banks.
    (learned,) = [a for a in reloaded.learned_aliases if a.bank == BANK]
    assert (learned.bank, learned.metric_id, learned.alias, learned.seen_in, learned.added_by) == (
        BANK, "total_deposits_etb", "Customer deposit balances", "Synthetic Bank 2025/26, PDF page 1", REVIEWER)
    assert config.learned_aliases_path.read_text(encoding="utf-8").startswith("# Table-row labels learned")
    with pytest.raises(LearnError, match="already linked"):
        add_learned_alias(reloaded.learned_aliases_path, reloaded.mappings_for(BANK), "total_deposits_etb",
                          "customer  DEPOSIT balances", "again", REVIEWER, BANK)
    with pytest.raises(LearnError, match="reviewer"):
        add_learned_alias(reloaded.learned_aliases_path, reloaded.mappings_for(BANK), "total_deposits_etb", "Other",
                          "x", " ", BANK)
    with pytest.raises(LearnError, match="Choose the bank"):
        add_learned_alias(reloaded.learned_aliases_path, reloaded.mappings, "total_deposits_etb", "Other", "x",
                          REVIEWER, " ")


def test_learned_label_is_used_for_the_same_bank_only(report, config_dir):
    _, _, config, extraction, _ = report
    learn_deposits(config)
    reloaded = load_config(config_dir, PROJECT_ROOT)
    same_bank = analyse(extraction, reloaded, METRICS, "2025/26", BANK).metric("total_deposits_etb")
    other_bank = analyse(extraction, reloaded, METRICS, "2025/26", "Other Bank").metric("total_deposits_etb")
    assert same_bank.primary.mapped.candidate.raw_value_text == "800,000"
    assert other_bank.status == "not_found"
    # The other bank can learn the same label for a different metric of its own.
    add_learned_alias(reloaded.learned_aliases_path, reloaded.mappings_for("Other Bank"), "loans_and_advances_etb",
                      "Customer deposit balances", "Other Bank 2025/26, PDF page 1", REVIEWER, "Other Bank")


# --- adding new figures to the saved report ------------------------------------------------

def test_learned_label_finds_the_figure_and_it_can_be_added(report, config_dir):
    conn, document, config, extraction, _ = report
    enter_deposits_from_the_page(conn, document, extraction)
    approved_before = review.review_state(conn, document.id, "total_deposits_etb")
    saved_before = load_candidates(conn, document.id)
    learn_deposits(config)

    reloaded = load_config(config_dir, PROJECT_ROOT)
    analysis = analyse(extraction, reloaded, METRICS, "2025/26", BANK)
    assert analysis.metric("total_deposits_etb").primary.mapped.candidate.raw_value_text == "800,000"
    assert suggest_aliases(review.approved_results(conn, document.id), extraction.candidates,
                           reloaded.mappings_for(BANK)) == []
    assert len(new_figures(conn, document.id, analysis).candidates) == 2  # 2026 and 2025 columns

    added, _ = review.add_new_figures(conn, document.id, analysis, EXTRACTOR_VERSION, REVIEWER)
    assert added == 2 and not new_figures(conn, document.id, analysis)
    (run,) = analysis_runs(conn, document.id)
    assert (run["candidates_added"], run["reviewer"]) == (2, REVIEWER)
    run_ids = dict(conn.execute("SELECT raw_value_text, analysis_run_id FROM candidates WHERE metric_id = ?",
                                ("total_deposits_etb",)).fetchall())
    assert run_ids == {"800,000": run["id"], "700,000": run["id"]}
    # Nothing saved earlier changed: earlier candidates and the approved manual value stay.
    assert all(c in load_candidates(conn, document.id) for c in saved_before)
    after = review.review_state(conn, document.id, "total_deposits_etb")
    assert (after.status, after.accepted_value) == ("approved", approved_before.accepted_value)
    with pytest.raises(ReviewError, match="no new figures"):
        review.add_new_figures(conn, document.id, analysis, EXTRACTOR_VERSION, REVIEWER)


def with_deposits_wording(config):
    return replace(config, mappings=config.mappings + (LabelMapping("total_deposits_etb", aliases=("Customer deposit balances",)),))


def test_added_figure_can_then_be_approved(report):
    conn, document, config, extraction, _ = report
    analysis = analyse(extraction, with_deposits_wording(config), METRICS, "2025/26")
    review.add_new_figures(conn, document.id, analysis, EXTRACTOR_VERSION, REVIEWER)
    deposits = next(c for c in load_candidates(conn, document.id, "total_deposits_etb") if c.raw_value_text == "800,000")
    review.approve(conn, deposits.id, REVIEWER)
    assert review.review_state(conn, document.id, "total_deposits_etb").accepted_value == 800_000_000


def test_figures_cannot_be_added_after_finishing_or_with_another_extractor(report):
    conn, document, config, extraction, _ = report
    analysis = analyse(extraction, with_deposits_wording(config), METRICS, "2025/26")
    with pytest.raises(ReviewError, match="saved with"):
        review.add_new_figures(conn, document.id, analysis, "pymupdf_extractor/9.9", REVIEWER)
    review.finish_extraction(conn, document.id, REVIEWER)
    with pytest.raises(ReviewError, match="finished"):
        review.add_new_figures(conn, document.id, analysis, EXTRACTOR_VERSION, REVIEWER)
    assert analysis_runs(conn, document.id) == []


def test_figures_can_be_added_after_reading_picture_pages_with_ocr(report):
    """Reading picture pages with OCR after saving (Report tools, D-066) adds ' + OCR (...)' to the
    version; the text reader is the same, so the new figures may still be added."""
    conn, document, config, extraction, _ = report
    analysis = analyse(extraction, with_deposits_wording(config), METRICS, "2025/26")
    added, _ = review.add_new_figures(conn, document.id, analysis, f"{EXTRACTOR_VERSION} + OCR (tesseract v5.5.3)",
                                      REVIEWER)
    assert added == 2
    assert review.same_text_reader(f"{EXTRACTOR_VERSION} + OCR (tesseract v5.4)", EXTRACTOR_VERSION)
    assert not review.same_text_reader("pymupdf_extractor/0.2 (PyMuPDF 1.28.2) + OCR (tesseract v5.5.3)",
                                       f"{EXTRACTOR_VERSION} + OCR (tesseract v5.5.3)")


def test_a_saved_key_pointing_at_another_number_is_not_approvable(report):
    """A newer extractor may number a page's rows differently (column blocks, D-060). A saved
    figure whose key now names another printed number must not be approved in its place."""
    conn, document, _, _, analysis = report
    saved = load_candidates(conn, document.id)
    assert saved_candidate_ids(saved, analysis) == {(c.metric_id, c.key): c.id for c in saved}
    moved = [replace(c, raw_value_text="9,999") if c.metric_id == "total_assets_etb" else c for c in saved]
    ids = saved_candidate_ids(moved, analysis)
    assert not any(metric_id == "total_assets_etb" for metric_id, _ in ids)
    assert len(ids) == len([c for c in saved if c.metric_id != "total_assets_etb"])


def test_analysis_runs_cannot_be_edited(report):
    conn, document, _, _, _ = report
    conn.execute("""INSERT INTO analysis_runs (document_id, extractor_version, candidates_added, findings_added,
                    reviewer, created_at) VALUES (?, 'v', 0, 0, 'r', 't')""", (document.id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE analysis_runs SET reviewer = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM analysis_runs")
