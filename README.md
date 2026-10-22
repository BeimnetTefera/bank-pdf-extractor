# Bank PDF Extractor

A local-first prototype for extracting financial metrics from bank annual-report PDFs, reviewing each candidate against source evidence, and exporting approved values to Excel.

## Status

**Planning and prototype stage.** This repository describes the intended design. Extraction accuracy is not assumed; the system must be tested against manually verified figures before use for business decisions.

## Goals

- Process one bank report at a time.
- Let a user select predefined financial metrics in the interface.
- Extract candidate values and retain source evidence (document, page, original label, unit, table/section context where available).
- Normalize labels and units using explicit, reviewable rules.
- Flag ambiguity and validation failures rather than silently guessing.
- Let a reviewer approve, reject, or correct a candidate.
- Preserve review decisions and export approved results with provenance to Excel.
- Keep PDFs, the database, and exports local by default.

## Non-goals for the first prototype

- Fully autonomous, guaranteed-correct extraction.
- Batch processing of all banks at once.
- Production multi-user access, authentication, or cloud deployment.
- External LLM/API calls.
- Automatic updates to the existing historical dataset.
- OCR for scanned PDFs until a real sample demonstrates it is needed.

## Proposed stack

- Python 3.14 (see decision D-010)
- Streamlit for the local web UI
- PyMuPDF and pdfplumber for initial PDF text/table inspection
- pandas and openpyxl for tabular data and Excel export
- SQLite for local persistence
- PyYAML for metric and label-mapping configuration
- pytest for tests

Use the versions in `requirements.txt`; update them only deliberately and test after changes.

## Setup

Create and activate a virtual environment, install dependencies, then run the tests and the app:

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Git Bash:
source .venv/Scripts/activate
# macOS / Linux:
source .venv/bin/activate

python -m pip install -r requirements.txt
python -m pytest
streamlit run app.py
```

The exact commands may vary by shell. Do not put real annual reports, database files, exports, or secrets in Git.

**Windows troubleshooting:** if importing pandas or pyarrow fails with `DLL load failed ... An Application Control policy has blocked this file`, Windows Smart App Control is blocking the package's compiled files. Changing Python or package versions does not reliably help. Turn it off under Windows Security → App & browser control → Smart App Control settings (check company policy first), or run the app under WSL.

## Configuration

| File | Purpose |
|---|---|
| `config/metrics.yaml` | Metrics to extract (the Shega dataset's bank columns). `active: true` makes a metric selectable. |
| `config/mappings.yaml` | Exact label aliases per metric, added after reviewing real report labels. |
| `config/settings.yaml` | Data folder, upload size/page limits, default reviewer name. |

Uploaded PDFs, the SQLite database and Excel exports are stored in `%LOCALAPPDATA%\BankPdfExtractor` by default (outside the project and OneDrive). Set `data_dir` in `config/settings.yaml` to change this. The app shows the active data folder on its home page.

If a config file is invalid, the app and the tests list every problem with the file, entry and fix needed.

## Project documents

Read these in order:

1. `docs/requirements.md`
2. `docs/architecture.md`
3. `docs/data-model.md`
4. `docs/extraction-and-mapping.md`
5. `docs/review-and-export.md`
6. `docs/security-and-privacy.md`
7. `docs/testing-and-acceptance.md`
8. `docs/implementation-plan.md`
9. `docs/decisions.md`

Claude Code instructions are in `CLAUDE.md`.

## Important financial-data rule

An extracted value is a **candidate**, not a verified fact. Every candidate must retain its source context. The app must never silently select between conflicting figures or mark a value approved merely because automated checks pass.
