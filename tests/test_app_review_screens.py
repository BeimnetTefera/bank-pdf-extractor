"""The review screens (spec section 5): metric steps, bulk approval, All Figures, the page viewer,
metric details and the work left on Bank Reports. Synthetic PDFs and a temporary data folder only."""

from contextlib import closing
from dataclasses import replace

from streamlit.testing.v1 import AppTest

from src import review_service
from src.candidate_queries import context_line
from src.database import connect
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, extract_candidates
from src.ui import extract_review
from tests.conftest import make_pdf
from tests.test_app_review import app, bank_reports_page, button, save  # noqa: F401  (app is a fixture)

ROWS_PER_PAGE, PAGES = 30, 5


def many_figures_pdf() -> bytes:
    """Five statement pages of 30 labelled rows, each with a 2026 and a 2025 figure (300 figures)."""
    pages = []
    for p in range(PAGES):
        rows = [("", ["Notes", "2026", "2025"])]
        rows += [(f"Line {p}-{i}", ["", f"{(p * 100 + i) * 1000 + 189:,}", f"{p * 100 + i:,}"])
                 for i in range(ROWS_PER_PAGE)]
        pages.append(rows)
    pages[0].append(("Large balance", ["", "189,512,266", "100"]))
    return make_pdf(pages)


def figures_screen(pdf: bytes) -> None:
    """The All Figures table on its own (run by AppTest.from_function)."""
    import streamlit as st

    from src.pdf_extractor import extract_candidates as extract
    from src.ui import extract_review as er

    picked = er._figures_table(extract(pdf), "f" * 64)
    st.session_state.picked = None if picked is None else picked.raw_value_text


