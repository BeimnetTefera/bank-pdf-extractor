"""Templates page, Wording tab: add and remove wording in a copy of the config (D-065)."""

import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.config_loader import CONFIG_DIR, PROJECT_ROOT, load_config


def templates_screen(config_dir: str) -> None:
    """The Templates page on its own, reading a given config folder (run by AppTest.from_function)."""
    from pathlib import Path

    from src.config_loader import PROJECT_ROOT, load_config
    from src.ui import pages

    pages.templates(load_config(Path(config_dir), PROJECT_ROOT))


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
