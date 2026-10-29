"""The wording editor (D-065; parked off Templates by the redesign, docs/redesign-backlog.md) and the
meaning editor of Templates (D-093), on a copy of the config."""

import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.config_loader import CONFIG_DIR, PROJECT_ROOT, load_config


def templates_screen(config_dir: str) -> None:
    """The wording editor on its own, reading a given config folder (run by AppTest.from_function)."""
    from pathlib import Path

    from src.config_loader import PROJECT_ROOT, load_config
    from src.ui import pages

    pages.wording_editor(load_config(Path(config_dir), PROJECT_ROOT))


@pytest.fixture
def config_dir(tmp_path) -> Path:
    return Path(shutil.copytree(CONFIG_DIR, tmp_path / "config"))


@pytest.fixture
def app(config_dir) -> AppTest:
    at = AppTest.from_function(templates_screen, args=(str(config_dir),), default_timeout=60)
    at.session_state["reviewer"] = "Analyst A"
    at.run()
    assert not at.exception
    at.selectbox(key="wording_metric").set_value("total_deposits_etb").run()
    return at


def wording_of(config_dir: Path, metric_id: str) -> tuple[str, ...]:
    mapping = next(m for m in load_config(config_dir, PROJECT_ROOT).mappings if m.metric_id == metric_id)
    return mapping.aliases + mapping.text_after + mapping.text_before


def test_wording_tab_lists_the_metrics_wording(app):
    table = next(d.value for d in app.dataframe if "Wording" in d.value.columns)
    assert "Deposits from customers" in list(table["Wording"])
    assert "Table label" in list(table["Kind"])


def test_add_wording_writes_it_with_the_note(app, config_dir):
    app.text_input[0].input("Total customer deposits")
    app.text_input[1].input("Test Bank p12")
    app.button[0].click().run()  # the form's Add button
    assert not app.exception
    assert "Total customer deposits" in wording_of(config_dir, "total_deposits_etb")
    text = (config_dir / "mappings.yaml").read_text(encoding="utf-8")
    assert '- "Total customer deposits"  # Test Bank p12; added by Analyst A' in text
    assert any("Added" in s.value for s in app.success)


def test_refused_wording_shows_why_and_changes_nothing(app, config_dir):
    before = (config_dir / "mappings.yaml").read_text(encoding="utf-8")
    app.text_input[0].input("Total assets")
    app.button[0].click().run()
    assert any("already means total_assets_etb" in e.value for e in app.error)
    assert (config_dir / "mappings.yaml").read_text(encoding="utf-8") == before


def test_remove_wording(app, config_dir):
    app.selectbox(key="wording_remove").set_value(
        next(o for o in app.selectbox(key="wording_remove").options if "Customer deposits" in o)).run()
    app.button(key="remove_wording").click().run()
    assert not app.exception
    assert "Customer deposits" not in wording_of(config_dir, "total_deposits_etb")


