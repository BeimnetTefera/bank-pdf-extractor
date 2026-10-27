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

When the whole extraction review of a report is finished (finish_extraction), its figures
are locked so the calculated values built on them stay valid; reopen_extraction unlocks them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.database import utc_now
from src.models import METRIC_LEVEL_RULES, AnalysisResult
from src.project_service import SavedCandidate, get_candidate, get_document, save_new_figures

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points
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
    source_bbox: tuple[float, float, float, float] | None = None  # box the reviewer drew on source_page
    # 'calculated' (D-052), 'not_reported' (D-053) or 'external' (from an external source, D-055); None otherwise
    entry_kind: str | None = None


@dataclass
class ReviewState:
    """Where a metric stands after replaying its decisions."""

    # open | approved | corrected (correction/manual entry awaiting approval) |
    # not_reported (approved: the report does not disclose it, D-053; no value, not zero)
    status: str = "open"
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
            reason: str, *, source_page: int | None = None, source_bbox: Box | None = None,
            sign: str | None = None) -> int:
    """source_page/source_bbox: where the reviewer marked the right number; without them the
    extracted figure's own position is the evidence. sign: 'positive' when the figure was printed
    in brackets and the value is recorded as a positive amount (D-068), so the direction is kept."""
    candidate = _candidate(conn, candidate_id)
    _require_reviewer(reviewer)
    _require_reason(reason, "a correction")
    if (source_page is None) != (source_bbox is None):
        raise ReviewError("A marked box needs both its page and its position.")
    if source_page is not None:
        _check_page(conn, candidate.document_id, source_page)
    return _append(conn, candidate.document_id, candidate.metric_id, candidate_id, "corrected",
                   value=parse_value(value), unit=_check_unit(unit), page=source_page, bbox=source_bbox,
                   reason=reason, reviewer=reviewer, sign=_check_sign(sign))


def manual_entry(conn: sqlite3.Connection, document_id: str, metric_id: str, reviewer: str, value: str | Decimal,
                 unit: str, source_page: int, reason: str, *, source_bbox: Box | None = None,
                 sign: str | None = None) -> int:
    """sign: as for correct()."""
    if get_document(conn, document_id) is None:
        raise ReviewError(f"Unknown document {document_id}.")
    _require_reviewer(reviewer)
    _require_reason(reason, "a manual entry")
    _check_page(conn, document_id, source_page)
    return _append(conn, document_id, metric_id, None, "manual_entry", value=parse_value(value),
                   unit=_check_unit(unit), page=source_page, bbox=source_bbox, reason=reason, reviewer=reviewer,
                   sign=_check_sign(sign))


def _check_sign(sign: str | None) -> str | None:
    if sign is not None and sign not in SIGN_CHOICES:
        raise ReviewError(f"Sign choice must be one of {SIGN_CHOICES}.")
    return sign


def not_reported(conn: sqlite3.Connection, document_id: str, metric_id: str, reviewer: str, reason: str) -> int:
    """Propose that the report does not disclose this metric (D-053). Not the same as Not found
    (the extractor failed) or 0. Approved like a correction; exported as a blank cell with the reason."""
    if get_document(conn, document_id) is None:
        raise ReviewError(f"Unknown document {document_id}.")
    _require_reviewer(reviewer)
    if not reason or len(reason.strip()) < 10:
        raise ReviewError("Say why it is not reported, e.g. 'The report does not disclose the number of "
                          "shareholders; searched the directors' report and notes 27-28'.")
    return _append(conn, document_id, metric_id, None, "manual_entry", reason=reason, reviewer=reviewer,
                   entry_kind="not_reported")


def approve_correction(conn: sqlite3.Connection, document_id: str, metric_id: str, reviewer: str, *,
                       acknowledge_warnings: bool = False, note: str = "") -> int:
    _require_reviewer(reviewer)
    pending = review_state(conn, document_id, metric_id).pending
    if pending is None:
        raise ReviewError("There is no correction or manual entry waiting for approval.")
    _require_acknowledgement(conn, document_id, metric_id, acknowledge_warnings)
    return _append(conn, document_id, metric_id, pending.candidate_id, "approved",
                   value=pending.reviewed_value, unit=pending.reviewed_unit, page=pending.source_page,
                   bbox=pending.source_bbox, approves=pending.id, acknowledged=acknowledge_warnings, reason=note,
                   reviewer=reviewer, sign=pending.sign_choice)


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


