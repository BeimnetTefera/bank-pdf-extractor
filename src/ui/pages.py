"""Smaller screens: Getting started, Reports (the home), Templates (metric configuration) and Settings."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from html import escape

import streamlit as st

from src import bulk_review, ocr, report_corrections, report_merge, review_service
from src.analysis import report_end_year
from src.config_loader import CONFIG_DIR, AppConfig, bank_key
from src.database import connect
from src.evidence import format_value
from src.excel_exporter import fiscal_year_label, report_years
from src.mapping_editor import KINDS, MappingEditError, add_wording, list_wording, remove_wording
from src.project_service import list_documents, recent_decisions
from src.ui import routes, style
from src.ui.export_panel import export_button

TESTER_PROMPT = CONFIG_DIR.parent / "docs" / "tester-prompt.md"  # shown on Home for the Claude browser extension

WORKFLOW = [
    ("Load a report", "Enter the bank and report year, then upload the annual report PDF.", "Available"),
    ("Check figures against the PDF", "Click a figure to see it boxed on its source page.", "Available"),
    ("Match figures to metrics", "Link figures to the Shega metrics, read units, flag conflicts.", "Available"),
    ("Approve, reject or correct", "Save the report, then every decision is saved with your name and time.", "Available"),
    ("Finish extraction", "Lock the approved figures when the review is done.", "Available"),
    ("Enter calculated columns", "On the Calculations page, the analyst works out ratios and calculated "
                                 "values from the approved figures and enters them with the formula.", "Available"),
    ("Export to Excel", "One workbook per year in the Shega spreadsheet layout, with source evidence.", "Available"),
]


def home() -> None:
    st.markdown("## Bank PDF Extractor")
    st.caption("Extract figures from bank annual reports, check them against the source page, and export approved values.")
    for number, (title, text, status) in enumerate(WORKFLOW, start=1):
        with st.container(border=True):
            title_col, status_col = st.columns([5, 1], vertical_alignment="center")
            title_col.markdown(f"**{number}. {title}**  \n{text}")
            with status_col:
                if status == "Available":
                    st.badge(status, color="green", icon=":material/check:")
                else:
                    st.badge(status, color="gray")
    st.caption("Start from **Extract & Review** in the menu.")
    with st.expander("Let Claude do a first review for you", icon=":material/smart_toy:"):
        st.markdown("Copy this prompt (button at the top right of the box) into the Claude browser extension "
                    "while this app is open, and replace `<MY NAME>`. Claude records each decision as "
                    "*Claude for your name*; check its work before you finish the report.")
        st.code(TESTER_PROMPT.read_text(encoding="utf-8"), language="markdown", wrap_lines=True)


REPORT_COLUMNS = [2.9, 1.0, 1.8, 1.5, 1.6, 1.0, 0.6]  # bank, year, progress, work left, status, open, more


def bank_reports(config: AppConfig, open_report: Callable[[str], str | None],
                 new_report: Callable[[], None] | None = None) -> None:
    """Reports, the home of the redesign (D-090): what is saved, how far each review is, what is left."""
    with closing(connect(config.settings.database_path)) as conn:
        documents = list_documents(conn)
        progress = {d["id"]: len(review_service.approved_results(conn, d["id"])) for d in documents}
        finished = {d["id"]: review_service.extraction_status(conn, d["id"]).finished for d in documents}
        work = {d["id"]: bulk_review.outstanding_work(conn, d["id"], config) for d in documents}
        years = report_years(conn)
        activity = recent_decisions(conn, tuple(ACTIVITY_VERBS))
    _reports_header(config, years, new_report)
    if not documents:
        st.info("No reports saved yet. Press **New report**, upload a PDF and press Save report.",
                icon=":material/inbox:")
        return
    totals = {d["id"]: len(json.loads(d["metric_ids"])) for d in documents}
    copies = Counter(d["sha256"] for d in documents)
    ready = [d for d in documents if finished[d["id"]]]
    waiting = [d for d in documents if work[d["id"]].count]
    approved, total = sum(progress.values()), sum(totals.values())
    style.stat_strip([
        ("Saved reports", str(len(documents)),
         f"{len({bank_key(d['bank_name']) for d in documents})} bank(s) · {len(years)} fiscal year(s)"),
        ("Figures approved", f"{approved} / {total}", f"{round(100 * approved / total) if total else 0}% of all metrics"),
        ("Waiting for review", str(sum(work[d["id"]].count for d in documents)), f"across {len(waiting)} report(s)"),
        ("Ready for calculations", str(len(ready)),
         ", ".join(f"{d['bank_name']} {_year(d)}" for d in ready) or "none yet"),
    ])
    _fix_names_and_years(config)
    _duplicates(config)
    table_col, feed_col = st.columns([3.6, 1], gap="medium")
    with table_col, st.container(key="panel_reports"):
        with st.container(key="reports_head"):
            head = st.columns(REPORT_COLUMNS, vertical_alignment="center")
            for col, title in zip(head, ("Bank", "Year", "Progress", "Work left", "Status")):
                col.markdown(title)
        for d in documents:
            _report_row(config, d, progress[d["id"]], totals[d["id"]], finished[d["id"]], copies[d["sha256"]] > 1,
                        work[d["id"]], open_report)
    with feed_col, st.container(key="panel_activity"):
        _activity(config, activity)
    _deletion_log(config)


def _reports_header(config: AppConfig, years: list[int], new_report: Callable[[], None] | None) -> None:
    title_col, export_col, new_col = st.columns([6, 1.1, 1.3], vertical_alignment="bottom")
    title_col.markdown("## Reports")
    title_col.caption(f"Annual reports saved for review, newest first · stored in {config.settings.data_dir}")
    if years:  # every saved report has a recognisable year (Save requires one)
        with export_col.popover("Export", icon=":material/download:", width="stretch"):
            st.markdown("**Export to Excel** · one workbook per fiscal year, with every bank saved for that year")
            end_year = st.selectbox("Fiscal year", years, format_func=fiscal_year_label, key="export_year")
            export_button(config, end_year, key=f"export_{end_year}")
    if new_report is not None:
        new_col.button("New report", icon=":material/add:", type="primary", width="stretch", on_click=new_report)


def _report_row(config: AppConfig, d, approved: int, total: int, finished: bool, duplicate: bool,
                work: bulk_review.OutstandingWork, open_report: Callable[[str], str | None]) -> None:
    with st.container(key=f"report_row_{d['id']}"):
        bank, year, progress, left, status, open_col, more_col = st.columns(REPORT_COLUMNS, vertical_alignment="center")
        bank.markdown(f"**{d['bank_name']}**")
        bank.html(f'<div class="bpe-file">{escape(d["original_filename"])} · {d["page_count"]} pages</div>')
        year.html(f'<span class="bpe-num">{escape(_year(d))}</span>')
        progress.progress(approved / total if total else 0.0, text=f"{approved} of {total} approved")
        left.caption(_work_text(config, work))
        if finished:
            status.badge("Finished", color="green", icon=":material/task_alt:")
        elif duplicate:
            status.badge("Duplicate", color="gray", icon=":material/content_copy:")
        else:
            status.badge("In review", color="orange")
        if open_col.button("Open", key=f"open_{d['id']}", type="primary", width="stretch"):
            error = open_report(d["id"])
            if error:
                st.error(error, icon=":material/error:")
        with more_col:
            _delete_report(config, d, approved)
        _work_left(config, d["id"], work, open_report)


def _work_text(config: AppConfig, work: bulk_review.OutstandingWork) -> str:
    """Work left in a few words: the metric names when there are one or two."""
    if not work.count:
        return "None"
    names = {m.id: m.display_name for m in config.metrics}
    metric_ids = [*work.corrections, *work.calculations, *work.not_reviewed]
    return ", ".join(names.get(m, m) for m in metric_ids) if work.count <= 2 else f"{work.count} metrics"


def _activity(config: AppConfig, decisions) -> None:
    st.markdown("**Recent activity**")
    if not decisions:
        st.caption("No review decisions yet.")
        return
    names = {m.id: m.display_name for m in config.metrics}
    events = []
    for r in decisions:
        what = ACTIVITY_VERBS.get(r["decision"], r["decision"].replace("_", " "))
        value = (f" · {format_value(Decimal(r['reviewed_value']), r['reviewed_unit'])}"
                 if r["reviewed_value"] is not None and r["decision"] == "approved" else "")
        events.append(
            f'<div class="bpe-event"><span class="bpe-avatar">{escape(style.initials(r["reviewer"]))}</span><span>'
            f'<b>{escape(r["reviewer"])}</b> {what} <b>{escape(names.get(r["metric_id"], r["metric_id"]))}</b>'
            f'{escape(value)}<br><span class="bpe-when">{escape(r["bank_name"])} {escape(_year(r))} · '
            f'{escape(_short_time(r["created_at"]))}</span></span></div>')
    st.html("".join(events))


ACTIVITY_VERBS = {"approved": "approved", "rejected": "rejected", "reopened": "reopened"}


def _short_time(iso: str) -> str:
    """'2026-10-26T07:52:03+00:00' -> '26 Oct, 07:52 UTC'."""
    when = datetime.fromisoformat(iso)
    return f"{when.day} {when:%b}, {when:%H:%M} UTC"


def _year(d) -> str:
    end_year = report_end_year(d["reporting_period"])
    return fiscal_year_label(end_year) if end_year else d["reporting_period"]


def _reviewer() -> str:
    return st.session_state.get("reviewer", "")


def _fix_names_and_years(config: AppConfig) -> None:
    """Saved bank names and years not in their one form (D-084, D-085): each fix is applied, and
    logged, only when the reviewer presses Apply."""
    with closing(connect(config.settings.database_path)) as conn:
        found = report_corrections.proposed_fixes(conn, config.banks)
    if not found:
        return
    count = len(found.fixes) + len(found.unmatched_banks) + len(found.unreadable_years)
    with st.expander(f"Fix names and years · {count} to check", icon=":material/edit_note:"):
        st.caption("Each fix is logged with your name; nothing changes until you press Apply.")
        if found.fixes:
            for fix in found.fixes:
                st.markdown(f"{report_corrections.FIELDS[fix.field]} of {fix.label}: "
                            f"“{fix.old_value}” → **“{fix.new_value}”**")
            if st.button(f"Apply {len(found.fixes)} fix(es)", key="apply_fixes", type="primary"):
                _run_fix(config, lambda conn: report_corrections.apply_fixes(
                    conn, found.fixes, _reviewer(), config.banks), "Names and years fixed.")
        for d in found.unmatched_banks:
            name_col, pick_col, apply_col = st.columns([3, 3, 1], vertical_alignment="bottom")
            name_col.markdown(f"Bank “{d['bank_name']}” is not in the bank list ({report_corrections.report_label(d)})")
            bank = pick_col.selectbox("Choose the bank", config.banks, index=None, key=f"fix_bank_{d['id']}",
                                      placeholder="Choose the bank")
            if apply_col.button("Apply", key=f"apply_bank_{d['id']}", disabled=bank is None, width="stretch"):
                _run_fix(config, lambda conn: report_corrections.correct_report(
                    conn, d["id"], "bank_name", bank, _reviewer(), report_corrections.CHOSEN_BANK_REASON,
                    config.banks), f"Bank set to {bank}.")
        if found.unreadable_years:
            st.markdown("Years that cannot be read as a fiscal year: choose the right one.")
            choices = [fiscal_year_label(y) for y in range(utc_year() + 1, 2000, -1)]
            for d in found.unreadable_years:
                name_col, pick_col, apply_col = st.columns([3, 3, 1], vertical_alignment="bottom")
                name_col.markdown(f"Year “{d['reporting_period']}” · {report_corrections.report_label(d)}")
                year = pick_col.selectbox("Choose the year", choices, index=None, key=f"fix_year_{d['id']}",
                                          placeholder="Choose the year")
                if apply_col.button("Apply", key=f"apply_year_{d['id']}", disabled=year is None, width="stretch"):
                    _run_fix(config, lambda conn: report_corrections.correct_report(
                        conn, d["id"], "reporting_period", year, _reviewer(), report_corrections.CHOSEN_YEAR_REASON,
                        config.banks), f"Year set to {year}.")


def utc_year() -> int:
    return datetime.now(timezone.utc).year


def _run_fix(config: AppConfig, action: Callable, done: str) -> None:
    try:
        with closing(connect(config.settings.database_path)) as conn:
            action(conn)
    except review_service.ReviewError as exc:
        st.error(str(exc), icon=":material/error:")
        return
    st.toast(done, icon=":material/check:")
    st.rerun()


def _duplicates(config: AppConfig) -> None:
    """One banner per PDF saved more than once, with Merge (D-086). Nothing changes until Merge is pressed."""
    with closing(connect(config.settings.database_path)) as conn:
        groups = report_merge.duplicate_groups(conn)
        for group in groups:
            _duplicate_group(config, conn, group)


def _duplicate_group(config: AppConfig, conn, group: report_merge.DuplicateGroup) -> None:
    by_id = {d["id"]: d for d in group.documents}
    names = {m.id: m.display_name for m in config.metrics}

    def label(document_id: str) -> str:
        d = by_id[document_id]
        return (f"{d['bank_name']} · {d['reporting_period']} · saved {d['uploaded_at'][:16].replace('T', ' ')} UTC · "
                f"{group.decision_counts[document_id]} decision(s) · report {document_id}")

    def listed(metric_ids: list[str]) -> str:
        return ", ".join(names.get(m, m) for m in metric_ids)

    ids = [d["id"] for d in group.documents]
    st.warning(f"**Same PDF saved {len(ids)} times** · {group.documents[0]['original_filename']}. Merging keeps "
               "the copy with more decisions.", icon=":material/content_copy:")
    with st.expander("Review duplicates", icon=":material/merge:"):
        keep = st.radio("Copy to keep", ids, index=ids.index(group.default_keep), format_func=label,
                        key=f"merge_keep_{group.sha256}")
        others = [i for i in ids if i != keep]
        other = others[0] if len(others) == 1 else st.selectbox(
            "Copy to merge into it", others, format_func=label, key=f"merge_other_{group.sha256}")
        plan = report_merge.plan_merge(conn, keep, other)
        if plan.copy_metrics:
            st.markdown(f"Will copy {plan.decisions_to_copy} decision(s) for: {listed(plan.copy_metrics)}.")
        else:
            st.markdown("No decisions to copy.")
        if plan.same_metrics:
            st.caption(f"Already decided the same in both copies: {listed(plan.same_metrics)}.")
        if plan.conflicts:
            st.markdown(f":orange[Decided differently in both copies, not copied: {listed(plan.conflicts)}.]")
        if plan.missing_figures:
            st.markdown(f":orange[The decided figure is not in the copy to keep, not copied: "
                        f"{listed(plan.missing_figures)}.]")
        for refusal in plan.refusals:
            st.error(refusal, icon=":material/block:")
        st.caption(f"Afterwards report {other} is deleted (logged, reason “Merged into report {keep}”). "
                   "It cannot be undone.")
        if st.button("Merge", key=f"merge_{group.sha256}", type="primary", disabled=not plan.allowed):
            try:
                result = report_merge.merge_reports(conn, keep, other, _reviewer(), config.settings.data_dir)
            except review_service.ReviewError as exc:
                st.error(str(exc), icon=":material/error:")
                return
            st.toast(f"Merged: {result.decisions_copied} decision(s) copied, {result.deleted.label} deleted.",
                     icon=":material/merge:")
            st.rerun()


WORK_GROUPS = (("corrections", "Corrections awaiting approval", ":material/edit_note:"),
               ("calculations", "Calculations awaiting approval", ":material/calculate:"),
               ("not_reviewed", "Not reviewed yet", ":material/pending:"))


def _work_left(config: AppConfig, document_id: str, work: bulk_review.OutstandingWork,
               open_report: Callable[[str], str | None]) -> None:
    """What still needs a reviewer on this report, each metric with a button that opens it."""
    if not work.count:
        st.caption(":material/task_alt: Nothing left to review on this report.")
        return
    names = {m.id: m.display_name for m in config.metrics}
    with st.expander(f"Work left ({work.count})", icon=":material/checklist:"):
        for field, title, icon in WORK_GROUPS:
            metric_ids = getattr(work, field)
            if not metric_ids:
                continue
            st.markdown(f"{icon} **{title} ({len(metric_ids)})**")
            for metric_id in metric_ids:
                name_col, open_col = st.columns([4, 1], vertical_alignment="center")
                name_col.markdown(names.get(metric_id, metric_id))
                if open_col.button("Open", key=f"open_{document_id}_{field}_{metric_id}", width="stretch"):
                    st.session_state.open_metric = metric_id  # selected once the report is open
                    error = open_report(document_id)
                    if error:
                        st.session_state.pop("open_metric", None)
                        st.error(error, icon=":material/error:")


def _delete_report(config: AppConfig, d, approved: int) -> None:
    """Delete one saved report, after a reason and an explicit confirmation (decision D-049)."""
    with st.popover("", icon=":material/more_horiz:", help="Evidence summary, delete", width="stretch"):
        routes.link("evidence_summary", "Evidence summary", ":material/table_view:", report=d["id"])
        st.divider()
        st.markdown(f"**Delete {d['bank_name']} · {d['reporting_period']}?**")
        st.markdown(f"This permanently removes its figures, checks, every review decision ({approved} approved "
                    "value(s)) and calculated values, and the stored PDF unless another saved copy uses it. "
                    "It cannot be undone. The deletion itself is logged with your name and reason.")
        reason = st.text_input("Reason", key=f"delete_reason_{d['id']}", placeholder="e.g. demo upload, saved twice")
        sure = st.checkbox("I understand this cannot be undone", key=f"delete_sure_{d['id']}")
        if st.button("Delete permanently", type="primary", key=f"delete_{d['id']}", disabled=not sure, width="stretch"):
            try:
                with closing(connect(config.settings.database_path)) as conn:
                    deleted = review_service.delete_report(conn, d["id"], st.session_state.get("reviewer", ""),
                                                           reason, config.settings.data_dir)
            except review_service.ReviewError as exc:
                st.error(str(exc), icon=":material/error:")
                return
            doc = st.session_state.get("doc")
            if doc and doc.get("document_id") == d["id"]:  # it was open in Extract & Review
                for key in ("doc", "selected_key", "selected_metric", "detail_mode", "marked", "calc_document"):
                    st.session_state.pop(key, None)
            st.toast(f"Deleted {deleted.label}. Export its year again to update the Excel file.",
                     icon=":material/delete:")
            st.rerun()


def _deletion_log(config: AppConfig) -> None:
    with closing(connect(config.settings.database_path)) as conn:
        rows = review_service.deletion_log(conn)
    if not rows:
        return
    with st.expander(f"Deleted reports ({len(rows)})", icon=":material/history:"):
        st.dataframe(
            [{"Report": f"{r['bank_name']} · {r['reporting_period']}", "File": r["original_filename"],
              "Approved values": r["approved_values"], "Reason": r["reason"], "Deleted by": r["reviewer"],
              "When (UTC)": r["deleted_at"][:16].replace("T", " ")} for r in rows],
            hide_index=True, width="stretch")


def templates(config: AppConfig) -> None:
    st.markdown("## Templates")
    template = config.export_template
    metrics_tab, wording_tab, layout_tab = st.tabs([f"Metrics ({len(config.metrics)})", "Wording",
                                                    f"Excel layout ({len(template.columns)} columns)"])
    with wording_tab:
        wording_editor(config)
    with metrics_tab:
        st.caption(
            f"Metrics from config/metrics.yaml: {len(config.active_metrics())} active of {len(config.metrics)}. "
            "Edit the YAML file to change them."
        )
        st.dataframe(
            [
                {
                    "Metric": m.display_name,
                    "Excel column": m.export_column or "— (Source Evidence only)",
                    "Pipeline column": m.shega_column or "—",
                    "Unit": m.expected_unit,
                    "Active": m.active,
                    "Definition": m.definition,
                    "Notes": m.notes or "",
                }
                for m in config.metrics
            ],
            hide_index=True,
            width="stretch",
            height=700,
        )
    with layout_tab:
        rows = excel_layout(config)
        st.caption(
            f"The Approved Data sheet of the Excel export, column by column, from config/export_template.yaml "
            f"(the Shega bank timeseries spreadsheet): {len(template.extracted())} extracted columns, reviewed in "
            f"Extract & Review, and {len(template.calculated)} calculated columns, entered by an analyst on the "
            "Calculations page after the extraction review is finished."
        )
        group = st.segmented_control("Show", ["All", "Extracted", "Calculated"], default="All", key="layout_group")
        shown = [r for r in rows if group in (None, "All") or r["Group"] == group]
        st.dataframe(shown, hide_index=True, width="stretch", height=700,
                     column_config={"#": st.column_config.NumberColumn(width=40)})


def wording_editor(config: AppConfig) -> None:
    """List, add and remove the wording of one metric in config/mappings.yaml (D-065)."""
    path = (config.config_dir or CONFIG_DIR) / "mappings.yaml"
    st.caption("How printed wording is linked to each metric (config/mappings.yaml, used for every bank). "
               "Table labels must match a statement line exactly; sentence wording sits right after or shortly "
               "before a number. Changes apply the next time a report is analysed; comments in the file are kept.")
    if message := st.session_state.pop("wording_message", None):
        st.success(message, icon=":material/check:")
    names = {m.id: m.display_name for m in config.metrics}
    metric_id = st.selectbox("Metric", [m.id for m in config.metrics], format_func=lambda i: names[i],
                             key="wording_metric")
    items = [w for w in list_wording(path) if w.metric_id == metric_id]
    learned = [a for a in config.learned_aliases if a.metric_id == metric_id]
    if items:
        st.dataframe([{"Kind": KINDS[w.kind], "Wording": w.text} for w in items], hide_index=True, width="stretch")
    else:
        st.info("No wording yet: this metric is only found when a reviewer marks it on the page.",
                icon=":material/info:")
    if learned:
        st.caption("Learned from reviews (config/learned_aliases.yaml, per bank): "
                   + "; ".join(f"“{a.alias}” ({a.bank or 'every bank'})" for a in learned))
    add_col, remove_col = st.columns(2)
    with add_col, st.form("add_wording", clear_on_submit=True, border=True):
        st.markdown("**Add wording**")
        kind = st.selectbox("Kind", list(KINDS), format_func=KINDS.get, key="wording_kind")
        text = st.text_input("Wording, as printed", placeholder="e.g. Deposits from customers")
        note = st.text_input("Where it was seen", placeholder="e.g. Zemen 2025/26 p22")
        if st.form_submit_button("Add", icon=":material/add:", type="primary"):
            _edit_wording(lambda: add_wording(path, metric_id, kind, text, _with_reviewer(note), set(names)),
                          f"Added “{' '.join(text.split())}” to {names[metric_id]}.")
    with remove_col, st.container(border=True):
        st.markdown("**Remove wording**")
        choice = st.selectbox("Wording", items, format_func=lambda w: f"{KINDS[w.kind]}: {w.text}",
                              index=None, placeholder="Choose wording to remove", key="wording_remove")
        if st.button("Remove", icon=":material/delete:", disabled=choice is None, key="remove_wording"):
            _edit_wording(lambda: remove_wording(path, metric_id, choice.kind, choice.text, set(names)),
                          f"Removed “{choice.text}” from {names[metric_id]}.")


def _with_reviewer(note: str) -> str:
    reviewer = (st.session_state.get("reviewer") or "").strip()
    return "; ".join(part for part in (note.strip(), f"added by {reviewer}" if reviewer else "") if part)


def _edit_wording(change: Callable[[], None], done: str) -> None:
    try:
        change()
    except MappingEditError as exc:
        st.error(str(exc), icon=":material/error:")
        return
    st.session_state.wording_message = done
    st.rerun()


def excel_layout(config: AppConfig) -> list[dict]:
    """One row per Excel column: extracted or calculated, and what fills it."""
    template = config.export_template
    by_column = {m.export_column: m for m in config.metrics if m.export_column}
    rows = []
    for number, column in enumerate(template.columns, start=1):
        metric = by_column.get(column)
        if column == template.bank_column:
            group, filled_by, status = "Report", "Bank chosen when saving", "Always filled"
        elif column == template.year_column:
            group, filled_by, status = "Report", "Report year, as 2025/26", "Always filled"
        elif column in template.calculated:
            group, filled_by, status = "Calculated", "Data analyst", "Entered on the Calculations page"
        elif metric is not None and metric.active:
            group, filled_by, status = "Extracted", metric.display_name, "Filled when approved"
        elif metric is not None:
            group, filled_by, status = "Extracted", metric.display_name, "Blank: metric is inactive"
        else:
            group, filled_by, status = "Extracted", "", "Blank: no metric for this column yet"
        rows.append({"#": number, "Excel column": column, "Group": group, "Filled by": filled_by, "Status": status})
    return rows


def settings(config: AppConfig) -> None:
    s = config.settings
    st.markdown("## Settings")
    st.caption("From config/settings.yaml. Edit the file and reload to change them.")
    with st.container(border=True):
        st.markdown(f"**Data folder**  \n`{s.data_dir}`")
        st.markdown(f"**Upload limit**  \n{s.max_upload_mb} MB, {s.max_pages} pages")
        st.markdown(f"**Reviewer name**  \n{s.default_reviewer}")
        per_bank: dict[str, int] = {}
        for a in config.learned_aliases:
            per_bank[a.bank or "every bank"] = per_bank.get(a.bank or "every bank", 0) + 1
        learned = ", ".join(f"{bank}: {n}" for bank, n in sorted(per_bank.items())) or "none yet"
        st.markdown(f"**Label aliases configured**  \n{len(config.mappings)} metric entries in mappings.yaml "
                    f"(every bank). Labels learned from reviews, per bank (config/{config.learned_aliases_path.name}): "
                    f"{learned}")
        st.markdown(f"**Cross-checks**  \n{len(config.checks)} sum check(s) from config/checks.yaml, plus "
                    "the prior-year comparison with last year's approved figures")
        engine = ocr.find_engine(s.tesseract_dir)
        if engine is None:
            st.markdown("**OCR (for picture pages)**  \nTesseract not found. Install it, or set `tesseract_dir`.")
        else:
            st.markdown(f"**OCR (for picture pages)**  \n{engine.version} · `{engine.executable.parent}` · runs "
                        "on this computer")
