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
| Full Extract workbench | The earlier Extract screen: metric table, All Figures (every number read, with filters), Validation Log, Approve all verified, and a figure's details | Extract page before D-094 | `extract_review.render`, opened when `st.session_state.workbench` is set (the app tests use it) |
| All figures list | Every number read from the PDF, filtered by page, year or value, with *Use this figure for a metric* | All Figures tab of the old Extract | `extract_review._figures_table`, `_figure_details` (the top search finds printed figures meanwhile) |
| Approve all verified | Approves in one go the suggested figures with no warning, converted and positive, after a tick | Old Extract, under the metric table | `extract_review._approve_all_verified`, `bulk_review` (D-072) |
| Full validation log | Every finding of the analysis, notes included, as one list | Validation Log tab of the old Extract | `metric_view.validation_log` (Extract now lists one status per metric, D-094) |
| "Opened for" label | Named the metric whose figure the Extract page opened at | Extract toolbar, left of the search box (dropped with the Extract mockup of 2026-10-28) | `extract_page._toolbar` before 2026-10-28 (git history) |
| Actions under an approved figure | Open evidence record, Recalculate, Reopen, and ‹ Previous / Next: metric (Alt+↑ / Alt+↓) | Review, right column, under an approved figure (dropped with the Review mockup of 2026-10-28); still shown for a metric to review | `review_panel._reopen`, `calc_panel.calculate_section`, `review_page._steps`; the Evidence page is still reachable by its address |
| Warnings filter | Lists only the metrics whose figure raised a warning | Review queue filters | `review_page` (status `flagged`); such metrics are under *To review* |
| Value in the queue | The approved or suggested value beside each metric in the Review queue | Review queue | `review_page._queue_value` before 2026-10-28 (git history) |
| Box legend | "Red: the suggestion you are checking · thin blue: figures already found on this page" | Under the page on Review | removed; recorded figures are now boxed in plain blue without names, as in the mockup; a suggestion being checked is still red |

Merging duplicate reports is not parked: the mockup's **Review duplicates** button on Reports
opens it.
