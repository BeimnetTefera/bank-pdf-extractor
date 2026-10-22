"""Review actions and review state. Decisions are appended, never edited or deleted.

Actions (one metric of one document at a time):
  approve            accept an extracted candidate; reviewer picks the sign convention
  approve_correction accept a pending correction or manual entry
  correct            propose a replacement value for a candidate (reason required)
  manual_entry       propose a value for a metric with no usable candidate (page + reason)
  reject             mark a candidate wrong (reason required)
  reopen             withdraw the current outcome (reason required)

The current state is derived by replaying the metric's decisions in order, so the full
history is always kept and the original extracted candidate is never changed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from src.database import utc_now
from src.models import METRIC_LEVEL_RULES
from src.project_service import SavedCandidate, get_candidate, get_document

SIGN_CHOICES = ("as_printed", "positive")
REVIEW_UNITS = ("ETB", "count", "fraction")


class ReviewError(ValueError):
    """Raised when a review action is not allowed; the message says what is missing."""


@dataclass(frozen=True)
class Decision:
    id: int
    metric_id: str
    candidate_id: int | None
    decision: str
    reviewed_value: Decimal | None
    reviewed_unit: str | None
    sign_choice: str | None
    source_page: int | None
    approves_decision_id: int | None
    warnings_acknowledged: bool
    reason: str
    reviewer: str
    created_at: str


@dataclass
class ReviewState:
    """Where a metric stands after replaying its decisions."""

    status: str = "open"  # open | approved | corrected (correction/manual entry awaiting approval)
    accepted_value: Decimal | None = None
    accepted_unit: str | None = None
    accepted_decision: Decision | None = None  # the approval that produced the value
    pending: Decision | None = None  # correction / manual entry awaiting approval
    rejected_candidate_ids: set[int] = field(default_factory=set)
    history: list[Decision] = field(default_factory=list)

    @property
    def is_human_entered(self) -> bool:
        """True when the accepted value came from a correction or manual entry, not extraction."""
        return self.accepted_decision is not None and self.accepted_decision.approves_decision_id is not None


# --- actions -----------------------------------------------------------------------

def approve(conn: sqlite3.Connection, candidate_id: int, reviewer: str, *, sign: str = "as_printed",
            acknowledge_warnings: bool = False, note: str = "") -> int:
    candidate = _candidate(conn, candidate_id)
    _require_reviewer(reviewer)
    if sign not in SIGN_CHOICES:
        raise ReviewError(f"Sign choice must be one of {SIGN_CHOICES}.")
    if candidate.normalized_value is None:
        raise ReviewError(
            "This figure could not be converted to the metric's unit "
            f"({candidate.normalization_issue or 'no value'}). Use Correct to enter the right value instead."
        )
    if candidate_id in review_state(conn, candidate.document_id, candidate.metric_id).rejected_candidate_ids:
        raise ReviewError("This candidate was rejected. Reopen the metric before approving it.")
    _require_acknowledgement(conn, candidate.document_id, candidate.metric_id, acknowledge_warnings, candidate_id)

    value = abs(candidate.normalized_value) if sign == "positive" else candidate.normalized_value
    return _append(conn, candidate.document_id, candidate.metric_id, candidate_id, "approved",
                   value=value, unit=candidate.normalized_unit, sign=sign,
                   acknowledged=acknowledge_warnings, reason=note, reviewer=reviewer)


def correct(conn: sqlite3.Connection, candidate_id: int, reviewer: str, value: str | Decimal, unit: str,
            reason: str) -> int:
    candidate = _candidate(conn, candidate_id)
    _require_reviewer(reviewer)
    _require_reason(reason, "a correction")
    return _append(conn, candidate.document_id, candidate.metric_id, candidate_id, "corrected",
                   value=_parse_value(value), unit=_check_unit(unit), reason=reason, reviewer=reviewer)


def manual_entry(conn: sqlite3.Connection, document_id: str, metric_id: str, reviewer: str, value: str | Decimal,
                 unit: str, source_page: int, reason: str) -> int:
    document = get_document(conn, document_id)
    if document is None:
        raise ReviewError(f"Unknown document {document_id}.")
    _require_reviewer(reviewer)
    _require_reason(reason, "a manual entry")
    if not isinstance(source_page, int) or not 1 <= source_page <= (document["page_count"] or 0):
        raise ReviewError(f"Enter the PDF page the value comes from (1 to {document['page_count']}).")
    return _append(conn, document_id, metric_id, None, "manual_entry", value=_parse_value(value),
                   unit=_check_unit(unit), page=source_page, reason=reason, reviewer=reviewer)


def approve_correction(conn: sqlite3.Connection, document_id: str, metric_id: str, reviewer: str, *,
                       acknowledge_warnings: bool = False, note: str = "") -> int:
    _require_reviewer(reviewer)
    pending = review_state(conn, document_id, metric_id).pending
    if pending is None:
        raise ReviewError("There is no correction or manual entry waiting for approval.")
    _require_acknowledgement(conn, document_id, metric_id, acknowledge_warnings)
    return _append(conn, document_id, metric_id, pending.candidate_id, "approved",
                   value=pending.reviewed_value, unit=pending.reviewed_unit, page=pending.source_page,
                   approves=pending.id, acknowledged=acknowledge_warnings, reason=note, reviewer=reviewer)


def reject(conn: sqlite3.Connection, candidate_id: int, reviewer: str, reason: str) -> int:
    candidate = _candidate(conn, candidate_id)
    _require_reviewer(reviewer)
    _require_reason(reason, "a rejection")
    return _append(conn, candidate.document_id, candidate.metric_id, candidate_id, "rejected",
                   reason=reason, reviewer=reviewer)


def reopen(conn: sqlite3.Connection, document_id: str, metric_id: str, reviewer: str, reason: str) -> int:
    _require_reviewer(reviewer)
    _require_reason(reason, "reopening")
    return _append(conn, document_id, metric_id, None, "reopened", reason=reason, reviewer=reviewer)


# --- state -------------------------------------------------------------------------

def decisions(conn: sqlite3.Connection, document_id: str, metric_id: str) -> list[Decision]:
    rows = conn.execute(
        "SELECT * FROM review_decisions WHERE document_id = ? AND metric_id = ? ORDER BY id",
        (document_id, metric_id),
    ).fetchall()
    return [_decision(r) for r in rows]


def review_state(conn: sqlite3.Connection, document_id: str, metric_id: str) -> ReviewState:
    state = ReviewState()
    for d in decisions(conn, document_id, metric_id):
        state.history.append(d)
        if d.decision == "approved":
            state.status, state.pending = "approved", None
            state.accepted_value, state.accepted_unit, state.accepted_decision = d.reviewed_value, d.reviewed_unit, d
        elif d.decision in ("corrected", "manual_entry"):
            # A new proposed value replaces any earlier outcome until it is approved.
            state.status, state.pending = "corrected", d
            state.accepted_value = state.accepted_unit = state.accepted_decision = None
        elif d.decision == "rejected":
            state.rejected_candidate_ids.add(d.candidate_id)
            if state.accepted_decision is not None and state.accepted_decision.candidate_id == d.candidate_id:
                state.status = "open"
                state.accepted_value = state.accepted_unit = state.accepted_decision = None
            if state.pending is not None and state.pending.candidate_id == d.candidate_id:
                state.status, state.pending = "open", None
        elif d.decision == "reopened":
            state.status, state.pending = "open", None
            state.accepted_value = state.accepted_unit = state.accepted_decision = None
            state.rejected_candidate_ids.clear()
    return state


@dataclass(frozen=True)
class ApprovedResult:
    document: sqlite3.Row
    metric_id: str
    value: Decimal
    unit: str
    approval: Decision
    candidate: SavedCandidate | None  # None for a manual entry
    source_page: int  # 1-based
    human_entered: bool


def approved_results(conn: sqlite3.Connection, document_id: str | None = None) -> list[ApprovedResult]:
    """Only metrics whose current state is approved. Everything else is excluded."""
    if document_id is None:
        documents = conn.execute("SELECT * FROM documents ORDER BY uploaded_at").fetchall()
    else:
        documents = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchall()
    results = []
    for doc in documents:
        metric_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT metric_id FROM review_decisions WHERE document_id = ? ORDER BY metric_id", (doc["id"],)
        )]
        for metric_id in metric_ids:
            state = review_state(conn, doc["id"], metric_id)
            if state.status != "approved":
                continue
            approval = state.accepted_decision
            candidate = get_candidate(conn, approval.candidate_id) if approval.candidate_id else None
            page = approval.source_page or (candidate.page_index + 1 if candidate else None)
            results.append(ApprovedResult(doc, metric_id, state.accepted_value, state.accepted_unit, approval,
                                          candidate, page, state.is_human_entered))
    return results


# --- helpers -----------------------------------------------------------------------

def _append(conn: sqlite3.Connection, document_id: str, metric_id: str, candidate_id: int | None, decision: str, *,
            reviewer: str, value: Decimal | None = None, unit: str | None = None, sign: str | None = None,
            page: int | None = None, approves: int | None = None, acknowledged: bool = False, reason: str = "") -> int:
    with conn:
        cursor = conn.execute(
            """INSERT INTO review_decisions (document_id, metric_id, candidate_id, decision, reviewed_value,
                   reviewed_unit, sign_choice, source_page, approves_decision_id, warnings_acknowledged, reason,
                   reviewer, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (document_id, metric_id, candidate_id, decision, None if value is None else str(value), unit, sign,
             page, approves, int(acknowledged), reason.strip(), reviewer.strip(), utc_now()),
        )
    return cursor.lastrowid


