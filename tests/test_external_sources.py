"""External sources (D-055) and external cross-checks (D-057): conversion, files, proposals,
approval, evidence, append-only rules, report deletion and the v6 -> v7 migration."""

import sqlite3
from decimal import Decimal

import pymupdf
import pytest

from src import database
from src import external_sources as ext
from src import review_service as review
from src.database import connect
from src.evidence import metric_evidence
from src.external_sources import SourceInput
from src.review_service import ReviewError
from tests.conftest import make_pdf
from tests.test_evidence import report  # noqa: F401 (report is a fixture)

REVIEWER = "Analyst A"
NOTE = "Not disclosed in the report; the NBE annual report gives it."
SOURCE_PDF = make_pdf([[("National Bank of Ethiopia annual report", [])],
                       [("Total assets of Synthetic Bank", ["1,234.6"])]])


def png_bytes() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 20), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    return pix.tobytes("png")


def source(**changes) -> SourceInput:
    fields = dict(title="NBE Annual Report 2025/26", publisher="National Bank of Ethiopia",
                  quote="Total assets of Synthetic Bank reached Birr 1.23 billion", printed_value="1.234567",
                  scale="billion", url="https://nbe.gov.et/annual-report-2025-26.pdf")
    return SourceInput(**(fields | changes))


# --- conversion ---------------------------------------------------------------------------------

@pytest.mark.parametrize("printed, scale, unit, expected, text", [
    ("1,234.5", "billion", "ETB", "1234500000000", "1,234.5 billion Birr = 1,234,500,000,000 ETB"),
    ("189,512,266", "birr_thousand", "ETB", "189512266000", "189,512,266 Birr'000 = 189,512,266,000 ETB"),
    ("2,500", "birr", "ETB", "2500", "2,500 Birr = 2,500 ETB"),
    ("4.5", "million", "count", "4500000", "4.5 million = 4,500,000"),
    ("12.5%", "percent", "fraction", "0.125", "12.5% = 0.125 (12.5%)"),
    ("(12.3)", "million", "ETB", "-12300000", "(12.3) million Birr = -12,300,000 ETB"),
])
def test_value_is_converted_to_the_metric_unit_and_shown(printed, scale, unit, expected, text):
    value = ext.convert(printed, scale, unit)
    assert value == Decimal(expected)
    assert ext.conversion_text(printed, scale, value, unit) == text


def test_conversion_refuses_a_scale_that_does_not_fit():
    with pytest.raises(ReviewError, match="cannot be stored as ETB"):
        ext.convert("12", "percent", "ETB")
    with pytest.raises(ReviewError, match="not a whole number"):
        ext.convert("4.5", "count", "count")
    with pytest.raises(ReviewError, match="not a number"):
        ext.convert("about twelve", "birr", "ETB")
    assert ext.scales_for("ETB") == ["birr", "birr_thousand", "million", "billion"]


# --- uploaded files -------------------------------------------------------------------------------

def test_uploaded_files_are_validated(settings):
    assert ext.validate_source_file(SOURCE_PDF, "nbe.pdf", settings).page_count == 2
    assert ext.validate_source_file(png_bytes(), "table.png", settings).media_type == "image/png"
    with pytest.raises(ReviewError, match="not a PDF or an image"):
        ext.validate_source_file(b"hello", "notes.txt", settings)
    with pytest.raises(ReviewError, match="not a PNG image"):
        ext.validate_source_file(b"not really a png", "fake.png", settings)
    with pytest.raises(ReviewError):
        ext.validate_source_file(b"%PDF-broken", "broken.pdf", settings)


