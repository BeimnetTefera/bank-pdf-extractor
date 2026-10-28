"""Learn wording (D-043), add new figures (D-044) and cross-checks (D-045) through the app.

Synthetic PDF; configuration copied to a temporary folder and a temporary data folder.
"""

import shutil
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import src.config_loader
from src import review_service
from src.config_loader import CONFIG_DIR, PROJECT_ROOT
from src.database import connect
from src.pdf_extractor import extract_candidates
from src.project_service import analysis_runs, load_candidates
from tests.conftest import make_pdf

APP = Path(__file__).resolve().parent.parent / "app.py"
PDF = make_pdf([[
    ("SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION", []),
    ("", ["Notes", "2026", "2025"]),
    ("Birr'000", []),
    ("Total assets", ["", "1,500,000", "1,200,000"]),
    ("Total liabilities", ["", "1,000,000", "800,000"]),
    ("Total equity", ["", "499,000", "400,000"]),
    ("Customer deposit balances", ["21", "800,000", "700,000"]),
]])
REVIEWER = "Analyst A"


class Upload:
    name = "synthetic.pdf"
    file_id = "synthetic-learning"

    def getvalue(self) -> bytes:
        return PDF


@pytest.fixture
def app(monkeypatch, tmp_path):
    config_dir = shutil.copytree(CONFIG_DIR, tmp_path / "config")
    real_load = src.config_loader.load_config

    def load_from_temp(*args, **kwargs):
        config = real_load(config_dir, PROJECT_ROOT)
        return replace(config, settings=replace(config.settings, data_dir=tmp_path / "appdata"))

    monkeypatch.setattr(src.config_loader, "load_config", load_from_temp)
    monkeypatch.setattr(st, "file_uploader", lambda *a, key=None, **k: Upload() if key == "upload_main" else None)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["workbench"] = True  # the parked full workbench (docs/redesign-backlog.md)
    at.session_state["bank"] = "Awash bank"
    at.session_state["period"] = "2025/26"
    at.session_state["reviewer"] = REVIEWER
    at.run()
    assert not at.exception
    at.db_path = tmp_path / "appdata" / "app.db"
    at.learned_path = config_dir / "learned_aliases.yaml"
    return at


def save(at: AppTest) -> str:
    next(b for b in at.button if b.label == "Save report").click().run()
    assert not at.exception
    return at.session_state["doc"]["document_id"]


def test_cross_checks_are_shown_and_a_failure_opens_them(app):
    (panel,) = [e for e in app.expander if "Cross-checks" in e.label]
    assert "1 failed" in panel.label  # assets 1,500,000 vs liabilities + equity 1,499,000
    assert any("off by 1,000,000" in m.value for m in panel.markdown)


def test_learn_wording_then_add_the_new_figures(app):
    document_id = save(app)
    figure = next(c for c in extract_candidates(PDF).candidates
                  if c.raw_label == "Customer deposit balances" and c.raw_value_text == "800,000")
    with closing(connect(app.db_path)) as conn:
        review_service.manual_entry(conn, document_id, "total_deposits_etb", REVIEWER, "800000000", "ETB", 1,
                                    "Picked from All Figures on PDF page 1", source_bbox=figure.bbox)
        review_service.approve_correction(conn, document_id, "total_deposits_etb", REVIEWER)
    app.run()
    assert any("“Customer deposit balances” · PDF page 1" in m.value for m in app.markdown)

    app.button(key="learn_0").click().run()
    assert not app.exception
    text = app.learned_path.read_text(encoding="utf-8")
    assert "alias: Customer deposit balances" in text and "Awash bank 2025/26, PDF page 1" in text
    assert "bank: Awash bank" in text  # learned for this bank's reports only
    assert not any(b.key == "learn_0" for b in app.button)  # learned, so no longer offered

    app.button(key="add_new_figures").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        assert len(analysis_runs(conn, document_id)) == 1
        values = {c.raw_value_text for c in load_candidates(conn, document_id, "total_deposits_etb")}
        assert values == {"800,000", "700,000"}
        assert review_service.review_state(conn, document_id, "total_deposits_etb").status == "approved"
    assert not any(b.key == "add_new_figures" for b in app.button)
