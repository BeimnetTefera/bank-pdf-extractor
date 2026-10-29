"""Calculations page, as in the approved mockup (canvas "Calculations page — 4 directions", option A made
clickable, 2026-10-29): once a report's extraction review is finished, a data analyst accepts the value
worked out for each calculated column from the approved figures (D-088), or enters their own.

Four tiles count what is ready, what differs from the printed figure, what misses an input and what is
entered. Below, the columns are grouped by statement; one group is open at a time and pages through its
rows, so the page keeps to the size of the window instead of growing downward.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from decimal import Decimal
from html import escape

import pandas as pd
import streamlit as st

from src import calculation_service, review_service
from src.config_loader import AppConfig
from src.database import connect
from src.project_service import list_documents
from src.report_period import fiscal_year
from src.review_service import ReviewError
from src.ui import routes

# The 26 columns in groups (the rest are "Network and customers").
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
SLOTS = 4  # an open group shows rows worth 4 slots per page; a row that differs from the report takes 2
ROW = [1.6, 1, 1, 1, 0.9]  # name and formula, calculated, printed, check, action
FILTERS = {"all": "All", "ready": "Ready", "diff": "Differences", "missing": "Missing inputs"}
OPEN, PAGE, FILTER, OWN, OWN_VALUE = "calc_open", "calc_page", "calc_filter", "calc_own", "calc_own_value"


def kind(auto: calculation_service.AutoValue | None, entered: calculation_service.CalculatedValue | None) -> str:
    """entered | missing (an input is not approved) | diff (above 0.5% from the printed figure) | ready."""
    if entered is not None and entered.value is not None:
        return "entered"
    if auto is None or auto.value is None:
        return "missing"
    return "diff" if _flagged(auto) else "ready"


def check(auto: calculation_service.AutoValue) -> tuple[str, str]:
    """(text, chip kind) comparing the calculated value with the printed one."""
    found = calculation_service.difference(auto.value, auto.reported)
    if found is None:
        return "Nothing to compare", "muted"
    gap, flagged = found
    share = f"{abs(gap) / abs(auto.reported) * 100:.1f}" if auto.reported else "0.0"
    if flagged:
        return f"{'+' if gap > 0 else '−'}{share}% · above 0.5%", "warn"
    return ("✓ Matches · 0", "ok") if not gap else (f"✓ Within 0.5% · {share}%", "ok")


def pages_of(columns: list[str], kinds: dict[str, str]) -> list[list[str]]:
    """The open group's rows cut into pages of SLOTS (a row that differs takes two)."""
    pages, current, used = [], [], 0
    for column in columns:
        size = 2 if kinds[column] == "diff" else 1
        if current and used + size > SLOTS:
            pages.append(current)
            current, used = [], 0
        current.append(column)
        used += size
    return [*pages, current] if current else pages


def render(config: AppConfig, open_report: Callable[[str], str | None]) -> None:
    with closing(connect(config.settings.database_path)) as conn:
        documents = {d["id"]: d for d in list_documents(conn)}
        status = {document_id: review_service.extraction_status(conn, document_id) for document_id in documents}
    title_col, side_col = st.columns([3, 2.4], vertical_alignment="bottom")
    title_col.markdown("## Calculations")
    title_col.html('<p class="bpe-sub">Review calculated figures against the approved report.</p>')
    if not documents:
        st.info("No reports saved yet. Review a report first.", icon=":material/inbox:")
        return
    if st.session_state.get("calc_document") not in documents:
        ready = [i for i in documents if status[i].finished]
        st.session_state.calc_document = ready[0] if ready else next(iter(documents))
    with side_col, st.container(key="calc_side", horizontal=True, vertical_alignment="center",
                                horizontal_alignment="right"):
        state_slot = st.empty()
        st.html('<span class="bpe-flabel">Report</span>', width="content")
        document_id = st.selectbox(
            "Report", list(documents), key="calc_document", label_visibility="collapsed", width=240,
            on_change=_reset_view,
            format_func=lambda i: f"{documents[i]['bank_name']} · {fiscal_year(documents[i]['reporting_period'])}")
    state = status[document_id]
    if state.finished:
        state_slot.html(f'<span class="bpe-chip bpe-ok">Extraction finished · {escape(state.reviewer)}</span>',
                        width="content")
    else:
        with state_slot.container(horizontal=True, vertical_alignment="center", width="content"):
            st.html('<span class="bpe-chip bpe-warn">Extraction not finished</span>', width="content")
            if st.button("Open in Review", key="calc_open_review"):
                if error := open_report(document_id):
                    st.error(error, icon=":material/error:")
    with closing(connect(config.settings.database_path)) as conn:
        autos = calculation_service.auto_values(conn, config, document_id)
        current = calculation_service.current_values(conn, document_id)
    columns = config.export_template.calculated
    kinds = {c: kind(autos.get(c), current.get(c)) for c in columns}
    counts = {k: sum(v == k for v in kinds.values()) for k in ("ready", "diff", "missing", "entered")}
    st.html(_tiles(counts, len(columns)))
    if error := st.session_state.pop("calc_error", None):
        st.error(error, icon=":material/error:")
    with st.container(key="panel_table_calc"):
        _toolbar(config, document_id, counts, len(columns), state.finished)
        _groups(config, document_id, columns, autos, current, kinds, state.finished, open_report)


