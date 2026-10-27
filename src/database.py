"""SQLite schema and connection. All SQL elsewhere uses parameters, never string formatting.

Audit rules are enforced by the database itself, not only by application code:
- candidates are immutable once saved (no UPDATE);
- review decisions, report events, calculated values and analysis runs are append-only
  (no UPDATE, no DELETE); so are external sources, external cross-checks and their
  withdrawals, and sector decisions (sector rows belong to no report and are never deleted).
The one exception is deleting a whole report (decision D-049): it is first logged in
report_deletions, and only rows of a report logged there can be deleted. The log itself
can never be changed or deleted.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 7  # 2: report events, calculated values, candidates.read_by_ocr; 3: decision boxes;
                    # 4: analysis runs (figures added to a saved report), candidates.analysis_run_id;
                    # 5: report deletions (whole reports may be deleted once logged);
                    # 6: calculated entries (input figures, calculations, calculation inputs), decisions.entry_kind;
                    # 7: external sources, external cross-checks (+ withdrawals), sector decisions

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
    read_by_ocr         INTEGER NOT NULL DEFAULT 0,  -- 1: read by OCR from a picture of the page
    analysis_run_id     INTEGER REFERENCES analysis_runs (id),  -- NULL: saved with the report
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
    source_page           INTEGER,            -- 1-based PDF page for manual entries / marked boxes
    source_bbox           TEXT,               -- JSON [x0, y0, x1, y1]: box the reviewer drew on the page
    approves_decision_id  INTEGER REFERENCES review_decisions (id),  -- a correction being approved
    warnings_acknowledged INTEGER NOT NULL DEFAULT 0,
    reason                TEXT NOT NULL DEFAULT '',
    reviewer              TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_decisions_metric ON review_decisions (document_id, metric_id);

-- Extraction review finished / reopened for a report; the latest event is the current state.
CREATE TABLE IF NOT EXISTS report_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents (id),
    event       TEXT NOT NULL CHECK (event IN ('extraction_finished', 'extraction_reopened')),
    reason      TEXT NOT NULL DEFAULT '',
    reviewer    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Values an analyst calculated from the approved figures; the latest entry per column counts.
CREATE TABLE IF NOT EXISTS calculated_values (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL REFERENCES documents (id),
    column_name  TEXT NOT NULL,           -- a calculated column of export_template.yaml
    value        TEXT,                    -- exact decimal as text; NULL clears the column
    formula_note TEXT NOT NULL DEFAULT '',
    reviewer     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calculated_values_doc ON calculated_values (document_id, column_name);

-- Figures added to a saved report after its wording (aliases) changed. Earlier candidates
-- and decisions are untouched; the added candidates point to their run.
CREATE TABLE IF NOT EXISTS analysis_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id       TEXT NOT NULL REFERENCES documents (id),
    extractor_version TEXT NOT NULL,
    candidates_added  INTEGER NOT NULL,
    findings_added    INTEGER NOT NULL,
    reviewer          TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- Figures marked on the report that feed a calculation but are not metrics (par value, net
-- operating income, a prior-year column, ...). The latest row per (document, figure_key) counts.
CREATE TABLE IF NOT EXISTS input_figures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL REFERENCES documents (id),
    figure_key   TEXT NOT NULL,            -- the name used in formulas
    label        TEXT NOT NULL,
    value        TEXT NOT NULL,            -- exact decimal, in unit
    unit         TEXT NOT NULL,
    printed_text TEXT NOT NULL DEFAULT '', -- the number as printed in the box
    page         INTEGER NOT NULL,         -- 1-based PDF page
    bbox         TEXT NOT NULL,            -- JSON [x0, y0, x1, y1]
    reason       TEXT NOT NULL,
    reviewer     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- A calculated value (D-052): the proposal decision it belongs to, and the formula as used.
CREATE TABLE IF NOT EXISTS calculations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents (id),
    decision_id INTEGER NOT NULL UNIQUE REFERENCES review_decisions (id),
    expression  TEXT NOT NULL,             -- in metric ids, e.g. a / avg(b, b_prev)
    words       TEXT NOT NULL,             -- in labels
    arithmetic  TEXT NOT NULL,             -- with the numbers used
    unrounded   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Every input of a calculation as it was when calculated: value, where it came from, its box.
CREATE TABLE IF NOT EXISTS calculation_inputs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    calculation_id     INTEGER NOT NULL REFERENCES calculations (id),
    document_id        TEXT NOT NULL,
    variable           TEXT NOT NULL,
    label              TEXT NOT NULL,
    value              TEXT NOT NULL,
    unit               TEXT,
    source_kind        TEXT NOT NULL CHECK (source_kind IN ('metric', 'prior_column', 'previous_report', 'figure')),
    source_document_id TEXT,              -- the report it came from (another one for previous_report)
    source_metric_id   TEXT,
    source_decision_id INTEGER,           -- the approval used, for metric inputs
    candidate_id       INTEGER,           -- the extracted figure, for prior_column inputs
    input_figure_id    INTEGER REFERENCES input_figures (id),
    page               INTEGER,           -- 1-based page of the figure used
    bbox               TEXT,
    printed_text       TEXT NOT NULL DEFAULT '',
    column_year        INTEGER,           -- the year column the figure was read from, when known
    row_label          TEXT NOT NULL DEFAULT ''
);

-- A document other than the bank's report that gives a figure (D-055): an external value of a
-- report's metric (decision_id), an external cross-check, or a sector value (document_id NULL).
-- The URL is stored as text only; the app never opens it (D-006).
CREATE TABLE IF NOT EXISTS external_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   TEXT REFERENCES documents (id),   -- NULL: a sector value, shared by every bank
    purpose       TEXT NOT NULL CHECK (purpose IN ('value', 'cross_check', 'sector')),
    decision_id   INTEGER UNIQUE REFERENCES review_decisions (id),  -- the proposal, for purpose 'value'
    title         TEXT NOT NULL,
    publisher     TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    file_sha256   TEXT,                   -- uploaded copy, stored as sources/<sha256>.<ext>
    file_name     TEXT,                   -- the uploaded file's name (metadata only)
    media_type    TEXT,                   -- application/pdf, image/png or image/jpeg
    page          INTEGER,                -- 1-based page of the uploaded file
    bbox          TEXT,                   -- JSON [x0, y0, x1, y1] on that page, in PDF points
    quote         TEXT NOT NULL,          -- the exact words around the figure
    printed_value TEXT NOT NULL,          -- the number as printed
    scale         TEXT NOT NULL,          -- birr | birr_thousand | million | billion | count | percent
    value         TEXT NOT NULL,          -- exact decimal, converted to unit
    unit          TEXT NOT NULL,
    reviewer      TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS external_sources_doc ON external_sources (document_id);

-- An external figure to check a metric of a report against (D-057). Withdrawn, never deleted.
CREATE TABLE IF NOT EXISTS external_cross_checks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id        TEXT NOT NULL REFERENCES documents (id),
    metric_id          TEXT NOT NULL,
    external_source_id INTEGER NOT NULL REFERENCES external_sources (id),
    note               TEXT NOT NULL DEFAULT '',
    reviewer           TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS external_cross_checks_metric ON external_cross_checks (document_id, metric_id);

CREATE TABLE IF NOT EXISTS external_cross_check_withdrawals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cross_check_id INTEGER NOT NULL UNIQUE REFERENCES external_cross_checks (id),
    document_id    TEXT NOT NULL,
    reason         TEXT NOT NULL,
    reviewer       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- Sector-wide figures per fiscal year (D-056), shared by every bank; replayed like review decisions.
CREATE TABLE IF NOT EXISTS sector_decisions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    fiscal_year          TEXT NOT NULL,     -- YYYY/YY
    metric_id            TEXT NOT NULL,
    decision             TEXT NOT NULL CHECK (decision IN ('proposed', 'approved', 'reopened')),
    entry_kind           TEXT CHECK (entry_kind IN ('external', 'not_reported')),
    value                TEXT,              -- exact decimal; NULL for not reported
    unit                 TEXT,
    external_source_id   INTEGER REFERENCES external_sources (id),
    approves_decision_id INTEGER REFERENCES sector_decisions (id),
    reason               TEXT NOT NULL DEFAULT '',
    reviewer             TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sector_decisions_metric ON sector_decisions (fiscal_year, metric_id);

CREATE TRIGGER IF NOT EXISTS external_sources_no_update BEFORE UPDATE ON external_sources
BEGIN SELECT RAISE(ABORT, 'External sources are append-only; record the source again instead.'); END;

CREATE TRIGGER IF NOT EXISTS external_sources_no_delete BEFORE DELETE ON external_sources
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'External sources cannot be deleted on their own; delete the whole report.'); END;

CREATE TRIGGER IF NOT EXISTS external_cross_checks_no_update BEFORE UPDATE ON external_cross_checks
BEGIN SELECT RAISE(ABORT, 'Cross-checks are append-only; withdraw one with a reason instead.'); END;

CREATE TRIGGER IF NOT EXISTS external_cross_checks_no_delete BEFORE DELETE ON external_cross_checks
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Cross-checks cannot be deleted; withdraw one with a reason instead.'); END;

CREATE TRIGGER IF NOT EXISTS cross_check_withdrawals_no_update BEFORE UPDATE ON external_cross_check_withdrawals
BEGIN SELECT RAISE(ABORT, 'Cross-check withdrawals are append-only.'); END;

CREATE TRIGGER IF NOT EXISTS cross_check_withdrawals_no_delete BEFORE DELETE ON external_cross_check_withdrawals
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Cross-check withdrawals cannot be deleted.'); END;

CREATE TRIGGER IF NOT EXISTS sector_decisions_no_update BEFORE UPDATE ON sector_decisions
BEGIN SELECT RAISE(ABORT, 'Sector decisions are append-only; add a new decision instead.'); END;

CREATE TRIGGER IF NOT EXISTS sector_decisions_no_delete BEFORE DELETE ON sector_decisions
BEGIN SELECT RAISE(ABORT, 'Sector decisions are append-only and cannot be deleted.'); END;

-- Whole reports deleted on request (D-049): what was deleted, by whom, when and why.
CREATE TABLE IF NOT EXISTS report_deletions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id       TEXT NOT NULL,          -- no reference: the document row is gone
    bank_name         TEXT NOT NULL,
    reporting_period  TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    approved_values   INTEGER NOT NULL,       -- how many approved figures were deleted with it
    reason            TEXT NOT NULL,
    reviewer          TEXT NOT NULL,
    deleted_at        TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS report_deletions_no_update BEFORE UPDATE ON report_deletions
BEGIN SELECT RAISE(ABORT, 'The deletion log cannot be changed.'); END;

CREATE TRIGGER IF NOT EXISTS report_deletions_no_delete BEFORE DELETE ON report_deletions
BEGIN SELECT RAISE(ABORT, 'The deletion log cannot be deleted.'); END;

CREATE TRIGGER IF NOT EXISTS candidates_immutable BEFORE UPDATE ON candidates
BEGIN SELECT RAISE(ABORT, 'Extracted candidates cannot be changed; record a review decision instead.'); END;

CREATE TRIGGER IF NOT EXISTS review_decisions_no_update BEFORE UPDATE ON review_decisions
BEGIN SELECT RAISE(ABORT, 'Review decisions are append-only; add a new decision instead.'); END;

CREATE TRIGGER IF NOT EXISTS report_events_no_update BEFORE UPDATE ON report_events
BEGIN SELECT RAISE(ABORT, 'Report events are append-only; add a new event instead.'); END;

CREATE TRIGGER IF NOT EXISTS calculated_values_no_update BEFORE UPDATE ON calculated_values
BEGIN SELECT RAISE(ABORT, 'Calculated values are append-only; enter a new value instead.'); END;

CREATE TRIGGER IF NOT EXISTS analysis_runs_no_update BEFORE UPDATE ON analysis_runs
BEGIN SELECT RAISE(ABORT, 'Analysis runs are append-only.'); END;

CREATE TRIGGER IF NOT EXISTS input_figures_no_update BEFORE UPDATE ON input_figures
BEGIN SELECT RAISE(ABORT, 'Input figures are append-only; record the figure again instead.'); END;

CREATE TRIGGER IF NOT EXISTS calculations_no_update BEFORE UPDATE ON calculations
BEGIN SELECT RAISE(ABORT, 'Calculations are append-only; calculate again instead.'); END;

CREATE TRIGGER IF NOT EXISTS calculation_inputs_no_update BEFORE UPDATE ON calculation_inputs
BEGIN SELECT RAISE(ABORT, 'Calculation inputs are append-only.'); END;

CREATE TRIGGER IF NOT EXISTS input_figures_no_delete BEFORE DELETE ON input_figures
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Input figures cannot be deleted on their own; delete the whole report.'); END;

CREATE TRIGGER IF NOT EXISTS calculations_no_delete BEFORE DELETE ON calculations
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Calculations cannot be deleted on their own; delete the whole report.'); END;

CREATE TRIGGER IF NOT EXISTS calculation_inputs_no_delete BEFORE DELETE ON calculation_inputs
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Calculation inputs cannot be deleted on their own; delete the whole report.'); END;

-- No row of a report may be deleted unless the whole report's deletion is logged first.
-- (Dropped and recreated so databases made before D-049 get the new rule.)
DROP TRIGGER IF EXISTS review_decisions_no_delete;
CREATE TRIGGER review_decisions_no_delete BEFORE DELETE ON review_decisions
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Review decisions are append-only and cannot be deleted.'); END;

DROP TRIGGER IF EXISTS report_events_no_delete;
CREATE TRIGGER report_events_no_delete BEFORE DELETE ON report_events
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Report events are append-only and cannot be deleted.'); END;

DROP TRIGGER IF EXISTS calculated_values_no_delete;
CREATE TRIGGER calculated_values_no_delete BEFORE DELETE ON calculated_values
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Calculated values are append-only and cannot be deleted.'); END;

DROP TRIGGER IF EXISTS analysis_runs_no_delete;
CREATE TRIGGER analysis_runs_no_delete BEFORE DELETE ON analysis_runs
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Analysis runs are append-only and cannot be deleted.'); END;

DROP TRIGGER IF EXISTS candidates_no_delete;
CREATE TRIGGER candidates_no_delete BEFORE DELETE ON candidates
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Extracted candidates cannot be deleted on their own; delete the whole report.'); END;

DROP TRIGGER IF EXISTS documents_no_delete;
CREATE TRIGGER documents_no_delete BEFORE DELETE ON documents
WHEN NOT EXISTS (SELECT 1 FROM report_deletions WHERE document_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'A report can only be deleted through Delete report, which logs it.'); END;
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open (and create if needed) the database at path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _add_missing_columns(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


# Columns added after a table was first created: (table, column, definition). Adding a
# column with a default keeps every existing row as it was.
_ADDED_COLUMNS = (
    ("candidates", "read_by_ocr", "INTEGER NOT NULL DEFAULT 0"),
    ("review_decisions", "source_bbox", "TEXT"),
    ("candidates", "analysis_run_id", "INTEGER REFERENCES analysis_runs (id)"),
    ("review_decisions", "entry_kind", "TEXT"),  # NULL: as before; 'calculated' (D-052)
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            with conn:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
