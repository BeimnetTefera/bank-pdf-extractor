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
- ~~OCR for scanned PDFs until a real sample demonstrates it is needed.~~ Added (decision D-038): Abay Bank's statement is photographed pages. See "Reports made of pictures (OCR)".

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
| `config/metrics.yaml` | Metrics to extract (the Shega dataset's bank columns). `active: true` makes a metric selectable; `level: sector` makes it a sector figure, entered once per year on the Sector data page. |
| `config/mappings.yaml` | Exact label aliases per metric, added after reviewing real report labels. |
| `config/learned_aliases.yaml` | Table labels learned from reviews (written by the app, see "The app learns each bank's wording"). Used like aliases in `mappings.yaml`. |
| `config/checks.yaml` | Arithmetic cross-checks between figures, e.g. total assets = total liabilities + total equity. |
| `config/settings.yaml` | Data folder, upload size/page limits, default reviewer name, Tesseract OCR folder. |
| `config/banks.yaml` | Banks offered in the Bank dropdown, spelled as in the Shega bank timeseries spreadsheet (exported in its `bank` column). |
| `config/export_template.yaml` | Column layout of the export's Approved Data sheet (the Shega spreadsheet header), and which columns are `calculated` (entered by an analyst). Each metric's `export_column` in `metrics.yaml` says which extracted column it fills. |

Uploaded PDFs, the SQLite database and Excel exports are stored in `%LOCALAPPDATA%\BankPdfExtractor` by default (outside the project and OneDrive). Set `data_dir` in `config/settings.yaml` to change this. The app shows the active data folder on its home page.

If a config file is invalid, the app and the tests list every problem with the file, entry and fix needed.

## Inspecting a PDF (Phase 2)

```bash
python -m scripts.inspect_pdf "/c/path/to/report.pdf" --find "total assets"
```

Prints page count, text availability and raw label/value rows with their PDF page. See `docs/manual-testing.md` for the full procedure and recorded observations. All output is unverified.

**Checking a page:** `python -m scripts.inspect_pdf report.pdf --page 22` also shows the column blocks and statement titles found on that page.

## Reviewing a report in the app

1. `streamlit run app.py`, open **Extract & Review**.
2. Choose the bank from the list, enter the report year (e.g. `2025/26`), upload the PDF, choose metrics if needed.
3. Click a metric to see its figure boxed on the PDF page, its source, unit conversion, checks and other candidates.
4. Press **Save report**, then for each metric: **Approve** (choose the sign; confirm any warnings), **Reject** (reason), **Correct** (value, unit, reason, then *Approve correction*), or **Enter manually** for metrics not found.
5. Your name (sidebar **Reviewer**) and the time are saved with every decision; extracted values are never overwritten.
6. When the review is done, press **Finish extraction** (under the metric table). The figures are locked; **Reopen extraction** (under the metric table, or on the Calculations page) unlocks them, with a reason.
7. On **Calculations**, the analyst sees the approved figures and enters the 26 calculated columns (ratios, averages, growth, "(Calculated)" values) with a note on the formula. Each column shows its draft formula, the **Auto value** the app works out from the approved figures, and the difference from the entered value (and from the printed figure for profit, EPS, ROA and ROE; ⚠ above 0.5%). **Use auto values for empty columns** records the auto values with their arithmetic. The formulas are drafts in `config/export_template.yaml` (`formulas`), listed for review in `docs/calculated-formulas-draft.md`.
8. Reopen saved reports later from **Reports** (the first item in the menu). Uploading a PDF that is already saved also reopens its review.
9. To remove a saved report (a demo, or one saved twice), open its **⋯** menu on **Reports**, give a reason and tick the confirmation. Everything of that report is deleted, including its stored PDF unless another copy uses it; the deletion is logged under **Deleted reports**. Export the year again to update its Excel file.

The **All Figures** tab lists every raw number in the PDF, including ones not matched to a metric.

### Typing values

Every value box reads what you type the same way. For a percentage, `22.99`, `22.99%`, `22.99 percent` and `0.2299` all store 0.2299 (22.99%). A bare number of 1 or less without a % sign (e.g. `0.5`) could mean half or half a percent, so the app asks which you mean (the default is a fraction: 0.5 = 50%). For amounts and counts you can use commas and a scale word: `1,234.5 million` stores 1,234,500,000. Under every box a line says **Will store ...**, so you see the saved value before you press anything.

### Approve says what it stores

The Approve area states the figure as printed, the page's unit and the full value saved, e.g. "Approve 189,512,266 (Birr'000) as 189,512,266,000 ETB".

