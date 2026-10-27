"""Sector data page (D-056): figures for the whole banking sector, one per fiscal year, shared by
every bank. Record a value from an external source, approve it, mark it not reported, reopen.
The rules are in src/sector_data.py; this module only collects input and shows the state.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing

import streamlit as st

from src import sector_data as sector
from src.config_loader import AppConfig, MetricDefinition
from src.database import connect
from src.evidence import SectorEvidence, format_value, sector_evidence
from src.review_service import ReviewError
from src.ui import routes, source_form
from src.ui.metric_view import esc

STATUS_COLORS = {"open": "gray", "pending": "blue", "approved": "green", "not_reported": "violet"}
OTHER_YEAR = "Another year..."


def render(config: AppConfig) -> None:
    st.markdown("## Sector data")
    st.caption("Figures for the whole banking sector (e.g. total deposits in the sector), recorded once per fiscal "
               "year from an external source such as the National Bank of Ethiopia. The Excel export puts the "
               "approved value in every bank row of that year.")
    metrics = sector.sector_metrics(config)
    if not metrics:
        st.info("No metric is marked `level: sector` in config/metrics.yaml.", icon=":material/info:")
        return
    year = _year_picker(config)
    if year is None:
        return
    with closing(connect(config.settings.database_path)) as conn:
        evidences = [sector_evidence(conn, config, year, m.id) for m in metrics]
        banks = sector.banks_for_year(conn, year)
    st.caption(f"Bank reports saved for {year}: " + (", ".join(banks) if banks else "none yet"))
    for metric, ev in zip(metrics, evidences):
        with st.container(border=True):
            _metric_block(config, metric, ev)


def _year_picker(config: AppConfig) -> str | None:
    with closing(connect(config.settings.database_path)) as conn:
        years = sector.fiscal_years(conn)
    wanted = st.query_params.get("year")
    options = [*years, OTHER_YEAR]
    index = options.index(wanted) if wanted in options else 0
    choice = st.selectbox("Fiscal year", options, index=index, key="sector_year")
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
    name_col, link_col = st.columns([4, 1], vertical_alignment="center")
    with name_col:
        st.markdown(f"#### {esc(metric.display_name)}")
        st.badge(ev.status_label, color=STATUS_COLORS[ev.status])
    with link_col:
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
        with st.expander("Record a value from an external source", icon=":material/travel_explore:"):
            source = source_form.source_form(key, metric.expected_unit, config.settings, sign=metric.sign)
            note = st.text_area("Note (optional)", key=f"{key}_note", height=70)
            if st.button("Save sector value", type="primary", key=f"{key}_save", width="stretch"):
                _run(config, lambda conn: sector.record_value(conn, config, config.settings, ev.fiscal_year, metric.id,
                                                              source, note, reviewer),
                     "Sector value saved. Approve it to use it.", lambda: source_form.clear_form(key))
        with st.popover("Not reported", icon=":material/do_not_disturb_on:"):
            reason = st.text_area("Why is there no figure for this year?", key=f"{key}_nr_reason", height=80,
                                  placeholder="e.g. NBE has not published the 2025/26 annual report yet.")
            if st.button("Record as not reported", type="primary", key=f"{key}_nr_save"):
                _run(config, lambda conn: sector.record_not_reported(conn, config, ev.fiscal_year, metric.id, reason,
                                                                     reviewer), "Recorded as not reported. Approve it.")
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
