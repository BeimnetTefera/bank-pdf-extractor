"""Calculations page: once a report's extraction review is finished, a data analyst works out
the calculated columns from the approved figures and enters them with a formula note.

The app shows the figures and records what the analyst enters; it calculates nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from decimal import Decimal

import pandas as pd
import streamlit as st

from src import calculation_service, review_service
from src.analysis import report_end_year
from src.config_loader import AppConfig
from src.database import connect
from src.excel_exporter import fiscal_year_label
from src.project_service import list_documents
from src.review_service import ApprovedResult, ReviewError


def render(config: AppConfig, open_report: Callable[[str], str | None]) -> None:
    st.markdown("## Calculations")
    st.caption("Work out the calculated columns from a report's approved figures and enter them with a note on "
               "the formula. The app records them; it does not calculate.")
    with closing(connect(config.settings.database_path)) as conn:
        documents = {d["id"]: d for d in list_documents(conn)}
        status = {document_id: review_service.extraction_status(conn, document_id) for document_id in documents}
    if not documents:
        st.info("No reports saved yet. Review a report in Extract & Review first.", icon=":material/inbox:")
        return

    if st.session_state.get("calc_document") not in documents:
        st.session_state.calc_document = next(iter(documents))
    document_id = st.selectbox(
        "Report", list(documents), key="calc_document",
        format_func=lambda i: f"{documents[i]['bank_name']} · {_year(documents[i])} · "
                              + ("ready for calculations" if status[i].finished else "extraction not finished"),
    )
    doc, state = documents[document_id], status[document_id]
    if not state.finished:
        st.info("The extraction review of this report is not finished yet. Finish it in Extract & Review "
                "(button under the metric table); the calculated columns open after that.",
                icon=":material/hourglass_top:")
        if st.button("Open in Extract & Review", icon=":material/fact_check:"):
            error = open_report(document_id)
            if error:
                st.error(error, icon=":material/error:")
        return

    status_col, reopen_col = st.columns([4, 1], vertical_alignment="center")
    status_col.success(f"Extraction review finished by {state.reviewer} · {state.at[:16].replace('T', ' ')} UTC"
                       + (f" · “{state.reason}”" if state.reason else ""), icon=":material/task_alt:")
    with reopen_col:
        _reopen(config, document_id)

    figures_col, calculated_col = st.columns([2, 3], gap="medium")
    with closing(connect(config.settings.database_path)) as conn:
        with figures_col:
            _approved_figures(conn, config, doc)
        with calculated_col:
            _calculated_editor(conn, config, document_id)


# --- left: the approved figures to calculate from -------------------------------------------

def _approved_figures(conn: sqlite3.Connection, config: AppConfig, doc: sqlite3.Row) -> None:
    template = config.export_template
    current = _by_column(config, review_service.approved_results(conn, doc["id"]))
    previous_doc = _previous_year_report(conn, doc)
    previous = _by_column(config, review_service.approved_results(conn, previous_doc["id"])) if previous_doc else {}

    st.markdown(f"**Approved figures** · {doc['bank_name']} {_year(doc)}")
    rows = []
    for column in (*template.extracted(), *sorted(set(current) - set(template.extracted()))):
        row = {"Figure": column, "Value": _show(current.get(column))}
        if previous_doc is not None:
            row[f"Previous year ({_year(previous_doc)})"] = _show(previous.get(column))
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=640)
    if previous_doc is None:
        st.caption("No saved report for this bank's previous year, so growth and averages need the previous "
                   "year's figures from the dataset.")
    st.caption("ETB in full Birr; percentages as decimals (0.29 = 29%). Blank = not approved.")


def _by_column(config: AppConfig, results: list[ApprovedResult]) -> dict[str, ApprovedResult]:
    """Approved results keyed by their Excel column (or the metric name when it has none)."""
    keyed = {}
    for r in results:
        try:
            metric = config.metric(r.metric_id)
            keyed[metric.export_column or f"{metric.display_name} (not an Excel column)"] = r
        except KeyError:
            keyed[r.metric_id] = r
    return keyed


def _previous_year_report(conn: sqlite3.Connection, doc: sqlite3.Row) -> sqlite3.Row | None:
    end_year = report_end_year(doc["reporting_period"])
    if end_year is None:
        return None
    candidates = [d for d in list_documents(conn)  # newest first
                  if d["bank_name"].casefold() == doc["bank_name"].casefold()
                  and report_end_year(d["reporting_period"]) == end_year - 1]
    return candidates[0] if candidates else None


def _show(result: ApprovedResult | None) -> str:
    if result is None:
        return ""
    if result.unit == "fraction":
        return f"{result.value.normalize():f} ({(result.value * 100).normalize():f}%)"
    return f"{result.value:,}"


# --- right: the calculated columns -------------------------------------------------------

def _calculated_editor(conn: sqlite3.Connection, config: AppConfig, document_id: str) -> None:
    template = config.export_template
    history = calculation_service.history(conn, document_id)
    current = calculation_service.current_values(conn, document_id)
    st.markdown(f"**Calculated columns** · {len(current)} of {len(template.calculated)} entered")
    st.caption("Type the value the way the dataset stores it (ratios and rates as decimals, e.g. 0.0352 for "
               "3.52%; amounts in full Birr) and how you calculated it. Clear a value to remove it.")
    original = pd.DataFrame({
        "Calculated column": list(template.calculated),
        "Value": [_plain(current[c].value) if c in current else "" for c in template.calculated],
        "Formula / note": [current[c].formula_note if c in current else "" for c in template.calculated],
        "Entered by": [current[c].reviewer if c in current else "" for c in template.calculated],
        "Entered at (UTC)": [current[c].created_at[:16].replace("T", " ") if c in current else ""
                             for c in template.calculated],
    })
    edited = st.data_editor(
        original, hide_index=True, width="stretch", height=640, num_rows="fixed",
        disabled=["Calculated column", "Entered by", "Entered at (UTC)"],
        key=f"calc_editor_{document_id}_{len(history)}",  # a fresh editor after every save
        column_config={
            "Calculated column": st.column_config.TextColumn(width=230),
            "Value": st.column_config.TextColumn(width=110),
            "Formula / note": st.column_config.TextColumn(width=260),
        },
    )
    if st.button("Save calculated values", type="primary", icon=":material/save:", width="stretch"):
        _save(config, document_id, original, edited)
    _history(history)


def _save(config: AppConfig, document_id: str, original: pd.DataFrame, edited: pd.DataFrame) -> None:
    changes = [
        (row["Calculated column"], _text(row["Value"]), _text(row["Formula / note"]))
        for (_, row), (_, before) in zip(edited.iterrows(), original.iterrows())
        if (_text(row["Value"]), _text(row["Formula / note"])) != (_text(before["Value"]), _text(before["Formula / note"]))
    ]
    if not changes:
        st.info("Nothing changed.", icon=":material/info:")
        return
    saved, errors = 0, []
    with closing(connect(config.settings.database_path)) as conn:
        for column, value, note in changes:
            try:
                calculation_service.enter_value(conn, config.export_template, document_id, column, value, note,
                                                st.session_state.get("reviewer", ""))
                saved += 1
            except ReviewError as exc:
                errors.append(f"**{column}**: {exc}")
    if errors:
        st.error("Not saved:  \n" + "  \n".join(errors), icon=":material/error:")
    if saved:
        st.toast(f"Saved {saved} calculated value(s)", icon=":material/check_circle:")
        if not errors:
            st.rerun()


def _history(history: list[calculation_service.CalculatedValue]) -> None:
    if not history:
        return
    with st.expander(f"History ({len(history)})"):
        for v in reversed(history):
            value = "cleared" if v.value is None else _plain(v.value)
            note = f" · “{v.formula_note}”" if v.formula_note else ""
            st.caption(f"{v.created_at[:16].replace('T', ' ')} · {v.reviewer} · {v.column} = {value}{note}")


def _reopen(config: AppConfig, document_id: str) -> None:
    with st.popover("Reopen extraction", icon=":material/lock_open:", width="stretch"):
        st.caption("Unlocks the figures in Extract & Review. Calculated values are kept; check them again after "
                   "the figures change.")
        reason = st.text_area("Reason", key=f"reopen_extraction_{document_id}")
        if st.button("Confirm reopen", type="primary", key=f"confirm_reopen_extraction_{document_id}"):
            try:
                with closing(connect(config.settings.database_path)) as conn:
                    review_service.reopen_extraction(conn, document_id, st.session_state.get("reviewer", ""), reason)
            except ReviewError as exc:
                st.error(str(exc), icon=":material/error:")
                return
            st.toast("Extraction review reopened", icon=":material/lock_open:")
            st.rerun()


def _year(doc: sqlite3.Row) -> str:
    end_year = report_end_year(doc["reporting_period"])
    return fiscal_year_label(end_year) if end_year else doc["reporting_period"]


def _plain(value: Decimal) -> str:
    return f"{value:f}"


def _text(value: object) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value).strip()
