"""D-084 / D-085: bank names and fiscal years fixed in place, logged; the year read from a PDF.
Synthetic PDFs and temporary databases only."""

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from streamlit.testing.v1 import AppTest

from src import report_corrections as fixes
from src.analysis import analyse
from src.config_loader import load_config
from src.database import connect
from src.document_manager import DocumentError, store_document
from src.pdf_extractor import EXTRACTOR_VERSION, extract_candidates
from src.project_service import get_document, list_documents, save_analysis
from src.report_period import canonical_period, period_from_text, suggested_period
from src.review_service import ReviewError, delete_report
from tests.conftest import make_pdf
from tests.test_app_review import parked_tools_page

BANKS = load_config().banks
REVIEWER = "Analyst A"


def save_report(settings, bank: str = "Bank of Abyssinia", period: str = "2025/26", pdf: bytes | None = None) -> str:
    pdf = pdf or make_pdf([[("Synthetic Bank S.C.", []), ("Total assets", ["1,234,567", "987,654"])]])
    document = store_document(pdf, "synthetic.pdf", bank, period, settings)
    analysis = analyse(extract_candidates(pdf), load_config(), ["total_assets_etb"], "2025/26")
    with closing(connect(settings.database_path)) as conn:
        save_analysis(conn, document, analysis, EXTRACTOR_VERSION)
    return document.id


def as_saved_earlier(settings, document_id: str, bank: str, period: str) -> None:
    """Make a report look as saved before D-084/D-085 (typed name, bare year)."""
    with closing(connect(settings.database_path)) as conn, conn:
        conn.execute("DROP TRIGGER documents_no_update")  # recreated by the next connect()
        conn.execute("UPDATE documents SET bank_name = ?, reporting_period = ? WHERE id = ?",
                     (bank, period, document_id))


# --- the fiscal year: one saved form (D-085) ------------------------------------------------

@pytest.mark.parametrize("typed, saved", [("2026", "2025/26"), ("FY2026", "2025/26"), ("2025-2026", "2025/26"),
                                          ("2025/26", "2025/26"), ("2025", "2024/25"), ("2022/23", "2022/23")])
def test_canonical_period(typed, saved):
    assert canonical_period(typed) == saved


@pytest.mark.parametrize("typed", ["", "last year", "2025/27", None])
def test_canonical_period_unreadable(typed):
    assert canonical_period(typed) is None


@pytest.mark.parametrize("typed", ["2026", "FY2026", "2025-2026"])
def test_save_stores_the_year_as_yyyy_yy(settings, typed):
    document_id = save_report(settings, period=typed)
    with closing(connect(settings.database_path)) as conn:
        assert get_document(conn, document_id)["reporting_period"] == "2025/26"


def test_save_refuses_an_unreadable_year(settings):
    with pytest.raises(DocumentError, match="not a fiscal year"):
        save_report(settings, period="last year")


@pytest.mark.parametrize("text", ["For the year ended 30 June 2026", "as at June 30, 2026",
                                  "for the period ended 30th June, 2026", "30 JUNE 2026", "June 30th 2026"])
def test_period_end_in_the_text(text):
    assert period_from_text([text]) == "2025/26"


def test_period_end_uses_the_latest_year_and_needs_30_june():
    assert period_from_text(["30 June 2025", "Statement as at 30 June 2026"]) == "2025/26"
    assert period_from_text(["31 December 2025", "June 2026", "29 June 2026"]) is None


def test_suggested_period_reads_the_first_pages_of_a_pdf():
    pdf = make_pdf([[("Annual report", [])], [("For the year ended 30 June 2026", [])]])
    assert suggested_period(pdf) == "2025/26"
    assert suggested_period(make_pdf([[("Total assets", ["1"])]])) is None
    late = make_pdf([[("Cover", [])]] * 6 + [[("For the year ended 30 June 2026", [])]])
    assert suggested_period(late) is None  # only the first pages are read
    assert suggested_period(b"not a pdf") is None


