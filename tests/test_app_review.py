"""Phase 5: the review workflow through the Streamlit app (synthetic PDF, temporary data folder)."""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import src.config_loader
import src.seed_data
from src import calculation_service, review_service
from src.database import connect
from src.page_marks import Mark
from src.pdf_extractor import TABLE_METHOD
from src.unit_normalizer import UnitEvidence
from src.project_service import list_documents, load_candidates
from tests.conftest import html_text
from tests.test_app_smoke import FakeUpload

APP = Path(__file__).resolve().parent.parent / "app.py"
TOTAL_ASSETS_KEY = (TABLE_METHOD, 0, 3, 0)  # page 1, "Total assets", 2026 column
BANK, OTHER_BANK = "Awash bank", "Zemen bank"  # names from config/banks.yaml; the PDF itself is synthetic


@pytest.fixture
def app(monkeypatch, tmp_path):
    real_load = src.config_loader.load_config

    def load_with_temp_data(*args, **kwargs):
        config = real_load(*args, **kwargs)
        return replace(config, settings=replace(config.settings, data_dir=tmp_path / "appdata"))

    monkeypatch.setattr(src.config_loader, "load_config", load_with_temp_data)
    monkeypatch.setattr(src.seed_data, "restore_seed", lambda *a, **k: False)  # start empty, not with the pilot reports
    # Like a real uploader, only the first-upload widget has a file; "Replace with another PDF" stays empty.
    monkeypatch.setattr(st, "file_uploader", lambda *a, key=None, **k: FakeUpload() if key == "upload_main" else None)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["workbench"] = True  # the parked full workbench (docs/redesign-backlog.md)
    at.session_state["bank"] = BANK
    at.session_state["period"] = "2025/26"
    at.session_state["reviewer"] = "Analyst A"
    at.run()
    assert not at.exception
    at.db_path = tmp_path / "appdata" / "app.db"
    return at


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def save(at: AppTest) -> str:
    button(at, "Save report").click().run()
    assert not at.exception
    return at.session_state["doc"]["document_id"]


def select_total_assets(at: AppTest) -> None:
    at.session_state["detail_mode"] = "metric"
    at.session_state["selected_metric"] = "total_assets_etb"
    at.session_state["selected_key"] = TOTAL_ASSETS_KEY
    at.run()
    assert not at.exception


def test_review_buttons_ask_to_save_first(app):
    select_total_assets(app)
    assert button(app, "Approve").disabled
    assert any("Save the report" in c.value for c in app.caption)


def test_bank_is_chosen_from_the_configured_list(app):
    bank = next(s for s in app.selectbox if s.label == "Bank")
    assert list(bank.options) == list(src.config_loader.load_config().banks) and bank.value == BANK
    bank.select(OTHER_BANK).run()
    assert app.session_state["bank"] == OTHER_BANK


def test_save_requires_a_bank(app):
    next(s for s in app.selectbox if s.label == "Bank").set_value(None).run()
    button(app, "Save report").click().run()
    assert any("Choose the bank" in e.value for e in app.error)
    assert "document_id" not in app.session_state["doc"]


def test_save_requires_a_report_year(app):
    app.session_state["period"] = ""
    app.run()
    button(app, "Save report").click().run()
    assert any("report year" in e.value for e in app.error)
    assert "document_id" not in app.session_state["doc"]


def test_save_report_stores_document_and_locks_metadata(app):
    document_id = save(app)
    with closing(connect(app.db_path)) as conn:
        (doc,) = list_documents(conn)
    assert (doc["id"], doc["bank_name"], doc["reporting_period"]) == (document_id, BANK, "2025/26")
    assert next(t for t in app.text_input if t.label == "Bank").disabled  # locked, shown as saved


def test_saving_the_same_pdf_again_warns(app):
    save(app)
    app.session_state["doc"] = {k: v for k, v in app.session_state["doc"].items() if k != "document_id"}
    app.run()
    button(app, "Save report").click().run()
    assert any("already saved" in w.value for w in app.warning)


