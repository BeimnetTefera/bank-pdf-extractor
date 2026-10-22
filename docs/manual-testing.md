# Manual Testing

Real reports are tested locally and never committed. Results below are observations, not
accuracy claims; extraction accuracy is measured in Phase 7 against manually verified values.

## Phase 2: one-PDF extraction check

### Procedure

From the project folder, with the virtual environment active (Git Bash):

```bash
source .venv/Scripts/activate
python -m scripts.inspect_pdf "/c/path/to/report.pdf"                    # summary
python -m scripts.inspect_pdf "/c/path/to/report.pdf" --find "total assets"
python -m scripts.inspect_pdf "/c/path/to/report.pdf" --page 111          # 1-based PDF page
python -m scripts.inspect_pdf "/c/path/to/report.pdf" --csv               # all rows to Excel
```

Check:
1. Page count matches the PDF viewer.
2. Pages reported as having no text are really images/scans (open them in a viewer).
3. For a few statement rows, the label and values match the PDF page shown.
4. The PDF page number shown (1-based) opens the right page in a PDF viewer.
5. The output says the results are unverified.

The CSV is written to the app data `exports` folder (outside the project), never to Git.

### Observations: Bank of Abyssinia Annual Report 2025-26 (2026-10-22)

| Check | Result |
|---|---|
| Validation | Accepted; 54.0 MB, 234 pages, unencrypted |
| Run time | ~7.5 s for the full report |
| Text availability | Text-based. PDF pages 9 and 34 flagged as low text; both are full-page images with a short caption |
| Raw candidates | 3,585 |
| Statement tables | Statement of financial position (PDF page 111) rebuilt correctly row by row: label, note number, current-year and prior-year values on one row. PyMuPDF's plain reading order for the same page is scrambled (all labels, then all numbers), so the word-position row grouping is required |
| Repeated figures | Profit before tax appears on the income statement and in the notes; the prior-year figures differ by 1 between the two pages. Both are kept as separate candidates |

Findings that affect later phases:

1. **Narrative / infographic figures were missed** by the table method: operational metrics (branches, ATMs, POS terminals, employees, ROE) appear in sentences with the number before the label (e.g. "... 981 branches, 1,761 ATMs ..."). Resolved by the narrative method (D-021); see below.
2. **Two-column narrative pages** can merge text from both columns into one row, producing misleading label/value pairs (e.g. PDF page 39). Reviewers must check context.
3. **Note-reference column.** Statement rows carry a note number (e.g. `16`) as the first value; the header row (`Notes  30 June 2026  30 June 2025`) is captured as its own row so Phase 3 can identify columns.
4. **Unit heading glyph.** `Birr'000` is extracted as `Birr�000` (the apostrophe does not map to a character). Unit detection must accept this form.
5. **EPS printed as a percentage** (`77%`), whereas `eps_etb` expects ETB. Business question.
6. **Printed page numbers** are not detected yet; only the PDF page index is reported.

### Narrative method added (extractor 0.2, 2026-10-22)

Re-run on the same BOA report: 4,585 raw candidates (3,587 table rows, 998 narrative).
Branches, ATMs, POS terminals, employees and ROE, which the table method missed, are now
captured with their surrounding words (PDF pages 26, 44, 49, 62).

Notes for Phase 3:

1. **Use the nearest words, not the whole window.** In "981 branches, 1,761 ATMs, 6,045 PoS
   terminals", the words after `981` include "ATMs" and "PoS". Matching must prefer the words
   immediately next to the number, or a list like this maps every number to every metric.
2. **One printed number can be caught by both methods** when a prose line ends with a number.
   Candidates with the same page and position are the same printed figure and must not be
   counted as independent confirmations.
3. **Volume.** About 1,000 narrative candidates per report; nearly all will stay unmapped.
   Only candidates near a configured alias should reach the reviewer.