# --- bank names and years fixed in place, logged (D-084, D-085) -----------------------------

def test_canonical_bank_ignores_case_and_spacing():
    assert fixes.canonical_bank("Bank of abyssinia", BANKS) == "Bank of Abyssinia"
    assert fixes.canonical_bank("  bank  of   ABYSSINIA ", BANKS) == "Bank of Abyssinia"
    assert fixes.canonical_bank("Bank of Abyssinia", BANKS) == "Bank of Abyssinia"
    assert fixes.canonical_bank("Abyssinia", BANKS) is None


def test_proposed_fixes_list_but_change_nothing(settings):
    """As in the real data: 'Bank of abyssinia' / '2026', and a listed name with a bare year."""
    first, second, odd = save_report(settings), save_report(settings), save_report(settings)
    as_saved_earlier(settings, first, "Bank of abyssinia", "2026")
    as_saved_earlier(settings, second, "Bank of Abyssinia", "2025")
    as_saved_earlier(settings, odd, "Mystery bank", "last year")
    with closing(connect(settings.database_path)) as conn:
        found = fixes.proposed_fixes(conn, BANKS)
        assert {(f.document_id, f.field, f.old_value, f.new_value) for f in found.fixes} == {
            (first, "bank_name", "Bank of abyssinia", "Bank of Abyssinia"),
            (first, "reporting_period", "2026", "2025/26"),
            (second, "reporting_period", "2025", "2024/25"),
        }
        assert [d["id"] for d in found.unmatched_banks] == [odd]
        assert [d["id"] for d in found.unreadable_years] == [odd]
        assert get_document(conn, first)["bank_name"] == "Bank of abyssinia"  # nothing changed yet
        assert fixes.corrections(conn) == []


def test_apply_fixes_logs_each_first_and_updates(settings):
    document_id = save_report(settings)
    as_saved_earlier(settings, document_id, "Bank of abyssinia", "2026")
    with closing(connect(settings.database_path)) as conn:
        found = fixes.proposed_fixes(conn, BANKS)
        assert fixes.apply_fixes(conn, found.fixes, REVIEWER, BANKS) == 2
        doc = get_document(conn, document_id)
        assert (doc["bank_name"], doc["reporting_period"]) == ("Bank of Abyssinia", "2025/26")
        log = {r["field"]: r for r in fixes.corrections(conn, document_id)}
        assert (log["bank_name"]["old_value"], log["bank_name"]["new_value"], log["bank_name"]["reviewer"]) == (
            "Bank of abyssinia", "Bank of Abyssinia", REVIEWER)
        assert (log["reporting_period"]["old_value"], log["reporting_period"]["new_value"]) == ("2026", "2025/26")
        assert log["bank_name"]["reason"] == fixes.NAME_REASON
        assert not fixes.proposed_fixes(conn, BANKS)


def test_apply_fixes_is_all_or_nothing(settings):
    document_id = save_report(settings)
    as_saved_earlier(settings, document_id, "Bank of abyssinia", "2026")
    with closing(connect(settings.database_path)) as conn:
        found = fixes.proposed_fixes(conn, BANKS)
        stale = found.fixes + [replace(found.fixes[0], old_value="something else")]
        with pytest.raises(ReviewError, match="changed since"):
            fixes.apply_fixes(conn, stale, REVIEWER, BANKS)
        assert get_document(conn, document_id)["bank_name"] == "Bank of abyssinia"
        assert fixes.corrections(conn) == []


