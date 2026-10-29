"""Templates, as in the approved mockup (canvas "Templates page redesign", 2026-10-29): the metrics and the
calculated columns, with each metric's meaning editable in the table (D-093).

Edits wait in st.session_state until Save, so they survive paging, searching and leaving the page. Save
writes them one by one with metric_editor.set_definition (signed with the reviewer's name, read back before
writing); what failed stays as an edit so it can be saved again.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from html import escape

import streamlit as st

from src.config_loader import CONFIG_DIR, AppConfig
from src.metric_editor import MIN_LENGTH, MetricEditError, set_definition
from src.ui import style

PAGE_SIZE = 15  # rows per page, as before the redesign
COLUMNS = [1.15, 0.55, 1.1, 2.8, 1.2, 0.7]  # metric, unit, Excel column, meaning, standard formula, status

TAB, PAGE, EDITS, EDITING, DRAFT, ERROR = "tpl_tab", "tpl_page", "tpl_edits", "tpl_editing", "tpl_draft", "tpl_error"
SELECTED, NOTICE, ONLY_EDITED = "tpl_selected", "tpl_notice", "tpl_only_edited"
QUERY, STATUS, MAPPING, KIND = "tpl_query", "tpl_status", "tpl_mapping", "tpl_kind"
FILTERS = {STATUS: ("Status", {"all": "All", "active": "Active", "inactive": "Inactive"}),
           MAPPING: ("Excel column", {"all": "All", "mapped": "Mapped", "unmapped": "Not mapped"}),
           KIND: ("Source", {"all": "All", "printed": "Printed", "formula": "Has a formula"})}

# Esc cancels and Ctrl+Enter keeps the meaning being edited; the character count follows the typing.
# The box may be drawn just after this runs, so it looks again for a moment.
KEYS_SCRIPT = """<script>(function attach(tries) {
  const box = document.querySelector('.st-key-tpl_editing textarea');
  if (!box) { if (tries < 40) setTimeout(() => attach(tries + 1), 50); return; }
  if (box.dataset.bpe) return;
  box.dataset.bpe = '1';
  box.focus();
  const count = () => document.querySelectorAll('.bpe-draftlen').forEach(
    e => { e.textContent = box.value.split(/\\s+/).filter(Boolean).join(' ').length; });
  box.addEventListener('input', count);
  box.addEventListener('keydown', e => {
    const click = name => document.querySelector(`.st-key-${name} button`)?.click();
    if (e.key === 'Escape') { e.preventDefault(); click('tpl_cancel'); }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { setTimeout(() => click('tpl_keep'), 0); }
  });
})(0);</script>"""


def clean(text: object) -> str:
    return " ".join(str(text or "").split())


def unsaved_count() -> int:
    """Meanings edited but not saved yet (for the menu)."""
    return len(st.session_state.get(EDITS, {}))


def filter_metrics(metrics: Sequence, edits: Mapping[str, str], query: str = "", status: str = "all",
                   mapping: str = "all", kind: str = "all", only: Sequence[str] | None = None) -> list:
    """The metrics whose name or meaning (as edited) holds the query and that pass the filters;
    with only, just those ids."""
    q = query.strip().casefold()
    shown = []
    for m in metrics:
        if q and q not in m.display_name.casefold() and q not in edits.get(m.id, m.definition).casefold():
            continue
        if status != "all" and (status == "active") != m.active:
            continue
        if mapping != "all" and (mapping == "mapped") != bool(m.export_column):
            continue
        if kind != "all" and (kind == "formula") != bool(m.formula):
            continue
        if only is not None and m.id not in only:
            continue
        shown.append(m)
    return shown


def page_slice(total: int, page: int, size: int = PAGE_SIZE) -> tuple[int, int, int, int]:
    """(start, end, page, pages) for the asked page, kept inside the pages there are."""
    pages = max(1, -(-total // size))
    page = min(max(0, page), pages - 1)
    return page * size, min(total, (page + 1) * size), page, pages


def save_notice(names: Mapping[str, str], saved: Sequence[str], failed: Sequence[tuple[str, str]],
                reviewer: str) -> dict:
    """The message after Save: what was written, and why anything was not."""
    plural = "" if len(saved) == 1 else "s"
    if not failed:
        return {"kind": "ok", "title": f"Saved {len(saved)} meaning{plural}",
                "lines": [f"{', '.join(names[i] for i in saved)} · signed “edited by {reviewer.strip()}”."]}
    if not saved and len({message for _, message in failed}) == 1:
        return {"kind": "bad", "title": "Nothing was saved", "lines": [failed[0][1]]}
    lines = [f"Saved {len(saved)}: {', '.join(names[i] for i in saved)}."] if saved else []
    lines += [f"{names[i]}: {message} Your text is kept; press Save to try again." for i, message in failed]
    return {"kind": "bad", "title": f"{len(failed)} of {len(saved) + len(failed)} not saved", "lines": lines}


def readable_formula(formula: str) -> str:
    """'profit_after_tax_etb / avg(total_equity_etb, total_equity_etb_prev)' ->
    'profit_after_tax ÷ avg(total_equity, last year)'."""
    text = re.sub(r"\b[a-z0-9_]+_prev\b", "last year", formula)
    text = re.sub(r"_etb\b", "", text)
    return text.replace(" / ", " ÷ ").replace(" * ", " × ").replace(" - ", " − ")


def render(config: AppConfig) -> None:
    st.markdown("## Templates")
    st.html('<p class="bpe-sub">What the app looks for in a report, what each column means, and where it goes in the '
            'Excel export.</p>')
    edits = st.session_state.setdefault(EDITS, {})
    if not edits:
        st.session_state[ONLY_EDITED] = False
    on_metrics = st.session_state.get(TAB, "metrics") == "metrics"
    _tabs(config, on_metrics)
    _notice()
    selected = next((m for m in config.metrics if m.id == st.session_state.get(SELECTED)), None) if on_metrics else None
    if selected is not None:
        table_col, panel_col = st.columns([2.6, 1], gap="medium")
    else:
        table_col = st.container()
    with table_col, st.container(key="panel_table_templates"):
        shown = _toolbar(config, on_metrics)
        start, end, page, pages = page_slice(len(shown), st.session_state.get(PAGE, 0))
        st.session_state[PAGE] = page
        with st.container(key="tpl_scroll"):
            if on_metrics:
                _metric_rows(config, shown[start:end])
            else:
                st.html(_calculated_table(config, shown[start:end]))
        _footer(len(shown), start, end, page, pages)
        if on_metrics:
            _save_bar(config)
    if selected is not None:
        with panel_col:
            _details(config, selected, shown)


def _tabs(config: AppConfig, on_metrics: bool) -> None:
    with st.container(key="tpl_tabs", horizontal=True, gap=None):
        for tab, label, count in (("metrics", "Metrics", len(config.metrics)),
                                  ("calc", "Calculated columns", len(config.export_template.calculated))):
            on = (tab == "metrics") == on_metrics
            st.button(f"{label} :{'blue' if on else 'gray'}-background[{count}]", key=f"tpl_tab_{tab}{'_on' if on else ''}",
                      type="tertiary", on_click=_switch_tab, args=(tab,))


def _switch_tab(tab: str) -> None:
    st.session_state.update({TAB: tab, PAGE: 0, QUERY: "", SELECTED: None})


def _notice() -> None:
    notice = st.session_state.get(NOTICE)
    if not notice:
        return
    with st.container(key=f"tpl_msg_{notice['kind']}", horizontal=True, vertical_alignment="top"):
        st.html(f'<div class="bpe-msg" role="status"><b>{escape(notice["title"])}</b>'
                + "".join(f"<div>{escape(line)}</div>" for line in notice["lines"]) + "</div>")
        st.button("", icon=":material/close:", key="tpl_msg_close", type="tertiary", help="Dismiss message",
                  on_click=st.session_state.pop, args=(NOTICE, None))


def _toolbar(config: AppConfig, on_metrics: bool) -> list:
    """Search, the filters and the count line; returns what to list."""
    edits = st.session_state[EDITS]
    label = "Search metrics by name or meaning" if on_metrics else "Search calculated columns by name or formula"
    with st.container(key="tpl_tools", horizontal=True, vertical_alignment="center"):
        query = st.text_input(label, key=QUERY, placeholder=label, label_visibility="collapsed",
                              icon=":material/search:", on_change=_first_page)
        if on_metrics:
            for key, (name, options) in FILTERS.items():
                with st.container(key=f"tpl_f_{key}", horizontal=True, vertical_alignment="center", width="content"):
                    st.html(f'<span class="bpe-flabel">{name}</span>', width="content")
                    st.selectbox(name, list(options), format_func=options.get, key=key, label_visibility="collapsed",
                                 on_change=_first_page, width=150)
    if on_metrics:
        only = st.session_state.get(ONLY_EDITED)
        editing = st.session_state.get(EDITING)
        shown = filter_metrics(config.metrics, edits, query, st.session_state[STATUS], st.session_state[MAPPING],
                               st.session_state[KIND], only=[*edits, editing] if only else None)
        total, noun = len(config.metrics), "metrics"
        filtered = bool(query.strip()) or only or any(st.session_state[k] != "all" for k in FILTERS)
    else:
        q = query.strip().casefold()
        shown = [c for c in config.export_template.calculated if not q or q in c.casefold()
                 or q in _formula_text(config, c).casefold()]
        total, noun = len(config.export_template.calculated), "calculated columns"
        filtered = bool(q)
    with st.container(key="tpl_countline", horizontal=True, vertical_alignment="center",
                      horizontal_alignment="distribute"):
        with st.container(horizontal=True, vertical_alignment="center", width="content"):
            st.html(f'<span class="bpe-minor" aria-live="polite">{total if len(shown) == total else f"{len(shown)} of {total}"} '
                    f'{noun}{"" if len(shown) == total else " match"}</span>', width="content")
            if filtered:
                st.button("Clear search and filters", key="tpl_clear", type="tertiary", on_click=_clear_filters)
        if on_metrics and edits:
            n = len(edits)
            text = (f"Showing {n} unsaved · show all" if st.session_state.get(ONLY_EDITED)
                    else f"{n} unsaved change{'' if n == 1 else 's'} · show only these")
            st.button(text, key=f"tpl_only{'_on' if st.session_state.get(ONLY_EDITED) else ''}",
                      on_click=_toggle_only_edited)
    return shown


def _first_page() -> None:
    st.session_state[PAGE] = 0


def _clear_filters() -> None:
    st.session_state.update({QUERY: "", ONLY_EDITED: False, PAGE: 0, **{key: "all" for key in FILTERS}})


def _toggle_only_edited() -> None:
    st.session_state.update({ONLY_EDITED: not st.session_state.get(ONLY_EDITED), PAGE: 0})


def _metric_rows(config: AppConfig, metrics: Sequence) -> None:
    with st.container(key="thead_tpl"):
        for col, name in zip(st.columns(COLUMNS, gap="medium"), ("Metric", "Unit", "Excel column", "Meaning",
                                                                  "Standard formula", "Status")):
            extra = ' <span class="bpe-thnote">· editable</span>' if name == "Meaning" else ""
            col.html(f'<div class="bpe-th">{name}{extra}</div>')
    if not metrics:
        st.html('<div class="bpe-empty">No metric matches. Try fewer words, or clear the filters.</div>')
    edits, editing, selected = st.session_state[EDITS], st.session_state.get(EDITING), st.session_state.get(SELECTED)
    for m in metrics:
        with st.container(key=f"trow_tpl_{m.id}{'_sel' if m.id == selected else ''}"):
            name_col, unit_col, excel_col, meaning_col, formula_col, status_col = st.columns(
                COLUMNS, gap="medium", vertical_alignment="top")
            name_col.button(m.display_name, key=f"tpl_open_{m.id}", type="tertiary", help="Show details",
                            on_click=st.session_state.__setitem__, args=(SELECTED, m.id))
            unit_col.html(f'<span class="bpe-tcell">{escape(m.expected_unit)}</span>')
            excel_col.html(f'<span class="bpe-tcell">{escape(m.export_column)}</span>' if m.export_column
                           else f'<span class="bpe-unmapped">{style.icon("remove")}Not mapped</span>')
            with meaning_col:
                _meaning_cell(config, m, edits, editing == m.id)
            formula_col.html(f'<code class="bpe-code">{escape(readable_formula(m.formula))}</code>' if m.formula
                             else f'<span class="bpe-tmuted">{"Printed in the report" if m.active else "Not used"}</span>')
            status_col.html(style.chip("Active", "ok") if m.active else style.chip("Inactive", "muted"))


def _meaning_cell(config: AppConfig, metric, edits: Mapping[str, str], editing: bool) -> None:
    edited = metric.id in edits
    if editing:
        with st.container(key="tpl_editing", gap="small"):
            st.text_area(f"Meaning of {metric.display_name}", key=DRAFT, label_visibility="collapsed", height=110)
            with st.container(horizontal=True, vertical_alignment="center", key="tpl_editbar"):
                st.button("Keep", key="tpl_keep", type="primary", on_click=_keep, args=(config,))
                st.button("Cancel", key="tpl_cancel", on_click=_cancel)
                error = st.session_state.get(ERROR)
                st.html(f'<span class="bpe-drafthint bad">{escape(error)}</span>' if error else
                        f'<span class="bpe-drafthint"><span class="bpe-draftlen">{len(clean(st.session_state.get(DRAFT)))}'
                        '</span> characters · Esc cancels · Ctrl+Enter keeps</span>', width="content")
            st.html(KEYS_SCRIPT.replace("<script>", f"<script>/* {metric.id} */"), unsafe_allow_javascript=True)
        return
    text = edits.get(metric.id, metric.definition)
    with st.container(key=f"tpl_mcell_{metric.id}{'_ed' if edited else ''}", gap=None):
        st.html(f'<div class="bpe-meaning" title="{escape(text)}">{escape(text)}</div>')
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.button("Edit", key=f"tpl_edit_{metric.id}", icon=":material/edit:", type="tertiary",
                      help=f"Edit the meaning of {metric.display_name}", on_click=_start_edit, args=(config, metric.id))
            if edited:
                st.html(style.chip("Edited, not saved", "warn"), width="content")
                st.button("Undo", key=f"tpl_undo_{metric.id}", type="tertiary",
                          help=f"Undo the edit to {metric.display_name}", on_click=_undo, args=(metric.id,))


def _metric(config: AppConfig, metric_id: str):
    return next(m for m in config.metrics if m.id == metric_id)


def _record(metric, text: str) -> None:
    """Keep text as the metric's unsaved meaning; back to the saved one means no edit."""
    if text == metric.definition:
        st.session_state[EDITS].pop(metric.id, None)
    else:
        st.session_state[EDITS][metric.id] = text


