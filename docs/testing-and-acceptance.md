# Testing and Acceptance Criteria

## 1. Testing principle

Extraction quality must be measured against manually verified source figures. Passing unit tests does not prove that the application extracts financial data correctly from every bank report.

Use synthetic fixtures in the repository. Keep real reports and sensitive values outside source control.

## 2. Unit tests

### Document manager
- Accept valid PDF input.
- Reject invalid file types.
- Generate stable SHA-256 for identical files.
- Avoid overwriting a document with the same filename but different content.
- Handle unreadable/encrypted files gracefully.

### PDF extractor
- Preserve page index and raw labels/value strings.
- Extract numeric and nonnumeric candidate text without losing raw evidence.
- Preserve multiple candidate occurrences.
- Return a clear no-text/scanned-document state.
- Avoid fabricating printed page numbers.

### Metric mapper
- Exact alias maps to the expected metric.
- Unknown label remains unmapped.
- Ambiguous alias is flagged.
- Original label/value remain unchanged.
- Unit conversion is correct for explicitly supported units.
- No component sum occurs without an explicit configured rule.

### Validator
- Detect missing/ambiguous candidates.
- Flag unit mismatch.
- Flag duplicate/overlapping total and component candidates where detectable.
- Treat year-on-year movement as a warning, not automatic correction.
- Preserve warnings and their rule IDs.

### Review and persistence
- Approve records a decision and timestamp.
- Reject requires a reason.
- Correct requires a replacement value and reason.
- Original candidate is unchanged after correction.
- Decisions survive app restart.
- Only eligible approved results appear in approved-only export.

### Excel exporter
- Workbook opens successfully.
- Expected sheets and columns exist.
- Approved-only export excludes rejected and unresolved values.
- Evidence metadata is included.
- Numeric precision and units are preserved.

## 3. Integration tests

- Upload a fixture PDF, extract candidates, map, validate, save, review, and export.
- Restart/reload data and confirm review state persists.
- Process the same document hash again and warn about duplicate processing.
- Handle missing metric candidates without treating them as zero.

## 4. Manual evaluation set

Before claiming the prototype works, select representative report pages and manually record expected values, labels, units, reporting periods, and page references. Include:
- a straightforward text-based report
- a report with complex tables or multiple candidate occurrences
- a scanned or image-heavy report if one exists in the actual set
- at least one case with unit ambiguity or a summary-rounded figure if available

Do not invent test outcomes. Record actual observed results.

## 5. Metrics to measure

- Candidate coverage: proportion of target metrics for which a candidate is found.
- Exact value accuracy: proportion of extracted candidates matching the manually verified target value and unit.
- Source accuracy: proportion citing the correct page/section and reporting-period column.
- Mapping accuracy: proportion mapped to the correct metric/component.
- Review time per metric.
- Percentage requiring manual correction or unresolved review.

Report denominators and test-set size. A small test set cannot establish broad reliability.

## 6. Acceptance criteria for the first usable prototype

- Can process one local PDF at a time.
- Can select metrics from configuration.
- Displays candidate values with source-page evidence.
- Keeps multiple candidates where appropriate.
- Flags unclear values instead of guessing.
- Allows approval, rejection, and correction with review history.
- Persists records across restarts.
- Exports approved values with provenance to Excel.
- No external data transfer occurs.
- Tests cover parsing, mapping, validation, review, persistence, and export.
- Known limitations are visible and documented.
