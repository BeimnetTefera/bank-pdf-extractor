"""Through the app: Approve says what is stored, the sign default, typed values, EPS from par value."""

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
from src.project_service import load_candidates
from tests.conftest import make_pdf

APP = Path(__file__).resolve().parent.parent / "app.py"
PDF = make_pdf([
    [
        ("SYNTHETIC BANK S.C. STATEMENT OF PROFIT OR LOSS", []),
        ("", ["Notes", "2023", "2022"]),
        ("Birr'000", []),
        ("Income tax expense", ["", "(1,357,194)", "(1,000,000)"]),
        ("Profit after tax", ["", "3,869,598", "3,235,824"]),
        ("Earnings per share", ["", "39%", "57%"]),
    ],
    [("27. Share capital: 12,500,000 ordinary shares of Birr 1000 each", [])],
])
REVIEWER = "Analyst A"


class Upload:
    name = "synthetic.pdf"
    file_id = "synthetic-units"

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
    return at


def open_metric(at: AppTest, metric_id: str) -> None:
    at.session_state["detail_mode"] = "metric"
    at.session_state["selected_metric"] = metric_id
    at.session_state["selected_key"] = None
    at.run()
    assert not at.exception


def test_approve_shows_the_stored_value_and_defaults_brackets_to_positive(app):
    open_metric(app, "profit_tax_etb")
    sign = next(r for r in app.radio if r.label == "Record the sign")
    assert sign.value == "As a positive amount"  # the metric's setting (sign: positive)
    assert any("(1,357,194) printed in brackets -> recorded as 1,357,194,000 ETB (positive)" in c.value
               for c in app.caption)
    assert any("Approve (1,357,194) (Birr'000) as 1,357,194,000 ETB" in m.value for m in app.markdown)
    next(b for b in app.button if b.label == "Approve" and not b.disabled).click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        state = review_service.review_state(conn, app.document_id, "profit_tax_etb")
    assert state.accepted_value == Decimal(1_357_194_000) and state.accepted_decision.sign_choice == "positive"


def test_profit_is_recorded_as_printed_by_default(app):
    open_metric(app, "profit_after_tax_etb")
    assert any("Approve 3,869,598 (Birr'000) as 3,869,598,000 ETB" in m.value for m in app.markdown)


def test_correct_reads_a_scale_word_and_shows_what_is_stored(app):
    open_metric(app, "profit_after_tax_etb")
    box = next(t for t in app.text_input if (t.key or "").startswith("correct_value_profit_after_tax_etb_"))
    box.input("3,869.6 million").run()
    assert any("Will store 3,869,600,000 ETB" in c.value for c in app.caption)
    next(t for t in app.text_area if (t.key or "").startswith("correct_reason_profit_after_tax_etb")).input(
        "Rounded figure from the highlights").run()
    next(b for b in app.button if b.label == "Save correction").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        pending = review_service.review_state(conn, app.document_id, "profit_after_tax_etb").pending
    assert pending.reviewed_value == Decimal(3_869_600_000)


def test_correct_in_brackets_offers_positive_or_negative_and_saves_the_choice(app):
    open_metric(app, "profit_tax_etb")
    box = next(t for t in app.text_input if (t.key or "").startswith("correct_value_profit_tax_etb_"))
    box.input("(1,357,195)").run()
    sign = next(r for r in app.radio if r.key == f"{box.key}_sign")
    assert sign.value == "positive"  # the metric's setting (sign: positive)
    assert sign.options == ["Record as positive (default)", "Keep negative"]
    assert any("Will store 1,357,195 ETB" in c.value for c in app.caption)
    sign.set_value("as_printed").run()
    assert any("Will store -1,357,195 ETB" in c.value for c in app.caption)
    next(t for t in app.text_area if (t.key or "").startswith("correct_reason_profit_tax_etb")).input(
        "Typed in full Birr").run()
    next(b for b in app.button if b.label == "Save correction").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        pending = review_service.review_state(conn, app.document_id, "profit_tax_etb").pending
    assert (pending.reviewed_value, pending.sign_choice) == (Decimal(-1_357_195), "as_printed")


def test_eps_printed_as_a_percentage_converts_with_the_par_value_in_one_click(app):
    open_metric(app, "eps_etb")
    convert = next(b for b in app.button if b.label.startswith("Convert:"))
    assert convert.label == "Convert: 39% x par value Birr 1,000 (p2) = 390 Birr"
    convert.click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        state = review_service.review_state(conn, app.document_id, "eps_etb")
        figures = conn.execute("SELECT figure_key, value, page FROM input_figures ORDER BY id").fetchall()
    assert state.status == "corrected" and state.pending.entry_kind == "calculated"
    assert state.pending.reviewed_value == Decimal("390.00")
    assert [tuple(r) for r in figures] == [("eps_percent", "0.39", 1), ("par_value", "1000", 2)]


def test_eps_cross_check_is_shown_when_profit_and_shares_are_approved(app):
    with closing(connect(app.db_path)) as conn:
        c = next(c for c in load_candidates(conn, app.document_id, "profit_after_tax_etb") if c.period == "current")
        review_service.approve(conn, c.id, REVIEWER)
        review_service.manual_entry(conn, app.document_id, "weighted_average_shares", REVIEWER, "9907298", "count",
                                    1, "Note 28")
        review_service.approve_correction(conn, app.document_id, "weighted_average_shares", REVIEWER)
    open_metric(app, "eps_etb")
    assert any("= 390.58 Birr per share" in c.value for c in app.caption)
