# Redesign backlog: tools to add back

**Status:** parked on 2026-10-28, when the screens were rebuilt to match the redesign mockup
exactly (canvas *Bank Extractor Redesign*). Each tool below still works in the code and the
data it uses is kept; it only has no place on the redesigned screens yet. We add them back
together once the redesign is finished.

| Tool | What it does | Where it was | Code that still has it |
|---|---|---|---|
| Delete a report | Permanently removes a saved report (figures, decisions, calculated values, stored PDF unless another copy uses it), after a reason and a tick; the deletion is logged | Bin / ⋯ menu on each report row on Bank Reports | `pages._delete_report`, `review_service.delete_report` (D-049) |
| Deleted reports log | Lists every deleted report with its reason, who and when | Expander at the bottom of Bank Reports | `pages._deletion_log`, `review_service.deletion_log` |
| Fix names and years | Proposes the one form of a bank name and of a fiscal year (YYYY/YY) for reports saved with another spelling; applied and logged only when pressed | Panel at the top of Bank Reports | `pages._fix_names_and_years`, `report_corrections` (D-084, D-085) |
| Evidence summary | Every metric of a report with its evidence, as a table, a CSV and an evidence PDF to hand to someone else | Link on each report on Bank Reports | `evidence_page.render_summary` (page `evidence_summary`, still reachable by its address) |
| Work left, per metric | Lists the corrections and calculations awaiting approval and the metrics not reviewed yet, each with an Open button | Expander on each report on Bank Reports | `pages._work_left`, `bulk_review.outstanding_work` (D-077) |
| Getting started | The workflow steps and the prompt testers copy into the Claude browser extension | Home page (later *Getting started*) | `pages.home`, `docs/tester-prompt.md` |
| Recent activity | The latest approvals, rejections and reopenings across all reports | Right column of Reports (stage 1) | `project_service.recent_decisions` (the query; the panel was removed) |
| Approved figures beside the calculations | The report's approved figures (and last year's) listed next to the calculated columns | Left half of Calculations | `calculations._approved_figures` |
| History of calculated entries | Every value entered or cleared for a calculated column, with who and when | Expander under Calculations | `calculations._history`, `calculation_service.history` |
| Sector "Not reported" | Records that no sector figure exists for a year, with a reason | Popover on Sector data | `sector_data.record_not_reported` |
| Note on a sector value | A free note saved with a sector value | Sector data form | `sector_data.record_value(..., note)` |
| Evidence: checks, other candidates, definition | The value's checks, the other figures found and why they were not used, the metric's definition | Evidence page | `evidence_page._checks_block`, `_candidates_block` |
| Wording editor | Add or remove the wording (table labels, words around a number) that finds a metric, in config/mappings.yaml | Wording tab of Templates | `pages.wording_editor`, `mapping_editor` (D-065) |
| Excel layout | The Approved Data sheet column by column, extracted or calculated | Excel layout tab of Templates | `pages.excel_layout` |
| New figures found after a wording change | Offers to add figures the current wording finds to a saved report | Top of Extract (still there) and not on Review | `extract_review._new_figures_prompt` |

Merging duplicate reports is not parked: the mockup's **Review duplicates** button on Reports
opens it.