def test_source_needs_title_quote_and_an_address_or_a_file(report):
    conn, config, document, _, _ = report
    settings = config.settings

    def record(src):
        return ext.record_external_value(conn, settings, document.id, "total_assets_etb", "ETB", src, NOTE, REVIEWER)

    with pytest.raises(ReviewError, match="document title"):
        record(source(title=" "))
    with pytest.raises(ReviewError, match="exact quote"):
        record(source(quote=""))
    with pytest.raises(ReviewError, match="address \\(URL\\) .* or upload a copy"):
        record(source(url=""))
    with pytest.raises(ReviewError, match="http:// or https://"):
        record(source(url="nbe.gov.et"))
    with pytest.raises(ReviewError, match="page of the uploaded file"):
        record(source(file_name="nbe.pdf", file_data=SOURCE_PDF))
    with pytest.raises(ReviewError, match="1 to 2"):
        record(source(file_name="nbe.pdf", file_data=SOURCE_PDF, page=3))
    with pytest.raises(ReviewError, match="only be marked on an uploaded file"):
        record(source(box=(1, 2, 3, 4)))
    with pytest.raises(ReviewError, match="Write a note"):
        ext.record_external_value(conn, settings, document.id, "total_assets_etb", "ETB", source(), "", REVIEWER)
    assert conn.execute("SELECT COUNT(*) FROM external_sources").fetchone()[0] == 0
    assert review.review_state(conn, document.id, "total_assets_etb").status == "open"


# --- a value from an external source --------------------------------------------------------------

def test_external_value_in_brackets_follows_the_sign_choice(report):
    """D-068 for external sources: brackets are recorded as positive or kept negative, as chosen,
    and the direction is kept (sign_choice) so the Evidence page shows it."""
    conn, config, document, _, _ = report
    positive = source(printed_value="(4.3)", scale="million", sign="positive")
    ext.record_external_value(conn, config.settings, document.id, "profit_tax_etb", "ETB", positive, NOTE, REVIEWER)
    pending = review.review_state(conn, document.id, "profit_tax_etb").pending
    assert (pending.reviewed_value, pending.sign_choice) == (Decimal(4_300_000), "positive")
    ev = metric_evidence(conn, config, document.id, "profit_tax_etb")
    assert ev.sign_text == ("Printed in brackets in the external source (negative); recorded as a positive amount, "
                            "as the reviewer chose.")
    assert ev.conversion == "(4.3) million Birr = 4,300,000 ETB (recorded as a positive amount)"

    kept = source(printed_value="(4.3)", scale="million", sign="as_printed")
    ext.record_external_value(conn, config.settings, document.id, "total_equity_etb", "ETB", kept, NOTE, REVIEWER)
    pending = review.review_state(conn, document.id, "total_equity_etb").pending
    assert (pending.reviewed_value, pending.sign_choice) == (Decimal(-4_300_000), "as_printed")
    assert "kept negative" in metric_evidence(conn, config, document.id, "total_equity_etb").sign_text
    ext.record_external_value(conn, config.settings, document.id, "total_assets_etb", "ETB", source(), NOTE, REVIEWER)
    assert review.review_state(conn, document.id, "total_assets_etb").pending.sign_choice is None  # not negative


def test_a_calculation_input_from_an_external_source_names_its_source(report):
    """An input taken from an approved metric whose value came from an external source keeps that
    source on the Evidence page (it is not printed in the report, so the source is its evidence)."""
    from src.calculated_entries import save_calculation

    conn, config, document, candidate, _ = report
    ext.record_external_value(conn, config.settings, document.id, "total_equity_etb", "ETB", source(), NOTE, REVIEWER)
    review.approve_correction(conn, document.id, "total_equity_etb", "Analyst B")
    review.approve(conn, candidate("total_assets_etb", "1,234,567"), REVIEWER)
    save_calculation(conn, config, document.id, "total_liabilities_etb", "total_assets_etb - total_equity_etb",
                     "Not printed", REVIEWER)
    ev = metric_evidence(conn, config, document.id, "total_liabilities_etb")
    assert ev.source_type == "calculated"
    assert [(i.variable, s.title, s.quote) for i, s in ev.calc_input_sources] == [
        ("total_equity_etb", "NBE Annual Report 2025/26", "Total assets of Synthetic Bank reached Birr 1.23 billion")]


