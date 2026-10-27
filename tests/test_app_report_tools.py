"""OCR stays available for a saved report, in its Report tools menu (D-066)."""

import json
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import src.config_loader
from src import ocr
from src.document_manager import compute_sha256
from src.ui import extract_review
from src.ui.extract_review import picture_pages_summary
from tests.conftest import make_pdf

APP = Path(__file__).resolve().parent.parent / "app.py"
REVIEWER = "Analyst A"


def pdf_with_a_picture_page() -> bytes:
    """Page 1: a text statement; page 2: only a picture (needs OCR)."""
    doc = pymupdf.open(stream=make_pdf([[("Total assets", ["1,234,567", "987,654"])], None]), filetype="pdf")
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), 0)
    pixmap.clear_with(200)
    doc[1].insert_image(pymupdf.Rect(100, 100, 300, 300), pixmap=pixmap)
    data = doc.tobytes()
    doc.close()
    return data


PDF = pdf_with_a_picture_page()


class Upload:
    name = "pictures.pdf"
    file_id = "synthetic-pictures"

    def getvalue(self) -> bytes:
        return PDF


def write_ocr_cache(path: Path, pages: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"engine": "fake", "pages": {str(p): [] for p in pages}}), encoding="utf-8")


@pytest.fixture
def app(monkeypatch, tmp_path):
    real_load = src.config_loader.load_config
    data_dir = tmp_path / "appdata"

    def load_with_temp_data(*args, **kwargs):
        config = real_load(*args, **kwargs)
        return replace(config, settings=replace(config.settings, data_dir=data_dir))

    monkeypatch.setattr(src.config_loader, "load_config", load_with_temp_data)
    monkeypatch.setattr(st, "file_uploader", lambda *a, key=None, **k: Upload() if key == "upload_main" else None)
    engine = ocr.OcrEngine(Path("tesseract.exe"), Path("tessdata"), "tesseract (test)")
    monkeypatch.setattr(extract_review, "_ocr_engine", lambda configured_dir: engine)
    cache = data_dir / "ocr" / f"{compute_sha256(PDF)}.json"
    write_ocr_cache(cache, [1])  # read before saving, as saving requires
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["bank"] = "Awash bank"
    at.session_state["period"] = "2025/26"
    at.session_state["reviewer"] = REVIEWER
    at.run()
    next(b for b in at.button if b.label == "Save report").click().run()
    assert not at.exception and at.session_state["doc"]["document_id"]
    at.ocr_cache = cache
    return at


def test_saved_report_shows_report_tools_with_picture_pages(app):
    assert any("picture page(s): PDF page(s) 2" in m.value and "Read with OCR: 2" in m.value for m in app.markdown)
    assert not any(b.key == "run_ocr_saved" for b in app.button)  # nothing left to read


def test_unread_picture_pages_can_be_read_after_saving(app, monkeypatch):
    app.ocr_cache.unlink()  # e.g. the OCR words were lost: page 2 is unread again
    app.run()
    assert any("Not read yet: 2" in m.value for m in app.markdown)
    calls = []

    def fake_run(pdf_bytes, pages, engine, path, on_page=None):
        calls.append(pages)
        write_ocr_cache(path, pages)
        return {p: [] for p in pages}

    monkeypatch.setattr(ocr, "run_ocr", fake_run)
    app.button(key="run_ocr_saved").click().run()
    assert not app.exception
    assert calls == [[1]]
    assert any("Read with OCR: 2" in m.value for m in app.markdown)


@pytest.mark.parametrize("needed,missing,expected", [
    ([], [], "Every page of this PDF has a text layer"),
    ([0, 3], [3], "PDF page(s) 1, 4. Read with OCR: 1. **Not read yet: 4.**"),
    ([2], [], "Read with OCR: 3."),
])
def test_picture_pages_summary(needed, missing, expected):
    assert expected in picture_pages_summary(needed, missing)
