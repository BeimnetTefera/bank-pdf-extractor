"""Smaller screens: Home, Bank Reports, Templates (metric configuration) and Settings."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import closing

import streamlit as st

from src import bulk_review, ocr, review_service
from src.config_loader import CONFIG_DIR, AppConfig
from src.database import connect
from src.excel_exporter import fiscal_year_label, report_years
from src.mapping_editor import KINDS, MappingEditError, add_wording, list_wording, remove_wording
from src.project_service import list_documents
from src.ui import routes
from src.ui.export_panel import export_button

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


def bank_reports(config: AppConfig, open_report: Callable[[str], str | None]) -> None:
    st.markdown("## Bank Reports")
    st.caption(f"Reports saved on this computer, in {config.settings.data_dir}.")
    with closing(connect(config.settings.database_path)) as conn:
        documents = list_documents(conn)
        progress = {d["id"]: len(review_service.approved_results(conn, d["id"])) for d in documents}
        finished = {d["id"]: review_service.extraction_status(conn, d["id"]).finished for d in documents}
        work = {d["id"]: bulk_review.outstanding_work(conn, d["id"], config) for d in documents}
        years = report_years(conn)
    if not documents:
        st.info("No reports saved yet. Upload a PDF in Extract & Review and press Save report.",
                icon=":material/inbox:")
        return
    if years:  # every saved report has a recognisable year (Save requires one)
        with st.container(border=True):
            st.markdown("**Export to Excel** · one workbook per fiscal year, with every bank saved for that year")
            year_col, button_col = st.columns([1, 2])
            end_year = year_col.selectbox("Fiscal year", years, format_func=fiscal_year_label, key="export_year",
                                          label_visibility="collapsed")
            with button_col:
                export_button(config, end_year, key=f"export_{end_year}")
    for d in documents:
        total = len(json.loads(d["metric_ids"]))
        with st.container(border=True):
            info_col, progress_col, open_col, delete_col = st.columns([5, 3, 1, 1], vertical_alignment="center")
            step = ":green[Extraction finished · ready for calculations]" if finished[d["id"]] else "In review"
            info_col.markdown(
                f"**{d['bank_name']} · {d['reporting_period']}** · {step}  \n"
                f"{d['original_filename']} · {d['page_count']} pages · saved {d['uploaded_at'][:16].replace('T', ' ')} UTC"
            )
            progress_col.progress(progress[d["id"]] / total if total else 0.0,
                                  text=f"{progress[d['id']]} of {total} metrics approved")
            if open_col.button("Open", key=f"open_{d['id']}", type="primary", width="stretch"):
                error = open_report(d["id"])
                if error:
                    st.error(error, icon=":material/error:")
            with info_col:
                routes.link("evidence_summary", "Evidence summary", ":material/table_view:", report=d["id"])
            with delete_col:
                _delete_report(config, d, progress[d["id"]])
            _work_left(config, d["id"], work[d["id"]], open_report)
    _deletion_log(config)


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
    with st.popover("", icon=":material/delete:", help="Delete this saved report", width="stretch"):
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