def test_external_value_is_a_proposal_approved_like_a_correction(report):
    conn, config, document, _, _ = report
    box = (50.0, 105.0, 330.0, 125.0)
    src = source(file_name="nbe.pdf", file_data=SOURCE_PDF, page=2, box=box, url="")
    decision_id = ext.record_external_value(conn, config.settings, document.id, "total_assets_etb", "ETB", src, NOTE,
                                            REVIEWER)
    state = review.review_state(conn, document.id, "total_assets_etb")
    assert state.status == "corrected" and state.pending.entry_kind == "external"
    assert state.pending.reviewed_value == Decimal("1234567000") and state.pending.source_page is None
    saved = ext.source_for_decision(conn, decision_id)
    assert (saved.purpose, saved.page, saved.box, saved.url) == ("value", 2, box, "")
    assert ext.source_path(config.settings.data_dir, saved.file_sha256, saved.media_type).is_file()
    assert saved.citation == "NBE Annual Report 2025/26 · National Bank of Ethiopia · page 2 of nbe.pdf"

    pending = metric_evidence(conn, config, document.id, "total_assets_etb")
    assert (pending.status, pending.source_type, pending.source_label) == ("pending", "external", "External source")
    review.approve_correction(conn, document.id, "total_assets_etb", "Analyst B")
    ev = metric_evidence(conn, config, document.id, "total_assets_etb")
    assert (ev.status, ev.source_type, ev.value, ev.page) == ("approved", "external", Decimal("1234567000"), None)
    assert ev.conversion == "1.234567 billion Birr = 1,234,567,000 ETB"
    assert ev.label == src.quote and ev.external.box == box
    assert ev.history[-2].action == "Value from an external source"
    result = next(r for r in review.approved_results(conn, document.id) if r.metric_id == "total_assets_etb")
    assert result.proposal.entry_kind == "external" and result.human_entered
    page = ext.source_page_png(config.settings.data_dir, ev.external)
    crop = ext.source_crop_png(config.settings.data_dir, ev.external)
    assert page.startswith(b"\x89PNG") and crop.startswith(b"\x89PNG")


def test_an_image_can_be_the_uploaded_copy(report):
    conn, config, document, _, _ = report
    src = source(file_name="table.png", file_data=png_bytes(), page=1, box=(1.0, 1.0, 20.0, 10.0))
    decision_id = ext.record_external_value(conn, config.settings, document.id, "total_assets_etb", "ETB", src, NOTE,
                                            REVIEWER)
    saved = ext.source_for_decision(conn, decision_id)
    assert saved.media_type == "image/png"
    assert ext.source_page_png(config.settings.data_dir, saved).startswith(b"\x89PNG")


def test_decision_and_source_are_saved_together_or_not_at_all(report, monkeypatch):
    conn, config, document, _, _ = report

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(ext, "insert_source", broken)
    with pytest.raises(sqlite3.OperationalError):
        ext.record_external_value(conn, config.settings, document.id, "total_assets_etb", "ETB", source(), NOTE,
                                  REVIEWER)
    assert conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 0


def test_locked_report_refuses_an_external_value(report):
    conn, config, document, _, _ = report
    review.finish_extraction(conn, document.id, REVIEWER)
    with pytest.raises(ReviewError, match="locked"):
        ext.record_external_value(conn, config.settings, document.id, "total_assets_etb", "ETB", source(), NOTE,
                                  REVIEWER)


# --- cross-checks ---------------------------------------------------------------------------------

def approve_assets(conn, candidate) -> None:
    review.approve(conn, candidate("total_assets_etb", "1,234,567"), REVIEWER)  # 1,234,567,000 ETB


@pytest.mark.parametrize("printed, scale, status", [
    ("1.2346", "billion", "match"),  # 0.003% off
    ("1,240", "million", "match"),  # 0.44% off
    ("1,250", "million", "mismatch"),  # 1.25% off
    ("1,234,569", "birr_thousand", "match"),  # within 0.5% anyway
])
def test_cross_check_compares_with_the_approved_value(report, printed, scale, status):
    conn, config, document, candidate, _ = report
    approve_assets(conn, candidate)
    ext.add_cross_check(conn, config.settings, document.id, "total_assets_etb", "ETB",
                        source(printed_value=printed, scale=scale), "", REVIEWER)
    ev = metric_evidence(conn, config, document.id, "total_assets_etb")
    assert [c.comparison.status for c in ev.cross_checks] == [status]
    assert len(ev.mismatches) == (status == "mismatch")


