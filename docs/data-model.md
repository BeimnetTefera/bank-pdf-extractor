# Data Model

This document defines the minimum records needed to retain evidence and review history. It is a logical model; implementation details can be adjusted while preserving the principles.

## 1. Core entities

### Document
One uploaded PDF version.

Fields:
- `id`: stable unique ID
- `bank_name`
- `reporting_period`
- `original_filename`
- `stored_path` (relative local path, not an arbitrary user-provided path)
- `sha256`
- `page_count` when available
- `uploaded_at`
- `processing_status`
- `parser_version` or app version when available

A corrected report has a different hash and should be a separate document record.

`bank_name` is spelled as in `config/banks.yaml` and `reporting_period` is always `YYYY/YY` (D-084, D-085; Save converts "2026" to "2025/26"). A document row never changes, except these two fields through a logged correction (below); the database refuses anything else. Documents with the same `sha256` are the same PDF saved more than once and can be merged on Bank Reports (D-086).

### Report correction (D-084, D-085)
A saved report's bank name or fiscal year fixed in place on Bank Reports (*Fix names and years*). Append-only: written first, in the same transaction as the document update, and never changed or deleted (only deleted with its whole report, D-049).

Fields:
- `id`, `document_id`
- `field` (`bank_name` or `reporting_period`), `old_value`, `new_value`
- `reason`, `reviewer`, `created_at`

### Metric definition
Configured in `config/metrics.yaml`.

Fields:
- `id`: stable machine-readable ID, e.g. `total_assets`
- `display_name`
- `definition`
- `expected_unit` or unit policy
- `active`
- optional `components` or explicit calculation policy, only when agreed

Do not invent business definitions. Unresolved definitions must be marked as open decisions.

### Candidate
One possible figure found in a document.

Fields:
- `id`
- `document_id`
- `page_index` (zero-based)
- `printed_page_number` (nullable, only if reliably known)
- `raw_label`
- `raw_value_text`
- `parsed_value` (nullable)
- `raw_unit` (nullable)
- `normalized_value` (nullable)
- `normalized_unit` (nullable)
- `metric_id` (nullable until mapped)
- `component_id` (nullable)
- `source_section` (nullable)
- `context_text` (limited surrounding text/table context)
- `table_or_row_context` when available
- `extraction_method`
- `created_at`

Do not discard a candidate because another candidate appears more plausible.

### Validation finding
A warning or result produced by a validation rule.

Fields:
- `id`
- `candidate_id` or related candidate IDs
- `rule_id`
- `severity` (info/warning/error)
- `message`
- `created_at`
- `resolved_at` (nullable)
- `resolution_note` (nullable)

### Review decision
A human action against a candidate.

Fields:
- `id`
- `candidate_id`
- `decision`: `approved`, `rejected`, or `corrected`
- `reviewed_value` and `reviewed_unit` when applicable
- `reason` (required for correction/rejection)
- `reviewer_label`
- `created_at`

Never mutate the original candidate to make it look as if the extractor produced the corrected value. A correction is a distinct decision.

### Analysis run (D-044)
Figures added to a saved report after its wording changed (e.g. a label learned from a review). Append-only.

Fields:
- `id`, `document_id`
- `extractor_version` (must equal the version the report was saved with)
- `candidates_added`, `findings_added`
- `reviewer`, `created_at`

Each added candidate stores its `analysis_run_id`; candidates saved with the report have none. Earlier candidates, findings and decisions are never changed.

## 2. Relationships

```text
Document 1 ---- many AnalysisRun 1 ---- many Candidate (added later)
Document 1 ---- many Candidate
Candidate 1 ---- many ValidationFinding
Candidate 1 ---- many ReviewDecision
MetricDefinition 1 ---- many Candidate (after mapping)
```

A candidate may have multiple review events over time. The current review state can be derived from the latest valid decision or stored as a convenience field with tests ensuring consistency.

## 3. Status definitions

- `needs_review`: candidate exists but no final review decision.
- `flagged`: a significant ambiguity or unresolved validation finding exists.
- `approved`: a reviewer approved the candidate.
- `rejected`: a reviewer rejected the candidate.
- `corrected`: a reviewer supplied a replacement value; the corrected value is the approved result only if the workflow explicitly records approval of that correction.
- `not_found`: no candidate was found for a requested metric; this is not the same as zero.

Avoid treating missing, zero, not applicable, and not disclosed as equivalent.

## 4. Evidence and page numbering

Store zero-based PDF page index internally because PDF libraries generally use it. Display a human-friendly one-based PDF page number in the UI. Store the printed page number separately only when it can be determined.

Every exported candidate should include document identity and page evidence. A page number alone is insufficient if multiple documents or report versions exist.

## 5. Units and numbers

- Preserve `raw_value_text` and `raw_unit`.
- Parse to a numeric type only when safe.
- Preserve decimal precision; do not round financial figures for convenience.
- Unit conversion must be explicit and tested.
- Never infer that a number is birr, thousands, or millions without evidence from the document or an agreed rule.
- If currency/unit cannot be established, flag it.

## 6. Privacy

SQLite records and stored paths may reveal sensitive financial information. Keep the database, uploads, and exports out of version control and protect local access.
