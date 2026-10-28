from pathlib import Path
from textwrap import dedent

import pytest

from src.config_loader import (
    ALLOWED_UNITS,
    ConfigError,
    default_data_dir,
    load_banks,
    load_checks,
    load_config,
    load_export_template,
    load_learned_aliases,
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


VALID_TEMPLATE = """\
bank_column: bank
year_column: fiscal_year
columns: [bank, fiscal_year, Total Assets, Profit Margin]
"""


def write(path: Path, text: str) -> Path:
    path.write_text(dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    write(tmp_path / "metrics.yaml", "metrics:\n" + VALID_METRIC)
    write(tmp_path / "mappings.yaml", "mappings: []\n")
    write(tmp_path / "settings.yaml", VALID_SETTINGS)
    write(tmp_path / "banks.yaml", "banks:\n  - Synthetic Bank\n")
    write(tmp_path / "export_template.yaml", VALID_TEMPLATE)
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


def test_repository_bank_list_loads():
    banks = load_config().banks
    assert "Bank of Abyssinia" in banks and len(banks) == len(set(banks))


# --- banks.yaml ---------------------------------------------------------------

def test_valid_banks_load(config_dir: Path):
    assert load_config(config_dir, config_dir).banks == ("Synthetic Bank",)


def test_bank_problems_are_reported_together(tmp_path: Path):
    path = write(tmp_path / "banks.yaml", "banks:\n  - Synthetic Bank\n  - synthetic bank \n  - ''\n")
    with pytest.raises(ConfigError, match=r"2 problem\(s\)") as error:
        load_banks(path)
    assert "more than once" in str(error.value) and "non-empty name" in str(error.value)


def test_empty_bank_list_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="empty"):
        load_banks(write(tmp_path / "banks.yaml", "banks: []\n"))


# --- export_template.yaml and export_column ------------------------------------

def test_repository_export_columns_are_in_the_template():
    config = load_config()
    mapped = {m.id: m.export_column for m in config.metrics if m.export_column}
    assert mapped["total_assets_etb"] == "Total Assets"
    assert set(mapped.values()) <= set(config.export_template.columns)
    assert config.export_template.columns[:2] == ("bank", "fiscal_year")


def test_valid_template_loads(config_dir: Path):
    template = load_export_template(config_dir / "export_template.yaml")
    assert template.columns == ("bank", "fiscal_year", "Total Assets", "Profit Margin")


def test_template_problems_are_reported_together(tmp_path: Path):
    path = write(tmp_path / "export_template.yaml",
                 "bank_column: name\nyear_column: fiscal_year\ncolumns: [fiscal_year, A, A]\n")
    with pytest.raises(ConfigError, match=r"2 problem\(s\)") as error:
        load_export_template(path)
    assert "more than once" in str(error.value) and "bank_column 'name'" in str(error.value)


def test_export_column_must_be_in_the_template(config_dir: Path):
    write(config_dir / "metrics.yaml", "metrics:\n" + VALID_METRIC + '    export_column: "Total assets"\n')
    with pytest.raises(ConfigError, match="'Total assets' is not a column"):
        load_config(config_dir, config_dir)
    write(config_dir / "metrics.yaml", "metrics:\n" + VALID_METRIC + '    export_column: "Total Assets"\n')
    assert load_config(config_dir, config_dir).metrics[0].export_column == "Total Assets"


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


def test_mapping_with_sentence_phrases_only_loads(tmp_path: Path):
    path = write(tmp_path / "mappings.yaml", """\
        mappings:
          - metric_id: branches
            text_after: ["branches"]
          - metric_id: roe_percent
            text_before: ["Return on equity", "ROE"]
        """)
    branches, roe = load_mappings(path, {"branches", "roe_percent"})
    assert branches.aliases == () and branches.text_after == ("branches",)
    assert roe.text_before == ("Return on equity", "ROE")


def test_mapping_needs_at_least_one_phrase_list(tmp_path: Path):
    path = write(tmp_path / "mappings.yaml", "mappings:\n  - metric_id: branches\n")
    with pytest.raises(ConfigError, match="add at least one of: aliases, text_after, text_before"):
        load_mappings(path, {"branches"})


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


# --- learned_aliases.yaml (D-043) and checks.yaml (D-045) ---------------------

LEARNED = """\
learned:
  - bank: Synthetic Bank
    metric_id: total_assets_etb
    alias: "Total Assets of the Bank"
    seen_in: "Synthetic Bank 2025/26, PDF page 4"
    added_by: Analyst A
    added_at: "2026-10-23T08:00:00+00:00"
"""


def test_learned_and_check_files_are_optional(config_dir: Path):
    config = load_config(config_dir, config_dir)
    assert config.learned_aliases == () and config.checks == ()
    assert config.learned_aliases_path == config_dir / "learned_aliases.yaml"


def has_alias(mappings, alias: str) -> bool:
    return any(alias in m.aliases for m in mappings)


def test_learned_aliases_apply_to_their_own_bank_only(config_dir: Path):
    write(config_dir / "learned_aliases.yaml", LEARNED)
    config = load_config(config_dir, config_dir)
    (learned,) = config.learned_aliases
    assert (learned.alias, learned.added_by, learned.bank) == ("Total Assets of the Bank", "Analyst A", "Synthetic Bank")
    assert not has_alias(config.mappings, "Total Assets of the Bank")  # mappings.yaml alone: every bank
    assert has_alias(config.mappings_for("Synthetic Bank"), "Total Assets of the Bank")
    assert has_alias(config.mappings_for("  synthetic  BANK "), "Total Assets of the Bank")  # case and spaces ignored
    assert not has_alias(config.mappings_for("Other Bank"), "Total Assets of the Bank")
    assert not has_alias(config.mappings_for(None), "Total Assets of the Bank")


def test_learned_alias_without_a_bank_applies_to_every_bank(config_dir: Path):
    write(config_dir / "learned_aliases.yaml", LEARNED.replace("  - bank: Synthetic Bank\n    metric_id", "  - metric_id"))
    config = load_config(config_dir, config_dir)
    assert config.learned_aliases[0].bank is None
    assert has_alias(config.mappings_for("Other Bank"), "Total Assets of the Bank")


def test_unquoted_learned_time_is_kept_as_text(tmp_path: Path):
    path = write(tmp_path / "learned_aliases.yaml", LEARNED.replace('"2026-10-23T08:00:00+00:00"', "2026-10-23 08:00:00"))
    (learned,) = load_learned_aliases(path, {"total_assets_etb"})
    assert learned.added_at.startswith("2026-10-23")


def test_learned_alias_problems_are_reported(tmp_path: Path):
    bad = LEARNED.replace("total_assets_etb", "total_asets").replace("    added_by: Analyst A\n", "")
    path = write(tmp_path / "learned_aliases.yaml", bad)
    with pytest.raises(ConfigError, match="missing required field"):
        load_learned_aliases(path, {"total_assets_etb"})
    path = write(tmp_path / "learned_aliases.yaml", LEARNED.replace("total_assets_etb", "total_asets"))
    with pytest.raises(ConfigError, match="'total_asets' is not defined"):
        load_learned_aliases(path, {"total_assets_etb"})


def test_repository_checks_load():
    checks = {c.id: c for c in load_config().checks}
    assert checks["balance_sheet"].parts == ("total_liabilities_etb", "total_equity_etb")
    assert checks["profit_and_tax"].total == "profit_before_tax_etb"


def test_check_problems_are_reported_together(tmp_path: Path):
    metrics = load_metrics(write(tmp_path / "metrics.yaml", "metrics:\n" + VALID_METRIC + VALID_METRIC.replace(
        "total_assets_etb", "branches").replace("ETB\n", "count\n")))
    path = write(tmp_path / "checks.yaml", """\
        checks:
          - {id: a, name: A, total: total_assets_etb, parts: [branches]}
          - {id: a, name: B, total: total_assets_etb, parts: [total_assets_etb]}
          - {id: c, name: C, total: total_assets_etb, parts: [missing_metric]}
          - {id: d, name: D, total: total_assets_etb, parts: []}
        """)
    with pytest.raises(ConfigError) as error:
        load_checks(path, metrics)
    message = str(error.value)
    assert "different units" in message and "duplicate id 'a'" in message
    assert "also listed as a part" in message and "unknown metric id(s) ['missing_metric']" in message
    assert "'parts' must be a non-empty list" in message


# --- app_url (Evidence links) ----------------------------------------------------------------

def test_app_url_defaults_to_localhost(tmp_path: Path):
    assert load_settings(write(tmp_path / "settings.yaml", VALID_SETTINGS)).app_url == "http://localhost:8501"


def test_app_url_is_read_and_trailing_slash_dropped(tmp_path: Path):
    path = write(tmp_path / "settings.yaml", VALID_SETTINGS + "app_url: http://reviewpc:8600/\n")
    assert load_settings(path).app_url == "http://reviewpc:8600"


def test_repository_formulas_load_and_name_only_known_figures():
    config = load_config()
    roe = config.metric("roe_percent")
    assert (roe.basis, roe.formula) == ("average_equity", "profit_after_tax_etb / avg(total_equity_etb, total_equity_etb_prev)")
    assert config.input_figure("par_value").expected_unit == "ETB"


def test_formula_problems_are_reported(config_dir: Path):
    metric = VALID_METRIC + "    formula: \"total_assets_etb * 2\"\n"
    write(config_dir / "metrics.yaml", "metrics:\n" + metric)
    with pytest.raises(ConfigError, match="uses the metric itself"):
        load_config(config_dir, config_dir)
    write(config_dir / "metrics.yaml", "metrics:\n" + VALID_METRIC + "    formula: \"made_up_name / 2\"\n")
    with pytest.raises(ConfigError, match=r"unknown name\(s\) \['made_up_name'\]"):
        load_config(config_dir, config_dir)
    write(config_dir / "metrics.yaml", "metrics:\n" + VALID_METRIC + "    formula: \"a +\"\n")
    with pytest.raises(ConfigError, match="cannot be read"):
        load_config(config_dir, config_dir)


def test_input_figures_need_unique_ids_and_known_units(config_dir: Path):
    figures = ("input_figures:\n  - {id: par_value, display_name: Par, expected_unit: ETB}\n"
               "  - {id: par_value, display_name: Again, expected_unit: USD}\n"
               "  - {id: total_assets_etb, display_name: Clash, expected_unit: ETB}\n")
    write(config_dir / "metrics.yaml", "metrics:\n" + VALID_METRIC + figures)
    with pytest.raises(ConfigError) as error:
        load_config(config_dir, config_dir)
    assert "already used" in str(error.value) and "expected_unit must be one of" in str(error.value)


def test_app_url_from_environment_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BANK_EXTRACTOR_APP_URL", "https://bank-extractor.streamlit.app/")
    path = write(tmp_path / "settings.yaml", VALID_SETTINGS + "app_url: http://reviewpc:8600\n")
    assert load_settings(path).app_url == "https://bank-extractor.streamlit.app"


def test_app_url_must_be_an_address(tmp_path: Path):
    path = write(tmp_path / "settings.yaml", VALID_SETTINGS + "app_url: localhost\n")
    with pytest.raises(ConfigError, match="app_url"):
        load_settings(path)


def test_column_formulas_are_checked(config_dir: Path):
    template = VALID_TEMPLATE + "calculated: [Profit Margin]\nformulas:\n"
    write(config_dir / "export_template.yaml", template + "  Profit Margin: {formula: 'total_assets_etb / 2', unit: fraction}\n")
    (f,) = load_config(config_dir, config_dir).export_template.formulas
    assert (f.column, f.formula, f.unit, f.compare_with) == ("Profit Margin", "total_assets_etb / 2", "fraction", None)

    write(config_dir / "export_template.yaml", template + "  Profit Margin: {formula: 'nope / 2', unit: fraction}\n")
    with pytest.raises(ConfigError, match=r"unknown name\(s\) \['nope'\]"):
        load_config(config_dir, config_dir)
    write(config_dir / "export_template.yaml", template + "  Total Assets: {formula: '1', unit: percent}\n")
    with pytest.raises(ConfigError, match="not a calculated column"):
        load_export_template(config_dir / "export_template.yaml")
    write(config_dir / "export_template.yaml", template + "  Profit Margin: {formula: '1', unit: percent}\n")
    with pytest.raises(ConfigError, match="'unit' must be one of"):
        load_export_template(config_dir / "export_template.yaml")