def figures_app() -> AppTest:
    at = AppTest.from_function(figures_screen, args=(many_figures_pdf(),), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def count_line(at: AppTest) -> str:
    return next(m.value for m in at.markdown if "figures match" in m.value)


# --- All Figures ----------------------------------------------------------------------------

def test_all_figures_shows_the_true_count_and_100_rows_a_page():
    at = figures_app()
    total = len([c for c in extract_candidates(many_figures_pdf()).candidates
                 if c.extraction_method != TABLE_METHOD or c.raw_label])  # numbers without a label are hidden
    assert f"**{total:,} figures match**" in count_line(at) and "page 1 of" in count_line(at)
    assert len(at.dataframe[0].value) == extract_review.FIGURES_PER_PAGE
    at.number_input(key=next(n.key for n in at.number_input if n.key.startswith("fig_page_"))).set_value(4).run()
    assert not at.exception
    assert len(at.dataframe[0].value) == total - 3 * extract_review.FIGURES_PER_PAGE
    assert list(at.dataframe[0].value.columns) == ["Figure", "Value", "Page", "Year", "Type"]


def test_search_finds_a_number_typed_without_commas():
    at = figures_app()
    at.text_input[0].input("189512266").run()
    table = at.dataframe[0].value
    assert list(table["Value"]) == ["189,512,266"] and "**1 figures match**" in count_line(at)


def test_filters_by_page_range_year_column_and_value_range():
    at = figures_app()
    at.number_input(key="fig_from_" + "f" * 12).set_value(2).run()
    at.number_input(key="fig_to_" + "f" * 12).set_value(3).run()
    assert set(at.dataframe[0].value["Page"]) == {2, 3}
    at.selectbox(key="fig_year_" + "f" * 12).select("2025").run()
    table = at.dataframe[0].value
    assert set(table["Year"]) == {"2025"} and len(table) == 2 * ROWS_PER_PAGE
    at.text_input(key="fig_min").input("110").run()
    at.text_input(key="fig_max").input("150").run()  # page 2 holds 100-129, page 3 holds 200-229
    assert sorted(at.dataframe[0].value["Value"], key=int) == [str(n) for n in range(110, 130)]
    at.text_input(key="fig_max").input("lots").run()
    assert any("is not a number" in w.value for w in at.warning)


def test_the_selected_row_shows_its_whole_row_or_sentence():
    table = extract_candidates(make_pdf([[("Total assets", ["", "1,234,567", "987,654"])]])).candidates[0]
    assert context_line(table) == table.row_text and "1,234,567" in context_line(table)
    sentence = replace(table, extraction_method=NARRATIVE_METHOD, text_before="The bank had", text_after="customers.")
    assert context_line(sentence) == "The bank had [1,234,567] customers."


# --- metric steps, keyboard shortcuts and bulk approval -------------------------------------

def test_next_and_previous_step_through_the_metrics_with_shortcuts(app):
    nxt = app.button(key="metric_next")
    assert nxt.proto.shortcut.lower() == extract_review.NEXT_SHORTCUT.lower()
    assert app.button(key="metric_prev").proto.shortcut.lower() == extract_review.PREVIOUS_SHORTCUT.lower()
    nxt.click().run()
    first = app.session_state["selected_metric"]
    app.button(key="metric_next").click().run()
    second = app.session_state["selected_metric"]
    assert second != first and not app.exception
    app.button(key="metric_prev").click().run()
    assert app.session_state["selected_metric"] == first


def test_approve_all_verified_needs_the_tick_and_records_each_approval(app):
    document_id = save(app)
    approve = app.button(key="bulk_approve")
    assert approve.label in ("Approve 1 figure", ) or approve.label.endswith(" figures")
    assert approve.disabled  # the reviewer confirms they checked the list first
    next(c for c in app.checkbox if c.label == "I checked this list against the report").check().run()
    next(b for b in app.button if b.key == "bulk_approve").click().run()
    assert not app.exception
    assert any(s.value.startswith("Approved ") for s in app.success)
    with closing(connect(app.db_path)) as conn:
        state = review_service.review_state(conn, document_id, "total_assets_etb")
    assert state.status == "approved" and state.accepted_decision.reviewer == "Analyst A"


# --- PDF viewer -----------------------------------------------------------------------------

def test_selecting_a_metric_zooms_to_it_until_the_user_picks_a_zoom(app):
    app.session_state["viewer_zoom"] = "100%"
    app.run()
    app.button(key="metric_next").click().run()
    assert app.session_state["viewer_zoom"] == extract_review.AUTO_ZOOM
    app.select_slider(key="viewer_zoom").set_value("150%").run()
    app.button(key="metric_next").click().run()
    assert app.session_state["viewer_zoom"] == "150%"  # the reviewer's choice is kept


def test_fit_page_and_full_screen_show_the_page(app):
    app.radio(key="viewer_fit").set_value(extract_review.FIT_PAGE).run()
    assert not app.exception
    app.button(key="viewer_full_screen").click().run()
    assert not app.exception
    assert extract_review._image_width(extract_review.FIT_WIDTH, 600, 800) == "stretch"
    assert extract_review._image_width(extract_review.FIT_PAGE, 600, 800) == int(
        (extract_review.VIEW_HEIGHT - 16) * 600 / 800)


# --- metric details -------------------------------------------------------------------------

def test_metric_details_explain_definition_unit_column_and_formula(app):
    app.session_state["detail_mode"] = "metric"
    app.session_state["selected_metric"] = "roe_percent"
    app.run()
    about = next(e for e in app.expander if e.label == "About this metric")
    text = " ".join(m.value for m in about.markdown).replace("\\", "")
    assert "Return on Equity: net income" in text and "Return on Equity (ROE) (Reported)" in text
    assert "average_equity" in text and "a percentage" in text
    assert any("avg(total_equity_etb, total_equity_etb_prev)" in c.value for c in about.code)


# --- Bank Reports: work left ----------------------------------------------------------------

def test_bank_reports_lists_work_left_and_opens_a_metric(app):
    document_id = save(app)
    page = AppTest.from_function(bank_reports_page, args=(str(app.db_path.parent),), default_timeout=60)
    page.run()
    assert any(e.label.startswith("Work left (") for e in page.expander)
    assert any("Not reviewed yet" in m.value for m in page.markdown)
    page.button(key=f"open_{document_id}_not_reviewed_total_assets_etb").click().run()
    assert not page.exception and page.session_state["open_metric"] == "total_assets_etb"
    # The review screen then opens with that metric selected.
    app.session_state["open_metric"] = "total_assets_etb"
    app.run()
    assert app.session_state["selected_metric"] == "total_assets_etb"
    assert app.session_state["detail_mode"] == "metric"
