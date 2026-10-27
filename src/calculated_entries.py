"""Calculated values as first-class entries (spec section 2, decision D-052).

A reviewer picks or types a formula; every name in it is resolved to a figure on the report:
  an approved metric of this report             -> 'metric'
  <metric>_prev                                  -> the prior-year column of that metric's row
                                                    ('prior_column'), else the same bank's
                                                    previous-year report ('previous_report')
  any other name (par_value, net_operating_income) -> an input figure marked on the page ('figure')
The result is saved as a proposal (decision manual_entry, entry_kind 'calculated') together with
the formula and a snapshot of every input with its page and box, in one transaction; it is
approved like any correction (D-030). A calculation is stale when an input it used has changed
since; that is worked out each time, never stored.

The legacy Calculations page (D-037, analyst-typed calculated columns) is unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from src.analysis import report_end_year
from src.config_loader import PREV_SUFFIX, AppConfig, bank_key
from src.database import utc_now
from src.formula import Formula, FormulaError, parse
from src.project_service import get_document, load_candidates
from src.review_service import ReviewError, extraction_status, insert_decision, parse_value, review_state

Box = tuple[float, float, float, float]
ROUNDING = {"fraction": Decimal("0.000001"), "ETB": Decimal("0.01"), "count": Decimal(1), "ratio": Decimal("0.0001")}
KIND_LABELS = {"metric": "approved metric", "prior_column": "prior-year column", "previous_report": "previous-year report",
               "figure": "figure marked on the page", "missing": "missing"}


@dataclass(frozen=True)
class CalcInput:
    """One input of a calculation: where its value comes from, and its box on the page."""

    variable: str
    label: str
    kind: str  # metric | prior_column | previous_report | figure | missing
    value: Decimal | None = None
    unit: str | None = None
    page: int | None = None
    box: Box | None = None
    printed: str = ""
    column_year: int | None = None
    row_label: str = ""
    source_document_id: str | None = None
    source_metric_id: str | None = None
    source_decision_id: int | None = None
    candidate_id: int | None = None
    input_figure_id: int | None = None
    problem: str = ""  # what is missing and how to supply it

    @property
    def where(self) -> str:
        """'PDF page 46 · 2023 column · row "Total equity" · approved metric'."""
        parts = []
        if self.page:
            parts.append(f"PDF page {self.page}")
        if self.column_year:
            parts.append(f"{self.column_year} column")
        if self.row_label:
            parts.append(f'row "{self.row_label}"')
        if self.printed:
            parts.append(f"printed {self.printed}")
        parts.append(KIND_LABELS[self.kind] + (" (another report)" if self.kind == "previous_report" else ""))
        return " · ".join(parts)


@dataclass(frozen=True)
class Preview:
    metric_id: str
    expression: str
    formula: Formula | None
    inputs: list[CalcInput]
    value: Decimal | None  # rounded to the metric's unit
    unrounded: Decimal | None
    words: str
    arithmetic: str
    problems: list[str]

    @property
    def ready(self) -> bool:
        return self.value is not None and not self.problems


@dataclass(frozen=True)
class SavedCalculation:
    id: int
    decision_id: int
    expression: str
    words: str
    arithmetic: str
    unrounded: Decimal
    created_at: str
    inputs: list[CalcInput]


# --- labels and units ---------------------------------------------------------------------

def variable_label(config: AppConfig, name: str) -> str:
    base = name[: -len(PREV_SUFFIX)] if name.endswith(PREV_SUFFIX) else None
    for metric_id, suffix in ((name, ""), (base, " (previous year)")):
        if metric_id is None:
            continue
        try:
            return config.metric(metric_id).display_name + suffix
        except KeyError:
            pass
    figure = config.input_figure(name)
    return figure.display_name if figure is not None else name.replace("_", " ").capitalize()


def variable_unit(config: AppConfig, name: str) -> str | None:
    for metric_id in (name, name[: -len(PREV_SUFFIX)] if name.endswith(PREV_SUFFIX) else None):
        if metric_id is None:
            continue
        try:
            return config.metric(metric_id).expected_unit
        except KeyError:
            pass
    figure = config.input_figure(name)
    return figure.expected_unit if figure is not None else None


def is_metric(config: AppConfig, name: str) -> bool:
    try:
        config.metric(name)
        return True
    except KeyError:
        return False


# --- resolving inputs -----------------------------------------------------------------------

def resolve_input(conn: sqlite3.Connection, config: AppConfig, document_id: str, name: str) -> CalcInput:
    label = variable_label(config, name)
    if is_metric(config, name):
        return _metric_input(conn, document_id, name, label)
    figure = latest_figure(conn, document_id, name)
    if figure is not None:
        return _figure_input(figure, name)
    if name.endswith(PREV_SUFFIX) and is_metric(config, name[: -len(PREV_SUFFIX)]):
        base = name[: -len(PREV_SUFFIX)]
        prior = _prior_column_input(conn, document_id, base, name, label)
        if prior is not None:
            return prior
        previous = _previous_report_input(conn, document_id, base, name, label)
        if previous is not None:
            return previous
        return CalcInput(name, label, "missing", problem=f"Mark {label} on the page: the prior-year column of "
                                                         f"{variable_label(config, base)} was not found.")
    return CalcInput(name, label, "missing", problem=f"Mark {label} on the page.")


def _metric_input(conn: sqlite3.Connection, document_id: str, metric_id: str, label: str) -> CalcInput:
    state = review_state(conn, document_id, metric_id)
    if state.status == "not_reported":
        return CalcInput(metric_id, label, "missing", problem=f"{label} is marked Not reported, so it cannot be used.")
    if state.status != "approved":
        status = "awaiting approval" if state.status == "corrected" else "not approved"
        return CalcInput(metric_id, label, "missing", problem=f"Approve {label} first (it is {status}).")
    decision = state.accepted_decision
    fields = dict(value=state.accepted_value, unit=state.accepted_unit, source_document_id=document_id,
                  source_metric_id=metric_id, source_decision_id=decision.id)
    candidate = _candidate(conn, decision.candidate_id) if decision.candidate_id is not None else None
    if decision.source_bbox is not None:
        fields |= dict(page=decision.source_page, box=tuple(decision.source_bbox))
    elif candidate is not None and not state.is_human_entered:
        fields |= dict(page=candidate["page_index"] + 1, box=tuple(json.loads(candidate["bbox"])),
                       printed=candidate["raw_value_text"], column_year=candidate["period_year"],
                       row_label=candidate["raw_label"])
    else:
        fields |= dict(page=decision.source_page or (candidate["page_index"] + 1 if candidate is not None else None))
    if candidate is not None and not fields.get("row_label") and decision.source_bbox is None:
        fields["row_label"] = candidate["raw_label"]
    return CalcInput(metric_id, label, "metric", **fields)


def _prior_column_input(conn: sqlite3.Connection, document_id: str, base: str, name: str, label: str) -> CalcInput | None:
    """The prior-year column of the metric's own row, as saved with the report."""
    flags = {r["id"]: r for r in conn.execute(
        "SELECT id, in_chart_row, is_duplicate FROM candidates WHERE document_id = ? AND metric_id = ?",
        (document_id, base))}
    for c in load_candidates(conn, document_id, base):
        f = flags.get(c.id)
        if c.period == "prior" and c.normalized_value is not None and not (f and (f["in_chart_row"] or f["is_duplicate"])):
            return CalcInput(name, label, "prior_column", value=c.normalized_value, unit=c.normalized_unit,
                             page=c.page_index + 1, box=c.bbox, printed=c.raw_value_text, column_year=c.period_year,
                             row_label=c.raw_label, source_document_id=document_id, candidate_id=c.id)
    return None