def saved_source(printed: str, scale: str, value: int) -> ext.SavedSource:
    return ext.SavedSource(1, "d", "cross_check", None, "T", "P", "", None, None, None, None, None, "q", printed,
                           scale, Decimal(value), "ETB", REVIEWER, "now")


def test_birr_thousand_rounding_of_two_thousand_is_a_match():
    in_thousands = saved_source("302", "birr_thousand", 302_000)
    assert ext.compare(Decimal(300_000), "ETB", in_thousands).status == "match"  # 0.67% off, but 2 thousand
    assert ext.compare(Decimal(299_000), "ETB", in_thousands).status == "mismatch"  # 3 thousand, 1%
    in_birr = saved_source("302,000", "birr", 302_000)
    assert ext.compare(Decimal(300_100), "ETB", in_birr).status == "mismatch"  # neither in Birr'000: 0.5% only
    assert ext.compare(Decimal(300_000), "ETB", in_birr).status == "match"  # approved from a Birr'000 statement
    assert ext.compare(None, None, in_thousands).status == "not_compared"


def test_cross_check_is_withdrawn_with_a_reason_never_deleted(report):
    conn, config, document, candidate, _ = report
    approve_assets(conn, candidate)
    check_id = ext.add_cross_check(conn, config.settings, document.id, "total_assets_etb", "ETB",
                                   source(printed_value="9", scale="billion"), "press release", REVIEWER)
    with pytest.raises(ReviewError, match="Say why"):
        ext.withdraw_cross_check(conn, check_id, REVIEWER, " ")
    ext.withdraw_cross_check(conn, check_id, REVIEWER, "The release counts the group, not the bank")
    with pytest.raises(ReviewError, match="already withdrawn"):
        ext.withdraw_cross_check(conn, check_id, REVIEWER, "again")
    ev = metric_evidence(conn, config, document.id, "total_assets_etb")
    (only,) = ev.cross_checks
    assert not only.check.active and only.comparison.status == "not_compared" and ev.mismatches == []
    with pytest.raises(sqlite3.DatabaseError, match="withdraw one with a reason"):
        conn.execute("DELETE FROM external_cross_checks WHERE id = ?", (check_id,))


def test_cross_check_may_be_added_after_the_review_is_finished(report):
    conn, config, document, candidate, _ = report
    approve_assets(conn, candidate)
    review.finish_extraction(conn, document.id, REVIEWER)
    ext.add_cross_check(conn, config.settings, document.id, "total_assets_etb", "ETB", source(), "", REVIEWER)
    assert len(ext.cross_checks(conn, document.id, "total_assets_etb")) == 1


def test_evidence_pdf_shows_the_external_source_and_cross_checks(report):
    from src.evidence import report_evidence
    from src.evidence_pdf import summary_pdf

    conn, config, document, candidate, _ = report
    approve_assets(conn, candidate)
    ext.add_cross_check(conn, config.settings, document.id, "total_assets_etb", "ETB",
                        source(printed_value="9", scale="billion"), "", REVIEWER)
    src = source(printed_value="1,234", scale="count", quote="The bank employs 1,234 staff",
                 file_name="nbe.pdf", file_data=SOURCE_PDF, page=2, box=(50.0, 105.0, 330.0, 125.0))
    ext.record_external_value(conn, config.settings, document.id, "employees", "count", src, NOTE, REVIEWER)
    evidences = report_evidence(conn, config, document.id)
    employees = next(e for e in evidences if e.metric_id == "employees")
    pdf = summary_pdf([(evidences, {})], "http://localhost:8501", "Evidence", "2026-10-26 10:00",
                      lambda e: ext.source_crop_png(config.settings.data_dir, e.external) if e.external else None)
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        text = " ".join(" ".join(page.get_text().split()) for page in doc)
        images = sum(len(page.get_images()) for page in doc)
    assert "“The bank employs 1,234 sta" in text and "https://nbe.gov.et" in text  # "ff" is a ligature
    assert "Cross-checks" in text and "Mismatch" in text and images >= 1
    assert employees.status == "pending" and employees.source_label == "External source"


