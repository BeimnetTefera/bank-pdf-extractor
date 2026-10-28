"""The redesigned screens that are new (D-092): the search box, the Review page and New report.
Synthetic PDF, temporary data folder."""

from contextlib import closing

from streamlit.testing.v1 import AppTest

from src import review_service, search
from src.config_loader import load_config
from src.database import connect
from src.document_manager import compute_sha256
from src.project_service import list_documents
from tests.conftest import html_text
from tests.test_app_review import BANK, app, save  # noqa: F401 (app is a fixture)
from tests.test_app_smoke import PDF


def review_screen(data_dir: str, document_id: str) -> None:
    """The Review page on its own, with a saved report open."""
    from dataclasses import replace
    from pathlib import Path

    import streamlit as st

    from src.config_loader import load_config
    from src.ui import extract_review, review_page

    config = load_config()
    config = replace(config, settings=replace(config.settings, data_dir=Path(data_dir)))
    if "doc" not in st.session_state:
        extract_review.open_saved(config, document_id)
    review_page.render(config)


def new_report_screen(data_dir: str) -> None:
    from dataclasses import replace
    from pathlib import Path

    import streamlit as st

    from src.config_loader import load_config
    from src.ui import new_report

    config = load_config()
    config = replace(config, settings=replace(config.settings, data_dir=Path(data_dir)))
    new_report.render(config, open_review=lambda: st.session_state.update(review_opened=True))


def test_search_finds_reports_metrics_and_printed_figures(app):
    document_id = save(app)
    config = load_config()
    with closing(connect(app.db_path)) as conn:
        assert [h.kind for h in search.search(conn, config, "awash")] == ["report"]
        metric = search.search(conn, config, "total assets")
        assert metric[0].kind == "metric" and metric[0].metric_id == "total_assets_etb"
        figure = [h for h in search.search(conn, config, "1,234,567") if h.kind == "figure"]
        assert figure and figure[0].document_id == document_id and figure[0].page == 1
        assert search.search(conn, config, "  ") == []


def test_review_page_steps_to_a_metric_and_approves_it(app):
    document_id = save(app)
    page = AppTest.from_function(review_screen, args=(str(app.db_path.parent), document_id), default_timeout=60)
    page.session_state["reviewer"] = "Analyst A"
    page.run()
    assert not page.exception
    assert f"{BANK} · 2025/26" in html_text(page) and "approved" in html_text(page)
    page.button(key="metric_next").click().run()  # Start: the first metric to review
    assert not page.exception and page.session_state["selected_metric"]
    page.button(key="q_total_assets_etb").click().run()  # from the queue (Financial performance)
    assert not page.exception and page.session_state["selected_metric"] == "total_assets_etb"
    approve = page.button(key="approve_main")
    assert approve.label == "Approve 1,234,567"
    approve.click().run()
    assert not page.exception
    with closing(connect(app.db_path)) as conn:
        state = review_service.review_state(conn, document_id, "total_assets_etb")
    assert state.status == "approved" and state.accepted_decision.reviewer == "Analyst A"


def test_new_report_reads_saves_and_opens_the_review(tmp_path):
    data_dir = tmp_path / "appdata"
    page = AppTest.from_function(new_report_screen, args=(str(data_dir),), default_timeout=60)
    page.session_state["reviewer"] = "Analyst A"
    page.session_state["nr_upload_data"] = {"name": "synthetic.pdf", "bytes": PDF, "sha": compute_sha256(PDF),
                                            "suggested": None, "pages": 2}  # as after an upload
    page.run()
    assert not page.exception
    page.selectbox(key="nr_bank").set_value(BANK).run()
    page.text_input(key="nr_year").input("2025/26").run()
    page.button(key="nr_read").click().run()
    assert not page.exception
    assert page.session_state["review_opened"]
    with closing(connect(data_dir / "app.db")) as conn:
        (doc,) = list_documents(conn)
    assert (doc["bank_name"], doc["reporting_period"]) == (BANK, "2025/26")


def test_review_queue_groups_fold_and_open(app):
    document_id = save(app)
    page = AppTest.from_function(review_screen, args=(str(app.db_path.parent), document_id), default_timeout=60)
    page.session_state["reviewer"] = "Analyst A"
    page.session_state["review_filter"] = "all"
    page.run()
    assert not page.exception
    groups = [b for b in page.button if (b.key or "").startswith("rv_group_")]
    assert groups and groups[0].label == "Network and customers"
    before = len([b for b in page.button if (b.key or "").startswith("q_")])
    groups[0].click().run()  # fold the first group: its metrics leave the list
    assert len([b for b in page.button if (b.key or "").startswith("q_")]) < before
    page.button(key=groups[0].key).click().run()  # open it again
    assert len([b for b in page.button if (b.key or "").startswith("q_")]) == before and not page.exception


def test_review_keeps_working_when_the_extract_fields_are_forgotten(app):
    """Streamlit drops a widget's value when its page is not shown; Review reads the saved report instead."""
    document_id = save(app)
    page = AppTest.from_function(review_screen, args=(str(app.db_path.parent), document_id), default_timeout=60)
    page.session_state["reviewer"] = "Analyst A"
    page.run()
    for key in ("bank", "period", "metric_selection"):
        del page.session_state[key]  # as after a few clicks on Review
    page.button(key="metric_next").click().run()
    assert not page.exception
    assert page.session_state["metric_selection"] and page.session_state["bank"] == BANK