def _commit_draft(config: AppConfig) -> None:
    """Fold the open draft into the edits when it is long enough, so moving to another row keeps it."""
    editing = st.session_state.get(EDITING)
    text = clean(st.session_state.get(DRAFT))
    if editing and len(text) >= MIN_LENGTH:
        _record(_metric(config, editing), text)


def _start_edit(config: AppConfig, metric_id: str) -> None:
    _commit_draft(config)
    st.session_state.update({EDITING: metric_id, ERROR: None,
                             DRAFT: st.session_state[EDITS].get(metric_id, _metric(config, metric_id).definition)})


def _keep(config: AppConfig) -> None:
    text = clean(st.session_state.get(DRAFT))
    if len(text) < MIN_LENGTH:
        st.session_state[ERROR] = f"Write the meaning in at least a short sentence ({MIN_LENGTH} characters)."
        return
    _record(_metric(config, st.session_state[EDITING]), text)
    st.session_state.update({EDITING: None, ERROR: None})


def _cancel() -> None:
    st.session_state.update({EDITING: None, ERROR: None})


def _undo(metric_id: str) -> None:
    st.session_state[EDITS].pop(metric_id, None)


def _footer(total: int, start: int, end: int, page: int, pages: int) -> None:
    with st.container(key="tpl_footer", horizontal=True, vertical_alignment="center", horizontal_alignment="distribute"):
        st.html(f'<span class="bpe-tcell">{f"Showing {start + 1}–{end} of {total}" if total else "Nothing to show"}</span>',
                width="content")
        with st.container(key="tpl_pages", horizontal=True, vertical_alignment="center", width="content"):
            st.button("", icon=":material/chevron_left:", key="tpl_prev", disabled=page == 0, help="Previous page",
                      on_click=st.session_state.__setitem__, args=(PAGE, page - 1))
            st.html(f'<span class="bpe-pageof">Page {page + 1} of {pages}</span>', width="content")
            st.button("", icon=":material/chevron_right:", key="tpl_next", disabled=page >= pages - 1, help="Next page",
                      on_click=st.session_state.__setitem__, args=(PAGE, page + 1))