### Brackets

Banks print some amounts in brackets, e.g. tax paid `(1,357,194)`. Most metrics record them as a positive amount; metrics that can really be negative (profit before and after tax, ROE, ROA, EPS) keep the sign as printed. This is the `sign` setting of each metric in `config/metrics.yaml` (`as_printed`, otherwise positive). For a figure printed in brackets, Approve offers a choice whose default comes from that setting; you can change it. A value you type in brackets (or with a minus) in Correct, Enter manually or a marked number gets the same choice, *Record as positive* or *Keep negative*, with the metric's setting marked "(default)"; the "Will store" line follows your choice, and the app remembers it (Evidence says "Typed in brackets (negative); recorded as a positive amount / kept negative, as the reviewer chose"). A value from an external source printed in brackets works the same way.

### Share counts in thousands

Some banks print the number of shares in thousands under the same Birr'000 heading as the amounts (Zemen 2025/26 page 63: 12,538, meaning 12,538,000). For the weighted average number of shares, a figure under a '000 heading that is below 1,000,000 is multiplied by 1,000, and a note says so; a larger figure is taken as printed (BoA: 9,907,298). The threshold is `count_in_thousands_below` in `config/metrics.yaml`.

### EPS as a percentage of par value

Some reports print EPS as a percentage of the par value of a share (e.g. "EPS 39%" with shares of Birr 1,000 each). When the app finds the bank's own par value in the report, Metric Details for EPS shows a **Convert** button, e.g. "Convert: 39% x par value Birr 1,000 (p113) = 390 Birr". The par value is read from wordings such as "ordinary shares of Birr 1,000 each", "par value of ETB 1,000 each", "par value per share Birr 1,000", "nominal value of Birr 1,000" or "Birr 1,000 per share" right after "share capital"; par values of other companies (investments) and price, dividend or earnings figures per share are skipped. One press records both figures (with their pages and boxes) and proposes the result as a calculation, all together or nothing if any part is refused; approve it like any other calculation. When profit after tax and the weighted average number of shares are both approved, a cross-check line shows profit / shares, for comparison only.

### Reviewing faster

The metric table shows each metric's **Value** (as stored) and **As printed**. The pills above it filter by status and show how many metrics each has; rows with a correction waiting for approval are highlighted. **Previous** and **Next** (or Alt+Up / Alt+Down) step through the rows shown. **Approve all verified** lists the suggested figures that are from the report year's column, converted, positive and without warnings, plus the metrics left for you and why. Check the list against the report, tick "I checked this list against the report", then press Approve; each figure is approved separately in your name.

### All Figures

Search for a number with or without commas (`189512266` finds `189,512,266`) or for words. Filters narrow the list by page range, table or sentence, year column and value range (as printed). The line above the table gives the true number of matches; the table shows 100 rows per page. Selecting a row shows its whole table row or sentence.

### PDF viewer

**Fit width** and **Fit page** size the page to the panel; **Full screen** opens a larger view. Selecting a figure zooms to 200% around it, until you move the zoom slider yourself; from then on your zoom is kept. Use the sliders to move around a zoomed page (mouse-wheel zoom and drag-to-pan are not available: dragging is used for marking numbers).

### Work left on Reports

Each saved report on **Reports** shows **Work left (n)**: corrections and calculations waiting for approval and metrics not yet reviewed (sector metrics are not counted), each with an **Open** button that opens the report at that metric.

### Marking the right number on the page

When the suggested figure is wrong, or a metric was not found:

1. Go to the page with the right number and switch on **Mark a number** (above the page).
2. Drag a box around the number. The app reads it (from the text, or from OCR on picture pages).
3. Below the page, in **Which figure is this?**, type part of the metric name (e.g. "assets") and pick it. The value is filled in from your box, converted with the page's unit (e.g. Birr'000 → full Birr); edit it if the box read it wrong. Press **Save marked value**, then approve it in the details panel.

Faster when the number was extracted: search **All Figures** (e.g. "paid-up"), select the right row, and press **Use this figure for a metric** in Figure Details. The same form opens there, with the value already filled in; the figure's exact page and position are saved as the evidence. If the report is not saved yet, the button reads **Save report and record this value**: one press saves the report and records the value (bank and year are still needed).

