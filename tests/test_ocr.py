"""OCR for picture pages: synthetic PDFs only (a text page photographed into an image)."""

import sqlite3
from dataclasses import replace

import pymupdf
import pytest

from src import ocr
from src.analysis import analyse
from src.config_loader import LabelMapping, load_config
from src.database import connect
from src.document_manager import store_document
from src.pdf_extractor import extract_candidates
from src.project_service import load_candidates, save_analysis
from tests.conftest import make_pdf

STATEMENT = [
    ("SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION", []),
    ("", ["2026", "2025"]),
    ("Birr'000", []),
    ("Total assets", ["1,234,567", "987,654"]),
]
# What OCR returns for that page: [x0, y0, x1, y1, text, block, line, word] in page points.
FAKE_WORDS = {0: [
    [300, 112, 330, 122, "2026", 1, 0, 0], [400, 112, 430, 122, "2025", 1, 0, 1],
    [50, 132, 90, 142, "Birr'000", 2, 0, 0],
    [50, 152, 75, 162, "Total", 3, 0, 0], [78, 152, 110, 162, "assets", 3, 0, 1],
    [300, 152, 345, 162, "1,234,567", 4, 0, 0], [400, 152, 440, 162, "987,654", 4, 0, 1],
]}


def photographed(pdf: bytes, dpi: int = 200) -> bytes:
    """The same pages as pictures only, like a scanned or photographed report."""
    with pymupdf.open(stream=pdf, filetype="pdf") as text_doc, pymupdf.open() as image_doc:
        for page in text_doc:
            picture = image_doc.new_page(width=page.rect.width, height=page.rect.height)
            picture.insert_image(picture.rect, pixmap=page.get_pixmap(dpi=dpi))
        return image_doc.tobytes()


@pytest.fixture
def config(settings):
    return replace(load_config(), settings=settings,
                   mappings=(LabelMapping("total_assets_etb", aliases=("Total assets",)),))


def test_only_picture_pages_need_ocr():
    mixed = make_pdf([STATEMENT])  # page 0 has text
    with pymupdf.open(stream=photographed(make_pdf([STATEMENT])), filetype="pdf") as pictures, \
            pymupdf.open(stream=mixed, filetype="pdf") as doc:
        doc.insert_pdf(pictures)  # page 1 is a picture
        assert ocr.pages_needing_ocr(doc.tobytes()) == [1]
    assert ocr.pages_needing_ocr(make_pdf([None])) == []  # empty page, no image: nothing to read


def test_supplied_ocr_words_are_extracted_and_marked():
    result = extract_candidates(photographed(make_pdf([STATEMENT])), FAKE_WORDS, "tesseract 5.5.3")
    assert result.ocr_pages == [0] and result.pages[0].unit_headings == ("Birr'000",)
    (current, _) = [c for c in result.candidates if c.raw_label == "Total assets"]
    assert (current.raw_value_text, current.ocr) == ("1,234,567", True)
    assert result.extractor_version.endswith("+ OCR (tesseract 5.5.3)")
    assert "read with OCR" in result.messages[0]


def test_tall_ocr_boxes_do_not_merge_neighbouring_rows():
    # As Tesseract returns them: right top edges, but boxes about two lines tall.
    words = {0: [
        [50, 100, 90, 111, "Cash", 0, 0, 0], [300, 100, 345, 122, "1,000", 1, 0, 0],
        [50, 113, 90, 135, "Loans", 2, 0, 0], [300, 113, 345, 135, "2,000", 3, 0, 0],
        [50, 126, 90, 137, "Deposits", 4, 0, 0], [300, 126, 345, 148, "3,000", 5, 0, 0],
    ]}
    result = extract_candidates(photographed(make_pdf([STATEMENT])), words)
    rows = {c.raw_label: c.raw_value_text for c in result.candidates}
    assert rows == {"Cash": "1,000", "Loans": "2,000", "Deposits": "3,000"}


def test_ocr_words_are_ignored_on_pages_with_text():
    result = extract_candidates(make_pdf([STATEMENT]), FAKE_WORDS)
    assert result.ocr_pages == [] and not any(c.ocr for c in result.candidates)


def test_ocr_figures_carry_a_warning_and_are_saved_as_ocr(config):
    pdf = photographed(make_pdf([STATEMENT]))
    analysis = analyse(extract_candidates(pdf, FAKE_WORDS), config, ["total_assets_etb"], "2025/26")
    result = analysis.metric("total_assets_etb")
    assert result.primary.normalized.value == 1_234_567_000
    assert any(f.rule_id == "OCR" and f.severity == "warning" for f in result.findings)
    assert result.status == "flagged"  # approval needs the warning acknowledged

    document = store_document(pdf, "scan.pdf", "Synthetic Bank", "2025/26", config.settings)
    conn = connect(config.settings.database_path)
    save_analysis(conn, document, analysis, "test")
    assert all(c.read_by_ocr for c in load_candidates(conn, document.id))
    conn.close()


def test_old_database_gets_the_ocr_column(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, document_id TEXT)")
    old.execute("INSERT INTO candidates (document_id) VALUES ('d1')")
    old.commit()
    old.close()
    conn = connect(path)
    (row,) = conn.execute("SELECT * FROM candidates").fetchall()
    assert (row["document_id"], row["read_by_ocr"]) == ("d1", 0)  # existing rows kept
    conn.close()


def test_cache_is_reused_and_unreadable_cache_is_ignored(tmp_path):
    path = tmp_path / "ocr" / "x.json"
    assert ocr.load_cache(path) == {}
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")
    assert ocr.load_cache(path) == {}


@pytest.mark.skipif(ocr.find_engine() is None, reason="Tesseract OCR is not installed")
def test_tesseract_reads_a_photographed_statement(tmp_path, monkeypatch):
    pdf = photographed(make_pdf([STATEMENT]))
    path = tmp_path / "ocr.json"
    progress = []
    words = ocr.run_ocr(pdf, [0], ocr.find_engine(), path, on_page=lambda done, total: progress.append(done))
    assert progress == [1] and path.is_file()
    result = extract_candidates(pdf, words)
    assert "1,234,567" in [c.raw_value_text for c in result.candidates if c.raw_label == "Total assets"]

    # A second run reads the saved words instead of running OCR again.
    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", lambda *a, **k: pytest.fail("OCR ran twice"))
    assert ocr.run_ocr(pdf, [0], ocr.find_engine(), path) == words
