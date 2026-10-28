"""Screen state that must survive page switches and reloads, and the marked-value form's checks
(synthetic PDF, temporary data folder)."""

from contextlib import closing
from decimal import Decimal

import streamlit as st
from streamlit.testing.v1 import AppTest

from src import review_service
from src.database import connect
from src.page_marks import Mark
from src.ui.extract_review import mark_input_problem, not_converted_reason
from src.unit_normalizer import UnitEvidence
from tests.test_app_review import APP, BANK, OTHER_BANK, TOTAL_ASSETS_KEY, app, button, save, select_total_assets  # noqa: F401

FIELDS = ("bank", "period", "metric_selection")
EXPORT = "Export 2025/26 to Excel (all banks)"


def visit(at: AppTest, url_path: str) -> None:
    """Show another page of the app (st.Page url_path; "" is Extract & Review, the default page).

    AppTest switches only to file pages, so the page hash is set directly."""
    at._page_hash = next(h for h, info in at._registered_pages.items() if info.get("url_pathname") == url_path)
    at.run()
    assert not at.exception


def round_trip_through_templates(at: AppTest) -> None:
    """Go to Templates and back. In the browser Streamlit then drops the values of the widgets that
    page does not show; that is done here by hand, since AppTest keeps some of them."""
    visit(at, "templates")
    for key in FIELDS:
        if key in at.session_state:
            del at.session_state[key]
    visit(at, "")


def field(at: AppTest, label: str):
    return next(w for w in (*at.text_input, *at.selectbox) if w.label == label)


# --- Bank, Report year and Metrics survive page switches and reloads -------------------------

def test_saved_report_keeps_its_fields_after_visiting_another_page(app):
    document_id = save(app)
    round_trip_through_templates(app)
    assert (field(app, "Bank").value, field(app, "Report year").value) == (BANK, "2025/26")
    assert app.session_state["metric_selection"]  # the saved report's metrics, not an empty list
    assert any(b.label == "Confirm: finish extraction" for b in app.button)  # inside Finish extraction
    assert any(b.label == EXPORT for b in app.button)
    assert not any("Save the report to approve" in c.value for c in app.caption)
    assert app.query_params.get("report") == document_id

    select_total_assets(app)
    next(b for b in app.button if b.label == "Approve" and not b.disabled).click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        assert review_service.review_state(conn, document_id, "total_assets_etb").status == "approved"


def test_fields_follow_the_saved_report_not_stale_values(app):
    save(app)
    app.session_state["period"] = ""  # whatever was left behind, the saved report's values win
    app.run()
    assert field(app, "Report year").value == "2025/26"
    assert any(b.label == EXPORT for b in app.button)


def test_a_link_with_the_report_reopens_it_in_a_new_session(app):
    document_id = save(app)
    fresh = AppTest.from_file(str(APP), default_timeout=60)  # a reload or a new browser tab
    fresh.session_state["workbench"] = True  # the parked full workbench (docs/redesign-backlog.md)
    fresh.session_state["reviewer"] = "Analyst A"
    fresh.query_params["report"] = document_id
    fresh.run()
    assert not fresh.exception
    assert fresh.session_state["doc"]["document_id"] == document_id
    assert (field(fresh, "Bank").value, field(fresh, "Report year").value) == (BANK, "2025/26")
    assert any(b.label == EXPORT for b in fresh.button)
    fresh.run()  # opened once; later reruns keep it without reopening
    assert fresh.session_state["doc"]["document_id"] == document_id
    assert fresh.query_params.get("report") == document_id


def test_a_link_to_a_missing_report_explains_and_is_dropped(app, monkeypatch):
    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: None)  # nothing uploaded in the new tab
    fresh = AppTest.from_file(str(APP), default_timeout=60)
    fresh.session_state["workbench"] = True  # the parked full workbench (docs/redesign-backlog.md)
    fresh.query_params["report"] = "no-such-report"
    fresh.run()
    assert not fresh.exception
    assert any("could not be opened" in e.value and "no longer in the database" in e.value for e in fresh.error)
    assert "report" not in fresh.query_params
    assert fresh.session_state.get("doc") is None
    fresh.run()
    assert not fresh.error  # not tried again


