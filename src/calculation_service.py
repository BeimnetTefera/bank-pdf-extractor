"""Calculated columns: values a data analyst works out from a report's approved figures.

Once the extraction review of a report is finished, the analyst enters each calculated column
with a note on how it was worked out. Where export_template.yaml gives the column a draft
formula, the app works it out from the approved figures as a proposal (auto_values, D-088) and
shows the difference from the entered and the printed value; it never stores it by itself.
Entries are appended, never edited: the latest entry per column is the current value, and an
empty entry clears the column. Values are stored exactly as typed, in the dataset's form
(e.g. 0.0352 for a 3.52% ratio).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from src.calculated_entries import ROUNDING, CalcInput, number_text, resolve_input, variable_label
from src.config_loader import AppConfig, ColumnFormula, ExportTemplate
from src.database import utc_now
from src.formula import FormulaError, parse
from src.review_service import ReviewError, extraction_status, parse_value, review_state

FLAG_ABOVE = Decimal("0.005")  # a difference above 0.5% of the value is flagged; smaller is rounding


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


# --- proposed values from the draft formulas (D-088) ----------------------------------------

@dataclass(frozen=True)
class AutoValue:
    column: str
    formula: ColumnFormula
    words: str  # the formula with figure names
    inputs: list[CalcInput]
    value: Decimal | None  # rounded to the formula's unit; None when an input is missing
    arithmetic: str  # "1,000 / 2,000 = 0.5"
    problems: list[str]
    reported: Decimal | None = None  # the approved printed figure of compare_with

    @property
    def note(self) -> str:
        """The formula note saved when the analyst accepts the value."""
        return f"Auto (draft formula): {self.words}: {self.arithmetic}"


def auto_values(conn: sqlite3.Connection, config: AppConfig, document_id: str) -> dict[str, AutoValue]:
    """Each calculated column with a formula, worked out from this report's approved figures."""
    return {f.column: auto_value(conn, config, document_id, f) for f in config.export_template.formulas}


def auto_value(conn: sqlite3.Connection, config: AppConfig, document_id: str, f: ColumnFormula) -> AutoValue:
    _, formula = parse(f.formula)  # checked when the config was loaded
    words = formula.render(lambda n: variable_label(config, n), words=True)
    inputs = [resolve_input(conn, config, document_id, name) for name in formula.names]
    reported = None
    if f.compare_with is not None:
        state = review_state(conn, document_id, f.compare_with)
        reported = state.accepted_value if state.status == "approved" else None
    problems = [i.problem for i in inputs if i.kind == "missing"]
    if problems:
        return AutoValue(f.column, f, words, inputs, None, "", problems, reported)
    try:
        unrounded = formula.evaluate({i.variable: i.value for i in inputs})
    except FormulaError as exc:
        return AutoValue(f.column, f, words, inputs, None, "", [str(exc)], reported)
    numbers = {i.variable: number_text(i.value, i.unit) for i in inputs}
    arithmetic = f"{formula.render(numbers.get)} = {number_text(unrounded, f.unit, exact=True)}"
    value = unrounded.quantize(ROUNDING[f.unit], rounding=ROUND_HALF_UP)
    return AutoValue(f.column, f, words, inputs, value, arithmetic, [], reported)


def difference(value: Decimal | None, reference: Decimal | None) -> tuple[Decimal, bool] | None:
    """value - reference, and whether it is big enough to flag (more than 0.5% of the reference)."""
    if value is None or reference is None:
        return None
    gap = value - reference
    if reference == 0:
        return gap, gap != 0
    return gap, abs(gap) / abs(reference) > FLAG_ABOVE


def accept_auto_values(conn: sqlite3.Connection, config: AppConfig, document_id: str, reviewer: str) -> list[str]:
    """Record the auto value of every column that has one and no entered value yet; the columns saved."""
    entered = current_values(conn, document_id)
    saved = []
    for column, auto in auto_values(conn, config, document_id).items():
        if auto.value is not None and column not in entered:
            enter_value(conn, config.export_template, document_id, column, auto.value, auto.note, reviewer)
            saved.append(column)
    return saved
