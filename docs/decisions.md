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
- Should the extractor also capture figures from narrative sentences and infographics (number before label, e.g. "981 branches")? Needed for most operational metrics; see docs/manual-testing.md.
- `eps_etb` expects ETB, but at least one bank prints EPS as a percentage. How should that be recorded?

## Change process

When a decision changes:
1. Update this log.
2. Update the affected specification.
3. Explain the impact on code/tests.
4. Ask for confirmation if the change affects scope, security, external data transfer, storage architecture, or business definitions.
