"""Evidence page and Evidence summary through Streamlit (synthetic PDF, temporary data folder)."""

from contextlib import closing
from pathlib import Path

from streamlit.testing.v1 import AppTest

from src import review_service
from src.database import connect
from src.project_service import load_candidates
from tests.test_app_review import (  # noqa: F401 (app is a fixture)
    APP,
    TOTAL_ASSETS_KEY,
    app,
    save,
    select_total_assets,
)


def evidence_screen(data_dir: str, summary: bool) -> None:
    """One evidence screen on its own, with a given data folder (run by AppTest.from_function)."""
    from dataclasses import replace
    from pathlib import Path

    from src.config_loader import load_config
    from src.ui import evidence_page

    config = load_config()
    config = replace(config, settings=replace(config.settings, data_dir=Path(data_dir)))
    (evidence_page.render_summary if summary else evidence_page.render_evidence)(config)


def screen(app_test: AppTest, summary: bool = False, **query) -> AppTest:
    at = AppTest.from_function(evidence_screen, args=(str(app_test.db_path.parent), summary), default_timeout=60)
    for key, value in query.items():
        at.query_params[key] = value
    at.session_state["reviewer"] = "Analyst A"
    return at.run()


def approve_total_assets(app_test: AppTest, document_id: str) -> None:
    with closing(connect(app_test.db_path)) as conn:
        candidate = next(c for c in load_candidates(conn, document_id, "total_assets_etb") if c.key == TOTAL_ASSETS_KEY)
        review_service.approve(conn, candidate.id, "Analyst A", note="Statement of financial position")


def test_evidence_page_shows_value_source_note_and_history(app):
    document_id = save(app)
    approve_total_assets(app, document_id)
    at = screen(app, report=document_id, metric="total_assets_etb")
    assert not at.exception
    text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "1,234,567,000 ETB" in text and "PDF page 1" in text
    assert "Approved by **Analyst A**" in text
    assert any("Statement of financial position" in t.value for t in at.text)  # the note in full
    assert "Approved" in text and "PDF figure" in text  # status and source badges
    history = at.dataframe[-1].value
    assert list(history["Action"])[-1] == "Approved the extracted figure"
    assert "Close-up of the line" in text  # page image and close-up drawn
    assert not any("cannot be shown" in w.value for w in at.warning)


def test_unknown_report_in_the_address_is_explained(app):
    save(app)
    at = screen(app, report="no-such-report", metric="total_assets_etb")
    assert not at.exception
    assert any("not in the database" in e.value for e in at.error)


def test_open_in_viewer_selects_the_metric_and_page(app):
    document_id = save(app)
    at = screen(app, report=document_id, metric="total_assets_etb")
    next(b for b in at.button if b.label == "Open in viewer at this page").click().run()
    assert not at.exception
    assert at.session_state["doc"]["document_id"] == document_id
    assert (at.session_state["selected_metric"], at.session_state["detail_mode"]) == ("total_assets_etb", "metric")
    assert at.session_state["viewer_page"] == 1 and at.session_state["selected_key"] == TOTAL_ASSETS_KEY


def test_summary_lists_metrics_with_links_and_downloads(app):
    document_id = save(app)
    approve_total_assets(app, document_id)
    at = screen(app, summary=True, report=document_id)
    assert not at.exception
    table = at.dataframe[0].value
    row = table[table["Metric"] == "Total assets"].iloc[0]
    assert (row["Status"], row["Source"]) == ("Approved", "PDF figure")
    assert row["Evidence"] == f"http://localhost:8501/evidence?report={document_id}&metric=total_assets_etb"
    next(b for b in at.button if b.label == "Prepare evidence PDF").click().run()
    assert not at.exception
    assert at.session_state["_evidence_pdf_ready"][1].startswith(b"%PDF")
    assert any(b.label == "Download evidence PDF" for b in at.get("download_button"))


def test_address_form_with_page_parameter_opens_the_evidence_page(app):
    document_id = save(app)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.query_params["page"] = "evidence"
    at.query_params["report"] = document_id
    at.query_params["metric"] = "total_assets_etb"
    at.run()
    assert not at.exception
    assert any(m.value == "## Evidence" for m in at.markdown)


def test_metric_details_link_to_the_evidence_page(app):
    save(app)
    select_total_assets(app)
    links = [p for p in app.get("page_link") if "evidence" in str(p.proto)]
    assert links, "Metric Details should offer View evidence page"
