"""Metric table, metric details and validation log for the Extract & Review screen."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd
import streamlit as st

from src.config_loader import MetricDefinition
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


def esc(text: str) -> str:
    """Escape Markdown so text from the PDF is shown literally."""
    return "".join(f"\\{ch}" if ch in _MARKDOWN_SPECIAL else ch for ch in text)


def normalized_text(mc: MetricCandidate) -> str:
    n = mc.normalized
    if n.value is None:
        return f"Not converted: {n.issue}"
    if n.unit == "fraction":
        return f"= {n.value.normalize():f} as a fraction ({(n.value * 100).normalize():f}%)"
    if n.unit == "count":
        return f"= {n.value:,} (count)"
    return f"= {n.value:,} ETB"


PRINTED_SCALES = {Decimal(1): "Birr", Decimal(1_000): "Birr'000", Decimal(1_000_000): "Birr m",
                  Decimal(1_000_000_000): "Birr bn"}


def full_value_text(value: Decimal, unit: str | None) -> str:
    """The value in the metric's unit, as approving records it: '189,512,266,000 ETB', '29%', '10,627'."""
    if unit == "fraction":
        return f"{(value * 100).normalize():f}%"
    return f"{value:,} ETB" if unit == "ETB" else f"{value:,}"


def printed_text(mc: MetricCandidate, expected_unit: str | None = None) -> str:
    """The figure as printed with the printed unit: '189,512,266 Birr'000'. A count or a
    percentage is shown as printed: a page's Birr heading does not apply to it."""
    raw = mc.mapped.candidate.raw_value_text
    if "count" in (expected_unit, mc.normalized.unit) or "fraction" in (expected_unit, mc.normalized.unit):
        return raw
    unit = mc.mapped.unit
    if unit.currency not in (None, "ETB"):
        return f"{raw} {unit.currency}" + ("" if unit.scale == 1 else f" × {unit.scale:,}")
    if unit.currency is None and unit.scale == 1:
        return raw
    return f"{raw} {PRINTED_SCALES.get(unit.scale, f'Birr × {unit.scale:,}')}"


def suggested_value_text(mc: MetricCandidate) -> str:
    n = mc.normalized
    return "not converted" if n.value is None else full_value_text(n.value, n.unit)


# --- table -------------------------------------------------------------------------

REVIEW_LABELS = {"approved": ("Approved", "green", "✅"), "corrected": ("Correction pending", "blue", "✏️"),
                 "not_reported": ("Not reported", "violet", "⊘")}
FILTER_ORDER = ("needs_review", "flagged", "corrected", "not_found", "approved", "not_reported")
PENDING_ROW_STYLE = "background-color: rgba(37, 99, 235, 0.16); font-weight: 600"


@dataclass(frozen=True)
class ReviewDisplay:
    """How a reviewed metric appears in the table (from its saved review state)."""

    status: str  # "approved", "corrected" or "not_reported"
    value_text: str  # the full value in the metric's unit, e.g. "1,234,567,000 ETB"
    printed_text: str = ""  # the figure as printed, e.g. "1,234,567 Birr'000", or where the value came from


def table_statuses(analysis: AnalysisResult, reviews: dict[str, ReviewDisplay]) -> dict[str, str]:
    """metric id -> its status in the table: the review state when reviewed, else the extraction status."""
    return {m.metric.id: reviews[m.metric.id].status if m.metric.id in reviews else m.status
            for m in analysis.metrics}


def metric_table(analysis: AnalysisResult, table_key: str,
                 reviews: dict[str, ReviewDisplay] | None = None) -> tuple[str | None, list[str]]:
    """One row per metric, under a status filter. Returns (the metric id of the selected row,
    if any; the metric ids shown, in order)."""
    reviews = reviews or {}
    statuses = table_statuses(analysis, reviews)
    counts = {s: list(statuses.values()).count(s) for s in FILTER_ORDER}
    summary = f"{counts['approved']} approved · " if reviews else ""
    st.markdown(
        f"**Extracted Metrics ({len(analysis.metrics)})** · {summary}"
        f"{counts['needs_review']} needs review · {counts['flagged']} flagged · {counts['not_found']} not found"
    )
    labels = REVIEW_LABELS | STATUS_LABELS
    chosen = st.pills("Show", list(FILTER_ORDER), selection_mode="multi", key="metric_status_filter",
                      format_func=lambda s: f"{labels[s][0]} ({counts[s]})", label_visibility="collapsed",
                      help="Show only metrics with these statuses. None chosen: all metrics.")
    chosen = [s for s in FILTER_ORDER if s in (chosen or [])]
    shown = [m for m in analysis.metrics if not chosen or statuses[m.metric.id] in chosen]
    rows = [_table_row(m, statuses[m.metric.id], reviews.get(m.metric.id)) for m in shown]
    frame = pd.DataFrame(rows, columns=["Metric", "Value", "As printed", "Status"])
    pending = [statuses[m.metric.id] == "corrected" for m in shown]
    styled = frame.style.apply(lambda row: [PENDING_ROW_STYLE if pending[row.name] else ""] * len(row), axis=1)
    event = st.dataframe(
        styled,
        hide_index=True,
        width="stretch",
        height=min(620, 38 + 35 * max(len(rows), 1)),
        on_select="rerun",
        selection_mode="single-row",
        key=f"{table_key}_{'_'.join(chosen)}",  # a new filter starts with no row selected
        column_config={
            "Metric": st.column_config.TextColumn(width=130),
            "Value": st.column_config.TextColumn(width=130, help="The full value in the metric's unit: what "
                                                 "approving records (and what Excel receives)."),
            "As printed": st.column_config.TextColumn(width=110, help="The figure as printed in the report, with "
                                                      "the printed unit. Approving converts it; do not correct it "
                                                      "for the unit."),
            "Status": st.column_config.TextColumn(width=105),
        },
    )
    if not shown:
        st.caption("No metric has the chosen status.")
    picked = event.selection.rows if event is not None else []
    ids = [m.metric.id for m in shown]
    return (ids[picked[0]] if picked and picked[0] < len(ids) else None), ids