def test_correct_report_only_to_a_listed_bank_or_a_yyyy_yy_year(settings):
    document_id = save_report(settings)
    as_saved_earlier(settings, document_id, "Mystery bank", "last year")
    with closing(connect(settings.database_path)) as conn:
        with pytest.raises(ReviewError, match="not in config/banks.yaml"):
            fixes.correct_report(conn, document_id, "bank_name", "Another bank", REVIEWER, "x", BANKS)
        with pytest.raises(ReviewError, match="YYYY/YY"):
            fixes.correct_report(conn, document_id, "reporting_period", "2026", REVIEWER, "x", BANKS)
        with pytest.raises(ReviewError, match="reviewer"):
            fixes.correct_report(conn, document_id, "bank_name", "Awash bank", " ", "x", BANKS)
        with pytest.raises(ReviewError, match="Only"):
            fixes.correct_report(conn, document_id, "sha256", "x", REVIEWER, "x", BANKS)
        fixes.correct_report(conn, document_id, "bank_name", "Awash bank", REVIEWER, fixes.CHOSEN_BANK_REASON, BANKS)
        fixes.correct_report(conn, document_id, "reporting_period", "2024/25", REVIEWER, "chosen", BANKS)
        doc = get_document(conn, document_id)
        assert (doc["bank_name"], doc["reporting_period"]) == ("Awash bank", "2024/25")
        assert len(fixes.corrections(conn, document_id)) == 2


def test_database_refuses_unlogged_document_changes_and_changes_to_the_log(settings):
    document_id = save_report(settings)
    with closing(connect(settings.database_path)) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="cannot be changed"):
            conn.execute("UPDATE documents SET bank_name = 'Awash bank' WHERE id = ?", (document_id,))
        with pytest.raises(sqlite3.DatabaseError, match="cannot be changed"):
            conn.execute("UPDATE documents SET sha256 = 'x' WHERE id = ?", (document_id,))
        fixes.correct_report(conn, document_id, "bank_name", "Awash bank", REVIEWER, "test", BANKS)
        with pytest.raises(sqlite3.DatabaseError, match="cannot be changed"):  # the log row allows only its change
            conn.execute("UPDATE documents SET bank_name = 'Zemen bank' WHERE id = ?", (document_id,))
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE report_corrections SET reason = 'x'")
        with pytest.raises(sqlite3.DatabaseError, match="delete the whole report"):
            conn.execute("DELETE FROM report_corrections")


def test_deleting_a_report_deletes_its_corrections(settings):
    document_id = save_report(settings)
    with closing(connect(settings.database_path)) as conn:
        fixes.correct_report(conn, document_id, "bank_name", "Awash bank", REVIEWER, "test", BANKS)
        delete_report(conn, document_id, REVIEWER, "Demo", settings.data_dir)
        assert list_documents(conn) == [] and fixes.corrections(conn) == []


# --- Bank Reports: Fix names and years -----------------------------------------------------

def test_fix_panel_applies_only_when_pressed(settings):
    document_id = save_report(settings)
    odd = save_report(settings)
    as_saved_earlier(settings, document_id, "Bank of abyssinia", "2026")
    as_saved_earlier(settings, odd, "Mystery bank", "2025/26")
    page = AppTest.from_function(parked_tools_page, args=(str(settings.data_dir),), default_timeout=60)
    page.session_state["reviewer"] = REVIEWER
    page.run()
    assert not page.exception
    assert any("“Bank of abyssinia” → **“Bank of Abyssinia”**" in m.value for m in page.markdown)
    with closing(connect(settings.database_path)) as conn:
        assert get_document(conn, document_id)["bank_name"] == "Bank of abyssinia"  # shown, not applied
    next(b for b in page.button if b.label == "Apply 2 fix(es)").click().run()
    assert not page.exception
    next(s for s in page.selectbox if s.label == "Choose the bank").select("Zemen bank").run()
    next(b for b in page.button if b.label == "Apply" and not b.disabled).click().run()
    assert not page.exception
    with closing(connect(settings.database_path)) as conn:
        doc = get_document(conn, document_id)
        assert (doc["bank_name"], doc["reporting_period"]) == ("Bank of Abyssinia", "2025/26")
        assert get_document(conn, odd)["bank_name"] == "Zemen bank"
        assert {r["reviewer"] for r in fixes.corrections(conn)} == {REVIEWER}
    assert not any(m.value.startswith("**Fix names and years**") for m in page.markdown)
