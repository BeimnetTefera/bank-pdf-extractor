"""Sector data (D-056): one value per fiscal year for every bank, append-only decisions, its
evidence, and the Excel export (sector columns, fall-back, cross-check flags, external values)."""

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from src import external_sources as ext
from src import review_service as review
from src import sector_data as sector
from src.config_loader import ConfigError, load_config, load_metrics
from src.database import connect
from src.evidence import EvidenceError, metric_evidence, sector_evidence, sector_evidence_url
from src.excel_exporter import APPROVED_SHEET, EVIDENCE_SHEET, METADATA_SHEET, METRIC_EVIDENCE_SHEET, build_workbook
from src.external_sources import SourceInput
from src.review_service import ReviewError
from tests.test_excel_exporter import save_report

METRIC = "sector_total_deposits_etb"
COLUMN = "Total deposit in the sector"
REVIEWER = "Analyst A"
SOURCE = SourceInput(title="NBE Annual Report 2025/26", publisher="National Bank of Ethiopia",
                     quote="Total deposits of the banking system reached Birr 2,345.6 billion",
                     printed_value="2,345.6", scale="billion", url="https://nbe.gov.et/report.pdf")


@pytest.fixture
def setup(settings):
    config = replace(load_config(), settings=settings)
    conn = connect(settings.database_path)
    yield conn, config
    conn.close()


def record_and_approve(conn, config, year: str = "2025/26") -> None:
    sector.record_value(conn, config, config.settings, year, METRIC, SOURCE, "NBE figure", REVIEWER)
    sector.approve(conn, config, year, METRIC, "Analyst B")