def _decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"], metric_id=row["metric_id"], candidate_id=row["candidate_id"], decision=row["decision"],
        reviewed_value=None if row["reviewed_value"] is None else Decimal(row["reviewed_value"]),
        reviewed_unit=row["reviewed_unit"], sign_choice=row["sign_choice"], source_page=row["source_page"],
        approves_decision_id=row["approves_decision_id"], warnings_acknowledged=bool(row["warnings_acknowledged"]),
        reason=row["reason"], reviewer=row["reviewer"], created_at=row["created_at"],
    )


def _candidate(conn: sqlite3.Connection, candidate_id: int) -> SavedCandidate:
    candidate = get_candidate(conn, candidate_id)
    if candidate is None:
        raise ReviewError(f"Unknown candidate {candidate_id}.")
    return candidate


def relevant_warnings(conn: sqlite3.Connection, document_id: str, metric_id: str,
                      candidate_id: int | None = None) -> list[sqlite3.Row]:
    """Warnings that must be acknowledged before approving: metric-wide ones (conflicts,
    ambiguous wording) and those about the figure being approved (same rule as D-027)."""
    rows = conn.execute(
        """SELECT * FROM findings WHERE document_id = ? AND metric_id = ?
           AND severity IN ('warning', 'error') AND resolved_at IS NULL AND rule_id != 'NOT-FOUND' ORDER BY id""",
        (document_id, metric_id),
    ).fetchall()
    return [
        r for r in rows
        if r["rule_id"] in METRIC_LEVEL_RULES or (candidate_id is not None and candidate_id in json.loads(r["candidate_ids"]))
    ]


def _require_acknowledgement(conn: sqlite3.Connection, document_id: str, metric_id: str, acknowledged: bool,
                             candidate_id: int | None = None) -> None:
    warnings = relevant_warnings(conn, document_id, metric_id, candidate_id)
    if warnings and not acknowledged:
        raise ReviewError(
            f"This metric has {len(warnings)} warning(s). Read them and confirm you have checked them before approving."
        )


def _require_reviewer(reviewer: str) -> None:
    if not reviewer or not reviewer.strip():
        raise ReviewError("Enter the reviewer name.")


def _require_reason(reason: str, action: str) -> None:
    if not reason or not reason.strip():
        raise ReviewError(f"A reason is required for {action}.")


def _parse_value(value: str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(",", "")
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        raise ReviewError(f"'{value}' is not a number. Use digits, an optional minus sign and a decimal point.") from None
    if not parsed.is_finite():
        raise ReviewError(f"'{value}' is not a finite number.")
    return parsed


def _check_unit(unit: str) -> str:
    if unit not in REVIEW_UNITS:
        raise ReviewError(f"Unit must be one of {REVIEW_UNITS}.")
    return unit
