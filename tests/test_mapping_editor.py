"""Editing config/mappings.yaml from the Templates page keeps every comment (D-065).

Every test works on a copy of the repository's mappings.yaml in a temporary folder.
"""

import shutil

import pytest

from src.config_loader import CONFIG_DIR, load_config, load_mappings
from src.mapping_editor import MappingEditError, Wording, add_wording, list_wording, remove_wording

METRIC_IDS = {m.id for m in load_config().metrics}


@pytest.fixture
def mappings(tmp_path):
    return shutil.copy(CONFIG_DIR / "mappings.yaml", tmp_path / "mappings.yaml")


def comments(path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip().startswith("#")]


def wording(path, metric_id, kind) -> tuple[str, ...]:
    entry = next((m for m in load_mappings(path, METRIC_IDS) if m.metric_id == metric_id), None)
    return getattr(entry, kind) if entry else ()


def test_add_to_a_one_line_list_keeps_comments(mappings):
    before = comments(mappings)
    add_wording(mappings, "total_assets_etb", "aliases", "Total Assets (Net)", "Test Bank p10", METRIC_IDS)
    assert wording(mappings, "total_assets_etb", "aliases") == ("Total assets", "Total Assets (Net)")
    assert comments(mappings) == before
    line = next(l for l in mappings.read_text(encoding="utf-8").splitlines() if "Total Assets (Net)" in l)
    assert "BOA p111" in line and "Test Bank p10" in line  # the note joins the existing comment


def test_add_to_a_block_list_adds_one_line_with_its_note(mappings):
    text_before = mappings.read_text(encoding="utf-8")
    add_wording(mappings, "loans_and_advances_etb", "aliases", "Net loans and advances", "Test Bank p5", METRIC_IDS)
    assert wording(mappings, "loans_and_advances_etb", "aliases")[-1] == "Net loans and advances"
    added = set(mappings.read_text(encoding="utf-8").splitlines()) - set(text_before.splitlines())
    assert added == {'      - "Net loans and advances"  # Test Bank p5'}


def test_add_a_new_kind_and_a_new_metric(mappings):
    add_wording(mappings, "total_assets_etb", "text_before", "total assets reached", "", METRIC_IDS)
    add_wording(mappings, "operating_income_etb", "aliases", "Total operating income", "BoA 2022/23 p45", METRIC_IDS)
    assert wording(mappings, "total_assets_etb", "text_before") == ("total assets reached",)
    assert wording(mappings, "operating_income_etb", "aliases") == ("Total operating income",)


def test_remove_keeps_other_wording_and_comments(mappings):
    remove_wording(mappings, "employees", "text_after", "employees", METRIC_IDS)
    assert wording(mappings, "employees", "text_after") == ("permanent employees",)
    remove_wording(mappings, "roa_percent", "text_before", "ROAA", METRIC_IDS)
    assert "ROAA" not in wording(mappings, "roa_percent", "text_before")
    assert "# BOA p26" in mappings.read_text(encoding="utf-8")


def test_removing_the_last_wording_removes_the_entry(mappings):
    remove_wording(mappings, "shareholders", "text_before", "number of shareholders", METRIC_IDS)
    assert not wording(mappings, "shareholders", "text_before")
    assert "metric_id: shareholders" not in mappings.read_text(encoding="utf-8")
    remove_wording(mappings, "weighted_average_shares", "aliases",
                   "Weighted average number of ordinary shares in issue", METRIC_IDS)
    assert "weighted_average_shares" not in mappings.read_text(encoding="utf-8")


def test_duplicates_and_wording_of_another_metric_are_refused(mappings):
    original = mappings.read_text(encoding="utf-8")
    with pytest.raises(MappingEditError, match="already listed"):
        add_wording(mappings, "total_assets_etb", "aliases", "total  ASSETS", "", METRIC_IDS)
    with pytest.raises(MappingEditError, match="already means total_assets_etb"):
        add_wording(mappings, "total_equity_etb", "aliases", "Total assets", "", METRIC_IDS)
    with pytest.raises(MappingEditError, match="Type the wording"):
        add_wording(mappings, "total_equity_etb", "aliases", "   ", "", METRIC_IDS)
    with pytest.raises(MappingEditError, match="not in"):
        remove_wording(mappings, "total_equity_etb", "aliases", "Equity", METRIC_IDS)
    assert mappings.read_text(encoding="utf-8") == original


def test_unknown_metric_is_refused_and_nothing_written(mappings):
    original = mappings.read_text(encoding="utf-8")
    with pytest.raises(MappingEditError, match="not defined in metrics.yaml"):
        add_wording(mappings, "no_such_metric", "aliases", "Something", "", METRIC_IDS)
    assert mappings.read_text(encoding="utf-8") == original


def test_quotes_and_hash_signs_are_written_safely(mappings):
    add_wording(mappings, "total_assets_etb", "aliases", 'Total "group" assets # 2', "p1 # x", METRIC_IDS)
    assert 'Total "group" assets # 2' in wording(mappings, "total_assets_etb", "aliases")


def test_list_wording(mappings):
    items = list_wording(mappings)
    assert Wording("total_assets_etb", "aliases", "Total assets") in items
    assert Wording("branches", "text_before", "total number of branches") in items