def test_level_sector_is_read_and_validated(tmp_path):
    config = load_config()
    assert [m.id for m in sector.sector_metrics(config)] == [METRIC]
    path = tmp_path / "metrics.yaml"
    path.write_text("metrics:\n  - id: x_etb\n    display_name: X\n    definition: d\n    expected_unit: ETB\n"
                    "    active: true\n    level: country\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="level 'country' is not supported"):
        load_metrics(path)


def test_fiscal_year_forms():
    assert sector.fiscal_year_key("2026") == sector.fiscal_year_key("2025-2026") == "2025/26"
    with pytest.raises(ReviewError, match="YYYY/YY"):
        sector.fiscal_year_key("next year")


def test_sector_value_is_proposed_approved_and_reopened(setup):
    conn, config = setup
    with pytest.raises(ReviewError, match="not a sector metric"):
        sector.record_value(conn, config, config.settings, "2025/26", "total_assets_etb", SOURCE, "", REVIEWER)
    sector.record_value(conn, config, config.settings, "2026", METRIC, SOURCE, "NBE figure", REVIEWER)
    state = sector.sector_state(conn, "2025/26", METRIC)
    assert state.status == "pending" and state.pending.value == Decimal("2345600000000")
    with pytest.raises(ReviewError, match="awaiting approval"):
        sector.record_value(conn, config, config.settings, "2025/26", METRIC, SOURCE, "again", REVIEWER)
    sector.approve(conn, config, "2025/26", METRIC, "Analyst B")
    state = sector.sector_state(conn, "2025/26", METRIC)
    assert (state.status, state.value, state.accepted.reviewer) == ("approved", Decimal("2345600000000"), "Analyst B")
    assert sector.state_source(conn, state).publisher == "National Bank of Ethiopia"
    with pytest.raises(ReviewError, match="already approved"):
        sector.record_not_reported(conn, config, "2025/26", METRIC, "Not published for this year", REVIEWER)
    with pytest.raises(ReviewError, match="reason"):
        sector.reopen(conn, config, "2025/26", METRIC, REVIEWER, "")
    sector.reopen(conn, config, "2025/26", METRIC, REVIEWER, "NBE revised the figure")
    assert sector.sector_state(conn, "2025/26", METRIC).status == "open"
    assert sector.sector_state(conn, "2024/25", METRIC).status == "open"  # other years are separate
    assert [d.decision for d in sector.decisions(conn, "2025/26", METRIC)] == ["proposed", "approved", "reopened"]


def test_sector_not_reported_needs_a_reason(setup):
    conn, config = setup
    with pytest.raises(ReviewError, match="Say why"):
        sector.record_not_reported(conn, config, "2025/26", METRIC, "n/a", REVIEWER)
    sector.record_not_reported(conn, config, "2025/26", METRIC, "NBE has not published 2025/26 yet", REVIEWER)
    sector.approve(conn, config, "2025/26", METRIC, REVIEWER)
    state = sector.sector_state(conn, "2025/26", METRIC)
    assert (state.status, state.value) == ("not_reported", None)


@pytest.mark.parametrize("statement", ["UPDATE sector_decisions SET value = '1'", "DELETE FROM sector_decisions",
                                       "DELETE FROM external_sources"])
def test_sector_rows_are_append_only_and_never_deleted(setup, statement):
    conn, config = setup
    record_and_approve(conn, config)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(statement)


def test_sector_rows_survive_deleting_a_report(setup):
    conn, config = setup
    document_id = save_report(conn, config, "Awash bank", "2025/26")
    record_and_approve(conn, config)
    review.delete_report(conn, document_id, REVIEWER, "Wrong file", config.settings.data_dir)
    assert sector.sector_state(conn, "2025/26", METRIC).status == "approved"


def test_sector_evidence_and_years(setup):
    conn, config = setup
    save_report(conn, config, "Awash bank", "2025/26")
    save_report(conn, config, "Abay Bank", "2026")
    record_and_approve(conn, config, "2024/25")
    assert sector.fiscal_years(conn) == ["2025/26", "2024/25"]
    assert sector.banks_for_year(conn, "2025/26") == ["Abay Bank", "Awash bank"]
    record_and_approve(conn, config)
    ev = sector_evidence(conn, config, "2026", METRIC)
    assert (ev.fiscal_year, ev.status_label, ev.value_text) == ("2025/26", "Approved", "2,345,600,000,000 ETB")
    assert ev.source.quote == SOURCE.quote and ev.proposal.reason == "NBE figure"
    assert ev.banks == ("Abay Bank", "Awash bank")
    assert sector_evidence_url("http://localhost:8501", "2025/26", METRIC) == \
        "http://localhost:8501/evidence?sector=2025%2F26&metric=sector_total_deposits_etb"


def test_open_sector_metrics_are_not_missing_work_in_a_report(setup):
    """Finish extraction lists open metrics as 'will stay blank'; a sector metric is filled from the
    Sector data page instead, so it is listed apart."""
    _, config = setup
    assert sector.without_sector_metrics(config, ["shareholders", METRIC, "total_assets_etb"]) == \
        (["shareholders", "total_assets_etb"], [METRIC])


def test_sector_evidence_refuses_a_bank_metric(setup):
    """A typed address naming a bank metric must not show it as an unrecorded sector value."""
    conn, config = setup
    with pytest.raises(EvidenceError, match="not a sector metric"):
        sector_evidence(conn, config, "2025/26", "total_assets_etb")
    with pytest.raises(EvidenceError, match="not a fiscal year"):
        sector_evidence(conn, config, "next year", METRIC)


# --- Excel ------------------------------------------------------------------------------------------

def rows(wb, sheet: str) -> list[dict]:
    values = list(wb[sheet].iter_rows(values_only=True))
    return [dict(zip(values[0], r)) for r in values[1:]]


def warnings(wb) -> list[str]:
    return [r[1] for r in wb[METADATA_SHEET].iter_rows(values_only=True) if r[0] == "Warning"]


def test_export_fills_the_sector_column_in_every_bank_row(setup):
    conn, config = setup
    first = save_report(conn, config, "Awash bank", "2025/26")
    second = save_report(conn, config, "Abay Bank", "2025/26")
    record_and_approve(conn, config)
    wb = build_workbook(conn, config, [first, second], exported_by=REVIEWER)
    assert [r[COLUMN] for r in rows(wb, APPROVED_SHEET)] == [2345600000000, 2345600000000]
    header = [c.value for c in wb[APPROVED_SHEET][1]]
    cell = wb[APPROVED_SHEET].cell(2, header.index(COLUMN) + 1)
    assert "sector=2025%2F26" in cell.hyperlink.target and "National Bank of Ethiopia" in cell.comment.text
    sector_rows = [r for r in rows(wb, METRIC_EVIDENCE_SHEET) if r["metric_id"] == METRIC]
    assert len(sector_rows) == 2 and sector_rows[0]["status"] == "Sector value approved"
    assert "NBE Annual Report 2025/26" in sector_rows[0]["external_source"]
    source_rows = [r for r in rows(wb, EVIDENCE_SHEET) if r["metric_id"] == METRIC]
    assert source_rows[0]["value_origin"].startswith("sector value") and source_rows[0]["printed_value"] == "2,345.6"


def test_export_falls_back_to_a_value_approved_in_the_report(setup):
    conn, config = setup
    document_id = save_report(conn, config, "Awash bank", "2025/26")
    review.manual_entry(conn, document_id, METRIC, REVIEWER, "1000000", "ETB", 1, "typed from the chairman's letter")
    review.approve_correction(conn, document_id, METRIC, REVIEWER)
    wb = build_workbook(conn, config, [document_id], exported_by=REVIEWER)
    assert rows(wb, APPROVED_SHEET)[0][COLUMN] == 1000000
    assert any("no approved sector value for 2025/26" in w for w in warnings(wb))
    record_and_approve(conn, config)  # now the sector table wins, with a note that they differ
    wb = build_workbook(conn, config, [document_id], exported_by=REVIEWER)
    assert rows(wb, APPROVED_SHEET)[0][COLUMN] == 2345600000000
    assert any("differs from the sector value" in w for w in warnings(wb))


def test_export_flags_a_cross_check_mismatch(setup):
    conn, config = setup
    document_id = save_report(conn, config, "Awash bank", "2025/26")
    approved = next(r for r in review.approved_results(conn, document_id) if r.metric_id == "total_assets_etb")
    wrong = replace(SOURCE, printed_value="9.9", scale="billion")
    ext.add_cross_check(conn, config.settings, document_id, "total_assets_etb", "ETB", wrong, "", REVIEWER)
    wb = build_workbook(conn, config, [document_id], exported_by=REVIEWER)
    row = next(r for r in rows(wb, METRIC_EVIDENCE_SHEET) if r["metric_id"] == "total_assets_etb")
    assert row["cross_check"].startswith("MISMATCH: NBE Annual Report 2025/26: Does not match")
    assert any("does not match its external cross-check" in w for w in warnings(wb))
    right = replace(SOURCE, printed_value=str(approved.value / 1000), scale="birr_thousand")
    check_id = ext.cross_checks(conn, document_id)[0].id
    ext.withdraw_cross_check(conn, check_id, REVIEWER, "The release counts the group")
    ext.add_cross_check(conn, config.settings, document_id, "total_assets_etb", "ETB", right, "", REVIEWER)
    wb = build_workbook(conn, config, [document_id], exported_by=REVIEWER)
    row = next(r for r in rows(wb, METRIC_EVIDENCE_SHEET) if r["metric_id"] == "total_assets_etb")
    assert row["cross_check"].startswith("Match: ") and not any("cross-check" in w for w in warnings(wb))


def test_export_cites_an_external_value(setup):
    conn, config = setup
    document_id = save_report(conn, config, "Awash bank", "2025/26")
    employees = replace(SOURCE, printed_value="12,345", scale="count", quote="The bank employs 12,345 staff")
    ext.record_external_value(conn, config.settings, document_id, "employees", "count", employees,
                              "Not in the report", REVIEWER)
    review.approve_correction(conn, document_id, "employees", REVIEWER)
    assert metric_evidence(conn, config, document_id, "employees").source_type == "external"
    wb = build_workbook(conn, config, [document_id], exported_by=REVIEWER)
    row = next(r for r in rows(wb, EVIDENCE_SHEET) if r["metric_id"] == "employees")
    assert row["value_origin"].startswith("from an external source")
    assert row["original_label_or_sentence"] == "The bank employs 12,345 staff" and row["pdf_page"] is None
    assert "https://nbe.gov.et/report.pdf" in row["external_source"]
    header = [c.value for c in wb[APPROVED_SHEET][1]]
    cell = wb[APPROVED_SHEET].cell(2, header.index(config.metric("employees").export_column) + 1)
    assert cell.value == 12345 and "Source: NBE Annual Report 2025/26" in cell.comment.text