def _previous_report_input(conn: sqlite3.Connection, document_id: str, base: str, name: str,
                           label: str) -> CalcInput | None:
    """The metric approved in the same bank's report for the previous fiscal year."""
    document = get_document(conn, document_id)
    end_year = report_end_year(document["reporting_period"]) if document else None
    if end_year is None:
        return None
    for row in conn.execute("SELECT * FROM documents WHERE id != ? ORDER BY uploaded_at DESC", (document_id,)):
        if bank_key(row["bank_name"]) != bank_key(document["bank_name"]) or report_end_year(row["reporting_period"]) != end_year - 1:
            continue
        found = _metric_input(conn, row["id"], base, label)
        if found.kind == "metric":
            return CalcInput(name, label, "previous_report", value=found.value, unit=found.unit, page=found.page,
                             box=found.box, printed=found.printed, column_year=found.column_year,
                             row_label=found.row_label, source_document_id=row["id"], source_metric_id=base,
                             source_decision_id=found.source_decision_id)
    return None


def _figure_input(row: sqlite3.Row, name: str) -> CalcInput:
    return CalcInput(name, row["label"], "figure", value=Decimal(row["value"]), unit=row["unit"], page=row["page"],
                     box=tuple(json.loads(row["bbox"])), printed=row["printed_text"],
                     source_document_id=row["document_id"], input_figure_id=row["id"])


