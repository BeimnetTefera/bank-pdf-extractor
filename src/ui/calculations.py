"""Calculations page: once a report's extraction review is finished, a data analyst works out
the calculated columns from the approved figures and enters them with a formula note.

The app shows the figures, proposes a value for each column that has a draft formula (worked
out from the approved figures, D-088) with its difference from the entered and the printed
value, and records what the analyst enters or accepts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from decimal import Decimal
from html import escape

import pandas as pd
import streamlit as st

from src import calculation_service, review_service
from src.analysis import report_end_year
from src.config_loader import AppConfig
from src.database import connect
from src.excel_exporter import fiscal_year_label
from src.project_service import list_documents
from src.review_service import ApprovedResult, ReviewError
from src.ui import routes, style


# The 26 columns in groups, as in the redesign mockup (screen 5, D-092).
GROUPS = (
    ("Profit and per share", ("Profit Before Tax (Calculated)", "Profit After Tax (Calculated)",
                              "Basic Earning per 1 share (EPS) (Calculated)", "Return on Assets (ROA) (Calculated)",
                              "Return on Equity (ROE) (Calculated)")),
    ("Balance sheet", ("Average Total Asset", "Deposit Growth Rate")),
    ("Efficiency and profitability", ("Cost to Income Ratio", "Net Interest Margin", "Asset Utilization Ratio",
                                      "Profit Margin")),
    ("Dividends", ("Dividend Payout Ratio (Dividends Paid/Net Profit)",
                   "Dividend Payout Ratio (Dividends paid/Total deposits)")),
    ("Liquidity, capital and leverage", ("Loan-to-Deposit Ratio", "Liquidity Ratio", "Capital Adequacy Ratio",
                                         "Leverage Ratio", "Equity-to-Asset Ratio")),
)
BLANK = "<span>&nbsp;</span>"  # st.html refuses an empty cell
COLUMNS = [2.5, 2.5, 1.5, 1.3, 1.5, 1.3, 1.1]  # column, formula, auto, printed, difference, entered, action
TABS = {"all": "All", "ready": "Ready", "missing": "Needs input", "differences": "Differences"}


def render(config: AppConfig, open_report: Callable[[str], str | None]) -> None:
    with closing(connect(config.settings.database_path)) as conn:
        documents = {d["id"]: d for d in list_documents(conn)}
        status = {document_id: review_service.extraction_status(conn, document_id) for document_id in documents}
    title_col, picker_col = st.columns([3, 1.6], vertical_alignment="bottom")
    title_col.markdown("## Calculations")
    title_col.html('<p class="bpe-sub">The 26 calculated columns, worked out from a report\'s approved figures. '
                   'You accept or override each one.</p>')
    if not documents:
        st.info("No reports saved yet. Review a report first.", icon=":material/inbox:")
        return
    if st.session_state.get("calc_document") not in documents:
        ready = [i for i in documents if status[i].finished]
        st.session_state.calc_document = ready[0] if ready else next(iter(documents))
    document_id = picker_col.selectbox(
        "Report", list(documents), key="calc_document",
        format_func=lambda i: f"{documents[i]['bank_name']} · {_year(documents[i])}"
                              + ("" if status[i].finished else " (review not finished)"))
    state = status[document_id]
    with closing(connect(config.settings.database_path)) as conn:
        autos = calculation_service.auto_values(conn, config, document_id)
        current = calculation_service.current_values(conn, document_id)
    template = config.export_template
    ready = [c for c, a in autos.items() if a.value is not None and c not in current]
    missing = [c for c, a in autos.items() if a.value is None]
    flagged = [c for c, a in autos.items() if _flagged(a)]
    with st.container(key="panel_facts"):
        cols = st.columns([1.6, 1.2, 1.4, 1.4, 1.3, 1.6], vertical_alignment="center")
        if state.finished:
            cols[0].html(style.chip("Extraction finished", "ok"))
            cols[1].html(f'<span class="bpe-minor">by <b style="color:#15181E;font-weight:500">'
                         f'{escape(state.reviewer)}</b></span>')
        else:
            cols[0].html(style.chip("Extraction not finished", "warn"))
            if cols[1].button("Open in Review", key="calc_open_review"):
                error = open_report(document_id)
                if error:
                    st.error(error, icon=":material/error:")
        cols[2].html(f'<span class="bpe-minor"><b class="bpe-num" style="color:#15181E">{len(ready)}</b> ready to accept</span>')
        cols[3].html(f'<span class="bpe-minor"><b class="bpe-num" style="color:#15181E">{len(missing)}</b> need an input</span>')
        cols[4].html(f'<span class="bpe-minor"><b class="bpe-num" style="color:#15181E">{len(current)}</b> of '
                     f'{len(template.calculated)} entered</span>')
        if state.finished:
            with cols[5]:
                _reopen(config, document_id)
    if error := st.session_state.pop("calc_error", None):
        st.error(error, icon=":material/error:")
    with st.container(key="draftnote"):
        note_col, link_col = st.columns([5, 1.3], vertical_alignment="center")
        note_col.markdown(":material/info: The formulas are **drafts** until Shega confirms them. ⚠ marks a "
                          "difference above 0.5%.")
        with link_col:
            routes.link("templates", "See the formula list")
    with st.container(key="panel_table_calc"):
        with st.container(key="calc_tools"):
            tab_col, accept_col = st.columns([4, 1.6], vertical_alignment="center")
            counts = {"all": len(template.calculated), "ready": len(ready), "missing": len(missing),
                      "differences": len(flagged)}
            tab = tab_col.segmented_control("Show", list(TABS), key="calc_tab", default="all",
                                            format_func=lambda k: f"{TABS[k]} {counts[k]}",
                                            label_visibility="collapsed") or "all"
            accept_col.button(f"Accept {len(ready)} auto values", key="calc_accept_all", type="primary",
                              width="stretch", disabled=not ready or not state.finished,
                              on_click=_accept_auto, args=(config, document_id))
        with st.container(key="thead_calc"):
            for col, title in zip(st.columns(COLUMNS, vertical_alignment="center"),
                                  ("Calculated column", "Formula (draft)", "Auto value", "Printed in report",
                                   "Difference", "Entered value", "")):
                col.markdown(title)
        shown = {"all": set(template.calculated), "ready": set(ready), "missing": set(missing),
                 "differences": set(flagged)}[tab]
        grouped = [*GROUPS, ("Network and customers",
                             tuple(c for c in template.calculated if not any(c in g for _, g in GROUPS)))]
        for group, columns in grouped:
            members = [c for c in columns if c in shown]
            if not members:
                continue
            with st.container(key=f"tgroup_{group.split()[0].lower()}"):
                st.html(f'<div class="bpe-qgroup" style="padding:7px 16px">{escape(group)}</div>')
            for column in members:
                _row(config, document_id, column, autos.get(column), current.get(column), state.finished, open_report)


def _flagged(auto: calculation_service.AutoValue) -> bool:
    found = calculation_service.difference(auto.value, auto.reported)
    return found is not None and found[1]


def _row(config: AppConfig, document_id: str, column: str, auto: calculation_service.AutoValue | None,
         entered: calculation_service.CalculatedValue | None, finished: bool,
         open_report: Callable[[str], str | None]) -> None:
    key = f"trow_calc_{config.export_template.calculated.index(column)}"
    with st.container(key=key):
        cols = st.columns(COLUMNS, vertical_alignment="center")
        cols[0].html(f'<span style="font-size:13px">{escape(column)}</span>')
        cols[1].html(f'<span class="bpe-minor" style="font-size:13px">{escape(auto.words if auto else "")}</span>')
        if auto is not None and auto.value is None:
            missing = [i.label for i in auto.inputs if i.kind == "missing"]
            cols[2].html(f'<span class="bpe-checkwarn" style="font-size:12px">missing: '
                         f'{escape(", ".join(missing) or "an input")}</span>')
        else:
            cols[2].html(f'<span class="bpe-num">{escape(_plain_auto(auto))}</span>')
        cols[3].html(f'<span class="bpe-num">{escape(_number(auto.reported, auto.formula.unit) if auto else "")}'
                     '</span>')
        cols[4].html(_difference_html(auto) or BLANK)
        value = "" if entered is None or entered.value is None else _plain(entered.value)
        cols[5].text_input("Entered value", value=value, key=f"calc_entered_{document_id}_{key}", placeholder="—",
                           label_visibility="collapsed", disabled=not finished,
                           on_change=_typed, args=(config, document_id, column, auto, f"calc_entered_{document_id}_{key}"))
        if not finished:
            return
        if auto is not None and auto.value is None:
            missing_ids = [i.variable for i in auto.inputs if i.kind == "missing"]
            if cols[6].button("Find input", key=f"find_{key}", width="stretch"):
                st.session_state.open_metric = missing_ids[0].removesuffix("_prev") if missing_ids else None
                error = open_report(document_id)
                if error:
                    st.error(error, icon=":material/error:")
        elif auto is not None and entered is None:
            if _flagged(auto):
                with cols[6].popover("Check", width="stretch"):
                    st.markdown(f"**{escape(column)}**")
                    st.code(auto.arithmetic, language=None, wrap_lines=True)
                    st.caption(f"Printed in the report: {_plain(auto.reported)}. The difference is above 0.5%.")
                    st.button("Accept the auto value", key=f"accept_{key}", type="primary",
                              on_click=_accept_one, args=(config, document_id, auto))
            else:
                cols[6].button("Accept", key=f"accept_{key}", width="stretch",
                               on_click=_accept_one, args=(config, document_id, auto))


def _plain_auto(auto: calculation_service.AutoValue | None) -> str:
    return "" if auto is None else _number(auto.value, auto.formula.unit)


def _number(value: Decimal | None, unit: str) -> str:
    """Amounts and counts with thousands separators; ratios as stored."""
    if value is None:
        return ""
    if unit in ("ETB", "count") and abs(value) >= 1000:
        return f"{value.normalize():,f}"
    return _plain(value.normalize())


def _difference_html(auto: calculation_service.AutoValue | None) -> str:
    found = calculation_service.difference(auto.value, auto.reported) if auto else None
    if found is None:
        return ""
    gap, flagged = found
    share = f" ({abs(gap) / abs(auto.reported) * 100:.1f}%)" if auto.reported and gap else ""
    if flagged:
        return f'<span class="bpe-num" style="color:#7A4900;font-weight:500">{gap.normalize():+f}{share} ⚠</span>'
    return f'<span class="bpe-num" style="color:#17663F">{"0" if not gap else f"{gap.normalize():+f}"}{share} ✓</span>'


def _accept_one(config: AppConfig, document_id: str, auto: calculation_service.AutoValue) -> None:
    try:
        with closing(connect(config.settings.database_path)) as conn:
            calculation_service.enter_value(conn, config.export_template, document_id, auto.column, auto.value,
                                            auto.note, st.session_state.get("reviewer", ""))
    except ReviewError as exc:
        st.session_state.calc_error = f"{auto.column}: {exc}"


def _typed(config: AppConfig, document_id: str, column: str, auto: calculation_service.AutoValue | None,
           key: str) -> None:
    """A value typed in the Entered value box: saved with a note saying it was typed (empty clears it)."""
    value = (st.session_state.get(key) or "").strip()
    note = f"Typed by the analyst (draft formula: {auto.words})" if auto else "Typed by the analyst"
    try:
        with closing(connect(config.settings.database_path)) as conn:
            calculation_service.enter_value(conn, config.export_template, document_id, column, value,
                                            note if value else "", st.session_state.get("reviewer", ""))
    except ReviewError as exc:
        st.session_state.calc_error = f"{column}: {exc}"


def _accept_auto(config: AppConfig, document_id: str) -> None:
    try:
        with closing(connect(config.settings.database_path)) as conn:
            saved = calculation_service.accept_auto_values(conn, config, document_id,
                                                           st.session_state.get("reviewer", ""))
    except ReviewError as exc:
        st.session_state.calc_error = str(exc)
        return
    st.toast(f"Saved {len(saved)} auto value(s)", icon=":material/check_circle:")


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
