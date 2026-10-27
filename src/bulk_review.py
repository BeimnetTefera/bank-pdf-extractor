"""Approve all verified (D-072) and the review work still open on a saved report (D-077).

Approve all verified is a shortcut for figures a reviewer would approve without a second look:
the suggested figure of a metric nobody has reviewed yet, from the report year's column,
converted to the metric's unit, positive, and with no warning. Anything else is left for a
person. Nothing is approved until the reviewer presses the confirmation (D-007), and each
figure is approved on its own, with the reviewer's name, through review_service.approve
(which still refuses any figure with a warning, D-031).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from src import review_service
from src.candidate_queries import CandidateKey
from src.config_loader import AppConfig
from src.models import AnalysisResult, MetricResult
from src.project_service import get_candidate, get_document
from src.review_service import ReviewError, ReviewState
from src.sector_data import without_sector_metrics

BULK_NOTE = "Approved with Approve all verified: report-year figure, converted, no warnings."


@dataclass(frozen=True)
class VerifiedFigure:
    """A suggested figure Approve all verified may approve."""

    metric_id: str
    metric_name: str
    candidate_id: int
    printed: str  # as printed, e.g. "1,234,567"
    value: Decimal  # the full value approving records, e.g. 1234567000
    unit: str  # "ETB", "count" or "fraction"
    page: int  # 1-based PDF page


@dataclass(frozen=True)
class LeftForPerson:
    metric_id: str
    metric_name: str
    reason: str


@dataclass(frozen=True)
class BulkResult:
    approved: list[str]  # metric ids
    skipped: list[LeftForPerson]  # changed since the list was shown, or refused by a review rule


def verified_figures(conn: sqlite3.Connection, document_id: str, analysis: AnalysisResult,
                     candidate_ids: dict[tuple[str, CandidateKey], int]) -> tuple[list[VerifiedFigure], list[LeftForPerson]]:
    """(figures that can be approved together, unreviewed metrics left for a person, with why).

    Metrics already reviewed or waiting for approval are in neither list."""
    if review_service.extraction_status(conn, document_id).finished:
        return [], []
    ready, left = [], []
    for result in analysis.metrics:
        state = review_service.review_state(conn, document_id, result.metric.id)
        if state.status != "open":
            continue
        reason = _why_left(conn, document_id, result, state, candidate_ids)
        if reason is not None:
            left.append(LeftForPerson(result.metric.id, result.metric.display_name, reason))
            continue
        primary = result.primary
        ready.append(VerifiedFigure(
            result.metric.id, result.metric.display_name, candidate_ids[(result.metric.id, primary.mapped.key)],
            primary.mapped.candidate.raw_value_text, primary.normalized.value, primary.normalized.unit,
            primary.mapped.candidate.page_index + 1))
    return ready, left


def approve_verified(conn: sqlite3.Connection, document_id: str, figures: list[VerifiedFigure],
                     reviewer: str) -> BulkResult:
    """Approve each listed figure one by one, with the reviewer's name. A figure whose metric
    was reviewed meanwhile, or whose saved value is no longer the one listed, is skipped."""
    if not reviewer or not reviewer.strip():
        raise ReviewError("Enter the reviewer name.")
    approved, skipped = [], []
    for f in figures:
        state = review_service.review_state(conn, document_id, f.metric_id)
        saved = get_candidate(conn, f.candidate_id)
        if state.status != "open":
            skipped.append(LeftForPerson(f.metric_id, f.metric_name, "It was reviewed meanwhile."))
            continue
        if saved is None or (saved.normalized_value, saved.normalized_unit) != (f.value, f.unit):
            skipped.append(LeftForPerson(f.metric_id, f.metric_name,
                                         "The saved figure is not the one listed; review it on its own."))
            continue
        try:
            review_service.approve(conn, f.candidate_id, reviewer, note=BULK_NOTE)
        except ReviewError as exc:
            skipped.append(LeftForPerson(f.metric_id, f.metric_name, str(exc)))
            continue
        approved.append(f.metric_id)
    return BulkResult(approved, skipped)


def _why_left(conn: sqlite3.Connection, document_id: str, result: MetricResult, state: ReviewState,
              candidate_ids: dict[tuple[str, CandidateKey], int]) -> str | None:
    """Why this unreviewed metric needs a person, or None when it can be approved with the others."""
    primary = result.primary
    if result.metric.is_sector:
        return "Sector figure: recorded on the Sector data page."
    if primary is None:
        return "No figure was found."
    if primary.mapped.period != "current":
        return "The suggested figure's year column was not identified."
    if primary.normalized.value is None:
        return f"Not converted: {primary.normalized.issue}"
    if primary.normalized.value < 0:
        return "Negative figure: a person chooses how its sign is recorded."
    if result.status != "needs_review":
        return "It has a warning to check."
    candidate_id = candidate_ids.get((result.metric.id, primary.mapped.key))
    if candidate_id is None:
        return "The suggested figure is not part of the saved report."
    if candidate_id in state.rejected_candidate_ids:
        return "The suggested figure was rejected."
    saved = get_candidate(conn, candidate_id)
    if saved is None or (saved.normalized_value, saved.normalized_unit) != (primary.normalized.value,
                                                                            primary.normalized.unit):
        return "The saved figure differs from the one on screen."
    if review_service.relevant_warnings(conn, document_id, result.metric.id, candidate_id):
        return "It has a warning to check."
    return None


# --- work left on a saved report -------------------------------------------------------

@dataclass(frozen=True)
class OutstandingWork:
    """Metrics of one saved report that still need a reviewer (sector metrics excluded)."""

    corrections: list[str]  # correction, manual entry or Not reported awaiting approval
    calculations: list[str]  # calculated value awaiting approval (D-052)
    not_reviewed: list[str]  # nothing decided yet

    @property
    def count(self) -> int:
        return len(self.corrections) + len(self.calculations) + len(self.not_reviewed)


def outstanding_work(conn: sqlite3.Connection, document_id: str, config: AppConfig) -> OutstandingWork:
    document = get_document(conn, document_id)
    metric_ids, _ = without_sector_metrics(config, json.loads(document["metric_ids"]) if document else [])
    corrections, calculations, not_reviewed = [], [], []
    for metric_id in metric_ids:
        state = review_service.review_state(conn, document_id, metric_id)
        if state.status == "open":
            not_reviewed.append(metric_id)
        elif state.status == "corrected":
            (calculations if state.pending.entry_kind == "calculated" else corrections).append(metric_id)
    return OutstandingWork(corrections, calculations, not_reviewed)