def _save_bar(config: AppConfig) -> None:
    n = len(st.session_state[EDITS])
    reviewer = (st.session_state.get("reviewer") or "").strip() or "no name"
    title = f"{n} meaning{'' if n == 1 else 's'} changed, not saved yet" if n else "No unsaved changes"
    with st.container(key="tpl_savebar", horizontal=True, vertical_alignment="center"):
        st.html(f'<div class="bpe-savetext"><b>{title}</b>'
                f'<span>Saved to config/metrics.yaml, each line signed “edited by {escape(reviewer)}” with the date.</span></div>')
        with st.container(horizontal=True, width="content", key="tpl_savebtns"):
            st.button("Discard all", key="tpl_discard", disabled=not n, on_click=_discard)
            st.button(f"Save {n} change{'' if n == 1 else 's'}" if n else "Save changes", key="tpl_save", type="primary",
                      icon=":material/save:", disabled=not n, on_click=_save, args=(config,))


def _discard() -> None:
    st.session_state.update({EDITS: {}, EDITING: None, ERROR: None, ONLY_EDITED: False})


def _save(config: AppConfig) -> None:
    """Write each edited meaning; the ones that fail stay as edits. A second click finds nothing to save."""
    _commit_draft(config)
    st.session_state.update({EDITING: None, ERROR: None})
    edits = st.session_state[EDITS]
    if not edits:
        return
    path = (config.config_dir or CONFIG_DIR) / "metrics.yaml"
    reviewer = st.session_state.get("reviewer") or ""
    saved, failed = [], []
    for metric_id, text in list(edits.items()):
        try:
            set_definition(path, metric_id, text, reviewer)
        except MetricEditError as exc:
            failed.append((metric_id, str(exc)))
            continue
        saved.append(metric_id)
        del edits[metric_id]
    st.session_state[NOTICE] = save_notice({m.id: m.display_name for m in config.metrics}, saved, failed, reviewer)


