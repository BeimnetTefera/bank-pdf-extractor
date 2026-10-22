from pathlib import Path
from textwrap import dedent

import pytest

from src.config_loader import (
    ALLOWED_UNITS,
    ConfigError,
    default_data_dir,
    load_config,
    load_mappings,
    load_metrics,
    load_settings,
)

VALID_METRIC = """\
  - id: total_assets_etb
    shega_column: total_assets_etb
    display_name: Total assets
    definition: "Synthetic test definition."
    expected_unit: ETB
    active: true
"""

VALID_SETTINGS = """\
data_dir: null
max_upload_mb: 100
max_pages: 1000
default_reviewer: null
"""


def write(path: Path, text: str) -> Path:
    path.write_text(dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    write(tmp_path / "metrics.yaml", "metrics:\n" + VALID_METRIC)
    write(tmp_path / "mappings.yaml", "mappings: []\n")
    write(tmp_path / "settings.yaml", VALID_SETTINGS)
    return tmp_path


# --- the real repository configuration ----------------------------------------

def test_repository_config_loads():
    config = load_config()
    assert config.metrics, "metrics.yaml should define at least one metric"
    assert config.active_metrics(), "at least one metric should be active"
    assert all(m.expected_unit in ALLOWED_UNITS for m in config.metrics)


def test_repository_derived_ratios_are_inactive():
    config = load_config()
    for metric_id in ("atm_to_branch_ratio", "agents_to_branch_ratio", "deposit_per_branch_etb"):
        assert config.metric(metric_id).active is False


# --- metrics.yaml -------------------------------------------------------------

def test_valid_metrics_load(config_dir: Path):
    (metric,) = load_metrics(config_dir / "metrics.yaml")
    assert metric.id == "total_assets_etb"
    assert metric.expected_unit == "ETB"
    assert metric.active is True
    assert metric.notes is None


def test_missing_file_is_reported(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_metrics(tmp_path / "metrics.yaml")


def test_invalid_yaml_reports_line(tmp_path: Path):
    path = write(tmp_path / "metrics.yaml", "metrics:\n  - id: [unclosed\n")
    with pytest.raises(ConfigError, match=r"invalid YAML \(line \d+"):
        load_metrics(path)


def test_missing_top_level_key(tmp_path: Path):
    path = write(tmp_path / "metrics.yaml", "items: []\n")
    with pytest.raises(ConfigError, match="top-level 'metrics:'"):
        load_metrics(path)


def test_missing_required_field_names_the_field(tmp_path: Path):
    path = write(tmp_path / "metrics.yaml", "metrics:\n" + VALID_METRIC.replace("    active: true\n", ""))
    with pytest.raises(ConfigError, match=r"total_assets_etb.*missing required field\(s\): active"):
        load_metrics(path)


def test_unknown_field_is_rejected(tmp_path: Path):
    path = write(tmp_path / "metrics.yaml", "metrics:\n" + VALID_METRIC + "    activ: true\n")
    with pytest.raises(ConfigError, match=r"unknown field\(s\) \['activ'\]"):
        load_metrics(path)


def test_unsupported_unit_lists_allowed_units(tmp_path: Path):
    path = write(tmp_path / "metrics.yaml", "metrics:\n" + VALID_METRIC.replace("ETB\n", "USD\n"))
    with pytest.raises(ConfigError, match="expected_unit 'USD' is not supported. Use one of"):
        load_metrics(path)


def test_active_must_be_boolean(tmp_path: Path):
    path = write(tmp_path / "metrics.yaml", "metrics:\n" + VALID_METRIC.replace("active: true", 'active: "maybe"'))
    with pytest.raises(ConfigError, match="'active' must be true or false"):
        load_metrics(path)


def test_duplicate_metric_id_is_rejected(tmp_path: Path):
    second = VALID_METRIC.replace("shega_column: total_assets_etb", "shega_column: other_col")
    path = write(tmp_path / "metrics.yaml", "metrics:\n" + VALID_METRIC + second)
    with pytest.raises(ConfigError, match="duplicate id 'total_assets_etb'"):
        load_metrics(path)


def test_metric_id_must_be_snake_case(tmp_path: Path):
    path = write(tmp_path / "metrics.yaml", "metrics:\n" + VALID_METRIC.replace("id: total_assets_etb", "id: TotalAssets"))
    with pytest.raises(ConfigError, match="lowercase snake_case"):
        load_metrics(path)


def test_all_problems_reported_together(tmp_path: Path):
    bad = VALID_METRIC.replace("ETB\n", "USD\n").replace("active: true", "active: 1")
    path = write(tmp_path / "metrics.yaml", "metrics:\n" + bad)
    with pytest.raises(ConfigError, match=r"2 problem\(s\)"):
        load_metrics(path)


# --- mappings.yaml ------------------------------------------------------------

def test_empty_mappings_load(config_dir: Path):
    assert load_mappings(config_dir / "mappings.yaml", {"total_assets_etb"}) == ()


def test_valid_mapping_loads(tmp_path: Path):
    path = write(tmp_path / "mappings.yaml", """\
        mappings:
          - metric_id: total_assets_etb
            aliases: ["Total assets", "Total Assets"]
        """)
    (mapping,) = load_mappings(path, {"total_assets_etb"})
    assert mapping.aliases == ("Total assets", "Total Assets")


def test_mapping_to_unknown_metric_is_rejected(tmp_path: Path):
    path = write(tmp_path / "mappings.yaml", """\
        mappings:
          - metric_id: total_asets
            aliases: ["Total assets"]
        """)
    with pytest.raises(ConfigError, match="'total_asets' is not defined in metrics.yaml"):
        load_mappings(path, {"total_assets_etb"})


def test_mapping_requires_non_empty_aliases(tmp_path: Path):
    path = write(tmp_path / "mappings.yaml", """\
        mappings:
          - metric_id: total_assets_etb
            aliases: []
        """)
    with pytest.raises(ConfigError, match="non-empty list"):
        load_mappings(path, {"total_assets_etb"})


def test_same_alias_under_two_metrics_is_allowed(tmp_path: Path):
    # The mapper (Phase 3) flags such labels as ambiguous; config must not hide them.
    path = write(tmp_path / "mappings.yaml", """\
        mappings:
          - metric_id: a
            aliases: ["Deposits"]
          - metric_id: b
            aliases: ["Deposits"]
        """)
    assert len(load_mappings(path, {"a", "b"})) == 2


# --- settings.yaml ------------------------------------------------------------

def test_default_data_dir_is_outside_project(config_dir: Path, tmp_path: Path):
    settings = load_settings(config_dir / "settings.yaml", project_root=tmp_path)
    assert settings.data_dir == default_data_dir()
    assert settings.database_path == settings.data_dir / "app.db"
    assert settings.default_reviewer  # falls back to the OS user name


def test_relative_data_dir_resolves_from_project_root(tmp_path: Path):
    path = write(tmp_path / "settings.yaml", VALID_SETTINGS.replace("data_dir: null", "data_dir: data"))
    settings = load_settings(path, project_root=tmp_path)
    assert settings.data_dir == (tmp_path / "data").resolve()


def test_explicit_reviewer_is_used(tmp_path: Path):
    path = write(tmp_path / "settings.yaml", VALID_SETTINGS.replace("default_reviewer: null", "default_reviewer: Analyst A"))
    assert load_settings(path).default_reviewer == "Analyst A"


@pytest.mark.parametrize("bad_value", ["0", "-5", "ten", "true"])
def test_upload_limit_must_be_positive_int(tmp_path: Path, bad_value: str):
    path = write(tmp_path / "settings.yaml", VALID_SETTINGS.replace("max_upload_mb: 100", f"max_upload_mb: {bad_value}"))
    with pytest.raises(ConfigError, match="'max_upload_mb' must be a whole number"):
        load_settings(path)


def test_unknown_setting_is_rejected(tmp_path: Path):
    path = write(tmp_path / "settings.yaml", VALID_SETTINGS + "max_page: 5\n")
    with pytest.raises(ConfigError, match=r"unknown setting\(s\) \['max_page'\]"):
        load_settings(path)