# --- finishing the extraction review ---------------------------------------------------

@dataclass(frozen=True)
class ExtractionStatus:
    finished: bool
    reviewer: str = ""
    at: str = ""
    reason: str = ""


def extraction_status(conn: sqlite3.Connection, document_id: str) -> ExtractionStatus:
    row = conn.execute("SELECT * FROM report_events WHERE document_id = ? ORDER BY id DESC LIMIT 1",
                       (document_id,)).fetchone()
    if row is None:
        return ExtractionStatus(False)
    return ExtractionStatus(row["event"] == "extraction_finished", row["reviewer"], row["created_at"], row["reason"])


def not_approved_metrics(conn: sqlite3.Connection, document_id: str) -> list[str]:
    """Metrics analysed for the report that are neither approved nor approved as Not reported (yet)."""
    document = get_document(conn, document_id)
    metric_ids = json.loads(document["metric_ids"]) if document else []
    return [m for m in metric_ids if review_state(conn, document_id, m).status not in ("approved", "not_reported")]


def finish_extraction(conn: sqlite3.Connection, document_id: str, reviewer: str, note: str = "") -> int:
    _require_reviewer(reviewer)
    if get_document(conn, document_id) is None:
        raise ReviewError(f"Unknown document {document_id}.")
    if extraction_status(conn, document_id).finished:
        raise ReviewError("The extraction review of this report is already finished.")
    return _append_event(conn, document_id, "extraction_finished", reviewer, note)


def reopen_extraction(conn: sqlite3.Connection, document_id: str, reviewer: str, reason: str) -> int:
    _require_reviewer(reviewer)
    _require_reason(reason, "reopening the extraction review")
    if not extraction_status(conn, document_id).finished:
        raise ReviewError("The extraction review of this report is not finished, so it cannot be reopened.")
    return _append_event(conn, document_id, "extraction_reopened", reviewer, reason)


def add_new_figures(conn: sqlite3.Connection, document_id: str, analysis: AnalysisResult, extractor_version: str,
                    reviewer: str) -> tuple[int, int]:
    """Add figures newly matched by changed wording to a saved report (decision D-044).

    Earlier candidates and decisions stay as they are. Only allowed while the extraction review
    is open, and only with the extractor version the report was saved with, so the same printed
    figure keeps the same position key. Returns (candidates added, findings added).
    """
    _require_reviewer(reviewer)
    document = get_document(conn, document_id)
    if document is None:
        raise ReviewError(f"Unknown document {document_id}.")
    if extraction_status(conn, document_id).finished:
        raise ReviewError("The extraction review of this report is finished. Reopen it (with a reason) "
                          "before adding figures.")
    if not same_text_reader(document["extractor_version"], extractor_version):
        raise ReviewError(f"This report was saved with {document['extractor_version']}, but the app now reads PDFs "
                          f"with {extractor_version}. Figures cannot be added safely; save the report again instead.")
    saved = save_new_figures(conn, document_id, analysis, extractor_version, reviewer)
    if saved is None:
        raise ReviewError("There are no new figures to add.")
    return saved[1], saved[2]


def same_text_reader(saved_version: str | None, current_version: str) -> bool:
    """True when both extractor versions read the text layer the same way. The OCR part
    (" + OCR (tesseract ...)") is ignored: OCR only adds figures on picture pages, which had
    none before, so every saved figure keeps its position key (D-066)."""
    def text_reader(version: str | None) -> str:
        return (version or "").split(" + OCR", 1)[0].strip()

    return text_reader(saved_version) == text_reader(current_version)


