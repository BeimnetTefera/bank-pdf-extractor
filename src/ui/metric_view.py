"""Metric table, metric details and validation log for the Extract & Review screen."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pandas as pd
import streamlit as st

from src.models import AnalysisResult, Finding, MetricCandidate, MetricResult
from src.pdf_extractor import NARRATIVE_METHOD, RawCandidate

STATUS_LABELS = {
    "needs_review": ("Needs review", "orange", "🟠"),
    "flagged": ("Flagged", "red", "🔴"),
    "not_found": ("Not found", "gray", "⚪"),
}
PERIOD_LABELS = {"current": "this year", "prior": "prior year", "other": "other year", "unknown": "year not identified"}
SEVERITY_ICONS = {"error": ":material/error:", "warning": ":material/warning:", "info": ":material/info:"}
_MARKDOWN_SPECIAL = set("\\`*_{}[]()#+-.!|<>~$")
PHASE4 = "Saving review decisions arrives in Phase 4."


def esc(text: str) -> str:
    """Escape Markdown so text from the PDF is shown literally."""
    return "".join(f"\\{ch}" if ch in _MARKDOWN_SPECIAL else ch for ch in text)


def unit_label(mc: MetricCandidate | None) -> str:
    if mc is None or mc.normalized.unit is None:
        return "?"
    if mc.normalized.unit == "fraction":
        return "%"
    if mc.normalized.unit == "count":
        return "count"
    return {Decimal(1_000): "ETB '000", Decimal(1_000_000): "ETB m", Decimal(1_000_000_000): "ETB bn"}.get(
        mc.mapped.unit.scale, "ETB"
    )


def normalized_text(mc: MetricCandidate) -> str:
    n = mc.normalized
    if n.value is None:
        return f"Not converted: {n.issue}"
    if n.unit == "fraction":
        return f"= {n.value.normalize():f} as a fraction ({(n.value * 100).normalize():f}%)"
    if n.unit == "count":
        return f"= {n.value:,} (count)"
    return f"= {n.value:,} ETB"


# --- table -------------------------------------------------------------------------

def metric_table(analysis: AnalysisResult, table_key: str) -> str | None:
    """Show one row per metric; return the metric id of the selected row, if any."""
    counts = {status: sum(m.status == status for m in analysis.metrics) for status in STATUS_LABELS}
    st.markdown(
        f"**Extracted Metrics ({len(analysis.metrics)})** · "
        f"{counts['needs_review']} needs review · {counts['flagged']} flagged · {counts['not_found']} not found"
    )
    rows = []
    for m in analysis.metrics:
        label, _, dot = STATUS_LABELS[m.status]
        primary = m.primary
        rows.append({
            "Metric": m.metric.display_name,
            "Value": primary.mapped.candidate.raw_value_text if primary else "—",
            "Unit": unit_label(primary) if primary else "",
            "Status": f"{dot} {label}",
        })
    event = st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        height=620,
        on_select="rerun",
        selection_mode="single-row",
        key=table_key,
        column_config={
            "Metric": st.column_config.TextColumn(width=150),
            "Value": st.column_config.TextColumn(width=95),
            "Unit": st.column_config.TextColumn(width=60),
            "Status": st.column_config.TextColumn(width=105),
        },
    )
    picked = event.selection.rows if event is not None else []
    return analysis.metrics[picked[0]].metric.id if picked and picked[0] < len(analysis.metrics) else None


# --- details -----------------------------------------------------------------------

def metric_details(result: MetricResult, shown: MetricCandidate | None, on_view: Callable[[RawCandidate], None], on_show_page: Callable[[int], None]) -> None:
    label, color, _ = STATUS_LABELS[result.status]
    st.badge(label, color=color)
    st.markdown(f"#### {esc(result.metric.display_name)}")

    if shown is None:
        st.caption("No figure was found in this report. This is not the same as zero.")
        _findings(result.findings)
        with st.expander("Definition"):
            st.write(result.metric.definition)
        st.button("Enter manually", icon=":material/edit:", disabled=True, width="stretch",
                  help="Manual entry with a source page arrives with review saving (Phase 4–5).", key="manual_entry")
        return

    c = shown.mapped.candidate
    if shown is not result.primary:
        st.caption("Showing an alternative candidate (not the suggested one).")
    st.markdown(f"## {esc(c.raw_value_text)}")
    st.caption(esc(normalized_text(shown)))

    year = shown.mapped.period_year
    period = PERIOD_LABELS[shown.mapped.period] + (f" (column {year})" if year else "")
    _field("Source", f"PDF page {c.page_index + 1} · {period}")
    if c.extraction_method == NARRATIVE_METHOD:
        _field("Sentence", f"…{c.text_before} [{c.raw_value_text}] {c.text_after}…")
    else:
        _field("Original label", c.raw_label)
    match = next(m for m in shown.mapped.matches if m.metric_id == result.metric.id)
    _field("Matched by", f"{match.basis}: “{match.matched_text}”")
    _field("Unit", shown.normalized.basis or shown.mapped.unit.source or "not identified")

    own = [f for f in result.findings if shown.mapped.key in f.candidate_keys and f.rule_id != "CONFLICT"]
    metric_level = [f for f in result.findings if f.rule_id in ("CONFLICT", "NOT-FOUND")]
    _findings(own + metric_level)

    others = [mc for mc in result.candidates if mc is not shown]
    if others:
        st.caption(f"Other candidates ({len(others)})")
        for i, mc in enumerate(others[:12]):
            oc = mc.mapped.candidate
            kind = "sentence" if oc.extraction_method == NARRATIVE_METHOD else "table"
            text_col, button_col = st.columns([4, 1], vertical_alignment="center")
            text_col.markdown(f"p{oc.page_index + 1} · `{oc.raw_value_text}` · {PERIOD_LABELS[mc.mapped.period]} · {kind}")
            button_col.button("", icon=":material/visibility:", key=f"view_alt_{i}", help="Show this candidate",
                              on_click=on_view, args=(oc,))
        if len(others) > 12:
            st.caption(f"…and {len(others) - 12} more.")

    with st.expander("Surrounding text"):
        st.text(c.context_text)
    with st.expander("Definition"):
        st.write(result.metric.definition)

    st.button("Show on page", icon=":material/description:", width="stretch",
              on_click=on_show_page, args=(c.page_index + 1,), key="show_on_page")
    a, r, k = st.columns(3)
    a.button("Approve", icon=":material/check:", type="primary", disabled=True, help=PHASE4, width="stretch")
    r.button("Reject", icon=":material/close:", disabled=True, help=PHASE4, width="stretch")
    k.button("Correct", icon=":material/edit:", disabled=True, width="stretch",
             help="Enter the right value, unit and a reason; the extracted value is kept. Arrives with Phase 4.")


def _findings(findings: list[Finding]) -> None:
    for f in sorted(findings, key=lambda f: ("error", "warning", "info").index(f.severity)):
        icon = SEVERITY_ICONS[f.severity]
        if f.severity == "info":
            st.caption(f"{icon} {esc(f.message)}")
        else:
            st.warning(esc(f.message), icon=icon)


def _field(name: str, value: str) -> None:
    st.caption(name)
    st.markdown(esc(value))


# --- validation log ----------------------------------------------------------------

def validation_log(analysis: AnalysisResult, extraction_messages: list[str], period_known: bool) -> None:
    if not period_known:
        st.warning("Enter the **Report year** (e.g. 2025/26) so this year's column can be told apart from last year's.",
                   icon=":material/calendar_month:")
    warnings = [f for f in analysis.findings if f.severity in ("error", "warning")]
    infos = [f for f in analysis.findings if f.severity == "info"]
    st.markdown(f"**Checks needing attention ({len(warnings)})**")
    names = {m.metric.id: m.metric.display_name for m in analysis.metrics}
    for f in warnings:
        st.markdown(f"- **{esc(names.get(f.metric_id, ''))}** · `{f.rule_id}` · {esc(f.message)}")
    with st.expander(f"Information notes ({len(infos)})"):
        for f in infos:
            st.markdown(f"- **{esc(names.get(f.metric_id, ''))}** · `{f.rule_id}` · {esc(f.message)}")
    with st.expander("Extraction notes"):
        for message in extraction_messages:
            st.markdown(f"- {esc(message)}")
    st.caption("Checks point out things to look at; they never change a figure.")