def _tiles(counts: dict[str, int], total: int) -> str:
    ready = counts["ready"] + counts["diff"]
    word = "difference" if counts["diff"] == 1 else "differences"
    return (f'<div class="bpe-ctiles"><div><span>Ready to accept</span><b>{ready}</b></div>'
            f'<div class="warn"><span>Needs review</span><b>{counts["diff"]} <small>{word}</small></b></div>'
            f'<div><span>Missing inputs</span><b>{counts["missing"]}</b></div>'
            f'<div><p><span>Entered values</span><b>{counts["entered"]} <small>/ {total}</small></b></p>'
            f'<i><em style="width:{round(counts["entered"] / total * 100) if total else 0}%"></em></i></div></div>')


def _toolbar(config: AppConfig, document_id: str, counts: dict[str, int], total: int, finished: bool) -> None:
    ready = counts["ready"] + counts["diff"]
    numbers = {"all": total, "ready": ready, "diff": counts["diff"], "missing": counts["missing"]}
    chosen = st.session_state.get(FILTER, "all")
    with st.container(key="calc_tools", horizontal=True, vertical_alignment="center"):
        st.html('<span class="bpe-ctitle">Calculation review</span>', width="content")
        for key, label in FILTERS.items():
            st.button(f"{label} {numbers[key]}", key=f"calc_chip_{key}{'_on' if key == chosen else ''}",
                      on_click=_set_filter, args=(key,))
        st.space("stretch")
        st.button(f"Accept {ready} auto value{'' if ready == 1 else 's'}" if ready else "All auto values accepted",
                  key="calc_accept_all", type="primary", disabled=not ready or not finished,
                  on_click=_accept_auto, args=(config, document_id))


def _set_filter(key: str) -> None:
    st.session_state.update({FILTER: key, PAGE: 0, OWN: None})


def _reset_view() -> None:
    st.session_state.update({PAGE: 0, OWN: None})
    st.session_state.pop(OPEN, None)


def _groups(config: AppConfig, document_id: str, columns: list[str], autos: dict, current: dict,
            kinds: dict[str, str], finished: bool, open_report: Callable[[str], str | None]) -> None:
    chosen = st.session_state.get(FILTER, "all")
    keep = {"all": {"ready", "diff", "missing", "entered"}, "ready": {"ready", "diff"}, "diff": {"diff"},
            "missing": {"missing"}}[chosen]
    rest = tuple(c for c in columns if not any(c in g for _, g in GROUPS))
    visible = [(name, [c for c in group if c in kinds and kinds[c] in keep])
               for name, group in (*GROUPS, ("Network and customers", rest))]
    visible = [(name, group) for name, group in visible if group]
    if not visible:
        st.html('<div class="bpe-empty">Nothing to show for this filter.</div>')
        return
    names = [name for name, _ in visible]
    open_name = st.session_state.get(OPEN, names[0])
    if open_name is not None and open_name not in names:
        open_name = names[0]
    for at, (name, group) in enumerate(visible):
        is_open = name == open_name
        with st.container(key=f"calc_grp_{at}{'_open' if is_open else ''}"):
            st.button(f"{name.upper()} :gray-background[{len(group)}]", key=f"calc_grp_btn_{at}", type="tertiary",
                      icon=":material/expand_more:" if is_open else ":material/chevron_right:", width="stretch",
                      on_click=_toggle, args=(name, is_open))
            st.html(_summary(group, kinds), width="content")
        if is_open:
            _open_group(config, document_id, group, autos, current, kinds, finished, open_report)


