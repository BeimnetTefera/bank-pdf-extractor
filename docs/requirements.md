# Product and Functional Requirements

## 1. Problem statement

The team needs financial metrics extracted from approximately eight banks' annual-report PDFs. The target metrics and prior-year data already exist. Banks may label equivalent metrics differently, report components separately, repeat figures in multiple sections, use rounded figures in summaries, or present different units and reporting scopes.

A simple PDF-to-spreadsheet conversion is insufficient because the result must be traceable and reviewed before it is treated as approved data.

## 2. Primary user

One internal analyst/reviewer operating the prototype locally. The prototype is single-user.

## 3. Main workflow

1. Start the local application.
2. Select a bank and reporting period, or enter them for the current document.
3. Upload one annual-report PDF.
4. Select the metrics to extract from the approved metric list.
5. Run extraction.
6. Inspect candidate values, source locations, original labels, units, alternative candidates, and warnings.
7. Open the relevant PDF page and compare the candidate with the source.
8. Approve, reject, or correct the candidate.
9. Export approved results to Excel, including provenance.
10. Reopen the project later without losing saved decisions.

## 4. Functional requirements

### FR-01: PDF intake
- Accept one PDF at a time.
- Validate file extension and basic readability.
- Show a useful error for invalid, encrypted, or unreadable PDFs.
- Associate each document with bank, reporting period, filename, upload time, and file hash.
- Do not send documents to external services.

### FR-02: Metric selection
- Show a list of predefined metrics configured in `config/metrics.yaml`.
- Allow the user to select a subset in the UI.
- Each metric has a stable ID, display name, definition/notes, expected unit or unit policy, and mapping references.
- The user should not need to edit Python code just to select a metric already configured.
- Adding a new official metric definition may require editing YAML in the first prototype.

### FR-03: Candidate extraction
- Extract text and table candidates using local PDF libraries.
- Preserve candidate label, value text as printed, parsed numeric value when safely parseable, page index, printed page number if identifiable, nearby context, and extraction method.
- Retain multiple plausible candidates when found.
- Never describe a candidate as verified merely because it was extracted.

### FR-04: Metric mapping and normalization
- Use explicit mappings and conservative matching rules.
- Normalize numeric representations and units only when the conversion is unambiguous.
- Preserve original label, original value text, and original unit.
- Do not sum components unless the metric definition and component mapping explicitly authorize it.
- Do not assume customer deposits and interbank deposits should be combined. That business definition must be agreed by the team.

### FR-05: Validation
- Flag missing values, ambiguous candidates, inconsistent units, potential duplicate totals/components, year/column ambiguity, and large year-on-year changes when comparison data is provided.
- Arithmetic checks are advisory and must not silently change figures.
- A warning is not necessarily an error; reviewers can record a reasoned decision.

### FR-06: Human review
- Display the PDF page/evidence and candidate data in the same review workflow.
- Allow approve, reject, and correct actions.
- Require a reason/note for a correction or rejection.
- Keep the original candidate unchanged and store the reviewer action separately.
- Record decision timestamp and reviewer label (single-user prototype may use a configurable local label; no authentication system in v1).
- Unresolved candidates must not appear in the approved-only export.

### FR-07: Persistence
- Save document metadata, candidates, validation findings, and review decisions in SQLite.
- Persist state across app restarts.
- Avoid duplicate processing of the same document version without warning.
- A corrected/replaced PDF must be treated as a new document version.

### FR-08: Excel export
- Provide an approved-only export.
- Optionally provide a full review workbook with candidates, warnings, and review statuses.
- Include bank, reporting period, metric ID/name, approved value, normalized unit, original label/value/unit, document name/hash, PDF page index, printed page if known, source section/context, extraction method, decision, and decision timestamp when available.
- Do not overwrite historical datasets or publish results elsewhere.

### FR-09: User feedback
- Show processing status and useful errors.
- Clearly distinguish Approved, Needs Review, Rejected, and Flagged/Unresolved states.
- Do not imply that an automated confidence score proves correctness.

## 5. Non-functional requirements

- Local-first: all PDF processing and storage remain on the user's machine.
- Traceability: every candidate should retain available evidence and document identity.
- Safety: fail visibly rather than silently dropping unclear data.
- Maintainability: extraction, mapping, validation, review, storage, UI, and export are separate responsibilities.
- Testability: core logic can be tested without running Streamlit.
- Usability: the interface should be understandable to an analyst, not only a developer.
- Prototype simplicity: no cloud deployment, multi-user auth, or service architecture unless later approved.

## 6. Out of scope for the first prototype

- Automatic approval of financial figures.
- Guaranteed extraction of all PDF layouts.
- Automatic OCR for scanned PDFs (evaluate after inspecting representative PDFs).
- External LLMs, external OCR APIs, or sending data to vendors.
- Batch ingestion of all eight banks at once.
- Production-grade multi-user permissions.
- Automatic write-back into the historical dataset.
- Scheduled ingestion or web scraping of bank websites.

## 7. Open business decisions

These must be confirmed with the user/team before they become implemented rules:

1. Exact definition of each target metric.
2. Standalone bank statements vs consolidated group statements.
3. Whether customer and interbank deposits are separate or combined for the desired metric.
4. Preferred reporting-year convention and whether fiscal year-end is consistent.
5. Approved source hierarchy when multiple values exist.
6. Whether previous-year values are available in a machine-readable format for comparison.
7. Who is allowed to approve values when the tool later becomes multi-user.

## 8. Prototype success criteria

The prototype is useful if a reviewer can:
- Upload one representative PDF and select configured metrics.
- See candidate figures with source page/context.
- Identify multiple candidates and warnings.
- Inspect the source page and approve, reject, or correct a candidate.
- Restart the app and retain decisions.
- Export approved values with provenance to Excel.
- Understand which cases failed or need manual work.

Extraction quality must be measured separately using manually verified values from representative reports.