def _table_row(m: MetricResult, status: str, reviewed: ReviewDisplay | None) -> dict[str, str]:
    label, _, dot = (REVIEW_LABELS | STATUS_LABELS)[status]
    primary = m.primary
    if reviewed is not None:
        value, printed = reviewed.value_text, reviewed.printed_text
    elif primary is not None:
        value, printed = suggested_value_text(primary), printed_text(primary, m.metric.expected_unit)
    else:
        value, printed = "—", ""
    return {"Metric": m.metric.display_name, "Value": value, "As printed": printed, "Status": f"{dot} {label}"}


# --- details -----------------------------------------------------------------------

def metric_details(result: MetricResult, shown: MetricCandidate | None, on_view: Callable[[RawCandidate], None],
                   on_show_page: Callable[[int], None], review_label: tuple[str, str] | None = None,
                   rejected_keys: frozenset = frozenset(), review_controls: Callable[[], None] | None = None) -> None:
    """review_controls, if given, is drawn right after the figure's checks, before alternatives."""
    label, color, _ = STATUS_LABELS[result.status]
    if review_label is not None:
        label, color = review_label
    st.badge(label, color=color)
    st.markdown(f"#### {esc(result.metric.display_name)}")

    if shown is None:
        st.caption("No figure was found in this report. This is not the same as zero.")
        _findings(result.findings)
        if review_controls is not None:
            review_controls()
        about_metric(result.metric)
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
    if review_controls is not None:
        review_controls()

    others = [mc for mc in result.candidates if mc is not shown]
    if others:
        st.caption(f"Other candidates ({len(others)})")
        for i, mc in enumerate(others[:12]):
            oc = mc.mapped.candidate
            kind = "sentence" if oc.extraction_method == NARRATIVE_METHOD else "table"
            mark = " · ~~rejected~~" if mc.mapped.key in rejected_keys else ""
            text_col, button_col = st.columns([4, 1], vertical_alignment="center")
            text_col.markdown(f"p{oc.page_index + 1} · `{oc.raw_value_text}` · {PERIOD_LABELS[mc.mapped.period]} · {kind}{mark}")
            button_col.button("", icon=":material/visibility:", key=f"view_alt_{i}", help="Show this candidate",
                              on_click=on_view, args=(oc,))
        if len(others) > 12:
            st.caption(f"…and {len(others) - 12} more.")

    with st.expander("Surrounding text"):
        st.text(c.context_text)
    about_metric(result.metric)

    st.button("Show on page", icon=":material/description:", width="stretch",
              on_click=on_show_page, args=(c.page_index + 1,), key="show_on_page")


UNIT_NAMES = {"ETB": "Birr (full amount, e.g. 1,234,567,000)", "count": "a count (e.g. branches)",
              "fraction": "a percentage (saved as a fraction: 29% = 0.29)"}


def about_metric(metric: MetricDefinition) -> None:
    """What the metric means and where it goes: definition, unit, Excel column, formula and basis."""
    with st.expander("About this metric", icon=":material/info:"):
        st.markdown(esc(metric.definition))
        _field("Unit", UNIT_NAMES.get(metric.expected_unit, metric.expected_unit))
        _field("Excel column", metric.export_column or "none (shown on the Source Evidence sheet only)")
        if metric.formula:
            st.caption("Formula")
            st.code(metric.formula, language=None, wrap_lines=True)
        if metric.basis:
            _field("Basis", metric.basis)
        if metric.notes:
            _field("Notes", metric.notes)


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