def upload_again(at: AppTest) -> None:
    """The reviewer uploads the same PDF in a new session (nothing loaded yet).

    A new session opened without a link has no ?report= in the address; with one, the link
    reopens the report instead (tested in test_app_state.py)."""
    at.query_params.pop("report", None)
    at.session_state["doc"] = None
    for key in ("bank", "period", "_bank_value", "_period_value"):
        at.session_state[key] = OTHER_BANK if key in ("bank", "_bank_value") else ""
    at.run()
    assert not at.exception


def test_uploading_a_saved_pdf_opens_its_review(app):
    document_id = save(app)
    with closing(connect(app.db_path)) as conn:
        review_service.approve(conn, next(c.id for c in load_candidates(conn, document_id, "total_assets_etb")
                                          if c.key == TOTAL_ASSETS_KEY), "Analyst A")
    upload_again(app)
    assert app.session_state["doc"]["document_id"] == document_id  # the saved review, not a new one
    assert (app.session_state["bank"], app.session_state["period"]) == (BANK, "2025/26")
    assert any("already saved as" in m.value and "your recorded values are here" in m.value for m in app.markdown)
    assert not any(b.label == "Save report" for b in app.button)
    with closing(connect(app.db_path)) as conn:
        assert len(list_documents(conn)) == 1


def test_a_separate_copy_can_still_be_saved(app):
    first = save(app)
    upload_again(app)
    button(app, "Save a separate copy instead").click().run()
    assert not app.exception
    second = app.session_state["doc"]["document_id"]
    assert second != first
    with closing(connect(app.db_path)) as conn:
        assert len(list_documents(conn)) == 2
    assert not any("already saved as" in m.value for m in app.markdown)


def bank_reports_page(data_dir: str) -> None:
    """The Reports screen on its own, with a given data folder (run by AppTest.from_function)."""
    from dataclasses import replace
    from pathlib import Path

    from src.config_loader import load_config
    from src.ui import pages

    config = load_config()
    pages.bank_reports(replace(config, settings=replace(config.settings, data_dir=Path(data_dir))), lambda _: None)


def parked_tools_page(data_dir: str) -> None:
    """The report tools parked off the redesigned screens (docs/redesign-backlog.md), on their own:
    fix names and years, then per report Delete and Work left, then the deletion log."""
    from contextlib import closing
    from dataclasses import replace
    from pathlib import Path

    import streamlit as st

    from src import bulk_review, review_service
    from src.config_loader import load_config
    from src.database import connect
    from src.project_service import list_documents
    from src.ui import pages

    config = load_config()
    config = replace(config, settings=replace(config.settings, data_dir=Path(data_dir)))
    pages._fix_names_and_years(config)
    with closing(connect(config.settings.database_path)) as conn:
        documents = list_documents(conn)
        rows = [(d, len(review_service.approved_results(conn, d["id"])), bulk_review.outstanding_work(conn, d["id"], config))
                for d in documents]
    if not documents:
        st.info("No reports saved yet.")
    for d, approved, work in rows:
        pages._delete_report(config, d, approved)
        pages._work_left(config, d["id"], work, lambda _: None)
    pages._deletion_log(config)


def test_delete_a_saved_report_from_bank_reports(app):
    save(app)
    page = AppTest.from_function(parked_tools_page, args=(str(app.db_path.parent),), default_timeout=60)
    page.session_state["reviewer"] = "Analyst A"
    page.run()
    delete = next(b for b in page.button if b.label == "Delete permanently")
    assert delete.disabled  # needs the confirmation tick first
    next(t for t in page.text_input if t.label == "Reason").input("Demo upload").run()
    next(c for c in page.checkbox if c.label == "I understand this cannot be undone").check().run()
    next(b for b in page.button if b.label == "Delete permanently").click().run()
    assert not page.exception
    with closing(connect(app.db_path)) as conn:
        assert list_documents(conn) == []
        (log,) = review_service.deletion_log(conn)
    assert (log["reason"], log["reviewer"]) == ("Demo upload", "Analyst A")
    assert any("No reports saved yet" in i.value for i in page.info)


