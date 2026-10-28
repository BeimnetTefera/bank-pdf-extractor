"""The Extract page, option F refined (D-094): the page opens by itself, a paged drawer of metrics.
Synthetic PDF, temporary data folder."""

from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from src.ui.extract_page import ROWS_PER_PAGE, metric_status
from tests.conftest import html_text
from tests.test_app_review import app, save  # noqa: F401 (app is a fixture)

PICKED, BACKUP = ("table", 0, 3, 0), ("table", 4, 1, 0)


def extract_screen(data_dir: str, document_id: str) -> None:
    """The Extract page on its own, with a saved report open."""
    from dataclasses import replace
    from pathlib import Path

    import streamlit as st

    from src.config_loader import load_config
    from src.ui import extract_page, extract_review

    config = load_config()
    config = replace(config, settings=replace(config.settings, data_dir=Path(data_dir)))
    if "doc" not in st.session_state:
        extract_review.open_saved(config, document_id)
    extract_page.render(config, open_review=lambda: st.session_state.update(review_opened=True),
                        open_reports=lambda: None)


def open_extract(app) -> AppTest:
    document_id = save(app)
    page = AppTest.from_function(extract_screen, args=(str(app.db_path.parent), document_id), default_timeout=60)
    page.session_state["reviewer"] = "Analyst A"
    page.run()
    assert not page.exception
    return page


def rows(page: AppTest) -> list[str]:
    """The metrics listed in the drawer: their row buttons are keyed ex_<metric id>."""
    metric_ids = set(page.session_state["metric_selection"])
    return [b.key[3:] for b in page.button if (b.key or "").startswith("ex_") and b.key[3:] in metric_ids]


def test_the_page_opens_by_itself_at_the_first_metric(app):
    page = open_extract(app)
    text = html_text(page)
    assert "Extract" in text and "metrics found" in text
    assert page.session_state["selected_metric"] in page.session_state["metric_selection"]  # picked without a click
    assert 0 < len(rows(page)) <= ROWS_PER_PAGE  # the drawer pages, it never grows


def test_clicking_a_row_opens_its_page_with_the_figure_boxed(app):
    page = open_extract(app)
    page.session_state["ex_filter"] = "all"
    page.run()
    page.button(key="ex_total_assets_etb").click().run()
    assert not page.exception
    assert page.session_state["selected_metric"] == "total_assets_etb"
    assert page.session_state["viewer_page"] == 1
    assert "Opened for <b>Total assets</b>" in "".join(h.proto.body for h in page.get("html"))
    page.button(key="ex_open_review").click().run()
    assert page.session_state["review_opened"]


def test_the_not_found_filter_lists_only_metrics_without_a_figure(app):
    page = open_extract(app)
    page.session_state["ex_filter"] = "not_found"
    page.run()
    assert not page.exception and rows(page)
    assert "total_assets_etb" not in rows(page)
    assert page.button(key="ex_page_next")  # more not-found metrics than one page holds


def test_the_checks_tab_lists_the_cross_checks(app):
    page = open_extract(app)
    page.session_state["ex_tab"] = "checks"
    page.run()
    assert not page.exception and "Checks point out where to look" in html_text(page)


def finding(rule_id: str, severity: str = "warning", keys=(PICKED,), message: str = "PDF page 2: x"):
    return SimpleNamespace(rule_id=rule_id, severity=severity, candidate_keys=keys, message=message)


def result(*findings, found: bool = True):
    primary = SimpleNamespace(mapped=SimpleNamespace(key=PICKED)) if found else None
    return SimpleNamespace(primary=primary, findings=list(findings))


def test_status_comes_from_the_value_the_app_picked_only():
    assert metric_status(result(finding("VALUE-PARSE", keys=(BACKUP,))), None).label == "Found"  # a backup reading
    assert metric_status(result(finding("PERIOD-UNKNOWN", "info")), None).label == "Found"  # a note, shown on Review
    dash = metric_status(result(finding("VALUE-PARSE", message="PDF page 2: Printed as a dash, nil")), None)
    assert (dash.group, dash.label) == ("needs", "Printed as a dash")
    conflict = metric_status(result(finding("CONFLICT", keys=(PICKED, BACKUP))), None)
    assert conflict.label == "2 values disagree"
    percent = metric_status(result(finding("VALUE-UNIT", message="Expected an amount in Birr, but the figure is "
                                                               "printed as a percentage.")), None)
    assert (percent.label, percent.kind) == ("Percentage, not Birr", "bad")
    assert metric_status(result(found=False), None).group == "not_found"
    assert metric_status(result(finding("CONFLICT")), SimpleNamespace(status="approved")).label == "Approved"


def test_the_search_box_lists_the_pages_with_a_word_and_opens_one(app):
    page = open_extract(app)
    page.text_input(key="ex_find").input("total assets").run()
    assert not page.exception
    hits = [b for b in page.button if (b.key or "").startswith("ex_hit_")]
    assert [b.label for b in hits] == ["1", "2"]  # the synthetic report prints it on both pages
    hits[1].click().run()
    assert page.session_state["viewer_page"] == 2 and page.session_state["ex_focus"] == "find"
    page.text_input(key="ex_find").input("no such words").run()
    assert "is not on any page of this report" in html_text(page)


def test_a_page_number_can_be_typed_and_the_whole_page_opened_large(app):
    page = open_extract(app)
    page.number_input(key="viewer_page").set_value(1).run()
    assert not page.exception and page.session_state["viewer_page"] == 1
    page.button(key="ex_full").click().run()
    assert not page.exception


def test_a_report_opened_from_extracts_open_reports_comes_back_to_extract(app):
    document_id = save(app)
    app.session_state["workbench"] = False  # the redesigned Extract page
    del app.session_state["doc"]
    app.run()
    next(b for b in app.button if b.label == "Open Reports").click().run()
    next(b for b in app.button if b.key == f"open_{document_id}").click().run()
    assert not app.exception
    assert any(b.key == "ex_go_review" for b in app.button)  # on Extract, not Review
    assert "reports_from_extract" not in app.session_state
