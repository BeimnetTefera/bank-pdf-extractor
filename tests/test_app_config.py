"""App-level settings: Streamlit config file and page setup (no PDFs, temporary data folder)."""

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
import streamlit as st
from streamlit import config as st_config
from streamlit.testing.v1 import AppTest

import src.config_loader

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app.py"
CONFIG_TOML = ROOT / ".streamlit" / "config.toml"


def _flatten(table: dict, prefix: str = "") -> dict[str, object]:
    """Turn {"client": {"toolbarMode": "viewer"}} into {"client.toolbarMode": "viewer"}."""
    options: dict[str, object] = {}
    for key, value in table.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and not name.startswith("theme"):
            options.update(_flatten(value, f"{name}."))
        else:
            options[name] = value
    return options


@pytest.fixture
def streamlit_options() -> dict[str, object]:
    return _flatten(tomllib.loads(CONFIG_TOML.read_text(encoding="utf-8")))


def test_config_hides_developer_options(streamlit_options):
    # "viewer" turns off the "c" (Clear caches) keyboard shortcut as well as the developer menu items.
    assert streamlit_options["client.toolbarMode"] == "viewer"


def test_config_keeps_privacy_and_upload_settings(streamlit_options):
    assert streamlit_options["browser.gatherUsageStats"] is False
    assert streamlit_options["server.maxUploadSize"] == 100


def test_config_uses_only_known_streamlit_options(streamlit_options):
    known = st_config.get_config_options()
    unknown = [name for name in streamlit_options if not name.startswith("theme") and name not in known]
    assert unknown == []


@pytest.fixture
def app(monkeypatch, tmp_path):
    real_load = src.config_loader.load_config

    def load_with_temp_data(*args, **kwargs):
        config = real_load(*args, **kwargs)
        return replace(config, settings=replace(config.settings, data_dir=tmp_path / "appdata"))

    monkeypatch.setattr(src.config_loader, "load_config", load_with_temp_data)
    page_configs: list[dict] = []
    real_set_page_config = st.set_page_config

    def recording_set_page_config(*args, **kwargs):
        page_configs.append(kwargs)
        return real_set_page_config(*args, **kwargs)

    monkeypatch.setattr(st, "set_page_config", recording_set_page_config)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.page_configs = page_configs
    return at


def test_app_renders_without_exception(app):
    app.run()
    assert not app.exception


def test_sidebar_starts_expanded(app):
    app.run()
    assert app.page_configs and all(c.get("initial_sidebar_state") == "expanded" for c in app.page_configs)


def test_menu_is_the_same_after_a_rerun(app):
    app.run()
    first = [e.proto.label for e in app.get("page_link")]
    app.run()
    assert not app.exception
    assert first[:7] == ["Reports", "Extract", "Review", "Calculations", "Sector data", "Templates", "Settings"]
    assert [e.proto.label for e in app.get("page_link")] == first