def test_open_saved_report_from_the_duplicate_warning(app):
    document_id = save(app)
    app.session_state["doc"] = {k: v for k, v in app.session_state["doc"].items() if k != "document_id"}
    app.session_state["bank"] = OTHER_BANK
    app.run()
    button(app, "Save report").click().run()
    button(app, "Open saved report").click().run()
    assert not app.exception
    assert app.session_state["doc"]["document_id"] == document_id
    assert app.session_state["bank"] == BANK  # the saved report's details are restored


def test_saved_report_keeps_a_name_not_in_the_list(app):
    """Reports saved before the bank list existed were typed by hand; they still show as saved."""
    document_id = save(app)
    with closing(connect(app.db_path)) as conn:  # as saved by hand before the list existed
        conn.execute("DROP TRIGGER documents_no_update")  # recreated by the next connect()
        conn.execute("UPDATE documents SET bank_name = ? WHERE id = ?", ("Bank of abyssinia", document_id))
        conn.commit()
    app.run()
    assert not app.exception
    assert next(t for t in app.text_input if t.label == "Bank").value == "Bank of abyssinia"


def test_approve_through_the_app_persists(app):
    document_id = save(app)
    select_total_assets(app)
    approve = next(b for b in app.button if b.label == "Approve" and not b.disabled)
    approve.click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        state = review_service.review_state(conn, document_id, "total_assets_etb")
    assert state.status == "approved" and state.accepted_decision.reviewer == "Analyst A"
    table = app.dataframe[0].value
    assert table[table["Metric"] == "Total assets"].iloc[0]["Status"].endswith("Approved")


def test_approve_states_the_saved_value_when_todays_reading_differs(app):
    """approve() stores the figure as converted when the report was saved (e.g. a share count saved
    before count_in_thousands_below, D-069): the Approve text must state that value, not today's reading."""
    document_id = save(app)
    with closing(connect(app.db_path)) as conn, conn:  # simulates a figure saved by an older version of the app
        conn.execute("DROP TRIGGER candidates_immutable")  # recreated by the next connect()
        conn.execute("UPDATE candidates SET normalized_value = '123' WHERE document_id = ? AND metric_id = "
                     "'total_assets_etb' AND extraction_method = ? AND page_index = ? AND row_index = ? AND "
                     "column_index = ?", (document_id, *TOTAL_ASSETS_KEY))
    select_total_assets(app)
    assert any("When the report was saved this figure was converted to 123 ETB" in w.value for w in app.warning)
    assert any(m.value.endswith("as 123 ETB**") for m in app.markdown)
    next(b for b in app.button if b.label == "Approve" and not b.disabled).click().run()
    with closing(connect(app.db_path)) as conn:
        assert review_service.review_state(conn, document_id, "total_assets_etb").accepted_value == 123


def test_reject_without_reason_shows_the_rule(app):
    save(app)
    select_total_assets(app)
    button(app, "Confirm reject").click().run()
    assert any("reason is required" in e.value for e in app.error)


def test_correction_then_approval(app):
    document_id = save(app)
    select_total_assets(app)
    candidate_id = next(k for k in app.session_state if str(k).startswith("correct_value_total_assets_etb_"))
    suffix = candidate_id.rsplit("_", 1)[1]
    app.text_input(key=f"correct_value_total_assets_etb_{suffix}").input("1234568000")
    app.text_area(key=f"correct_reason_total_assets_etb_{suffix}").input("Last digit misread")
    button(app, "Save correction").click().run()
    assert not app.exception
    assert any("awaiting approval" in i.value for i in app.info)
    button(app, "Approve correction").click().run()
    with closing(connect(app.db_path)) as conn:
        (result,) = review_service.approved_results(conn, document_id)
    assert str(result.value) == "1234568000" and result.human_entered
    row = app.dataframe[0].value.set_index("Metric").loc["Total assets"]
    assert row["Value"].startswith("1,234,568,000")