def _details(config: AppConfig, metric, shown: Sequence) -> None:
    """Everything about one metric beside the table, with its saved meaning when it has been edited."""
    edits = st.session_state[EDITS]
    with st.container(key="panel_tpl_details"):
        head_col, close_col = st.columns([5, 1], vertical_alignment="top")
        head_col.html(f'<div class="bpe-cap">Metric</div><div class="bpe-abouttitle" style="margin-top:2px">'
                      f'{escape(metric.display_name)}</div><div class="bpe-file bpe-mono">{escape(metric.id)}</div>')
        close_col.button("", icon=":material/close:", key="tpl_detail_close", type="tertiary", help="Close details",
                         on_click=st.session_state.__setitem__, args=(SELECTED, None))
        formula = (readable_formula(metric.formula) if metric.formula
                   else "None: printed in the report" if metric.active else "None: not used")
        facts = [("Status", "Active" if metric.active else "Inactive", ""), ("Unit", metric.expected_unit, ""),
                 ("Excel column", metric.export_column or "Not mapped", ""), ("Standard formula", formula, "bpe-mono"),
                 ("Stored in", "config/\u200bmetrics.yaml", "bpe-mono")]  # may break after the slash
        st.html('<dl class="bpe-dl bpe-tpldl">' + "".join(f'<dt>{k}</dt><dd class="{c}">{escape(v)}</dd>' for k, v, c in facts)
                + f'</dl><div class="bpe-cap" style="margin-top:16px">Meaning</div>'
                f'<p class="bpe-fullmeaning">{escape(edits.get(metric.id, metric.definition))}</p>')
        if metric.id in edits:
            st.html(f'<div class="bpe-savedbox"><b>Saved version (before your edit)</b>{escape(metric.definition)}</div>')
        st.button("Edit meaning in the table", key="tpl_detail_edit", icon=":material/edit:",
                  on_click=_edit_from_details, args=(config, metric.id, [m.id for m in shown]))


