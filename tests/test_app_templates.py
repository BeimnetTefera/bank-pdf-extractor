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
    from src.ui import pages

    pages.templates(load_config(Path(config_dir), PROJECT_ROOT))


def test_templates_shows_each_metrics_meaning(config_dir):
    at = AppTest.from_function(templates_page, args=(str(config_dir),), default_timeout=60)
    at.run()
    assert not at.exception
    assert len(at.tabs) == 2  # Metrics and Calculated columns; Wording and Excel layout are gone
    table = at.dataframe[0].value
    assert "Meaning" in table.columns and table["Meaning"].iloc[0].startswith("Sum of all assets")