def _summary(group: list[str], kinds: dict[str, str]) -> str:
    n = {k: sum(kinds[c] == k for c in group) for k in ("ready", "missing", "entered", "diff")}
    parts = [f"{n['ready']} ready" if n["ready"] else "", f"{n['missing']} missing input" if n["missing"] else "",
             f"{n['entered']} entered" if n["entered"] else ""]
    text = escape(" · ".join(p for p in parts if p))
    diff = f'<b>{"· " if text else ""}{n["diff"]} difference</b>' if n["diff"] else ""
    return f'<span class="bpe-grpsum">{text} {diff}</span>'


def _toggle(name: str, is_open: bool) -> None:
    st.session_state.update({OPEN: None if is_open else name, PAGE: 0, OWN: None})


def _open_group(config: AppConfig, document_id: str, group: list[str], autos: dict, current: dict,
                kinds: dict[str, str], finished: bool, open_report: Callable[[str], str | None]) -> None:
    pages = pages_of(group, kinds)
    page = min(max(0, st.session_state.get(PAGE, 0)), len(pages) - 1)
    st.session_state[PAGE] = page
    with st.container(key="calc_body"):
        for column in pages[page]:
            index = config.export_template.calculated.index(column)
            auto, entered = autos.get(column), current.get(column)
            if kinds[column] == "missing":
                _missing_row(document_id, column, index, auto, open_report)
            elif kinds[column] == "diff":
                _diff_row(config, document_id, column, index, auto, finished)
            else:
                _row(config, document_id, column, index, auto, entered, finished)
    before = sum(len(p) for p in pages[:page])
    with st.container(key="calc_gpager", horizontal=True, vertical_alignment="center",
                      horizontal_alignment="distribute"):
        st.html(f'<span class="bpe-tcell">Showing {before + 1}–{before + len(pages[page])} of {len(group)}</span>',
                width="content")
        with st.container(key="calc_gpages", horizontal=True, vertical_alignment="center", width="content"):
            st.button("", icon=":material/chevron_left:", key="calc_prev", disabled=page == 0, help="Previous page",
                      on_click=_set_page, args=(page - 1,))
            st.html(f'<span class="bpe-pageof">Page {page + 1} of {len(pages)}</span>', width="content")
            st.button("", icon=":material/chevron_right:", key="calc_next", disabled=page >= len(pages) - 1,
                      help="Next page", on_click=_set_page, args=(page + 1,))


def _set_page(page: int) -> None:
    st.session_state.update({PAGE: page, OWN: None})


def _name_cell(column: str, auto: calculation_service.AutoValue | None) -> str:
    name, words = escape(column.removesuffix(" (Calculated)")), escape(auto.words if auto else "")
    return f'<div class="bpe-cname" title="{name}">{name}</div><div class="bpe-cwords" title="{words}">{words}</div>'


def _figure_cells(cols, auto: calculation_service.AutoValue) -> None:
    cols[0].html(_name_cell(auto.column, auto))
    cols[1].html(f'<div class="bpe-clabel">Calculated</div><div class="bpe-cval">{escape(_number(auto.value, auto.formula.unit))}</div>')
    printed = (f'<div class="bpe-cval">{escape(_number(auto.reported, auto.formula.unit))}</div>'
               if auto.reported is not None else '<div class="bpe-cnone">not printed</div>')
    cols[2].html(f'<div class="bpe-clabel">Printed in report</div>{printed}')
    text, chip = check(auto)
    cols[3].html(f'<span class="bpe-chip bpe-{chip}">{escape(text)}</span>')


def _row(config: AppConfig, document_id: str, column: str, index: int, auto: calculation_service.AutoValue,
         entered: calculation_service.CalculatedValue | None, finished: bool) -> None:
    with st.container(key=f"trow_calc_{index}"):
        cols = st.columns(ROW, vertical_alignment="center", gap="medium")
        _figure_cells(cols, auto)
        if entered is not None and entered.value is not None:
            own = "" if entered.value == auto.value else f" {_plain(entered.value)}"
            cols[4].html(f'<span class="bpe-centered">✓ Entered{escape(own)} · {escape(entered.reviewer)}</span>')
        else:
            cols[4].button("Accept", key=f"accept_trow_calc_{index}", width="stretch", disabled=not finished,
                           on_click=_accept_one, args=(config, document_id, auto))


