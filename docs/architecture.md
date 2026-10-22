# Architecture

## 1. Architecture goals

- Build a useful local prototype quickly.
- Make every extracted figure traceable to its source.
- Separate candidate extraction from financial interpretation and approval.
- Keep core logic testable without the UI.
- Leave room to add OCR or a richer review interface only when evidence justifies it.

## 2. High-level flow

```text
Analyst
  |
  v
Streamlit UI
  |---- selects bank, period, metrics
  |---- uploads one PDF
  v
Document Manager ----> Local PDF storage + document metadata
  |
  v
PDF Extraction Engine (PyMuPDF / pdfplumber)
  |
  v
Candidate Records (raw label/value/context/page)
  |
  v
Metric Mapper + Unit Normalizer <---- metrics.yaml / mappings.yaml
  |
  v
Validation Engine
  |
  v
Review UI <---- PDF evidence viewer
  | approve / reject / correct
  v
Review Service + SQLite
  |
  v
Excel Exporter
  |
  v
Approved-only workbook / full review workbook
```

## 3. Components and responsibilities

### UI (`app.py`, optionally `src/ui/`)
- Streamlit screens, widgets, and user interaction.
- Calls service functions; should not implement parsing or financial business rules.
- Displays source evidence and review state.

### Document manager (`src/document_manager.py`)
- Validates PDF inputs and calculates SHA-256.
- Creates a document record and a stable local stored filename.
- Keeps the original PDF unchanged.
- Avoids unsafe path handling and accidental overwrites.

### PDF extractor (`src/pdf_extractor.py`)
- Reads text and table structures using local libraries.
- Produces candidate records with evidence.
- Must preserve page index (zero-based internally) and printed page number only when it can be determined reliably.
- Does not choose the final business metric or approve values.
- Keep parser-specific logic behind a small interface so another parser can be added later.

### Metric mapper (`src/metric_mapper.py`)
- Maps raw labels to configured metric/component IDs using exact aliases first.
- Optional conservative normalized-text matching may suggest candidates, but ambiguous matches must be flagged.
- Applies explicit unit conversions.
- Keeps original fields intact.

### Validator (`src/validator.py`)
- Produces findings with severity, rule ID, message, and affected candidate IDs.
- Checks format, missing/duplicate candidates, period/column ambiguity, unit mismatches, possible total/component overlap, and optional historical movement.
- Does not auto-correct ambiguous values.

### Review service (`src/review_service.py`)
- Implements approve/reject/correct transitions.
- Requires correction/rejection notes.
- Keeps candidate data immutable after extraction; correction is a separate review record or revision.
- Enforces that only eligible candidates can be approved and only approved records enter the approved-only export.

### Persistence (`src/database.py`)
- SQLite schema initialization and repository functions.
- Use parameterized SQL.
- Keep database access out of UI and extraction modules.
- Store timestamps in ISO 8601 format, ideally UTC internally.

### Excel exporter (`src/excel_exporter.py`)
- Exports approved values and source provenance.
- Provides a separate full-review workbook if requested.
- Avoid formulas or macros; plain data tables are sufficient.
- Use safe, deterministic column names and include a README/metadata sheet if helpful.

## 4. Data flow rules

- Candidate extraction is not approval.
- Raw evidence is retained even when a normalized metric is produced.
- Metric definitions and mappings are configuration, not hidden code constants.
- Validation findings inform review; they do not automatically determine correctness.
- Corrections must preserve the initial extracted candidate and record the reviewer’s replacement value and reason.
- Export reads persisted approved records; it does not re-run extraction.
- Never automatically merge output into the existing historical dataset.

## 5. Technology choices

| Need | Initial choice | Why | Reconsider if |
|---|---|---|---|
| Local UI | Streamlit | Fast Python-first prototype | Multiple concurrent users or complex interaction requirements emerge |
| PDF text/layout | PyMuPDF | Fast local PDF inspection and page rendering/text extraction | Specific table layouts require another parser |
| Table extraction | pdfplumber | Useful table and word-position extraction | Tests show poor performance on representative reports |
| Dataframes/Excel | pandas + openpyxl | Familiar tabular workflow and XLSX output | Output format changes |
| Local persistence | SQLite | No server to administer; adequate for single user | Shared multi-user use is required |
| Configuration | YAML + PyYAML | Human-readable metric and alias definitions | Configuration management needs a database UI |
| Tests | pytest | Simple automated regression testing | — |

Use only dependencies justified by a concrete requirement. PyMuPDF and pdfplumber overlap in some capabilities; use them where each adds value, and avoid maintaining two extraction paths without a test-backed reason.

## 6. Error handling

- Invalid/encrypted/unreadable PDF: show a clear error and do not create misleading candidates.
- No text extracted: explain that the document may be scanned and mark OCR as a follow-up, rather than silently returning an empty successful result.
- Numeric parse failure: preserve original text and flag for review.
- Ambiguous mapping: keep alternatives and request human choice.
- Database/export failure: show actionable error and do not claim success.
- Missing source location: mark evidence incomplete and prevent automatic approval.

## 7. Future options (not v1)

- OCR engine for scanned pages.
- Additional parsers selected by document characteristics.
- Internal authentication and role-based approval.
- Shared database and centralized document storage.
- Browser-based deployment.
- LLM-assisted candidate ranking, only after security/privacy review and measured benefit.
- Bank website monitoring or report discovery.

Any future option requires a separate decision; do not implement by assumption.
