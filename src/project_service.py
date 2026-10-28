"""Save an analysed report to SQLite and load saved reports back.

A report is saved once, with the metric candidates and findings produced at that time
(and the extractor version used). Saved candidates are immutable; review decisions are
added on top of them by review_service.

When the wording in the configuration changes later, figures newly matched in a saved
report can be added to it (save_new_figures, decision D-044). They are recorded as an
analysis run; nothing saved earlier is changed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from src.candidate_queries import CandidateKey
from src.database import utc_now
from src.document_manager import Document, DocumentError
from src.models import AnalysisResult, Finding, MetricCandidate
from src.report_period import canonical_period


@dataclass(frozen=True)
class SavedCandidate:
    """A candidate as stored: the evidence plus how it was read. Read-only."""

    id: int
    document_id: str
    metric_id: str
    rank: int
    extraction_method: str
    page_index: int
    row_index: int
    column_index: int
    raw_label: str
    raw_value_text: str
    text_before: str
    text_after: str
    row_text: str
    context_text: str
    bbox: tuple[float, float, float, float]
    normalized_value: Decimal | None
    normalized_unit: str | None
    raw_unit: str
    conversion_basis: str
    normalization_issue: str | None
    period: str
    period_year: int | None
    match_basis: str
    matched_text: str
    read_by_ocr: bool = False

    @property
    def key(self) -> CandidateKey:
        return (self.extraction_method, self.page_index, self.row_index, self.column_index)


def find_documents_by_hash(conn: sqlite3.Connection, sha256: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM documents WHERE sha256 = ? ORDER BY uploaded_at", (sha256,)).fetchall()


def list_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM documents ORDER BY uploaded_at DESC").fetchall()


def recent_decisions(conn: sqlite3.Connection, decisions: tuple[str, ...], limit: int = 5) -> list[sqlite3.Row]:
    """The latest review decisions of the given kinds across all saved reports, newest first, with their report."""
    marks = ", ".join("?" * len(decisions))
    return conn.execute(
        f"""SELECT r.decision, r.metric_id, r.reviewed_value, r.reviewed_unit, r.reviewer, r.created_at,
                   d.id AS document_id, d.bank_name, d.reporting_period
            FROM review_decisions r JOIN documents d ON d.id = r.document_id
            WHERE r.decision IN ({marks}) ORDER BY r.id DESC LIMIT ?""", (*decisions, limit)).fetchall()


def get_document(conn: sqlite3.Connection, document_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()


def save_analysis(
    conn: sqlite3.Connection,
    document: Document,
    analysis: AnalysisResult,
    extractor_version: str,
) -> dict[tuple[str, CandidateKey], int]:
    """Store the document, its metric candidates and findings in one transaction.

    The report year is saved in its one form, YYYY/YY (D-085): "2026" is saved as "2025/26".
    Returns {(metric_id, candidate key): candidates.id}.
    """
    period = canonical_period(document.reporting_period)
    if period is None:
        raise DocumentError(f"'{document.reporting_period}' is not a fiscal year. Enter it like 2025/26.")
    now = utc_now()
    ids: dict[tuple[str, CandidateKey], int] = {}
    with conn:  # commits on success, rolls back on any error
        conn.execute(
            """INSERT INTO documents (id, bank_name, reporting_period, original_filename, stored_path, sha256,
                                      page_count, uploaded_at, processing_status, extractor_version, metric_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (document.id, document.bank_name, period, document.original_filename,
             document.stored_path, document.sha256, document.page_count, document.uploaded_at, "analysed",
             extractor_version, json.dumps([m.metric.id for m in analysis.metrics])),
        )
        for result in analysis.metrics:
            for rank, mc in enumerate(result.candidates):
                ids[(result.metric.id, mc.mapped.key)] = _insert_candidate(conn, document.id, result.metric.id, rank, mc, now)
        for finding in analysis.findings:
            _insert_finding(conn, document.id, finding, ids, now)
    return ids


@dataclass(frozen=True)
class NewFigures:
    """Figures in a fresh analysis of a saved report that are not saved with it yet."""

    candidates: list[tuple[str, int, MetricCandidate]]  # (metric id, rank, candidate)
    findings: list[Finding]

    def __bool__(self) -> bool:
        return bool(self.candidates or self.findings)


def saved_candidate_ids(saved: list[SavedCandidate], analysis: AnalysisResult) -> dict[tuple[str, CandidateKey], int]:
    """{(metric id, candidate key): saved candidate id} for the review screen.

    A saved figure whose key now points at another printed number (a page read differently by a
    newer extractor, e.g. split into column blocks, D-060) is left out, so approving on screen
    can never record a figure other than the one shown."""
    current = {(r.metric.id, mc.mapped.key): mc.mapped.candidate for r in analysis.metrics for mc in r.candidates}
    ids = {}
    for c in saved:
        now = current.get((c.metric_id, c.key))
        if now is None or (now.raw_value_text == c.raw_value_text and now.page_index == c.page_index):
            ids[(c.metric_id, c.key)] = c.id
    return ids


def new_figures(conn: sqlite3.Connection, document_id: str, analysis: AnalysisResult) -> NewFigures:
    """Compare a fresh analysis with what is saved. Only the report's own metrics are considered."""
    document = get_document(conn, document_id)
    metric_ids = set(json.loads(document["metric_ids"])) if document else set()
    saved = {(c.metric_id, c.key) for c in load_candidates(conn, document_id)}
    saved_findings = {(f["metric_id"], f["rule_id"], f["message"]) for f in load_findings(conn, document_id)}
    candidates = [
        (result.metric.id, rank, mc)
        for result in analysis.metrics if result.metric.id in metric_ids
        for rank, mc in enumerate(result.candidates)
        if (result.metric.id, mc.mapped.key) not in saved
    ]
    findings = [
        f for f in analysis.findings
        if f.metric_id in metric_ids and (f.metric_id, f.rule_id, f.message) not in saved_findings
    ]
    return NewFigures(candidates, findings)


