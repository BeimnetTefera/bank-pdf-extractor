"""Smoke tests: the Streamlit app renders without errors (synthetic PDF only)."""

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from src.pdf_extractor import TABLE_METHOD
from tests.conftest import make_pdf

APP = Path(__file__).resolve().parent.parent / "app.py"
PDF = make_pdf(
    [
        [("Synthetic Bank S.C. statement of financial position", []), ("Total assets", ["1,234,567", "987,654"])],
        [("Notes to the financial statements", []), ("Total assets", ["1,234,567", "987,000"])],
    ]
)


class FakeUpload:
    name = "synthetic.pdf"
    file_id = "synthetic-1"

    def getvalue(self) -> bytes:
        return PDF


def test_app_renders_without_a_pdf():
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert any("Upload an annual report" in i.value for i in at.info)


def test_app_shows_figures_and_details_for_an_uploaded_pdf(monkeypatch):
    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: FakeUpload())
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    table = at.dataframe[0].value
    assert list(table.columns) == ["Figure", "Value", "Page", "Type"]
    assert "Total assets" in set(table["Figure"])

    at.session_state["selected_key"] = (TABLE_METHOD, 0, 1, 1)  # page 1, "Total assets" prior year
    at.session_state["viewer_page"] = 1
    at.run()
    assert not at.exception
    markdown = " ".join(m.value for m in at.markdown)
    assert "987,654" in markdown
    assert "987,000" in markdown and "different value" in markdown  # same label, page 2