def test_unsaved_upload_keeps_typed_bank_and_year_after_visiting_another_page(app):
    field(app, "Bank").select(OTHER_BANK).run()
    field(app, "Report year").input("2024/25").run()
    round_trip_through_templates(app)
    assert (field(app, "Bank").value, field(app, "Report year").value) == (OTHER_BANK, "2024/25")
    assert "document_id" not in app.session_state["doc"]
    assert "report" not in app.query_params  # only a saved report goes in the address


# --- the marked-value form ------------------------------------------------------------------

def use_total_assets_figure_for(at: AppTest, metric_id: str) -> None:
    at.session_state["detail_mode"] = "figure"
    at.session_state["selected_key"] = TOTAL_ASSETS_KEY  # printed 1,234,567 under Birr'000
    at.run()
    button(at, "Use this figure for a metric").click().run()
    next(s for s in at.selectbox if s.label == "Which figure is this?").select(metric_id).run()
    assert not at.exception


def value_box(at: AppTest, metric_id: str):
    return next(t for t in at.text_input if t.label == "Value" and (t.key or "").startswith(f"mark_value_{metric_id}"))


def note_box(at: AppTest):
    return next(t for t in at.text_area if t.label == "Note" and (t.key or "").startswith("mark_note"))


def test_empty_value_is_refused_and_the_form_is_kept(app):
    document_id = save(app)
    use_total_assets_figure_for(app, "roe_percent")
    note_box(app).input("Ratio from the highlights page").run()
    button(app, "Save marked value").click().run()
    assert not app.exception
    assert any("Value box is empty" in e.value for e in app.error)
    # Every field stays as typed, on the same form.
    assert app.session_state["detail_mode"] == "figure"
    assert next(s for s in app.selectbox if s.label == "Which figure is this?").value == "roe_percent"
    assert value_box(app, "roe_percent").value == ""
    assert note_box(app).value == "Ratio from the highlights page"
    with closing(connect(app.db_path)) as conn:
        assert review_service.review_state(conn, document_id, "roe_percent").history == []

    value_box(app, "roe_percent").input("twenty").run()
    button(app, "Save marked value").click().run()
    assert any("'twenty' is not a number" in e.value for e in app.error)
    assert value_box(app, "roe_percent").value == "twenty"


def test_empty_value_does_not_save_an_unsaved_report(app):
    use_total_assets_figure_for(app, "roe_percent")
    button(app, "Save report and record this value").click().run()
    assert any("Value box is empty" in e.value for e in app.error)
    assert "document_id" not in app.session_state["doc"]


def test_fraction_metric_from_a_birr_figure_is_typed_by_hand(app):
    document_id = save(app)
    use_total_assets_figure_for(app, "roe_percent")
    box = value_box(app, "roe_percent")
    assert box.value == "" and not box.disabled
    assert any("This figure is in Birr; Return on equity (ROE) is a percentage: type it, e.g. 29% or 0.29" in c.value
               for c in app.caption)
    box.input("0.29").run()
    assert value_box(app, "roe_percent").value == "0.29"  # the form is still there after the rerun
    button(app, "Save marked value").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        pending = review_service.review_state(conn, document_id, "roe_percent").pending
    assert (pending.reviewed_value, pending.reviewed_unit, pending.source_page) == (Decimal("0.29"), "fraction", 1)
    assert app.session_state["selected_metric"] == "roe_percent"  # shown, ready to approve, once saved


def mark(printed: str | None, unit: UnitEvidence) -> Mark:
    return Mark(0, (1.0, 2.0, 3.0, 4.0), printed or "", printed, unit)


def test_reasons_for_an_empty_value_box():
    birr = UnitEvidence("ETB", Decimal(1000), "page heading 'Birr'000'")
    plain = UnitEvidence(source="no unit heading on the page")
    assert not_converted_reason(mark("1,234", birr), None, "Return on equity (ROE)", "fraction") == (
        "This figure is in Birr; Return on equity (ROE) is a percentage: type it, e.g. 29% or 0.29.")
    assert not_converted_reason(mark("29%", plain), None, "Employees", "count").startswith(
        "This figure is a percentage; Employees is a count")
    assert "box does not hold exactly one readable number" in not_converted_reason(mark(None, plain), None, "X", "ETB")


def test_marked_value_input_checks():
    assert "empty" in mark_input_problem("  ", "note")
    assert "not a number" in mark_input_problem("1,2a", "note")
    assert "note" in mark_input_problem("0.29", " ")
    assert mark_input_problem("1,234.5", "note") is None