def _calculations_page_script():
    from src.config_loader import load_config
    from src.ui import calculations

    calculations.render(load_config(), open_report=lambda document_id: None)


def test_finish_extraction_locks_figures_and_opens_calculations(app):
    document_id = save(app)
    select_total_assets(app)
    next(b for b in app.button if b.label == "Approve" and not b.disabled).click().run()
    button(app, "Confirm: finish extraction").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        assert review_service.extraction_status(conn, document_id).finished
    assert any("Extraction review finished" in s.value for s in app.success)
    assert not any(b.label == "Approve" for b in app.button)  # figures are locked
    assert any("figures are locked" in c.value for c in app.caption)

    page = AppTest.from_function(_calculations_page_script, default_timeout=60)
    page.run()
    assert not page.exception
    assert "of 26 entered" in html_text(page) and "Extraction finished" in html_text(page)

    save_page = AppTest.from_function(_save_calculated_script, default_timeout=60)
    save_page.session_state["document_id"] = document_id
    save_page.session_state["reviewer"] = "Analyst A"
    save_page.run()
    assert not save_page.exception
    assert any("how 'Leverage Ratio' was calculated" in e.value for e in save_page.error)  # no note: refused
    with closing(connect(app.db_path)) as conn:
        values = calculation_service.current_values(conn, document_id)
    assert {c: str(v.value) for c, v in values.items()} == {"Loan-to-Deposit Ratio": "0.85"}


def test_reopen_extraction_from_the_review_screen(app):
    document_id = save(app)
    select_total_assets(app)
    next(b for b in app.button if b.label == "Approve" and not b.disabled).click().run()
    button(app, "Confirm: finish extraction").click().run()
    app.text_area(key="reopen_extraction_reason_table").input("Wrong figure approved")
    app.button(key="reopen_extraction_table").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        assert not review_service.extraction_status(conn, document_id).finished
    assert not any("figures are locked" in c.value for c in app.caption)  # per-metric controls are back


def test_correction_uses_the_box_marked_on_the_page(app):
    document_id = save(app)
    select_total_assets(app)
    box = (295.0, 150.0, 360.0, 165.0)
    app.session_state["marked"] = Mark(0, box, "1,234,568", "1,234,568",
                                       UnitEvidence("ETB", Decimal(1000), "page heading 'Birr'000'"))
    app.run()
    value = next(t for t in app.text_input if (t.key or "").startswith("correct_value_total_assets_etb_")
                 and "_m" in t.key)
    assert value.value == "1234568000"  # suggested from the mark, in full Birr
    reason = next(t for t in app.text_area if (t.key or "").startswith("correct_reason_total_assets_etb_"))
    reason.input("Picked the wrong row")
    button(app, "Save correction").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        pending = review_service.review_state(conn, document_id, "total_assets_etb").pending
    assert (pending.source_page, pending.source_bbox, pending.reviewed_value) == (1, box, Decimal("1234568000"))
    assert "marked" not in app.session_state  # a mark is used once


def test_record_a_marked_number_for_a_chosen_metric(app):
    document_id = save(app)
    box = (100.0, 200.0, 140.0, 212.0)
    app.session_state["marked"] = Mark(0, box, "10,627", "10,627", UnitEvidence(source="no unit heading on the page"))
    app.run()
    which = next(s for s in app.selectbox if s.label == "Which figure is this?")
    assert which.value is None and "Employees" in which.options  # searchable list of metric names
    which.select("employees").run()
    value = next(t for t in app.text_input if t.label == "Value" and (t.key or "").startswith("mark_value_employees"))
    assert value.value == "10627"  # taken from the box; still editable
    value.input("10628").run()
    button(app, "Save marked value").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        pending = review_service.review_state(conn, document_id, "employees").pending
    # Employees had no figure, so this is a manual entry whose evidence is the box.
    assert (pending.decision, pending.reviewed_value, pending.source_page, pending.source_bbox) == (
        "manual_entry", Decimal("10628"), 1, box)
    assert app.session_state["selected_metric"] == "employees"  # ready to approve