def _diff_row(config: AppConfig, document_id: str, column: str, index: int, auto: calculation_service.AutoValue,
              finished: bool) -> None:
    """A value above 0.5% from the printed figure: the arithmetic, the evidence, accept or enter your own."""
    with st.container(key=f"trow_calc_{index}_diff"):
        cols = st.columns(ROW, vertical_alignment="center", gap="medium")
        _figure_cells(cols, auto)
        with st.container(key=f"calc_box_{index}", horizontal=True, vertical_alignment="center"):
            if st.session_state.get(OWN) == column:
                st.html(f'<span class="bpe-clabel" style="font-size:13px">Your value for '
                        f'{escape(column.removesuffix(" (Calculated)"))}</span>', width="content")
                st.text_input("Your value", key=OWN_VALUE, placeholder=f"e.g. {_plain(auto.reported)}",
                              label_visibility="collapsed", width=180)
                st.space("stretch")
                st.button("Cancel", key="calc_own_cancel", on_click=st.session_state.__setitem__, args=(OWN, None))
                st.button("Save my value", key="calc_own_save", type="primary",
                          on_click=_save_own, args=(config, document_id, auto))
                return
            st.html(f'<div class="bpe-cboxtext"><b>Difference above the 0.5% limit</b>'
                    f'<code>{escape(auto.arithmetic)}</code></div>')
            if st.button("View evidence", key=f"calc_evidence_{index}") and auto.formula.compare_with:
                routes.go("evidence", report=document_id, metric=auto.formula.compare_with)
            st.button("Enter my own value", key=f"calc_own_{index}", disabled=not finished,
                      on_click=_start_own, args=(column,))
            st.button(f"Accept {_number(auto.value, auto.formula.unit)}", key=f"accept_trow_calc_{index}",
                      type="primary", disabled=not finished, on_click=_accept_one, args=(config, document_id, auto))


def _missing_row(document_id: str, column: str, index: int, auto: calculation_service.AutoValue | None,
                 open_report: Callable[[str], str | None]) -> None:
    missing = [i for i in auto.inputs if i.kind == "missing"] if auto else []
    with st.container(key=f"trow_calc_{index}_missing"):
        name_col, need_col, chip_col, action_col = st.columns([1.6, 2.06, 1, 0.9], vertical_alignment="center",
                                                              gap="medium")
        name_col.html(_name_cell(column, auto))
        need = " and ".join(i.label for i in missing) or "an input"
        need_col.html(f'<span class="bpe-cneed">Needs <b>{escape(need)}</b>, not approved in this report.</span>')
        chip_col.html('<span class="bpe-chip bpe-dashed">Missing input</span>')
        if action_col.button("Find input", key=f"find_trow_calc_{index}", width="stretch"):
            st.session_state.open_metric = missing[0].variable.removesuffix("_prev") if missing else None
            if error := open_report(document_id):
                st.error(error, icon=":material/error:")


def _start_own(column: str) -> None:
    st.session_state.update({OWN: column, OWN_VALUE: ""})


def _save_own(config: AppConfig, document_id: str, auto: calculation_service.AutoValue) -> None:
    """The analyst's own value for a column that differs from the report, noted as typed."""
    value = (st.session_state.get(OWN_VALUE) or "").strip()
    if not value:
        st.session_state.calc_error = "Type a value first, or press Cancel."
        return
    try:
        with closing(connect(config.settings.database_path)) as conn:
            calculation_service.enter_value(conn, config.export_template, document_id, auto.column, value,
                                            f"Typed by the analyst (draft formula: {auto.words})",
                                            st.session_state.get("reviewer", ""))
    except ReviewError as exc:
        st.session_state.calc_error = f"{auto.column}: {exc}"
        return
    st.session_state[OWN] = None


def _flagged(auto: calculation_service.AutoValue) -> bool:
    found = calculation_service.difference(auto.value, auto.reported)
    return found is not None and found[1]


def _number(value: Decimal | None, unit: str) -> str:
    """Amounts and counts with thousands separators; ratios as stored."""
    if value is None:
        return ""
    if unit in ("ETB", "count") and abs(value) >= 1000:
        return f"{value.normalize():,f}"
    return _plain(value.normalize())


def _accept_one(config: AppConfig, document_id: str, auto: calculation_service.AutoValue) -> None:
    try:
        with closing(connect(config.settings.database_path)) as conn:
            calculation_service.enter_value(conn, config.export_template, document_id, auto.column, auto.value,
                                            auto.note, st.session_state.get("reviewer", ""))
    except ReviewError as exc:
        st.session_state.calc_error = f"{auto.column}: {exc}"


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


def _plain(value: Decimal | None) -> str:
    return "" if value is None else f"{value:f}"


def _text(value: object) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value).strip()
