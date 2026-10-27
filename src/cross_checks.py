"""Cross-checks between figures (decision D-045). Advisory only: they never change, approve or
reject a figure, and they are not saved; they are worked out again whenever the report is shown.

Two kinds:
  sum checks   total = sum of parts within one report (config/checks.yaml), e.g.
               total assets = total liabilities + total equity
  last year    this report's prior-year column against the figures approved in the same
               bank's report for the previous fiscal year

Each check uses the metric's current figure: the approved value, else a correction awaiting
approval, else the suggested (not yet reviewed) figure. The result says which were used.
Signs are ignored (the size of each figure is compared), because banks print expenses in
brackets and reviewers record either sign (D-028). The allowed difference is the rounding of
the printed figures: half of the last printed digit of each.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from src.analysis import report_end_year
from src.candidate_queries import CandidateKey
from src.config_loader import SumCheck
from src.models import AnalysisResult
from src.review_service import ReviewState, approved_results

MAX_IMPLIED_EXPONENT = 6  # trailing zeros beyond a million are not read as rounding


@dataclass(frozen=True)
class Figure:
    value: Decimal
    tolerance: Decimal  # rounding of the printed figure, in the metric's unit
    source: str  # "approved", "awaiting approval" or "suggested"


@dataclass(frozen=True)
class CheckResult:
    name: str
    outcome: str  # "pass", "fail" or "skipped"
    message: str


@dataclass(frozen=True)
class PreviousReport:
    label: str  # e.g. "Awash bank 2024/25"
    values: dict[str, Decimal]  # approved figures by metric id


def run_checks(conn: sqlite3.Connection, checks: tuple[SumCheck, ...], analysis: AnalysisResult,
               states: dict[str, ReviewState], candidate_ids: dict[tuple[str, CandidateKey], int],
               bank_name: str, period: str) -> list[CheckResult]:
    figures = current_figures(analysis, states, candidate_ids)
    end_year = report_end_year(period)
    results = sum_checks(checks, figures, analysis)
    if end_year is not None:
        previous = previous_report(conn, bank_name, end_year)
        results += prior_year_checks(analysis, previous, f"{end_year - 2}/{str(end_year - 1)[2:]}")
    return results


def current_figures(analysis: AnalysisResult, states: dict[str, ReviewState],
                    candidate_ids: dict[tuple[str, CandidateKey], int]) -> dict[str, Figure]:
    """Each metric's figure to check: approved, else awaiting approval, else suggested."""
    key_by_id = {candidate_id: key for (_, key), candidate_id in candidate_ids.items()}
    figures: dict[str, Figure] = {}
    for result in analysis.metrics:
        metric_id = result.metric.id
        state = states.get(metric_id)
        by_key = {mc.mapped.key: mc for mc in result.candidates}
        if state is not None and state.status == "approved":
            decision = state.accepted_decision
            mc = None if state.is_human_entered else by_key.get(key_by_id.get(decision.candidate_id))
            tolerance = mc.tolerance if mc is not None else implied_tolerance(state.accepted_value)
            figures[metric_id] = Figure(state.accepted_value, tolerance, "approved")
        elif state is not None and state.status == "corrected" and state.pending.reviewed_value is not None:
            value = state.pending.reviewed_value
            figures[metric_id] = Figure(value, implied_tolerance(value), "awaiting approval")
        elif result.primary is not None and result.primary.normalized.value is not None:
            primary = result.primary
            rejected = state is not None and candidate_ids.get((metric_id, primary.mapped.key)) in state.rejected_candidate_ids
            if not rejected:
                figures[metric_id] = Figure(primary.normalized.value, primary.tolerance, "suggested")
    return figures