# --- audit rules ------------------------------------------------------------------------------------

@pytest.mark.parametrize("statement, message", [
    ("UPDATE external_sources SET value = '1'", "append-only"),
    ("DELETE FROM external_sources", "cannot be deleted on their own"),
    ("UPDATE external_cross_checks SET note = 'x'", "append-only"),
    ("UPDATE external_cross_check_withdrawals SET reason = 'x'", "append-only"),
    ("DELETE FROM external_cross_check_withdrawals", "cannot be deleted"),
])
def test_external_rows_are_append_only(report, statement, message):
    conn, config, document, candidate, _ = report
    approve_assets(conn, candidate)
    check_id = ext.add_cross_check(conn, config.settings, document.id, "total_assets_etb", "ETB", source(), "",
                                   REVIEWER)
    ext.withdraw_cross_check(conn, check_id, REVIEWER, "Wrong year in the source")
    with pytest.raises(sqlite3.DatabaseError, match=message):
        conn.execute(statement)


def test_delete_report_removes_its_sources_checks_and_files(report):
    conn, config, document, candidate, _ = report
    settings = config.settings
    approve_assets(conn, candidate)
    check_id = ext.add_cross_check(conn, settings, document.id, "total_assets_etb", "ETB",
                                   source(file_name="nbe.pdf", file_data=SOURCE_PDF, page=2), "", REVIEWER)
    ext.withdraw_cross_check(conn, check_id, REVIEWER, "Wrong year in the source")
    ext.record_external_value(conn, settings, document.id, "employees", "count",
                              source(printed_value="1,234", scale="count"), NOTE, REVIEWER)
    stored = ext.source_path(settings.data_dir, ext.get_source(conn, 1).file_sha256, "application/pdf")
    assert stored.is_file()
    deleted = review.delete_report(conn, document.id, REVIEWER, "Uploaded the wrong file", settings.data_dir)
    for table in ("external_sources", "external_cross_checks", "external_cross_check_withdrawals"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert not stored.exists() and stored.name in deleted.files_removed


# --- migration --------------------------------------------------------------------------------------

def v6_schema() -> str:
    """The schema as it was before version 7: without the external and sector tables."""
    start = database._SCHEMA.index("-- A document other than the bank's report")
    end = database._SCHEMA.index("-- Whole reports deleted on request")
    return database._SCHEMA[:start] + database._SCHEMA[end:]


def test_a_version_6_database_gets_the_new_tables_and_keeps_its_rows(tmp_path):
    path = tmp_path / "app.db"
    old = sqlite3.connect(path)
    old.executescript(v6_schema())
    old.execute("""INSERT INTO documents (id, bank_name, reporting_period, original_filename, stored_path, sha256,
                   uploaded_at, processing_status) VALUES ('d1', 'Old Bank', '2024/25', 'old.pdf', 'p', 'abc',
                   '2025-01-01T00:00:00+00:00', 'saved')""")
    old.execute("""INSERT INTO review_decisions (document_id, metric_id, decision, reviewed_value, reviewed_unit,
                   source_page, reason, reviewer, created_at) VALUES ('d1', 'employees', 'manual_entry', '12', 'count',
                   1, 'typed', 'A', '2025-01-01T00:00:00+00:00')""")
    old.execute("PRAGMA user_version = 6")
    old.commit()
    old.close()

    conn = connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION == 7
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"external_sources", "external_cross_checks", "external_cross_check_withdrawals",
                "sector_decisions"} <= tables
        state = review.review_state(conn, "d1", "employees")
        assert state.status == "corrected" and state.pending.reviewed_value == Decimal(12)
        assert ext.cross_checks(conn, "d1") == []
    finally:
        conn.close()