def _edit_from_details(config: AppConfig, metric_id: str, shown: Sequence[str]) -> None:
    if metric_id in shown:
        st.session_state[PAGE] = shown.index(metric_id) // PAGE_SIZE
    _start_edit(config, metric_id)


def _formula_text(config: AppConfig, column: str) -> str:
    f = config.export_template.formula(column)
    return readable_formula(f.formula) if f else ""


def _calculated_table(config: AppConfig, columns: Sequence[str]) -> str:
    """The calculated columns with their draft formula (D-088) and the metric each is compared with."""
    names = {m.id: m.display_name for m in config.metrics}
    rows = []
    for column in columns:
        f = config.export_template.formula(column)
        formula = f'<code class="bpe-code">{escape(readable_formula(f.formula))}</code>' if f else "—"
        compare = names.get(f.compare_with, f.compare_with) if f and f.compare_with else "—"
        rows.append(f"<tr><td><b>{escape(column)}</b></td><td>{formula}</td><td>{escape(f.unit) if f else ''}</td>"
                    f"<td>{escape(compare)}</td></tr>")
    if not rows:
        rows.append('<tr><td colspan="4" class="bpe-empty">No calculated column matches.</td></tr>')
    return ('<table class="bpe-tpltable"><thead><tr><th>Calculated column</th><th>Formula (draft)</th><th>Unit</th>'
            '<th>Compared with</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")