def sum_checks(checks: tuple[SumCheck, ...], figures: dict[str, Figure], analysis: AnalysisResult) -> list[CheckResult]:
    """Checks whose metrics are all part of this report; the others are left out."""
    names = {m.metric.id: m.metric.display_name for m in analysis.metrics}
    results = []
    for check in checks:
        metric_ids = (check.total, *check.parts)
        if not all(m in names for m in metric_ids):
            continue
        missing = [names[m] for m in metric_ids if m not in figures]
        if missing:
            results.append(CheckResult(check.name, "skipped", f"Needs a figure for: {', '.join(missing)}."))
            continue
        total = abs(figures[check.total].value)
        parts = sum(abs(figures[p].value) for p in check.parts)
        allowed = sum(figures[m].tolerance for m in metric_ids)
        difference = abs(total - parts)
        terms = " + ".join(f"{names[p]} {_number(abs(figures[p].value))}" for p in check.parts)
        if difference <= allowed:
            message = f"{names[check.total]} {_number(total)} = {terms}"
            if difference:
                message += f" (difference {_number(difference)}, within rounding)"
            message += "."
            outcome = "pass"
        else:
            message = (f"{names[check.total]} is {_number(total)}, but {terms} = {_number(parts)}: "
                       f"off by {_number(difference)} (rounding allows {_number(allowed)}). "
                       "One of these figures is probably wrong.")
            outcome = "fail"
        unreviewed = [names[m] for m in metric_ids if figures[m].source != "approved"]
        if unreviewed:
            message += f" Not yet approved: {', '.join(unreviewed)}."
        results.append(CheckResult(check.name, outcome, message))
    return results


def previous_report(conn: sqlite3.Connection, bank_name: str, end_year: int) -> PreviousReport | None:
    """The newest saved report of the bank for the previous fiscal year that has approved figures."""
    rows = conn.execute("SELECT * FROM documents WHERE bank_name = ? ORDER BY uploaded_at DESC, id DESC",
                        (bank_name,)).fetchall()
    for row in rows:
        if report_end_year(row["reporting_period"]) != end_year - 1:
            continue
        values = {r.metric_id: r.value for r in approved_results(conn, row["id"])}
        if values:
            return PreviousReport(f"{row['bank_name']} {row['reporting_period']}", values)
    return None


def prior_year_checks(analysis: AnalysisResult, previous: PreviousReport | None,
                      previous_year: str) -> list[CheckResult]:
    """Compare this report's prior-year column with last year's approved figures."""
    priors = {}
    for result in analysis.metrics:
        mc = next((mc for mc in result.candidates
                   if mc.mapped.period == "prior" and mc.normalized.value is not None
                   and mc.mapped.duplicate_of is None and not mc.mapped.in_chart_row), None)
        if mc is not None:
            priors[result.metric] = mc
    if not priors:
        return []
    if previous is None:
        return [CheckResult("Last year's figures", "skipped",
                            f"No {previous_year} report of this bank with approved figures is saved, so the "
                            "prior-year column cannot be compared with last year's figures.")]
    results = []
    for metric, mc in priors.items():
        if metric.id not in previous.values:
            continue
        old = abs(previous.values[metric.id])
        printed = abs(mc.normalized.value)
        allowed = mc.tolerance + implied_tolerance(old)
        difference = abs(printed - old)
        c = mc.mapped.candidate
        where = f"prior-year column {c.raw_value_text} (PDF page {c.page_index + 1})"
        unit = metric.expected_unit
        if difference <= allowed:
            results.append(CheckResult(f"{metric.display_name}: last year", "pass",
                                       f"The {where} matches {previous.label}: {_number(old, unit)}."))
        else:
            results.append(CheckResult(
                f"{metric.display_name}: last year", "fail",
                f"The {where} is {_number(printed, unit)}, but {previous.label} has {_number(old, unit)} approved: "
                f"off by {_number(difference, unit)}. The figure may have been restated, or one of them is wrong."))
    return results


def implied_tolerance(value: Decimal) -> Decimal:
    """Rounding shown by a typed figure's trailing zeros: 286,226,976,000 -> 500; 0.29 -> 0.005."""
    exponent = min(value.normalize().as_tuple().exponent, MAX_IMPLIED_EXPONENT)
    return Decimal(5).scaleb(exponent - 1)


def _number(value: Decimal, unit: str = "ETB") -> str:
    if unit == "fraction":
        return f"{(value * 100).normalize():f}%"
    return f"{value:,f}"  # fixed-point: 5.00E+6 -> 5,000,000
