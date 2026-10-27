"""Reports saved more than once from the same PDF, and merging them into one (D-086).

Copies with the same file fingerprint (sha256) form a duplicate group. Merge keeps one copy (by
default the one with the most review decisions) and copies the other copy's review decisions onto
the same figure in the kept copy: same metric and same position key (extraction method, page, row,
column). Copies are new decisions, in their original order, with the reason
"Merged from report <id>: <original reason>" and the merging reviewer. A metric decided
differently in both copies is not copied and is listed. When the other copy has input figures,
calculations or external sources (or calculated values or cross-checks), Merge is refused.
Afterwards the other copy is deleted through the logged delete (D-049). All of it is one
transaction: either everything is copied and the other copy deleted, or nothing changes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from src.project_service import get_document
from src.review_service import (
    DeletedReport,
    ReviewError,
    delete_report_rows,
    extraction_status,
    insert_decision,
    remove_unused_files,
    review_state,
)

# Work saved with a report that Merge cannot copy (table, what it is called on screen).
NOT_COPIED = (("input_figures", "input figure(s)"), ("calculations", "calculation(s)"),
              ("external_sources", "external source(s)"), ("external_cross_checks", "external cross-check(s)"),
              ("calculated_values", "calculated value(s)"))


@dataclass(frozen=True)
class DuplicateGroup:
    sha256: str
    documents: list[sqlite3.Row]  # oldest first
    decision_counts: dict[str, int]  # document id -> review decisions

    @property
    def default_keep(self) -> str:
        """The copy with the most review decisions; the oldest when they tie."""
        return max(self.documents, key=lambda d: (self.decision_counts[d["id"]], -self.documents.index(d)))["id"]


@dataclass
class MergePlan:
    keep_id: str
    other_id: str
    copy_metrics: list[str] = field(default_factory=list)  # metrics whose decisions will be copied
    decisions_to_copy: int = 0
    same_metrics: list[str] = field(default_factory=list)  # decided the same in both copies: nothing to copy
    conflicts: list[str] = field(default_factory=list)  # decided differently in both copies: not copied
    missing_figures: list[str] = field(default_factory=list)  # a decided figure is not in the kept copy
    refusals: list[str] = field(default_factory=list)  # why Merge cannot be done

    @property
    def allowed(self) -> bool:
        return not self.refusals


@dataclass(frozen=True)
class MergeResult:
    decisions_copied: int
    metrics_copied: list[str]
    deleted: DeletedReport


def duplicate_groups(conn: sqlite3.Connection) -> list[DuplicateGroup]:
    """Saved reports sharing a file fingerprint, two or more copies each."""
    rows = conn.execute(
        """SELECT * FROM documents WHERE sha256 IN
               (SELECT sha256 FROM documents GROUP BY sha256 HAVING COUNT(*) > 1)
           ORDER BY sha256, uploaded_at, id""").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["sha256"], []).append(row)
    return [DuplicateGroup(sha, docs, {d["id"]: _decision_count(conn, d["id"]) for d in docs})
            for sha, docs in groups.items()]


def plan_merge(conn: sqlite3.Connection, keep_id: str, other_id: str) -> MergePlan:
    """What Merge would copy, what it would not, and why it may be refused. Changes nothing."""
    plan = MergePlan(keep_id, other_id)
    keep, other = get_document(conn, keep_id), get_document(conn, other_id)
    if keep is None or other is None:
        plan.refusals.append("One of the copies is no longer in the database.")
        return plan
    if keep_id == other_id:
        plan.refusals.append("Choose two different copies.")
        return plan
    if keep["sha256"] != other["sha256"]:
        plan.refusals.append("These reports were not saved from the same PDF.")
        return plan
    for table, name in NOT_COPIED:
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE document_id = ?", (other_id,)).fetchone()[0]
        if count:
            plan.refusals.append(f"The other copy has {count} {name}, which Merge cannot copy.")
    kept_keys = _candidate_ids_by_key(conn, keep_id)
    other_keys = {cid: key for key, cid in _candidate_ids_by_key(conn, other_id).items()}
    for metric_id, rows in _decisions_by_metric(conn, other_id).items():
        if conn.execute("SELECT 1 FROM review_decisions WHERE document_id = ? AND metric_id = ?",
                        (keep_id, metric_id)).fetchone():
            same = _outcome(review_state(conn, keep_id, metric_id)) == _outcome(review_state(conn, other_id, metric_id))
            (plan.same_metrics if same else plan.conflicts).append(metric_id)
        elif any(r["candidate_id"] is not None and other_keys.get(r["candidate_id"]) not in kept_keys for r in rows):
            plan.missing_figures.append(metric_id)
        else:
            plan.copy_metrics.append(metric_id)
            plan.decisions_to_copy += len(rows)
    if plan.decisions_to_copy and extraction_status(conn, keep_id).finished:
        plan.refusals.append("The extraction review of the copy to keep is finished, so its figures are locked. "
                             "Reopen it (with a reason) before merging.")
    return plan


def merge_reports(conn: sqlite3.Connection, keep_id: str, other_id: str, reviewer: str,
                  data_dir: Path) -> MergeResult:
    """Copy the other copy's decisions into the kept copy, then delete the other copy through the
    logged delete (D-049), in one transaction."""
    if not reviewer or not reviewer.strip():
        raise ReviewError("Enter the reviewer name.")
    plan = plan_merge(conn, keep_id, other_id)
    if not plan.allowed:
        raise ReviewError(" ".join(plan.refusals))
    kept_keys = _candidate_ids_by_key(conn, keep_id)
    other_keys = {cid: key for key, cid in _candidate_ids_by_key(conn, other_id).items()}
    by_metric = _decisions_by_metric(conn, other_id)
    with conn:  # one transaction: copied and deleted, or nothing
        for metric_id in plan.copy_metrics:
            new_ids: dict[int, int] = {}  # other copy's decision id -> the copy's id
            for row in by_metric[metric_id]:  # in their original order
                candidate_id = None if row["candidate_id"] is None else kept_keys[other_keys[row["candidate_id"]]]
                approves = row["approves_decision_id"]
                if approves is not None and approves not in new_ids:
                    raise ReviewError(f"Decision {row['id']} approves a decision that cannot be copied; "
                                      "nothing was merged.")
                new_ids[row["id"]] = insert_decision(
                    conn, keep_id, metric_id, candidate_id, row["decision"], reviewer=reviewer,
                    value=row["reviewed_value"], unit=row["reviewed_unit"], sign=row["sign_choice"],
                    page=row["source_page"],
                    bbox=None if row["source_bbox"] is None else tuple(json.loads(row["source_bbox"])),
                    approves=None if approves is None else new_ids[approves],
                    acknowledged=bool(row["warnings_acknowledged"]),
                    reason=f"Merged from report {other_id}: {row['reason']}".rstrip(),
                    entry_kind=row["entry_kind"])
        pending = delete_report_rows(conn, other_id, reviewer, f"Merged into report {keep_id}")
    deleted = remove_unused_files(conn, pending, data_dir)
    return MergeResult(plan.decisions_to_copy, plan.copy_metrics, deleted)


def _decision_count(conn: sqlite3.Connection, document_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM review_decisions WHERE document_id = ?", (document_id,)).fetchone()[0]


def _candidate_ids_by_key(conn: sqlite3.Connection, document_id: str) -> dict[tuple, int]:
    """(metric id, extraction method, page, row, column) -> candidates.id."""
    rows = conn.execute("SELECT id, metric_id, extraction_method, page_index, row_index, column_index "
                        "FROM candidates WHERE document_id = ?", (document_id,)).fetchall()
    return {(r["metric_id"], r["extraction_method"], r["page_index"], r["row_index"], r["column_index"]): r["id"]
            for r in rows}


def _decisions_by_metric(conn: sqlite3.Connection, document_id: str) -> dict[str, list[sqlite3.Row]]:
    by_metric: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM review_decisions WHERE document_id = ? ORDER BY id", (document_id,)):
        by_metric.setdefault(row["metric_id"], []).append(row)
    return by_metric


def _outcome(state) -> tuple:
    """Where a metric stands, for comparing two copies: status, accepted value and pending proposal."""
    pending = state.pending
    return (state.status, state.accepted_value, state.accepted_unit,
            None if pending is None else (pending.reviewed_value, pending.reviewed_unit, pending.entry_kind))