def test_a_meaning_is_edited_signed_and_read_back(config_dir):
    from src.metric_editor import MetricEditError, set_definition

    path = config_dir / "metrics.yaml"
    set_definition(path, "total_assets_etb", "Everything the bank owns, in full Birr.", "Analyst A")
    metric = next(m for m in load_config(config_dir, PROJECT_ROOT).metrics if m.id == "total_assets_etb")
    assert metric.definition == "Everything the bank owns, in full Birr."
    assert "# edited by Analyst A" in path.read_text(encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(MetricEditError, match="short sentence"):
        set_definition(path, "total_assets_etb", "Assets", "Analyst A")
    with pytest.raises(MetricEditError, match="your name"):
        set_definition(path, "total_assets_etb", "Everything the bank owns.", " ")
    assert path.read_text(encoding="utf-8") == before


def templates_page(config_dir: str) -> None:
    from pathlib import Path

    from src.config_loader import PROJECT_ROOT, load_config
    from src.ui import templates_page

    templates_page.render(load_config(Path(config_dir), PROJECT_ROOT))


@pytest.fixture
def page(config_dir) -> AppTest:
    at = AppTest.from_function(templates_page, args=(str(config_dir),), default_timeout=60)
    at.session_state["reviewer"] = "Analyst A"
    at.run()
    assert not at.exception
    return at


def html_text(at: AppTest) -> str:
    return "".join(h.proto.body for h in at.get("html"))


def metric_of(config_dir: Path, metric_id: str):
    return next(m for m in load_config(config_dir, PROJECT_ROOT).metrics if m.id == metric_id)


NEW_MEANING = "Everything the bank owns at year end, in full Birr."


def edit(at: AppTest, metric_id: str, text: str) -> AppTest:
    at.button(key=f"tpl_edit_{metric_id}").click().run()
    at.text_area(key="tpl_draft").input(text)
    return at.button(key="tpl_keep").click().run()


def test_templates_lists_metrics_with_meaning_and_not_mapped(page):
    assert len(page.button(key="tpl_tab_metrics_on").label) and page.button(key="tpl_tab_calc")
    text = html_text(page)
    assert "Sum of all assets owned or controlled" in text
    assert "Not mapped" in text  # Total assets (previous year) has no Excel column
    assert "Printed in the report" in text and "Showing 1–15 of 49" in text and "Page 1 of 4" in text
    assert page.button(key="tpl_save").disabled and page.button(key="tpl_prev").disabled


def test_an_edit_is_kept_across_pages_and_saved_signed(page, config_dir):
    edit(page, "total_assets_etb", NEW_MEANING)
    assert "Edited, not saved" in html_text(page)
    assert not page.button(key="tpl_save").disabled and page.button(key="tpl_save").label == "Save 1 change"
    assert metric_of(config_dir, "total_assets_etb").definition.startswith("Sum of all assets")  # nothing written yet
    page.button(key="tpl_next").click().run()
    page.button(key="tpl_prev").click().run()
    assert NEW_MEANING in html_text(page)
    page.button(key="tpl_save").click().run()
    assert not page.exception
    assert metric_of(config_dir, "total_assets_etb").definition == NEW_MEANING
    assert "# edited by Analyst A" in (config_dir / "metrics.yaml").read_text(encoding="utf-8")
    assert "Saved 1 meaning" in html_text(page) and page.button(key="tpl_save").disabled


def test_cancel_and_undo_leave_the_meaning_as_saved(page):
    page.button(key="tpl_edit_total_assets_etb").click().run()
    page.text_area(key="tpl_draft").input(NEW_MEANING)
    page.button(key="tpl_cancel").click().run()
    assert page.session_state["tpl_edits"] == {}
    edit(page, "total_assets_etb", NEW_MEANING)
    page.button(key="tpl_undo_total_assets_etb").click().run()
    assert page.session_state["tpl_edits"] == {} and page.button(key="tpl_save").disabled


def test_a_short_meaning_is_refused_before_saving(page):
    edit(page, "total_assets_etb", "Assets")
    assert "at least a short sentence" in html_text(page)
    assert page.session_state["tpl_edits"] == {}


def test_moving_to_another_row_keeps_the_draft(page):
    page.button(key="tpl_edit_total_assets_etb").click().run()
    page.text_area(key="tpl_draft").input(NEW_MEANING)
    page.button(key="tpl_edit_total_equity_etb").click().run()
    assert page.session_state["tpl_edits"] == {"total_assets_etb": NEW_MEANING}


def test_a_failed_save_says_why_and_keeps_the_edit(page, config_dir):
    before = (config_dir / "metrics.yaml").read_text(encoding="utf-8")
    page.session_state["reviewer"] = ""
    edit(page, "total_assets_etb", NEW_MEANING)
    page.button(key="tpl_save").click().run()
    text = html_text(page)
    assert "Nothing was saved" in text and "Enter your name on Settings first" in text
    assert page.session_state["tpl_edits"] == {"total_assets_etb": NEW_MEANING}
    assert (config_dir / "metrics.yaml").read_text(encoding="utf-8") == before
    page.session_state["reviewer"] = "Analyst A"  # retry once the name is set
    page.button(key="tpl_save").click().run()
    assert metric_of(config_dir, "total_assets_etb").definition == NEW_MEANING


def test_search_reads_names_and_meanings_and_filters_narrow(page):
    page.text_input(key="tpl_query").input("nostro").run()
    text = html_text(page)
    assert "2 of 49 metrics match" in text  # foreign assets and deposits with foreign banks, by meaning
    page.button(key="tpl_clear").click().run()
    page.selectbox(key="tpl_mapping").set_value("unmapped").run()
    assert "10 of 49 metrics match" in html_text(page)
    page.selectbox(key="tpl_status").set_value("inactive").run()
    assert "8 of 49 metrics match" in html_text(page)


def test_search_on_a_later_page_goes_back_to_page_one(page):
    page.button(key="tpl_next").click().run()
    page.text_input(key="tpl_query").input("deposit").run()
    assert page.session_state["tpl_page"] == 0


def test_calculated_columns_tab(page):
    page.button(key="tpl_tab_calc").click().run()
    text = html_text(page)
    assert "Profit Before Tax (Calculated)" in text and "Showing 1–15 of 26" in text
    assert "Profit before tax" in text  # compared with, by the metric's name
    assert not page.button(key="tpl_tab_calc_on").disabled


def test_details_panel_shows_saved_version_of_an_edited_meaning(page):
    edit(page, "total_assets_etb", NEW_MEANING)
    page.button(key="tpl_open_total_assets_etb").click().run()
    text = html_text(page)
    assert "Saved version (before your edit)" in text and "config/metrics.yaml" in text
    page.button(key="tpl_detail_close").click().run()
    assert "Saved version" not in html_text(page)


def test_filter_and_paging_helpers():
    from src.ui.templates_page import filter_metrics, page_slice, save_notice

    assert page_slice(49, 9) == (45, 49, 3, 4) and page_slice(0, 2) == (0, 0, 0, 1)
    config = load_config(CONFIG_DIR, PROJECT_ROOT)
    assert len(filter_metrics(config.metrics, {}, kind="formula")) == 5
    edited = {"branches": "A new meaning that says zebra."}
    assert [m.id for m in filter_metrics(config.metrics, edited, "zebra")] == ["branches"]
    notice = save_notice({"a": "A", "b": "B"}, ["a"], [("b", "Not saved: x.")], "Analyst A")
    assert notice["title"] == "1 of 2 not saved" and notice["lines"][0] == "Saved 1: A."