def test_use_a_figure_from_all_figures_for_a_metric(app):
    document_id = save(app)
    app.session_state["detail_mode"] = "figure"
    app.session_state["selected_key"] = TOTAL_ASSETS_KEY  # the figure the reviewer found by searching
    app.run()
    button(app, "Use this figure for a metric").click().run()
    assert not app.exception
    next(s for s in app.selectbox if s.label == "Which figure is this?").select("paid_up_capital_etb").run()
    value = next(t for t in app.text_input if (t.key or "").startswith("mark_value_paid_up_capital_etb"))
    assert value.value == "1234567000"  # printed 1,234,567 under Birr'000
    button(app, "Save marked value").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        pending = review_service.review_state(conn, document_id, "paid_up_capital_etb").pending
        figure = next(c for c in load_candidates(conn, document_id, "total_assets_etb") if c.key == TOTAL_ASSETS_KEY)
    # The exact page and position of the picked figure are the evidence.
    assert (pending.source_page, pending.source_bbox) == (1, tuple(round(v, 2) for v in figure.bbox))
    assert pending.reason == "Picked from All Figures on PDF page 1"


def test_a_figure_can_be_used_before_the_report_is_saved(app):
    """One press saves the report and records the picked value, so it shows in Extracted Data."""
    app.session_state["detail_mode"] = "figure"
    app.session_state["selected_key"] = TOTAL_ASSETS_KEY
    app.run()
    button(app, "Use this figure for a metric").click().run()
    next(s for s in app.selectbox if s.label == "Which figure is this?").select("paid_up_capital_etb").run()
    assert not any(b.label == "Save marked value" for b in app.button)
    button(app, "Save report and record this value").click().run()
    assert not app.exception
    document_id = app.session_state["doc"]["document_id"]  # saved
    with closing(connect(app.db_path)) as conn:
        pending = review_service.review_state(conn, document_id, "paid_up_capital_etb").pending
    assert (pending.reviewed_value, pending.source_page) == (Decimal("1234567000"), 1)
    assert app.session_state["selected_metric"] == "paid_up_capital_etb"  # ready to approve


def test_figure_used_before_saving_still_needs_bank_and_year(app):
    next(s for s in app.selectbox if s.label == "Bank").set_value(None).run()
    app.session_state["detail_mode"] = "figure"
    app.session_state["selected_key"] = TOTAL_ASSETS_KEY
    app.run()
    button(app, "Use this figure for a metric").click().run()
    next(s for s in app.selectbox if s.label == "Which figure is this?").select("paid_up_capital_etb").run()
    button(app, "Save report and record this value").click().run()
    assert any("Choose the bank" in e.value for e in app.error)
    assert "document_id" not in app.session_state["doc"]


def test_figure_details_warn_about_last_years_column(app):
    app.session_state["detail_mode"] = "figure"
    app.session_state["selected_key"] = (TABLE_METHOD, 0, 3, 1)  # Total assets, 2025 column
    app.run()
    assert not app.exception
    assert any("2025 column, which is last year's figure" in w.value and "use the 2026 column" in w.value
               for w in app.warning)
    app.session_state["selected_key"] = TOTAL_ASSETS_KEY  # 2026 column: no warning
    app.run()
    assert not any("last year's figure" in w.value for w in app.warning)


def test_a_marked_value_replaces_the_old_suggestion_on_the_page(app):
    from src.ui.extract_review import _marked_values

    document_id = save(app)
    box = (295.0, 150.0, 360.0, 165.0)
    with closing(connect(app.db_path)) as conn:
        assets = next(c for c in load_candidates(conn, document_id, "total_assets_etb") if c.rank == 0)
        review_service.correct(conn, assets.id, "Analyst A", "1234568000", "ETB", "Marked on PDF page 1",
                               source_page=1, source_bbox=box)
        states = {"total_assets_etb": review_service.review_state(conn, document_id, "total_assets_etb"),
                  "employees": review_service.review_state(conn, document_id, "employees")}
    assert _marked_values(states) == {"total_assets_etb": (1, box)}  # only metrics valued from a box

    select_total_assets(app)
    assert not app.exception
    assert app.session_state["viewer_page"] == 1


