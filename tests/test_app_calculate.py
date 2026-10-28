"""Calculate in Metric Details, input figures and the calculation's evidence, through the app (D-052)."""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import src.config_loader
from src import review_service
from src.database import connect
from src.page_marks import Mark
from src.project_service import load_candidates
from src.unit_normalizer import UnitEvidence
from tests.conftest import html_text, make_pdf
from tests.test_app_evidence import screen

APP = Path(__file__).resolve().parent.parent / "app.py"
PDF = make_pdf([[
    ("SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION AND PROFIT OR LOSS", []),
    ("", ["Notes", "2023", "2022"]),
    ("Birr'000", []),
    ("Total equity", ["", "19,475,296", "14,216,185"]),
    ("Profit after tax", ["", "3,872,832", "3,235,824"]),
]])
REVIEWER = "Analyst A"


class Upload:
    name = "synthetic.pdf"
    file_id = "synthetic-calc"

    def getvalue(self) -> bytes:
        return PDF


@pytest.fixture
def app(monkeypatch, tmp_path):
    real_load = src.config_loader.load_config

    def load_with_temp_data(*args, **kwargs):
        config = real_load(*args, **kwargs)
        return replace(config, settings=replace(config.settings, data_dir=tmp_path / "appdata"))

    monkeypatch.setattr(src.config_loader, "load_config", load_with_temp_data)
    monkeypatch.setattr(st, "file_uploader", lambda *a, key=None, **k: Upload() if key == "upload_main" else None)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["bank"] = "Awash bank"
    at.session_state["period"] = "2022/23"
    at.session_state["reviewer"] = REVIEWER
    at.run()
    next(b for b in at.button if b.label == "Save report").click().run()
    assert not at.exception
    at.db_path = tmp_path / "appdata" / "app.db"
    at.document_id = at.session_state["doc"]["document_id"]
    with closing(connect(at.db_path)) as conn:
        for metric_id in ("profit_after_tax_etb", "total_equity_etb"):
            c = next(c for c in load_candidates(conn, at.document_id, metric_id) if c.period == "current")
            review_service.approve(conn, c.id, REVIEWER)
    return at


def open_metric(at: AppTest, metric_id: str) -> None:
    at.session_state["detail_mode"] = "metric"
    at.session_state["selected_metric"] = metric_id
    at.session_state["selected_key"] = None
    at.run()
    assert not at.exception


def test_calculate_roe_approve_and_see_its_evidence(app):
    open_metric(app, "roe_percent")
    assert any("profit_after_tax_etb / avg(total_equity_etb, total_equity_etb_prev)" in c.value for c in app.code)
    assert any(c.value.startswith("3,872,832,000 / avg(19,475,296,000, 14,216,185,000) = 0.2298") for c in app.code)
    text = " ".join(m.value for m in app.markdown).replace("\\", "")  # labels are escaped for Markdown
    assert "Total equity (previous year)" in text and "2022 column" in text  # where the prior-year figure is
    next(t for t in app.text_area if t.key == "calc_note_roe_percent").input("ROE is not printed").run()
    next(b for b in app.button if b.label == "Save calculation").click().run()
    assert not app.exception
    next(b for b in app.button if b.label == "Approve calculation").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        state = review_service.review_state(conn, app.document_id, "roe_percent")
    assert (state.status, state.accepted_value) == ("approved", Decimal("0.229900"))

    page = screen(app, report=app.document_id, metric="roe_percent")
    assert not page.exception
    shown = " ".join(m.value for m in page.markdown) + " ".join(c.value for c in page.caption) + html_text(page)
    assert "How the value was worked out" in shown and "Calculated from 3 printed figures" in shown
    assert "PDF page 1" in shown and "Profit after tax" in shown and "Total equity" in shown
    assert "Calculated with a formula" in list(page.dataframe[-1].value["Action"])


def test_mark_an_input_figure_for_eps(app):
    open_metric(app, "eps_etb")
    next(b for b in app.button if b.key == "calc_mark_eps_etb_eps_percent").click().run()
    assert not app.exception
    assert app.session_state["calc_input"]["variable"] == "eps_percent"
    assert app.session_state["mark_mode"] is True  # Mark a number switched on for the reviewer
    assert any("for a calculation" in i.value for i in app.info)
    app.session_state["marked"] = Mark(0, (300.0, 140.0, 330.0, 152.0), "39%", "39%", UnitEvidence(source="none"))
    app.run()
    value = next(t for t in app.text_input if (t.key or "").startswith("input_value_eps_percent"))
    assert value.value == "39%"  # pre-filled as printed; stored as the fraction 0.39 (D-067)
    assert any("Will store 0.39 (39%)" in c.value for c in app.caption)
    next(b for b in app.button if b.label == "Save input").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        (row,) = conn.execute("SELECT figure_key, value, unit, page FROM input_figures").fetchall()
    assert tuple(row) == ("eps_percent", "0.39", "fraction", 1)
    assert "calc_input" not in app.session_state and app.session_state["selected_metric"] == "eps_etb"


def test_a_bracketed_eps_percentage_stays_negative(app):
    """EPS can be negative (metrics.yaml sign: as_printed), so a loss-year '(5%)' of par is not made positive."""
    open_metric(app, "eps_etb")
    next(b for b in app.button if b.key == "calc_mark_eps_etb_eps_percent").click().run()
    app.session_state["marked"] = Mark(0, (300.0, 140.0, 330.0, 152.0), "(5%)", "(5%)", UnitEvidence(source="none"))
    app.run()
    value = next(t for t in app.text_input if (t.key or "").startswith("input_value_eps_percent"))
    assert value.value == "-5%"
    next(b for b in app.button if b.label == "Save input").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        (row,) = conn.execute("SELECT value FROM input_figures").fetchall()
    assert row[0] == "-0.05"