@dataclass(frozen=True)
class DeletedReport:
    label: str  # e.g. "Bank of Abyssinia 2025"
    approved_values: int
    files_removed: list[str]  # stored PDF / OCR words, removed when no other saved copy uses them


def delete_report(conn: sqlite3.Connection, document_id: str, reviewer: str, reason: str,
                  data_dir: Path) -> DeletedReport:
    """Delete a whole saved report: its figures, checks, decisions, calculated values, external
    sources and cross-checks and, if nothing else uses them, its stored PDF, OCR words and uploaded
    source copies (decision D-049).

    The deletion is logged first (who, when, why, what) in report_deletions, which can never be
    changed; the database refuses to delete a report's rows unless that log entry exists.
    Exported Excel files are not changed: export the year again to update them.
    """
    with conn:  # one transaction: all of it is deleted, or nothing
        pending = delete_report_rows(conn, document_id, reviewer, reason)
    return remove_unused_files(conn, pending, data_dir)


@dataclass(frozen=True)
class PendingDeletion:
    """A report whose rows were deleted (in a transaction not committed yet); its files come next."""

    label: str
    approved_values: int
    sha256: str
    stored_path: str
    source_files: list[sqlite3.Row]


def delete_report_rows(conn: sqlite3.Connection, document_id: str, reviewer: str, reason: str) -> PendingDeletion:
    """Log the deletion in report_deletions, then delete every row of the report, without
    committing: the caller commits (delete_report, or a merge that copies decisions first, D-086)."""
    _require_reviewer(reviewer)
    _require_reason(reason, "deleting a report")
    document = get_document(conn, document_id)
    if document is None:
        raise ReviewError("That report is no longer in the database.")
    approved = len(approved_results(conn, document_id))
    label = f"{document['bank_name']} {document['reporting_period']}"
    source_files = conn.execute("SELECT DISTINCT file_sha256, media_type FROM external_sources WHERE document_id = ? "
                                "AND file_sha256 IS NOT NULL", (document_id,)).fetchall()
    conn.execute(
        """INSERT INTO report_deletions (document_id, bank_name, reporting_period, original_filename, sha256,
               approved_values, reason, reviewer, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (document_id, document["bank_name"], document["reporting_period"], document["original_filename"],
         document["sha256"], approved, reason.strip(), reviewer.strip(), utc_now()),
    )
    # Children first: calculation inputs point to calculations and input figures, calculations and
    # external sources to decisions, decisions to candidates, candidates to analysis runs.
    # Sector values (no report) are never deleted.
    for table in ("calculation_inputs", "calculations", "external_cross_check_withdrawals",
                  "external_cross_checks", "external_sources", "review_decisions", "input_figures", "findings",
                  "candidates", "analysis_runs", "report_events", "calculated_values", "report_corrections"):
        conn.execute(f"DELETE FROM {table} WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    return PendingDeletion(label, approved, document["sha256"], document["stored_path"], list(source_files))


def remove_unused_files(conn: sqlite3.Connection, pending: PendingDeletion, data_dir: Path) -> DeletedReport:
    """After the deletion is committed: remove the stored PDF, OCR words and uploaded source copies
    that no saved report uses any more."""
    removed = []
    still_used = conn.execute("SELECT 1 FROM documents WHERE sha256 = ?", (pending.sha256,)).fetchone()
    if not still_used:  # another saved copy of the same PDF keeps the file
        for path in (data_dir / pending.stored_path, data_dir / "ocr" / f"{pending.sha256}.json"):
            if path.is_file():
                path.unlink()
                removed.append(path.name)
    for source in pending.source_files:  # uploaded copies of external sources that nothing else uses
        if conn.execute("SELECT 1 FROM external_sources WHERE file_sha256 = ?", (source["file_sha256"],)).fetchone():
            continue
        extension = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}.get(source["media_type"])
        path = data_dir / "sources" / f"{source['file_sha256']}.{extension}"
        if extension and path.is_file():
            path.unlink()
            removed.append(path.name)
    return DeletedReport(pending.label, pending.approved_values, removed)


def deletion_log(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM report_deletions ORDER BY id DESC").fetchall()


def _append_event(conn: sqlite3.Connection, document_id: str, event: str, reviewer: str, reason: str) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO report_events (document_id, event, reason, reviewer, created_at) VALUES (?, ?, ?, ?, ?)",
            (document_id, event, reason.strip(), reviewer.strip(), utc_now()),
        )
    return cursor.lastrowid


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
            not_reported = (state.pending is not None and state.pending.entry_kind == "not_reported"
                            and d.approves_decision_id == state.pending.id)
            state.status, state.pending = ("not_reported" if not_reported else "approved"), None
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
    proposal: Decision | None = None  # the correction / manual entry this approval accepted

    @property
    def box_marked_by_reviewer(self) -> bool:
        return self.approval.source_bbox is not None

    @property
    def evidence_box(self) -> Box | None:
        """Where the value is on source_page: the reviewer's box, else the extracted figure's position."""
        if self.approval.source_bbox is not None:
            return self.approval.source_bbox
        return self.candidate.bbox if self.candidate is not None else None


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
            proposal = next((d for d in state.history if d.id == approval.approves_decision_id), None)
            results.append(ApprovedResult(doc, metric_id, state.accepted_value, state.accepted_unit, approval,
                                          candidate, page, state.is_human_entered, proposal))
    return results


# --- helpers -----------------------------------------------------------------------

def _append(conn: sqlite3.Connection, document_id: str, metric_id: str, candidate_id: int | None, decision: str,
            **fields) -> int:
    with conn:
        return insert_decision(conn, document_id, metric_id, candidate_id, decision, **fields)


def insert_decision(conn: sqlite3.Connection, document_id: str, metric_id: str, candidate_id: int | None,
                    decision: str, *, reviewer: str, value: Decimal | None = None, unit: str | None = None,
                    sign: str | None = None, page: int | None = None, bbox: Box | None = None,
                    approves: int | None = None, acknowledged: bool = False, reason: str = "",
                    entry_kind: str | None = None) -> int:
    """Add one decision without committing, so a caller can save it with related rows in one transaction."""
    if extraction_status(conn, document_id).finished:
        raise ReviewError("The extraction review of this report is finished, so its figures are locked. "
                          "Reopen the extraction review (with a reason) to change them.")
    cursor = conn.execute(
        """INSERT INTO review_decisions (document_id, metric_id, candidate_id, decision, reviewed_value,
               reviewed_unit, sign_choice, source_page, source_bbox, approves_decision_id,
               warnings_acknowledged, reason, reviewer, created_at, entry_kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (document_id, metric_id, candidate_id, decision, None if value is None else str(value), unit, sign,
         page, None if bbox is None else json.dumps([round(v, 2) for v in bbox]), approves, int(acknowledged),
         reason.strip(), reviewer.strip(), utc_now(), entry_kind),
    )
    return cursor.lastrowid


def _decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"], metric_id=row["metric_id"], candidate_id=row["candidate_id"], decision=row["decision"],
        reviewed_value=None if row["reviewed_value"] is None else Decimal(row["reviewed_value"]),
        reviewed_unit=row["reviewed_unit"], sign_choice=row["sign_choice"], source_page=row["source_page"],
        approves_decision_id=row["approves_decision_id"], warnings_acknowledged=bool(row["warnings_acknowledged"]),
        reason=row["reason"], reviewer=row["reviewer"], created_at=row["created_at"],
        source_bbox=None if row["source_bbox"] is None else tuple(json.loads(row["source_bbox"])),
        entry_kind=row["entry_kind"],
    )


def _check_page(conn: sqlite3.Connection, document_id: str, page: int) -> None:
    document = get_document(conn, document_id)
    if not isinstance(page, int) or not 1 <= page <= (document["page_count"] or 0):
        raise ReviewError(f"Enter the PDF page the value comes from (1 to {document['page_count']}).")


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


def parse_value(value: str | Decimal) -> Decimal:
    """A typed number ('1,234.5', '-0.29') as an exact Decimal; ReviewError if it is not one."""
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