def test_only_the_selected_metric_is_bold_and_named():
    from src.pdf_render import BOX_STYLES, HIGHLIGHT_COLOR, MARK_COLOR

    focus, other = BOX_STYLES["focus"], BOX_STYLES["recorded"]
    assert focus.color == BOX_STYLES["marked"].color == MARK_COLOR and focus.labelled and focus.fill
    assert not other.labelled and other.width < focus.width  # thin, no name over the figures
    assert BOX_STYLES["selected"].color == HIGHLIGHT_COLOR  # only an unrecorded suggestion is red


def test_manual_entry_needs_a_mark(app):
    save(app)
    app.session_state["detail_mode"] = "metric"
    app.session_state["selected_metric"] = "employees"
    app.run()
    assert button(app, "Save entry").disabled
    assert any("Mark a number" in i.value for i in app.info)


def _save_calculated_script():
    import pandas as pd
    import streamlit as st

    from src.config_loader import load_config
    from src.ui import calculations

    if not st.session_state.get("saved_once"):  # the save reruns the page; save only once
        st.session_state.saved_once = True
        original = pd.DataFrame({"Calculated column": ["Loan-to-Deposit Ratio", "Leverage Ratio"],
                                 "Value": ["", ""], "Formula / note": ["", ""]})
        edited = original.assign(Value=["0.85", "7.4"], **{"Formula / note": ["Total Loans / Total Deposits", ""]})
        calculations._save(load_config(), st.session_state["document_id"], original, edited)


def test_export_from_the_review_screen(app):
    save(app)
    button(app, "Export 2025/26 to Excel (all banks)").click().run()
    assert any("Nothing is approved yet for 2025/26" in e.value for e in app.error)

    select_total_assets(app)
    next(b for b in app.button if b.label == "Approve" and not b.disabled).click().run()
    button(app, "Export 2025/26 to Excel (all banks)").click().run()
    assert not app.exception
    (path,) = (app.db_path.parent / "exports").glob("*.xlsx")
    assert path.name == "approved_2025-26.xlsx"
    assert any(path.name in s.value for s in app.success)


def test_a_value_worked_out_in_its_note_shows_its_inputs_not_the_marked_box(app):
    """Entered before Calculate existed: one input marked, the arithmetic in the note (D-089)."""
    document_id = save(app)
    with closing(connect(app.db_path)) as conn:
        assets = next(c for c in load_candidates(conn, document_id, "total_assets_etb") if c.key == TOTAL_ASSETS_KEY)
        review_service.manual_entry(conn, document_id, "employees", "Analyst A", "2222221", "count", 1,
                                    "Worked out: 1,234,567 + 987,654 = 2,222,221 (p1)", source_bbox=assets.bbox)
        review_service.approve_correction(conn, document_id, "employees", "Analyst A")
    app.session_state["detail_mode"] = "metric"
    app.session_state["selected_metric"] = "employees"
    app.run()
    assert not app.exception
    assert any("is calculated" in m.value for m in app.markdown)  # close-ups of its inputs above the PDF
    assert [b.label for b in app.button if b.label.startswith("Open page")] == ["Open page 1"]  # both on page 1
    assert any("**CALCULATED**" in m.value for m in app.markdown)
    assert any("Gives the saved 2222221" in m.value for m in app.markdown)
    inputs = next(d.value for d in app.dataframe if "Input" in d.value.columns)
    # named by their printed row and year column, as the note does not name them
    assert list(inputs["Input"]) == ["Total assets 2026", "Total assets 2025"]
    assert list(inputs["Page"]) == ["p1", "p1"]
    assert not [b for b in app.button if b.label == "Show the marked box"]  # that box is only one input
    assert not any("No figure found" in w.value for w in app.warning)
    assert any("calculated by the reviewer" in s.value for s in app.success)
