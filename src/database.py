"""SQLite schema and connection. All SQL elsewhere uses parameters, never string formatting.

Audit rules are enforced by the database itself, not only by application code:
- candidates are immutable once saved (no UPDATE);
- review decisions are append-only (no UPDATE, no DELETE).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id                TEXT PRIMARY KEY,
    bank_name         TEXT NOT NULL,
    reporting_period  TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_path       TEXT NOT NULL,          -- relative to the app data folder
    sha256            TEXT NOT NULL,
    page_count        INTEGER,
    uploaded_at       TEXT NOT NULL,          -- ISO 8601, UTC
    processing_status TEXT NOT NULL,
    extractor_version TEXT,
    metric_ids        TEXT NOT NULL DEFAULT '[]'  -- JSON list of metrics analysed
);
CREATE INDEX IF NOT EXISTS documents_sha256 ON documents (sha256);

CREATE TABLE IF NOT EXISTS candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id         TEXT NOT NULL REFERENCES documents (id),
    metric_id           TEXT NOT NULL,
    rank                INTEGER NOT NULL,     -- advisory order within the metric, 0 = suggested
    extraction_method   TEXT NOT NULL,
    page_index          INTEGER NOT NULL,     -- zero-based PDF page
    row_index           INTEGER NOT NULL,
    column_index        INTEGER NOT NULL,
    printed_page_number INTEGER,
    raw_label           TEXT NOT NULL,
    raw_value_text      TEXT NOT NULL,
    text_before         TEXT NOT NULL,
    text_after          TEXT NOT NULL,
    row_text            TEXT NOT NULL,
    context_text        TEXT NOT NULL,
    bbox                TEXT NOT NULL,        -- JSON [x0, y0, x1, y1] in PDF points
    parsed_value        TEXT,                 -- exact decimal as text
    is_percent          INTEGER NOT NULL,
    raw_unit            TEXT NOT NULL,        -- where the unit came from, as shown to reviewers
    normalized_value    TEXT,
    normalized_unit     TEXT,
    conversion_basis    TEXT NOT NULL,
    normalization_issue TEXT,
    period              TEXT NOT NULL,
    period_year         INTEGER,
    match_basis         TEXT NOT NULL,
    matched_text        TEXT NOT NULL,
    is_ambiguous        INTEGER NOT NULL,
    in_chart_row        INTEGER NOT NULL,
    is_duplicate        INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (document_id, metric_id, extraction_method, page_index, row_index, column_index)
);

CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     TEXT NOT NULL REFERENCES documents (id),
    metric_id       TEXT,
    candidate_ids   TEXT NOT NULL DEFAULT '[]',   -- JSON list of candidates.id
    rule_id         TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolution_note TEXT
);

CREATE TABLE IF NOT EXISTS review_decisions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id           TEXT NOT NULL REFERENCES documents (id),
    metric_id             TEXT NOT NULL,
    candidate_id          INTEGER REFERENCES candidates (id),  -- NULL for manual entry / reopen
    decision              TEXT NOT NULL CHECK (decision IN
                              ('approved', 'rejected', 'corrected', 'manual_entry', 'reopened')),
    reviewed_value        TEXT,               -- exact decimal as text
    reviewed_unit         TEXT,
    sign_choice           TEXT,               -- 'as_printed' or 'positive' (approvals of extracted values)
    source_page           INTEGER,            -- 1-based PDF page for manual entries
    approves_decision_id  INTEGER REFERENCES review_decisions (id),  -- a correction being approved
    warnings_acknowledged INTEGER NOT NULL DEFAULT 0,
    reason                TEXT NOT NULL DEFAULT '',
    reviewer              TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_decisions_metric ON review_decisions (document_id, metric_id);

CREATE TRIGGER IF NOT EXISTS candidates_immutable BEFORE UPDATE ON candidates
BEGIN SELECT RAISE(ABORT, 'Extracted candidates cannot be changed; record a review decision instead.'); END;

CREATE TRIGGER IF NOT EXISTS review_decisions_no_update BEFORE UPDATE ON review_decisions
BEGIN SELECT RAISE(ABORT, 'Review decisions are append-only; add a new decision instead.'); END;

CREATE TRIGGER IF NOT EXISTS review_decisions_no_delete BEFORE DELETE ON review_decisions
BEGIN SELECT RAISE(ABORT, 'Review decisions are append-only and cannot be deleted.'); END;
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open (and create if needed) the database at path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