Figure Details shows the figure's **Year column**. Statements print this year and last year side by side, so check it: if the number is in another year's column than the report year (e.g. the 2024 column of a 2024/25 report, which ends in June 2025), a warning says which column to use instead.

The box is saved with the value. On the page, every recorded value is boxed in the marker's blue: the selected metric's box bold, shaded and named ("(awaiting approval)" until approved), the others as thin outlines so names never cover figures. The suggestion it replaced is no longer boxed, so whoever checks the work sees exactly which number was used. A red box is only a suggested figure you are looking at. Corrections and **Enter manually** in the details panel use your mark too; a manual entry needs one.

### The app learns each bank's wording

Banks word their statements differently ("Total assets", "Less: Income tax expense", …), and the app only recognises wording it has been given. When you record a value from a table row it did not recognise (a marked box or **Use this figure for a metric**) and approve it:

1. Under the metric table, **Learn wording (N to add)** lists those rows' labels. Press **Add** for a label that always means that metric in this bank's reports. It is saved in `config/learned_aliases.yaml` with the bank, your name and where it was seen, and is recognised in **that bank's** reports from then on; other banks keep their own wording (`mappings.yaml` still applies to every bank). Delete the entry there to undo it. Nothing is learned until you press Add. A label that already means another metric is not offered: e.g. "Total assets" for *Total assets (previous year)*, which is the same row's other year column, not different wording.
2. A saved report then shows **N figure(s) found with the current wording are not part of this saved report**. Press **Add them to the saved report** to make them approvable. Nothing already saved or reviewed changes, and the addition is recorded with your name. (Only while the extraction review is open.)

**Wording.** Templates → Wording lists, adds and removes the table labels and sentence wording of each metric in `config/mappings.yaml`, keeping the file's comments. Labels learned per bank stay in `config/learned_aliases.yaml`.

### How figures are found (extractor 0.3)

Pages printed as two tables side by side (landscape spreads) are read one side at a time, and each number takes the year of the heading it lines up with. Statement lines are suggested before notes and sentences. When total assets = liabilities + equity (or profit before tax = profit after tax + tax) balances with exactly one set of figures, that set is suggested first and figures that break it are flagged. In sentences, a stated total ("bringing the total number of branches to 849") is preferred to a change ("opened 123 additional branches"), and "6.6 percent" reads as 6.6%. A note that prints another figure than the statement is shown as an info note, not a conflict. New warnings: IDENTITY, SENTENCE-CHANGE, RELATED-LINE (a second statement line that may belong in the total, e.g. interest-free deposits) and COUNT-SCALE. Info notes: APPROXIMATE, NOTE-DIFFERS.

Reports saved with an older extractor still open and keep every decision; figures the new extractor finds can be viewed but not added to them (save the report again for a fresh review).

### Cross-checks

Under the metric table, **Cross-checks** compares figures that must agree, and opens by itself when one fails:

- total assets = total liabilities + total equity, and profit before tax = profit after tax + income tax (from `config/checks.yaml`);
- this report's prior-year column against the figures approved in the same bank's previous-year report, if that report is saved.

They use approved values where there are any, otherwise the suggested figures, and allow for rounding of the printed figures. A failure means "look here": the checks never change, approve or reject a figure. Failed checks are also listed in **Finish extraction**.

## Evidence pages

Every value has an **Evidence page** that shows why it is what it is: the value in full Birr and as printed, who approved it and when, the note in full, the PDF page with the box and a close-up of the line, the other figures found and why they were not used, and the full review history. Open it with **View evidence page** in Metric Details, or from the **Evidence summary** (in the **⋯** menu of each report on Reports), which lists every metric of a report and offers a CSV and an evidence PDF to hand to someone else. **Open in viewer at this page** jumps back to Extract & Review at that figure.

In the Excel export every value cell links to its Evidence page and carries a comment with its source and note, and the **Evidence** sheet lists every metric with its status. The links use `app_url` in `config/settings.yaml` (default `http://localhost:8501`), so they open where the app is running.

## Calculated values

When the report does not print a value (ROE, ROA, EPS in Birr, operating expense, mobile + internet users…), open the metric in Extract & Review and use **Calculate from other figures**:

1. Take the **standard formula** (from `config/metrics.yaml`, the same for every bank, with its basis, e.g. ROE on average equity) or **type your own** with metric ids, `<metric id>_prev` for the previous year, and new names for figures you will mark.
2. Each input shows where it comes from: an approved metric (page, year column, row), the prior-year column of the same row, the bank's previous-year report, or a figure you mark. A missing figure has **Mark it**: drag a box around the number (or pick it in All Figures) and save it as the input. An input that is not approved yet blocks the calculation.
3. Check the arithmetic with the real numbers, write a note, **Save calculation**, then **Approve calculation**.

