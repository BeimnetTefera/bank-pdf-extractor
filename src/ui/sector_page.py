"""Sector data page (D-056): figures for the whole banking sector, one per fiscal year, shared by
every bank. Record a value from an external source, approve it, mark it not reported, reopen.
The rules are in src/sector_data.py; this module only collects input and shows the state.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from html import escape

import streamlit as st

from src import sector_data as sector
from src.config_loader import AppConfig, MetricDefinition
from src.database import connect
from src.evidence import SectorEvidence, format_value, sector_evidence
from src.review_service import ReviewError
from src.ui import routes, source_form, style
from src.ui.metric_view import esc

STATUS_COLORS = {"open": "gray", "pending": "blue", "approved": "green", "not_reported": "violet"}
OTHER_YEAR = "Another year..."


def render(config: AppConfig) -> None:
    """Sector data, as in the redesign mockup (screen 7, D-092)."""
    title_col, year_col = st.columns([3, 2], vertical_alignment="bottom")
    title_col.markdown("## Sector data")
    title_col.html('<p class="bpe-sub">Figures for the whole banking sector, recorded once per fiscal year and used '
                   "for every bank's row in the export.</p>")
    metrics = sector.sector_metrics(config)
    if not metrics:
        st.info("No metric is marked `level: sector` in config/metrics.yaml.", icon=":material/info:")
        return
    with year_col:
        year = _year_picker(config)
    if year is None:
        return
    with closing(connect(config.settings.database_path)) as conn:
        evidences = [sector_evidence(conn, config, year, m.id) for m in metrics]
        banks = sector.banks_for_year(conn, year)
    form_col, side_col = st.columns([3, 1.3], gap="medium")
    with form_col:
        for metric, ev in zip(metrics, evidences):
            with st.container(key=f"panel_sector_{metric.id}"):
                _metric_block(config, metric, ev)
    with side_col, st.container(key="panel_sector_used"):
        st.markdown(f"**Used in the {year} export by**")
        recorded = all(ev.status == "approved" for ev in evidences)
        for bank in banks:
            st.html(f'<div class="bpe-kv"><span style="color:#15181E">{escape(bank)}</span><span class="bpe-minor">'
                    f'{"filled in" if recorded else "blank until recorded"}</span></div>')
        if not banks:
            st.caption("No bank report saved for this year yet.")
        st.caption("A value saved here needs approval, like any correction. Blank cells in the export mean "
                   "\"not recorded\", never zero.")


def _year_picker(config: AppConfig) -> str | None:
    with closing(connect(config.settings.database_path)) as conn:
        years = sector.fiscal_years(conn)
    wanted = st.query_params.get("year")
    options = [*years, OTHER_YEAR]
    index = options.index(wanted) if wanted in options else 0
    choice = st.segmented_control("Fiscal year", options, default=options[index], key="sector_year",
                                  label_visibility="collapsed") or options[index]
    if choice == OTHER_YEAR:
        typed = st.text_input("Fiscal year (YYYY/YY)", key="sector_year_typed", placeholder="e.g. 2025/26")
        if not typed.strip():
            return None
        try:
            choice = sector.fiscal_year_key(typed)
        except ReviewError as exc:
            st.error(str(exc), icon=":material/error:")
            return None
    if st.query_params.get("year") != choice:
        st.query_params["year"] = choice
    return choice


def _metric_block(config: AppConfig, metric: MetricDefinition, ev: SectorEvidence) -> None:
    kind = {"approved": "ok", "pending": "warn"}.get(ev.status, "muted")
    label = "Not recorded" if ev.status == "open" else ev.status_label
    st.html(f'<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center"><span style="font-size:17px;'
            f'font-weight:600">{escape(metric.display_name)}</span>{style.chip(f"{label} for {ev.fiscal_year}", kind)}'
            f'<span class="bpe-minor" style="margin-left:auto">Excel column: {escape(metric.export_column or "none")}'
            '</span></div>')
    if ev.status != "open":
        routes.link("evidence", "Evidence", ":material/verified:", sector=ev.fiscal_year, metric=metric.id)
    if ev.status in ("approved", "pending") and ev.proposal is not None and ev.proposal.entry_kind == "external":
        suffix = " (awaiting approval)" if ev.status == "pending" else ""
        st.markdown(f"**{esc(format_value(ev.shown_value, ev.proposal.unit))}**{suffix}")
        if ev.source is not None:
            st.markdown(source_form.source_lines(ev.source))
            st.caption(f"As printed: {ev.source.conversion}")
    if ev.proposal is not None and (ev.status == "not_reported" or ev.proposal.entry_kind == "not_reported"):
        st.markdown(f"**Not reported** for {ev.fiscal_year}" + (" (awaiting approval)" if ev.status == "pending"
                                                               else "") + f". Reason: {esc(ev.proposal.reason)}")
    if ev.approval is not None:
        st.caption(f"Approved by {ev.approval.reviewer} on {ev.approval.created_at[:16].replace('T', ' ')} UTC")

    key = f"sector_{metric.id}_{ev.fiscal_year.replace('/', '_')}"
    reviewer = st.session_state.get("reviewer", "")
    if ev.status == "open":
        source = source_form.source_form(key, metric.expected_unit, config.settings, sign=metric.sign)
        _, cancel_col, save_col = st.columns([4, 1, 1.6])
        cancel_col.button("Cancel", key=f"{key}_cancel", width="stretch", on_click=source_form.clear_form, args=(key,))
        if save_col.button("Save for approval", type="primary", key=f"{key}_save", width="stretch"):
            _run(config, lambda conn: sector.record_value(conn, config, config.settings, ev.fiscal_year, metric.id,
                                                          source, "", reviewer),
                 "Sector value saved. Approve it to use it.", lambda: source_form.clear_form(key))
    elif ev.status == "pending":
        approve_col, withdraw_col = st.columns(2)
        if approve_col.button("Approve", icon=":material/check:", type="primary", key=f"{key}_approve",
                              width="stretch"):
            _run(config, lambda conn: sector.approve(conn, config, ev.fiscal_year, metric.id, reviewer),
                 f"{metric.display_name} for {ev.fiscal_year} approved")
        with withdraw_col:
            _reopen(config, metric, ev, key, reviewer, "Withdraw")
    else:
        _reopen(config, metric, ev, key, reviewer, "Reopen")


def _reopen(config: AppConfig, metric: MetricDefinition, ev: SectorEvidence, key: str, reviewer: str,
            label: str) -> None:
    with st.popover(label, icon=":material/undo:", width="stretch"):
        reason = st.text_area("Reason", key=f"{key}_reopen_reason")
        if st.button("Confirm", type="primary", key=f"{key}_reopen"):
            _run(config, lambda conn: sector.reopen(conn, config, ev.fiscal_year, metric.id, reviewer, reason),
                 f"{metric.display_name} for {ev.fiscal_year} reopened")


def _run(config: AppConfig, action: Callable[[object], object], success: str,
         on_success: Callable[[], None] | None = None) -> None:
    """Run a sector action; show the rule that blocked it, if any."""
    try:
        with closing(connect(config.settings.database_path)) as conn:
            action(conn)
    except ReviewError as exc:
        st.error(str(exc), icon=":material/error:")
        return
    if on_success is not None:
        on_success()
    st.toast(success, icon=":material/check_circle:")
    st.rerun()
