"""Sector data (spec section 2c, D-056): figures for the whole banking sector, per fiscal year.

Some metrics describe the whole sector, not one bank (`level: sector` in metrics.yaml, e.g.
total deposits in the sector). They come from an external source such as the National Bank of
Ethiopia and are recorded once per fiscal year (YYYY/YY), shared by every bank: the Excel export
fills that column in every bank row of the year. Values reviewers approved inside a bank's own
report before this existed stay readable and are the export's fall-back.

Decisions are append-only (database triggers) and replayed like review decisions:
  record_value          propose a value from an external source (with its source row)
  record_not_reported   propose that no source gives the figure for the year (reason required)
  approve               accept the proposal
  reopen                withdraw a proposal or the current outcome (reason required)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from src.analysis import report_end_year
from src.config_loader import AppConfig, AppSettings, MetricDefinition
from src.database import utc_now
from src.external_sources import SavedSource, SourceInput, get_source, insert_source, prepare
from src.review_service import ReviewError


@dataclass(frozen=True)
class SectorDecision:
    id: int
    fiscal_year: str
    metric_id: str
    decision: str  # proposed | approved | reopened
    entry_kind: str | None  # external | not_reported (proposals)
    value: Decimal | None
    unit: str | None
    external_source_id: int | None
    approves_decision_id: int | None
    reason: str
    reviewer: str
    created_at: str


@dataclass
class SectorState:
    """Where a sector metric stands for a fiscal year after replaying its decisions."""

    status: str = "open"  # open | pending | approved | not_reported
    value: Decimal | None = None
    unit: str | None = None
    accepted: SectorDecision | None = None  # the approval
    pending: SectorDecision | None = None  # a proposal awaiting approval
    history: list[SectorDecision] = field(default_factory=list)

    @property
    def proposal(self) -> SectorDecision | None:
        """The proposal behind the current value: the pending one, or the one the approval accepted."""
        if self.pending is not None:
            return self.pending
        if self.accepted is None:
            return None
        return next((d for d in self.history if d.id == self.accepted.approves_decision_id), None)


# --- metrics and years ------------------------------------------------------------------------

def sector_metrics(config: AppConfig) -> tuple[MetricDefinition, ...]:
    return tuple(m for m in config.metrics if m.is_sector)


def is_sector_metric(config: AppConfig, metric_id: str) -> bool:
    return any(m.id == metric_id for m in sector_metrics(config))


def without_sector_metrics(config: AppConfig, metric_ids: list[str]) -> tuple[list[str], list[str]]:
    """(bank metrics, sector metrics) of a list, in order. A sector metric left open in a report is not
    missing work: its export column is filled from the Sector data page."""
    sector = {m.id for m in sector_metrics(config)}
    return [m for m in metric_ids if m not in sector], [m for m in metric_ids if m in sector]


def fiscal_year_key(text: str) -> str:
    """'2025/26', '2026', '2025-2026' -> '2025/26' (the Shega fiscal_year form, D-017)."""
    end_year = report_end_year(text or "")
    if end_year is None:
        raise ReviewError(f"'{text}' is not a fiscal year. Use the form YYYY/YY, e.g. 2025/26.")
    from src.excel_exporter import fiscal_year_label  # local: the exporter imports this module

    return fiscal_year_label(end_year)


def fiscal_years(conn: sqlite3.Connection) -> list[str]:
    """Fiscal years with a saved report or a sector decision, newest first."""
    years = {r["fiscal_year"] for r in conn.execute("SELECT DISTINCT fiscal_year FROM sector_decisions")}
    for row in conn.execute("SELECT reporting_period FROM documents"):
        end_year = report_end_year(row["reporting_period"])
        if end_year is not None:
            years.add(fiscal_year_key(str(end_year)))
    return sorted(years, reverse=True)


def banks_for_year(conn: sqlite3.Connection, fiscal_year: str) -> list[str]:
    """Banks with a saved report for the fiscal year: their export rows get the sector value."""
    end_year = report_end_year(fiscal_year)
    names = {r["bank_name"] for r in conn.execute("SELECT bank_name, reporting_period FROM documents")
             if report_end_year(r["reporting_period"]) == end_year}
    return sorted(names, key=str.casefold)


# --- actions ------------------------------------------------------------------------------------

def record_value(conn: sqlite3.Connection, config: AppConfig, settings: AppSettings, fiscal_year: str, metric_id: str,
                 source: SourceInput, note: str, reviewer: str) -> int:
    """Propose the sector value for the year from an external source; returns the decision id."""
    metric = _sector_metric(config, metric_id)
    year = fiscal_year_key(fiscal_year)
    _require(reviewer, "Enter the reviewer name.")
    _require_open(conn, year, metric)
    value, file = prepare(source, metric.expected_unit, settings)
    with conn:  # the source and the decision together, or nothing
        source_id = insert_source(conn, source, value, metric.expected_unit, file, purpose="sector", reviewer=reviewer)
        return _insert(conn, year, metric_id, "proposed", reviewer, note, entry_kind="external", value=value,
                       unit=metric.expected_unit, source_id=source_id)


def record_not_reported(conn: sqlite3.Connection, config: AppConfig, fiscal_year: str, metric_id: str, reason: str,
                        reviewer: str) -> int:
    """Propose that no source gives this sector figure for the year (not zero, not missing work)."""
    metric = _sector_metric(config, metric_id)
    year = fiscal_year_key(fiscal_year)
    _require(reviewer, "Enter the reviewer name.")
    if not reason or len(reason.strip()) < 10:
        raise ReviewError("Say why it is not reported, e.g. 'NBE has not published the 2025/26 annual report yet'.")
    _require_open(conn, year, metric)
    with conn:
        return _insert(conn, year, metric_id, "proposed", reviewer, reason, entry_kind="not_reported")


def approve(conn: sqlite3.Connection, config: AppConfig, fiscal_year: str, metric_id: str, reviewer: str,
            note: str = "") -> int:
    _sector_metric(config, metric_id)
    year = fiscal_year_key(fiscal_year)
    _require(reviewer, "Enter the reviewer name.")
    pending = sector_state(conn, year, metric_id).pending
    if pending is None:
        raise ReviewError("There is no sector value waiting for approval.")
    with conn:
        return _insert(conn, year, metric_id, "approved", reviewer, note, value=pending.value, unit=pending.unit,
                       source_id=pending.external_source_id, approves=pending.id)


def reopen(conn: sqlite3.Connection, config: AppConfig, fiscal_year: str, metric_id: str, reviewer: str,
           reason: str) -> int:
    _sector_metric(config, metric_id)
    year = fiscal_year_key(fiscal_year)
    _require(reviewer, "Enter the reviewer name.")
    _require(reason, "A reason is required for reopening.")
    if sector_state(conn, year, metric_id).status == "open":
        raise ReviewError("There is nothing to reopen: no sector value is recorded for this year.")
    with conn:
        return _insert(conn, year, metric_id, "reopened", reviewer, reason)


# --- state --------------------------------------------------------------------------------------

def decisions(conn: sqlite3.Connection, fiscal_year: str, metric_id: str) -> list[SectorDecision]:
    rows = conn.execute("SELECT * FROM sector_decisions WHERE fiscal_year = ? AND metric_id = ? ORDER BY id",
                        (fiscal_year, metric_id)).fetchall()
    return [SectorDecision(r["id"], r["fiscal_year"], r["metric_id"], r["decision"], r["entry_kind"],
                           None if r["value"] is None else Decimal(r["value"]), r["unit"], r["external_source_id"],
                           r["approves_decision_id"], r["reason"], r["reviewer"], r["created_at"]) for r in rows]


def sector_state(conn: sqlite3.Connection, fiscal_year: str, metric_id: str) -> SectorState:
    state = SectorState()
    for d in decisions(conn, fiscal_year, metric_id):
        state.history.append(d)
        if d.decision == "proposed":
            state.status, state.pending = "pending", d
            state.value = state.unit = state.accepted = None
        elif d.decision == "approved":
            kind = state.pending.entry_kind if state.pending is not None else None
            state.status = "not_reported" if kind == "not_reported" else "approved"
            state.value, state.unit, state.accepted, state.pending = d.value, d.unit, d, None
        elif d.decision == "reopened":
            state.status, state.pending = "open", None
            state.value = state.unit = state.accepted = None
    return state


def state_source(conn: sqlite3.Connection, state: SectorState) -> SavedSource | None:
    """The external source behind the current (or pending) sector value."""
    proposal = state.proposal
    if proposal is None or proposal.external_source_id is None:
        return None
    return get_source(conn, proposal.external_source_id)


# --- helpers ------------------------------------------------------------------------------------

def _insert(conn: sqlite3.Connection, year: str, metric_id: str, decision: str, reviewer: str, reason: str, *,
            entry_kind: str | None = None, value: Decimal | None = None, unit: str | None = None,
            source_id: int | None = None, approves: int | None = None) -> int:
    return conn.execute(
        """INSERT INTO sector_decisions (fiscal_year, metric_id, decision, entry_kind, value, unit, external_source_id,
               approves_decision_id, reason, reviewer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (year, metric_id, decision, entry_kind, None if value is None else str(value), unit, source_id, approves,
         (reason or "").strip(), reviewer.strip(), utc_now())).lastrowid


def _sector_metric(config: AppConfig, metric_id: str) -> MetricDefinition:
    metric = next((m for m in sector_metrics(config) if m.id == metric_id), None)
    if metric is None:
        raise ReviewError(f"'{metric_id}' is not a sector metric (level: sector in config/metrics.yaml).")
    return metric


def _require_open(conn: sqlite3.Connection, year: str, metric: MetricDefinition) -> None:
    status = sector_state(conn, year, metric.id).status
    if status == "pending":
        raise ReviewError(f"{metric.display_name} for {year} already has a value awaiting approval. Approve it or "
                          "withdraw it first.")
    if status in ("approved", "not_reported"):
        raise ReviewError(f"{metric.display_name} for {year} is already approved. Reopen it (with a reason) first.")


def _require(text: str, message: str) -> None:
    if not text or not text.strip():
        raise ReviewError(message)
