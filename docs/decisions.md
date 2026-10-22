# Decision Log and Open Questions

Use this file to record decisions as they are made. Do not silently convert an assumption into a confirmed requirement.

## Initial working decisions

| ID | Decision / assumption | Status | Reason |
|---|---|---|---|
| D-001 | First prototype is local-first and single-user | Confirmed 2026-10-22 | Fast prototype and lower data exposure |
| D-002 | Use Streamlit for the first UI | Confirmed 2026-10-22 | Python-first and fast to build |
| D-003 | Process one bank PDF at a time | Confirmed 2026-10-22 | Matches intended working process |
| D-004 | Use SQLite for local persistence | Confirmed 2026-10-22 | Simple local storage without a server |
| D-005 | Export approved results to Excel | Confirmed 2026-10-22 | Matches current analyst workflow |
| D-006 | No external AI/API calls in v1 | Confirmed 2026-10-22 | Protect sensitive documents and keep scope small |
| D-007 | Extracted figures remain candidates until reviewed | Required principle | Financial correctness and traceability |
| D-008 | Preserve raw evidence and review history | Required principle | Auditability and correction tracking |
| D-009 | Do not overwrite historical dataset | Confirmed 2026-10-22 | Keep extraction output separate and reviewable |
| D-010 | Use Python 3.14 (not 3.12) | Confirmed 2026-10-22 | Only version installed; all dependencies publish 3.14 Windows wheels |
| D-011 | Project is a Git repository; commits only when the user asks | Confirmed 2026-10-22 | `.gitignore` keeps PDFs, database and exports out of history |
| D-012 | App data (uploads, SQLite, exports) defaults to `%LOCALAPPDATA%\BankPdfExtractor`, configurable in `config/settings.yaml` | Confirmed 2026-10-22 | Keeps working files out of the OneDrive-synced project folder. The source PDFs are public, but extracted/reviewed data should stay local |
| D-013 | Target metrics are the BANK table columns of the Shega data pipeline (`src/bronze/schema.py`, `BANK_COLUMNS`) | Confirmed 2026-10-22 | Output must line up with the existing Shega dataset. Definitions are copied from the pipeline descriptions and remain provisional |
| D-014 | Derived ratios (per-branch, per-account-holder, average assets, digital penetration) are `active: false` and never calculated by the app | Proposed | No calculation policy agreed; avoids inventing business rules |
| D-015 | ROE, ROA and EPS are extracted only as printed in the report, not calculated | Proposed | Same reason as D-014 |
| D-016 | Reviewer label is a free-text name stored with every review decision (default: Windows user name, editable in the app). No login | Proposed | Answers "who approved this figure" for the audit trail without building authentication |
| D-017 | Reporting period uses the Shega `fiscal_year` format `YYYY/YY` (Ethiopian fiscal year, July–June), e.g. `2025/26` | Proposed | Matches the existing dataset key |
| D-018 | Config is validated by hand-written checks into dataclasses; Pydantic not added | Confirmed 2026-10-22 | Config is small and already fully validated and tested; avoids a dependency |
| D-019 | Phase 2 extraction uses PyMuPDF only (word positions grouped into rows); pdfplumber is not used yet | Confirmed 2026-10-22 | One extraction path until a real report shows PyMuPDF failing; BOA balance sheet rows were rebuilt correctly |
| D-020 | Stored PDFs are named by their SHA-256 hash (`uploads/<sha256>.pdf`); the original filename is kept only as metadata | Confirmed 2026-10-22 | Repeated filenames cannot overwrite a different report; user filenames are never used as paths |
| D-021 | Also extract numbers from prose text blocks with the words around them (`pymupdf_narrative`), kept separate from table rows. Bare years are skipped | Confirmed 2026-10-22 | Operational metrics (branches, ATMs, employees, ROE) are written in sentences, not tables |
| D-022 | Mapping wording lives in `config/mappings.yaml`: exact table `aliases`, sentence `text_after` (≤1 word gap) and `text_before` (≤4 word gap, no other number in between). Matching ignores case, spacing and ligatures | Confirmed 2026-10-22 | Approved alias list from the BOA report; nearest-word rule stops lists like "981 branches, 1,761 ATMs" from cross-matching |
| D-023 | Table rows that also contain a year (charts) are kept as alternatives with an info note, never used in conflict checks; bare years and values under a "Notes" column are never mapped | Proposed | BOA highlight charts pair labels with the wrong year's bar |
| D-024 | Not mapped for now: total deposits table lines (no summing of components), total revenue/expense, NPL, foreign assets, NBE bills, agents, mobile users, cardholders, account holders. Total deposits is taken only from an explicit sentence total | Confirmed 2026-10-22 | No agreed definition or no matching label; these show as "Not found" for manual review |
| D-025 | A reviewer can **correct** an extracted value (new value, unit and a required reason), stored as a separate review record; the extracted value is never overwritten and the corrected value is labelled human-corrected. For "Not found" metrics, a value can be **entered manually** with its source page and a note | Correction confirmed 2026-10-22 (user request); manual entry Proposed | Extraction will not always be right; the audit trail must show who changed what and why. Built with persistence (Phases 4–5) |
| D-026 | Conflict check: this year's figures (table columns under the report-year heading, plus sentence figures) must agree within rounding (half of the last printed digit); prior-year disagreements are info notes | Proposed | Rounded summaries (318.79 billion) must not be reported as conflicts with exact statement figures |
| D-027 | A metric is "Flagged" only for conflicts, ambiguous wording, or a warning on its suggested figure; issues on weaker alternatives are shown with them | Proposed | Avoids flagging every metric that also appears in a chart or a note |
| D-028 | Sign convention is the reviewer's choice per figure: when approving, record the value "as printed" or "as a positive amount"; the choice is stored with the decision. Corrections can enter any value | Confirmed 2026-10-22 | Banks print expenses such as tax in brackets; the person reviewing decides how it is recorded |

The user should confirm or revise proposed decisions before they are treated as final.

## Open business questions

- ~~What is the authoritative list of target metrics?~~ Resolved by D-013.
- What is the agreed definition of each metric? (Provisional: Shega pipeline descriptions.)
- Which reporting scope is required: standalone bank or consolidated group?
- Which source hierarchy should be used for each metric?
- Are interbank deposits part of `total_deposits_etb`? (The pipeline definition says customer deposits only.)
- How should the reporting year and fiscal year-end be represented? (Proposed: D-017.)
- Is previous-year data available in a file that can be used for comparisons? (The Shega BANK table may serve; to confirm.)
- ~~Where may sensitive PDFs, database files, and exports be stored under company policy?~~ Resolved by D-012.
- Does the team require a particular reviewer name or approval convention? (Proposed: D-016.)
- ~~Should the extractor also capture figures from narrative sentences and infographics?~~ Resolved by D-021.
- `eps_etb` expects ETB, but at least one bank prints EPS as a percentage. How should that be recorded?
- ~~Sign convention for expense metrics~~ Resolved by D-028: the reviewer chooses per figure.
- `total_assets_previous_year_etb` is the prior-year column of "Total assets". Linking it needs a "prior-year column" option on a mapping entry; not built yet.
- Total deposits: should the balance-sheet lines "Deposits from customers" + "Interest free customers deposits" be combined (they sum to BOA's stated 318.79 billion)?

## Change process

When a decision changes:
1. Update this log.
2. Update the affected specification.
3. Explain the impact on code/tests.
4. Ask for confirmation if the change affects scope, security, external data transfer, storage architecture, or business definitions.
