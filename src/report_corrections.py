"""Fix a saved report's bank name or fiscal year in place, logged (D-084, D-085).

config/banks.yaml is the master list of bank names, and the fiscal year is always YYYY/YY. A report
saved earlier with "Bank of abyssinia" or "2026" is fixed to "Bank of Abyssinia" / "2025/26". Each
fix first writes an append-only report_corrections row (old value, new value, reason, who, when),
then updates the document, in one transaction; the database refuses any other change to a document.

Nothing here runs by itself: proposed_fixes only lists what could be fixed, and the reviewer
applies it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.database import utc_now
from src.project_service import get_document, list_documents
from src.report_period import canonical_period
from src.review_service import ReviewError

FIELDS = {"bank_name": "Bank", "reporting_period": "Report year"}
NAME_REASON = "Bank name differs from config/banks.yaml only in case or spacing (D-084)"
YEAR_REASON = "Report year saved in the fiscal-year form YYYY/YY (D-085)"
CHOSEN_BANK_REASON = "Bank chosen from config/banks.yaml by the reviewer (D-084)"
CHOSEN_YEAR_REASON = "Report year chosen by the reviewer (D-085)"


@dataclass(frozen=True)
class ProposedFix:
    document_id: str
    field: str  # bank_name | reporting_period
    old_value: str
    new_value: str
    reason: str
    label: str  # the report as listed, e.g. "Bank of abyssinia · 2026 (report.pdf)"


@dataclass(frozen=True)
class NameAndYearFixes:
    fixes: list[ProposedFix]          # can be applied as they are
    unmatched_banks: list[sqlite3.Row]  # documents whose bank is not in the list: the reviewer chooses
    unreadable_years: list[sqlite3.Row]  # documents whose year cannot be read: the reviewer chooses

    def __bool__(self) -> bool:
        return bool(self.fixes or self.unmatched_banks or self.unreadable_years)


def _squeezed(name: str) -> str:
    return "".join(name.split()).casefold()


def canonical_bank(name: str, banks: tuple[str, ...] | list[str]) -> str | None:
    """The listed spelling of a bank name that matches apart from case and spacing, else None."""
    key = _squeezed(name or "")
    return next((bank for bank in banks if _squeezed(bank) == key), None)


def report_label(document: sqlite3.Row) -> str:
    return f"{document['bank_name']} · {document['reporting_period']} ({document['original_filename']})"


def proposed_fixes(conn: sqlite3.Connection, banks: tuple[str, ...] | list[str]) -> NameAndYearFixes:
    """What could be fixed in the saved reports; nothing is changed."""
    fixes, unmatched, unreadable = [], [], []
    for d in list_documents(conn):
        bank = canonical_bank(d["bank_name"], banks)
        if bank is None:
            unmatched.append(d)
        elif bank != d["bank_name"]:
            fixes.append(ProposedFix(d["id"], "bank_name", d["bank_name"], bank, NAME_REASON, report_label(d)))
        period = canonical_period(d["reporting_period"])
        if period is None:
            unreadable.append(d)
        elif period != d["reporting_period"]:
            fixes.append(ProposedFix(d["id"], "reporting_period", d["reporting_period"], period, YEAR_REASON,
                                     report_label(d)))
    return NameAndYearFixes(fixes, unmatched, unreadable)


def correct_report(conn: sqlite3.Connection, document_id: str, field: str, new_value: str, reviewer: str,
                   reason: str, banks: tuple[str, ...] | list[str]) -> int:
    """Log, then make, one fix of a report's bank name or fiscal year, in one transaction.

    The new bank must be spelled as in config/banks.yaml; the new year must be YYYY/YY.
    Returns the report_corrections id."""
    with conn:
        return insert_correction(conn, document_id, field, new_value, reviewer, reason, banks)


def apply_fixes(conn: sqlite3.Connection, fixes: list[ProposedFix], reviewer: str,
                banks: tuple[str, ...] | list[str]) -> int:
    """Apply proposed fixes together: all of them, or none. Returns how many were applied."""
    with conn:
        for fix in fixes:
            document = get_document(conn, fix.document_id)
            if document is None or document[fix.field] != fix.old_value:
                raise ReviewError(f"{fix.label} changed since the list was shown. Look at the list again.")
            insert_correction(conn, fix.document_id, fix.field, fix.new_value, reviewer, fix.reason, banks)
    return len(fixes)


def insert_correction(conn: sqlite3.Connection, document_id: str, field: str, new_value: str, reviewer: str,
                      reason: str, banks: tuple[str, ...] | list[str]) -> int:
    """correct_report without committing, so a caller can apply several fixes in one transaction."""
    if field not in FIELDS:
        raise ReviewError(f"Only {', '.join(FIELDS)} of a saved report can be fixed.")
    if not reviewer or not reviewer.strip():
        raise ReviewError("Enter the reviewer name.")
    if not reason or not reason.strip():
        raise ReviewError("A reason is required for fixing a report.")
    document = get_document(conn, document_id)
    if document is None:
        raise ReviewError("That report is no longer in the database.")
    new_value = (new_value or "").strip()
    if field == "bank_name" and new_value not in banks:
        raise ReviewError(f"'{new_value}' is not in config/banks.yaml. Choose a bank from the list.")
    if field == "reporting_period" and canonical_period(new_value) != new_value:
        raise ReviewError(f"'{new_value}' is not a fiscal year in the form YYYY/YY, e.g. 2025/26.")
    old_value = document[field]
    if old_value == new_value:
        raise ReviewError(f"The {FIELDS[field].lower()} is already '{new_value}'.")
    correction_id = conn.execute(
        """INSERT INTO report_corrections (document_id, field, old_value, new_value, reason, reviewer, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (document_id, field, old_value, new_value, reason.strip(), reviewer.strip(), utc_now()),
    ).lastrowid
    # field is one of FIELDS (checked above), never user text
    conn.execute(f"UPDATE documents SET {field} = ? WHERE id = ?", (new_value, document_id))
    return correction_id


def corrections(conn: sqlite3.Connection, document_id: str | None = None) -> list[sqlite3.Row]:
    if document_id is None:
        return conn.execute("SELECT * FROM report_corrections ORDER BY id DESC").fetchall()
    return conn.execute("SELECT * FROM report_corrections WHERE document_id = ? ORDER BY id DESC",
                        (document_id,)).fetchall()