The Evidence page shows the formula in words and symbols, the arithmetic, and every page that holds an input with the inputs boxed in **green**. A value typed with **Enter manually** whose note holds the arithmetic (entered before Calculate existed) is shown the same way in Extract & Review: its inputs boxed in green and a **Calculated** card in Metric Details, instead of a box on one of the inputs. If an input is changed later, the calculation is marked **stale**.

## External sources and sector data

When a figure is not printed in the bank's report, open the metric in Extract & Review and use *Value from an external source*. Enter the document title, publisher, its address (stored as text; the app never opens it) and/or upload a copy (PDF or image; give the page and, optionally, drag a box around the figure), the exact quote, and the value as printed with its scale. The app shows the converted value, and the entry needs approval like a correction. A value printed in brackets offers *Record as positive* or *Keep negative*, starting from the metric's sign setting (see Brackets); cross-checks keep the figure as printed, since they compare sizes. When a calculation uses a metric whose value came from an external source, its Evidence page names that source (title, publisher, page and quote) next to the input.

Sector-wide figures (metrics with `level: sector` in `config/metrics.yaml`, such as total deposits in the sector) are entered once per fiscal year on the **Sector data** page: record, approve, mark not reported, or reopen. The Excel export puts the approved sector value in every bank row of that year.

**External cross-checks.** Any metric can be checked against an external figure (*Cross-check with an external source* in Metric Details, or on its Evidence page). The Evidence page flags Match (within 0.5%, or 2 thousand Birr of rounding for Birr'000 figures) or Mismatch. Cross-checks are withdrawn with a reason, never deleted, and mismatches are listed in the export's Export Metadata and in the `cross_check` column of the Evidence sheet.

Uploaded source copies are kept in the app data folder under `sources/`, named by their SHA-256 fingerprint.

## Reports made of pictures (OCR)

Some reports are photographs or scans: their pages have no text layer, so nothing can be extracted directly. The app then offers **Read with OCR** in Extract & Review.

- Needs **Tesseract OCR** installed on this computer (Windows installer: github.com/UB-Mannheim/tesseract, with English). The app finds it on PATH or in `C:\Program Files\Tesseract-OCR`; otherwise set `tesseract_dir` in `config/settings.yaml`. Settings shows whether it was found.
- OCR runs locally, only on picture pages, at about 10 seconds per page. The words are saved in the data folder (`ocr/<pdf fingerprint>.json`), so each PDF is read once and a stopped run continues.
- Every figure read by OCR carries a warning: check each digit against the page (the figure is boxed on the page image) and tick the warning before approving.
- Read the picture pages before **Save report**, so their figures are part of the saved report.
- **OCR after saving.** A saved report's **Report tools** menu shows which picture pages were read with OCR and can read the rest; the review screen then offers **Add them to the saved report** for the figures found.

## Exporting to Excel

There is **one workbook per fiscal year**, holding every bank saved for that year: `approved_2025-26.xlsx`, `approved_2026-27.xlsx`, and so on. Press **Export 2025/26 to Excel (all banks)** under the metric table of a saved report, or pick the year under **Export** on **Reports**. The file is written to the `exports` folder in the data folder and offered for download; exporting the same year again replaces it with an up-to-date copy (close it in Excel first). A year typed as `2026` counts as `2025/26` (July 2025 to June 2026). It contains only approved values:

| Sheet | Contents |
|---|---|
| Approved Data | One row per bank report, sorted by bank, in the exact column layout of the Shega bank timeseries spreadsheet (`bank`, `fiscal_year`, `Total Revenue`, … `Customers per Branch`; see `config/export_template.yaml`). Blank = no value, not zero. Extracted columns hold approved figures; calculated columns hold the analyst's entries. ETB in full Birr; percentages as fractions (0.29 = 29%) |
| Source Evidence | One row per value: bank, year, spreadsheet column, PDF page, printed figure and label, extracted / corrected / entered manually, sign choice, who approved it and when, PDF file and hash |
| Export Metadata | Fiscal year, export time, exported by, extractor version, what was excluded, warnings (e.g. a bank saved twice for the year) |

Rejected figures, corrections not yet approved and unreviewed metrics are never exported.

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
