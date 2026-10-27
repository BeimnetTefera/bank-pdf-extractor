"""Calculated columns: values a data analyst works out from a report's approved figures.

The app never calculates them (decision D-014). Once the extraction review of a report is
finished, the analyst enters each calculated column with a note on how it was worked out.
Entries are appended, never edited: the latest entry per column is the current value, and an
empty entry clears the column. Values are stored exactly as typed, in the dataset's form
(e.g. 0.0352 for a 3.52% ratio).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from src.config_loader import ExportTemplate
from src.database import utc_now
from src.review_service import ReviewError, extraction_status, parse_value


@dataclass(frozen=True)
class CalculatedValue:
    id: int
    column: str
    value: Decimal | None  # None: the column was cleared
    formula_note: str
    reviewer: str
    created_at: str


def enter_value(conn: sqlite3.Connection, template: ExportTemplate, document_id: str, column: str,
                value: str | Decimal | None, formula_note: str, reviewer: str) -> int:
    """Record a calculated value (or clear it with an empty value)."""
    if not reviewer or not reviewer.strip():
        raise ReviewError("Enter the reviewer name.")
    if column not in template.calculated:
        raise ReviewError(f"'{column}' is not a calculated column.")
    if not extraction_status(conn, document_id).finished:
        raise ReviewError("Finish the extraction review of this report first, so the figures are final.")
    parsed = None if value is None or str(value).strip() == "" else parse_value(value)
    if parsed is not None and not formula_note.strip():
        raise ReviewError(f"Add a note on how '{column}' was calculated (e.g. Total Loans / Total Deposits).")
    with conn:
        cursor = conn.execute(
            """INSERT INTO calculated_values (document_id, column_name, value, formula_note, reviewer, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (document_id, column, None if parsed is None else str(parsed), formula_note.strip(), reviewer.strip(),
             utc_now()),
        )
    return cursor.lastrowid


def history(conn: sqlite3.Connection, document_id: str) -> list[CalculatedValue]:
    rows = conn.execute("SELECT * FROM calculated_values WHERE document_id = ? ORDER BY id", (document_id,)).fetchall()
    return [
        CalculatedValue(r["id"], r["column_name"], None if r["value"] is None else Decimal(r["value"]),
                        r["formula_note"], r["reviewer"], r["created_at"])
        for r in rows
    ]


def current_values(conn: sqlite3.Connection, document_id: str) -> dict[str, CalculatedValue]:
    """The latest entry per column, leaving out cleared columns."""
    latest = {v.column: v for v in history(conn, document_id)}
    return {column: v for column, v in latest.items() if v.value is not None}