def latest_figure(conn: sqlite3.Connection, document_id: str, figure_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM input_figures WHERE document_id = ? AND figure_key = ? ORDER BY id DESC LIMIT 1",
                        (document_id, figure_key)).fetchone()


def _candidate(conn: sqlite3.Connection, candidate_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()


# --- preview and save ----------------------------------------------------------------------

def preview(conn: sqlite3.Connection, config: AppConfig, document_id: str, metric_id: str, expression: str) -> Preview:
    """Everything shown before saving: inputs and where each comes from, the arithmetic, the result."""
    try:
        target, formula = parse(expression)
    except FormulaError as exc:
        return Preview(metric_id, expression, None, [], None, None, "", "", [str(exc)])
    problems = []
    if target is not None and target != metric_id:
        problems.append(f"The formula is for '{target}', but this is {variable_label(config, metric_id)}.")
    if metric_id in formula.names:
        problems.append("The formula uses the value it is meant to work out.")
    inputs = [resolve_input(conn, config, document_id, name) for name in formula.names]
    problems += [i.problem for i in inputs if i.kind == "missing"]
    words = formula.render(lambda n: variable_label(config, n), words=True)
    values = {i.variable: i.value for i in inputs if i.value is not None}
    if len(values) < len(inputs) or problems:
        return Preview(metric_id, formula.text, formula, inputs, None, None, words, "", problems)
    try:
        unrounded = formula.evaluate(values)
    except FormulaError as exc:
        return Preview(metric_id, formula.text, formula, inputs, None, None, words, "", [str(exc)])
    unit = variable_unit(config, metric_id)
    value = unrounded.quantize(ROUNDING.get(unit, Decimal("0.000001")), rounding=ROUND_HALF_UP)
    numbers = {i.variable: number_text(i.value, i.unit) for i in inputs}
    arithmetic = f"{formula.render(numbers.get)} = {number_text(unrounded, unit, exact=True)}"
    return Preview(metric_id, formula.text, formula, inputs, value, unrounded, words, arithmetic, [])


def save_calculation(conn: sqlite3.Connection, config: AppConfig, document_id: str, metric_id: str, expression: str,
                     note: str, reviewer: str) -> int:
    """Save a calculated value as a proposal with its formula and inputs; returns the decision id."""
    with conn:  # the decision, the formula and every input together, or nothing
        return _insert_calculation(conn, config, document_id, metric_id, expression, note, reviewer)


def _insert_calculation(conn: sqlite3.Connection, config: AppConfig, document_id: str, metric_id: str,
                        expression: str, note: str, reviewer: str) -> int:
    """Check and add the calculation's rows without committing (the caller's transaction)."""
    if not reviewer or not reviewer.strip():
        raise ReviewError("Enter the reviewer name.")
    if not note or not note.strip():
        raise ReviewError("Write a note: say why this formula, e.g. 'ROE is not printed; average-equity basis'.")
    p = preview(conn, config, document_id, metric_id, expression)
    if not p.ready:
        raise ReviewError("The calculation cannot be saved yet: " + " ".join(p.problems or ["no result."]))
    decision_id = insert_decision(conn, document_id, metric_id, None, "manual_entry", reviewer=reviewer,
                                  value=p.value, unit=variable_unit(config, metric_id), reason=note,
                                  entry_kind="calculated")
    calculation_id = conn.execute(
        """INSERT INTO calculations (document_id, decision_id, expression, words, arithmetic, unrounded, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (document_id, decision_id, p.expression, p.words, p.arithmetic, str(p.unrounded), utc_now())).lastrowid
    for i in p.inputs:
        conn.execute(
            """INSERT INTO calculation_inputs (calculation_id, document_id, variable, label, value, unit,
                   source_kind, source_document_id, source_metric_id, source_decision_id, candidate_id,
                   input_figure_id, page, bbox, printed_text, column_year, row_label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (calculation_id, document_id, i.variable, i.label, str(i.value), i.unit, i.kind, i.source_document_id,
             i.source_metric_id, i.source_decision_id, i.candidate_id, i.input_figure_id, i.page,
             None if i.box is None else json.dumps([round(v, 2) for v in i.box]), i.printed, i.column_year,
             i.row_label))
    return decision_id


def record_input_figure(conn: sqlite3.Connection, document_id: str, figure_key: str, label: str, value: str | Decimal,
                        unit: str, page: int, bbox: Box, printed: str, reason: str, reviewer: str) -> int:
    """Record a figure marked on the page for use in formulas; a later record of the same key replaces it."""
    with conn:
        return _insert_input_figure(conn, document_id, figure_key, label, value, unit, page, bbox, printed, reason,
                                    reviewer)


def _insert_input_figure(conn: sqlite3.Connection, document_id: str, figure_key: str, label: str,
                         value: str | Decimal, unit: str, page: int, bbox: Box, printed: str, reason: str,
                         reviewer: str) -> int:
    """Check and add one input figure without committing (the caller's transaction)."""
    if not reviewer or not reviewer.strip():
        raise ReviewError("Enter the reviewer name.")
    if not reason or not reason.strip():
        raise ReviewError("Write a note on where the figure is from.")
    if extraction_status(conn, document_id).finished:
        raise ReviewError("The extraction review of this report is finished. Reopen it to mark figures.")
    parsed = parse_value(value)
    return conn.execute(
        """INSERT INTO input_figures (document_id, figure_key, label, value, unit, printed_text, page, bbox,
               reason, reviewer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (document_id, figure_key, label, str(parsed), unit, printed or "", page,
         json.dumps([round(v, 2) for v in bbox]), reason.strip(), reviewer.strip(), utc_now())).lastrowid


# --- EPS printed as a percentage of par value (D-070) -------------------------------------------

EPS_METRIC = "eps_etb"
EPS_FORMULA = "eps_percent * par_value"


@dataclass(frozen=True)
class FoundFigure:
    """A figure found in the PDF (page and box) to record as an input figure."""

    value: Decimal
    page: int
    bbox: Box
    printed: str


def eps_conversion_text(eps_percent: Decimal, par: Decimal, par_page: int) -> str:
    """'Convert: 39% x par value Birr 1,000 (p113) = 390 Birr'."""
    result = eps_percent * par
    return (f"Convert: {(eps_percent * 100).normalize():f}% x par value Birr {par:,f} (p{par_page}) = "
            f"{number_text(result, 'ETB', exact=True)} Birr")


def convert_eps_from_par(conn: sqlite3.Connection, config: AppConfig, document_id: str, eps_percent: FoundFigure,
                         par_value: FoundFigure, reviewer: str) -> int:
    """Record the printed EPS percentage and the par value as input figures (their boxes are the
    evidence) and save eps_percent * par_value as a calculated proposal, approved like any calculation.
    All three are saved in one transaction: if any part is refused, nothing is recorded."""
    note_eps = f"EPS printed as {eps_percent.printed} of par value on PDF page {eps_percent.page}"
    note_par = f"Par value stated on PDF page {par_value.page}"
    with conn:
        _insert_input_figure(conn, document_id, "eps_percent", variable_label(config, "eps_percent"),
                             eps_percent.value, "fraction", eps_percent.page, eps_percent.bbox, eps_percent.printed,
                             note_eps, reviewer)
        _insert_input_figure(conn, document_id, "par_value", variable_label(config, "par_value"), par_value.value,
                             "ETB", par_value.page, par_value.bbox, par_value.printed, note_par, reviewer)
        return _insert_calculation(conn, config, document_id, EPS_METRIC, EPS_FORMULA,
                                   f"EPS printed as a percentage of par value: {eps_percent.printed} x Birr "
                                   f"{par_value.printed} (p{par_value.page})", reviewer)


def eps_cross_check(conn: sqlite3.Connection, document_id: str) -> tuple[Decimal, Decimal, Decimal] | None:
    """(profit after tax, weighted average shares, profit / shares) when both are approved, else None."""
    profit = review_state(conn, document_id, "profit_after_tax_etb")
    shares = review_state(conn, document_id, "weighted_average_shares")
    if profit.status != "approved" or shares.status != "approved" or not shares.accepted_value:
        return None
    return profit.accepted_value, shares.accepted_value, profit.accepted_value / shares.accepted_value


# --- reading back ------------------------------------------------------------------------------

def saved_calculation(conn: sqlite3.Connection, decision_id: int) -> SavedCalculation | None:
    row = conn.execute("SELECT * FROM calculations WHERE decision_id = ?", (decision_id,)).fetchone()
    if row is None:
        return None
    inputs = [CalcInput(r["variable"], r["label"], r["source_kind"], value=Decimal(r["value"]), unit=r["unit"],
                        page=r["page"], box=None if r["bbox"] is None else tuple(json.loads(r["bbox"])),
                        printed=r["printed_text"], column_year=r["column_year"], row_label=r["row_label"],
                        source_document_id=r["source_document_id"], source_metric_id=r["source_metric_id"],
                        source_decision_id=r["source_decision_id"], candidate_id=r["candidate_id"],
                        input_figure_id=r["input_figure_id"])
              for r in conn.execute("SELECT * FROM calculation_inputs WHERE calculation_id = ? ORDER BY id", (row["id"],))]
    return SavedCalculation(row["id"], row["decision_id"], row["expression"], row["words"], row["arithmetic"],
                            Decimal(row["unrounded"]), row["created_at"], inputs)


def stale_reasons(conn: sqlite3.Connection, calc: SavedCalculation) -> list[str]:
    """Inputs that changed since the calculation: the value would now be different, or unsupported."""
    reasons = []
    for i in calc.inputs:
        if i.kind in ("metric", "previous_report"):
            if get_document(conn, i.source_document_id) is None:
                reasons.append(f"{i.label}: the report it came from was deleted.")
                continue
            state = review_state(conn, i.source_document_id, i.source_metric_id)
            if state.status != "approved":
                reasons.append(f"{i.label} is no longer approved.")
            elif state.accepted_value != i.value:
                reasons.append(f"{i.label} changed from {number_text(i.value, i.unit)} to "
                               f"{number_text(state.accepted_value, state.accepted_unit)}.")
        elif i.kind == "figure":
            latest = latest_figure(conn, i.source_document_id, i.variable)
            if latest is not None and latest["id"] != i.input_figure_id and Decimal(latest["value"]) != i.value:
                reasons.append(f"{i.label} was marked again: {number_text(i.value, i.unit)} → "
                               f"{number_text(Decimal(latest['value']), latest['unit'])}.")
    return reasons


def number_text(value: Decimal | None, unit: str | None, exact: bool = False) -> str:
    """How a number is written in arithmetic: 3,872,832,000 · 0.39 (39%) · 10,627."""
    if value is None:
        return "?"
    if exact and value != value.to_integral_value():
        value = value.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP)  # 10 decimals is plenty to check
    shown = value.normalize()
    return f"{shown:f}" if unit == "fraction" else f"{shown:,f}"