def save_new_figures(conn: sqlite3.Connection, document_id: str, analysis: AnalysisResult, extractor_version: str,
                     reviewer: str) -> tuple[int, int, int] | None:
    """Add the new figures and findings as one analysis run, in one transaction.

    Returns (run id, candidates added, findings added), or None when there was nothing new.
    Rules about who may do this, and when, are in review_service.add_new_figures.
    """
    new = new_figures(conn, document_id, analysis)
    if not new:
        return None
    now = utc_now()
    with conn:
        run_id = conn.execute(
            """INSERT INTO analysis_runs (document_id, extractor_version, candidates_added, findings_added,
                                          reviewer, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (document_id, extractor_version, len(new.candidates), len(new.findings), reviewer.strip(), now),
        ).lastrowid
        ids = {(c.metric_id, c.key): c.id for c in load_candidates(conn, document_id)}
        for metric_id, rank, mc in new.candidates:
            ids[(metric_id, mc.mapped.key)] = _insert_candidate(conn, document_id, metric_id, rank, mc, now, run_id)
        for finding in new.findings:
            _insert_finding(conn, document_id, finding, ids, now)
    return run_id, len(new.candidates), len(new.findings)


def analysis_runs(conn: sqlite3.Connection, document_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM analysis_runs WHERE document_id = ? ORDER BY id", (document_id,)).fetchall()


def _insert_finding(conn: sqlite3.Connection, document_id: str, finding: Finding,
                    ids: dict[tuple[str, CandidateKey], int], now: str) -> None:
    candidate_ids = [ids[(finding.metric_id, k)] for k in finding.candidate_keys if (finding.metric_id, k) in ids]
    conn.execute(
        """INSERT INTO findings (document_id, metric_id, candidate_ids, rule_id, severity, message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (document_id, finding.metric_id, json.dumps(candidate_ids), finding.rule_id, finding.severity,
         finding.message, now),
    )


def _insert_candidate(conn: sqlite3.Connection, document_id: str, metric_id: str, rank: int,
                      mc: MetricCandidate, now: str, run_id: int | None = None) -> int:
    m, c = mc.mapped, mc.mapped.candidate
    match = next(x for x in m.matches if x.metric_id == metric_id)
    cursor = conn.execute(
        """INSERT INTO candidates (
               document_id, metric_id, rank, extraction_method, page_index, row_index, column_index,
               printed_page_number, raw_label, raw_value_text, text_before, text_after, row_text, context_text,
               bbox, parsed_value, is_percent, raw_unit, normalized_value, normalized_unit, conversion_basis,
               normalization_issue, period, period_year, match_basis, matched_text, is_ambiguous, in_chart_row,
               is_duplicate, created_at, read_by_ocr, analysis_run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (document_id, metric_id, rank, c.extraction_method, c.page_index, c.row_index, c.column_index,
         c.printed_page_number, c.raw_label, c.raw_value_text, c.text_before, c.text_after, c.row_text,
         c.context_text, json.dumps(list(c.bbox)), _text(m.parsed.value), int(m.parsed.is_percent), m.unit.source,
         _text(mc.normalized.value), mc.normalized.unit, mc.normalized.basis, mc.normalized.issue, m.period,
         m.period_year, match.basis, match.matched_text, int(m.is_ambiguous), int(m.in_chart_row),
         int(m.duplicate_of is not None), now, int(c.ocr), run_id),
    )
    return cursor.lastrowid


def load_candidates(conn: sqlite3.Connection, document_id: str, metric_id: str | None = None) -> list[SavedCandidate]:
    sql = "SELECT * FROM candidates WHERE document_id = ?"
    params: list = [document_id]
    if metric_id is not None:
        sql += " AND metric_id = ?"
        params.append(metric_id)
    rows = conn.execute(sql + " ORDER BY metric_id, rank", params).fetchall()
    return [_saved_candidate(r) for r in rows]


def get_candidate(conn: sqlite3.Connection, candidate_id: int) -> SavedCandidate | None:
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    return _saved_candidate(row) if row else None


def load_findings(conn: sqlite3.Connection, document_id: str, metric_id: str | None = None) -> list[sqlite3.Row]:
    if metric_id is None:
        return conn.execute("SELECT * FROM findings WHERE document_id = ? ORDER BY id", (document_id,)).fetchall()
    return conn.execute(
        "SELECT * FROM findings WHERE document_id = ? AND metric_id = ? ORDER BY id", (document_id, metric_id)
    ).fetchall()


def _saved_candidate(row: sqlite3.Row) -> SavedCandidate:
    return SavedCandidate(
        id=row["id"], document_id=row["document_id"], metric_id=row["metric_id"], rank=row["rank"],
        extraction_method=row["extraction_method"], page_index=row["page_index"], row_index=row["row_index"],
        column_index=row["column_index"], raw_label=row["raw_label"], raw_value_text=row["raw_value_text"],
        text_before=row["text_before"], text_after=row["text_after"], row_text=row["row_text"],
        context_text=row["context_text"], bbox=tuple(json.loads(row["bbox"])),
        normalized_value=_decimal(row["normalized_value"]), normalized_unit=row["normalized_unit"],
        raw_unit=row["raw_unit"], conversion_basis=row["conversion_basis"],
        normalization_issue=row["normalization_issue"], period=row["period"], period_year=row["period_year"],
        match_basis=row["match_basis"], matched_text=row["matched_text"], read_by_ocr=bool(row["read_by_ocr"]),
    )


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)
