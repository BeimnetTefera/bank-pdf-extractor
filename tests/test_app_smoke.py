"""Smoke tests: the Streamlit app renders without errors (synthetic PDF only)."""

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from src.pdf_extractor import TABLE_METHOD
from tests.conftest import make_pdf

APP = Path(__file__).resolve().parent.parent / "app.py"
PDF = make_pdf(
    [
        [
            ("Synthetic Bank S.C. statement of financial position", []),
            ("", ["Notes", "2026", "2025"]),
            ("Birr'000", []),
            ("Total assets", ["", "1,234,567", "987,654"]),
        ],
        [("Notes to the financial statements", []), ("Total assets", ["", "1,234,567", "987,000"])],
    ]
)


class FakeUpload:
    name = "synthetic.pdf"
    file_id = "synthetic-1"

    def getvalue(self) -> bytes:
        return PDF


def loaded_app(monkeypatch) -> AppTest:
    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: FakeUpload())
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["period"] = "2025/26"
    return at.run()


def test_app_renders_without_a_pdf():
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert any("Upload an annual report" in i.value for i in at.info)


def test_metric_table_lists_configured_metrics(monkeypatch):
    at = loaded_app(monkeypatch)
    assert not at.exception
    metrics, figures = at.dataframe[0].value, at.dataframe[1].value
    assert list(metrics.columns) == ["Metric", "Value", "Unit", "Status"]
    row = metrics[metrics["Metric"] == "Total assets"].iloc[0]
    assert (row["Value"], row["Unit"]) == ("1,234,567", "ETB '000")
    assert metrics[metrics["Metric"] == "Branches"].iloc[0]["Status"].endswith("Not found")
    assert list(figures.columns) == ["Figure", "Value", "Page", "Type"]


def test_metric_details_show_source_and_conversion(monkeypatch):
    at = loaded_app(monkeypatch)
    at.session_state["detail_mode"] = "metric"
    at.session_state["selected_metric"] = "total_assets_etb"
    at.session_state["selected_key"] = (TABLE_METHOD, 0, 3, 0)
    at.run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "1,234,567" in text and "1,234,567,000 ETB" in text
    assert "PDF page 1" in text and "this year" in text


def test_raw_figure_details_still_available(monkeypatch):
    at = loaded_app(monkeypatch)
    at.session_state["detail_mode"] = "figure"
    at.session_state["selected_key"] = (TABLE_METHOD, 0, 3, 1)  # page 1, Total assets prior year
    at.session_state["viewer_page"] = 1
    at.run()
    assert not at.exception
    markdown = " ".join(m.value for m in at.markdown)
    assert "987,654" in markdown
    assert "987,000" in markdown and "different value" in markdown  # same label, page 2
