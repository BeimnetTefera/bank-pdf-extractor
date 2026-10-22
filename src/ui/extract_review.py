"""Extract & Review screen. Layout follows docs/ui-mockup.png.

Current scope: read-only. The PDF is held in memory for this session only, and review
actions are shown but disabled until persistence exists (Phase 4).

Selection state (st.session_state):
  selected_key     candidate shown in the viewer and details (a CandidateKey)
  selected_metric  metric whose details are shown, when detail_mode == "metric"
  detail_mode      "metric" (picked in the metric table) or "figure" (picked in All Figures)
"""

from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st

from src.analysis import analyse, report_end_year
from src.candidate_queries import (
    candidate_key,
    display_label,
    filter_candidates,
    find_by_key,
    same_label_elsewhere,
    same_value_elsewhere,
)
from src.config_loader import AppConfig
from src.document_manager import DocumentError, compute_sha256, validate_pdf
from src.models import AnalysisResult
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, ExtractionResult, RawCandidate, extract_candidates
from src.pdf_render import focus_of, render_page_png
from src.ui import metric_view

METHOD_FILTERS = {"All": None, "Table rows": TABLE_METHOD, "Sentences": NARRATIVE_METHOD}
SHORT_METHOD_LABELS = {TABLE_METHOD: "Table", NARRATIVE_METHOD: "Text"}
ZOOM_LEVELS = {"100%": 1.0, "150%": 1.5, "200%": 2.0, "300%": 3.0, "400%": 4.0}
BASE_RESOLUTION = 1.5


@st.cache_data(show_spinner=False, max_entries=3)
def _extract(sha256: str, _pdf_bytes: bytes) -> ExtractionResult:
    return extract_candidates(_pdf_bytes)


@st.cache_data(show_spinner=False, max_entries=10)
def _analyse(sha256: str, period: str, metric_ids: tuple[str, ...], config_fingerprint: str,
             _extraction: ExtractionResult, _config: AppConfig) -> AnalysisResult:
    return analyse(_extraction, _config, list(metric_ids), period)


@st.cache_data(show_spinner=False, max_entries=200)
def _render(sha256: str, page_index: int, highlight: tuple | None, zoom: float, view_zoom: float,
            focus: tuple[float, float], _pdf_bytes: bytes) -> bytes:
    return render_page_png(_pdf_bytes, page_index, highlight, zoom=zoom, view_zoom=view_zoom, focus=focus)


def render(config: AppConfig) -> None:
    st.markdown("## Extract & Review")
    st.caption("Review the extracted figures, check each against its source page, and (from Phase 4) approve the final values.")

    doc = _document_bar(config)
    if doc is None:
        st.info(
            "Upload an annual report to start. The PDF is read on this computer only and is not "
            "saved yet. Every figure shown is an unverified candidate until a reviewer approves it."
        )
        return

    with st.spinner("Reading the PDF (first time only)..."):
        extraction = _extract(doc["sha"], doc["bytes"])
    metric_ids = tuple(st.session_state.metric_selection)
    fingerprint = hashlib.sha256(repr((config.metrics, config.mappings)).encode()).hexdigest()
    analysis = _analyse(doc["sha"], st.session_state.period, metric_ids, fingerprint, extraction, config)

    viewer_col, table_col, detail_col = st.columns([9, 10, 6], gap="small")
    # Fill the table first so a newly selected row moves the viewer in the same run.
    with table_col:
        _middle_panel(extraction, analysis, doc["sha"])
    selected = find_by_key(extraction.candidates, st.session_state.get("selected_key"))
    with viewer_col:
        _pdf_viewer(doc, extraction, selected)
    with detail_col:
        _details_panel(extraction, analysis, selected)


# --- top bar -------------------------------------------------------------------

def _document_bar(config: AppConfig) -> dict | None:
    # Keep values when switching pages (widget state is dropped for widgets not shown).
    for key, default in (("bank", ""), ("period", ""), ("metric_selection", [m.id for m in config.active_metrics()])):
        st.session_state[key] = st.session_state.get(key, default)
    doc = st.session_state.get("doc")
    names = {m.id: m.display_name for m in config.metrics}

    with st.container(border=True):
        bank_col, period_col, metrics_col, status_col, file_col = st.columns([3, 2, 2, 2, 3], vertical_alignment="bottom")
        bank_col.text_input("Bank", key="bank", placeholder="e.g. Bank of Abyssinia")
        period_col.text_input("Report year", key="period", placeholder="e.g. 2025/26")
        with metrics_col.popover(f"Metrics ({len(st.session_state.metric_selection)})", icon=":material/checklist:", width="stretch"):
            st.multiselect(
                "Metrics to extract", options=[m.id for m in config.active_metrics()], key="metric_selection",
                format_func=lambda metric_id: names[metric_id],
                help="From config/metrics.yaml (active metrics). Inactive ones need a definition first.",
            )
        with status_col:
            if doc is None:
                st.badge("No report loaded", color="gray")
            else:
                st.badge("In review · not saved", color="orange", icon=":material/pending:")
        with file_col:
            if doc is None:
                uploaded = st.file_uploader("Annual report PDF", type=["pdf"], key="upload_main")
            else:
                with st.popover(f"{doc['name'][:38]}", icon=":material/picture_as_pdf:", width="stretch"):
                    uploaded = st.file_uploader("Replace with another PDF", type=["pdf"], key="upload_replace")
        if uploaded is not None:
            _load_upload(uploaded, config)
    return st.session_state.get("doc")


def _load_upload(uploaded, config: AppConfig) -> None:
    current = st.session_state.get("doc")
    if current is not None and current.get("file_id") == uploaded.file_id:
        return
    data = uploaded.getvalue()
    try:
        validate_pdf(data, uploaded.name, config.settings)
    except DocumentError as exc:
        st.error(str(exc))
        return
    sha = compute_sha256(data)
    if current is None or current["sha"] != sha:
        for key in ("selected_key", "selected_metric", "detail_mode", "_picks"):
            st.session_state.pop(key, None)
        st.session_state.viewer_page = 1
    st.session_state.doc = {"name": uploaded.name, "bytes": data, "sha": sha, "file_id": uploaded.file_id}
    st.rerun()


# --- selection ---------------------------------------------------------------------

def _is_new_pick(table: str, pick: object) -> bool:
    """True only when a table's selection changed, so an old selection never overrides a newer one."""
    picks = st.session_state.setdefault("_picks", {})
    if pick is None:
        picks[table] = None  # deselected: picking the same row again counts as new
        return False
    if picks.get(table) == pick:
        return False
    picks[table] = pick
    return True


def _show_candidate(candidate: RawCandidate, extraction: ExtractionResult) -> None:
    st.session_state.selected_key = candidate_key(candidate)
    st.session_state.viewer_page = candidate.page_index + 1
    _centre_on(candidate, extraction)  # the zoom level the user chose is kept


# --- middle: tables ----------------------------------------------------------------

def _middle_panel(extraction: ExtractionResult, analysis: AnalysisResult, sha: str) -> None:
    with st.container(border=True):
        metrics_tab, figures_tab, log_tab = st.tabs(["Extracted Data", "All Figures", "Validation Log"])
        with metrics_tab:
            metric_id = metric_view.metric_table(analysis, f"metrics_{sha[:12]}_{len(analysis.metrics)}")
            if _is_new_pick("metrics", metric_id):
                st.session_state.selected_metric = metric_id
                st.session_state.detail_mode = "metric"
                primary = analysis.metric(metric_id).primary
                if primary is not None:
                    _show_candidate(primary.mapped.candidate, extraction)
        with figures_tab:
            candidate = _figures_table(extraction, sha)
            if candidate is not None and _is_new_pick("figures", candidate_key(candidate)):
                st.session_state.detail_mode = "figure"
                _show_candidate(candidate, extraction)
        with log_tab:
            metric_view.validation_log(analysis, extraction.messages, report_end_year(st.session_state.period) is not None)


def _figures_table(extraction: ExtractionResult, sha: str) -> RawCandidate | None:
    search_col, filter_col = st.columns([4, 1], vertical_alignment="bottom")
    search = search_col.text_input("Search", placeholder="Search labels and nearby words, e.g. branches",
                                   label_visibility="collapsed")
    with filter_col.popover("", icon=":material/filter_list:", help="Filters", width="stretch"):
        method_name = st.radio("Found by", list(METHOD_FILTERS), key="method_filter")
        page = st.number_input("PDF page (0 = all)", min_value=0, max_value=extraction.page_count, key="page_filter")
        hide_unlabelled = st.checkbox("Hide table numbers without a label", value=True, key="hide_unlabelled",
                                      help="Hides page numbers, year headers and similar. Nothing is deleted.")

    shown = filter_candidates(extraction.candidates, search=search, method=METHOD_FILTERS[method_name],
                              page_index=page - 1 if page else None)
    if hide_unlabelled:
        shown = [c for c in shown if c.extraction_method != TABLE_METHOD or c.raw_label]
    st.markdown(f"**All figures in the PDF ({len(shown):,}** of {len(extraction.candidates):,}) · raw, including unmapped")
    event = st.dataframe(
        pd.DataFrame({
            "Figure": [display_label(c) for c in shown],
            "Value": [c.raw_value_text for c in shown],
            "Page": [c.page_index + 1 for c in shown],
            "Type": [SHORT_METHOD_LABELS[c.extraction_method] for c in shown],
        }),
        hide_index=True, width="stretch", height=560, on_select="rerun", selection_mode="single-row",
        # A new key when the filters change resets the selection to match the new rows.
        key=f"figures_{sha[:12]}_{hash((search, method_name, page, hide_unlabelled))}",
        column_config={
            "Figure": st.column_config.TextColumn(width=200),
            "Value": st.column_config.TextColumn(width=95),
            "Page": st.column_config.NumberColumn(width=45, format="%d"),
            "Type": st.column_config.TextColumn(width=55, help="Table row or sentence text"),
        },
    )
    rows = event.selection.rows if event is not None else []
    return shown[rows[0]] if rows and rows[0] < len(shown) else None


# --- left: PDF viewer --------------------------------------------------------------

def _go_to(page: int) -> None:
    st.session_state.viewer_page = page


def _centre_on(candidate: RawCandidate, extraction: ExtractionResult) -> None:
    page = extraction.pages[candidate.page_index]
    fx, fy = focus_of(candidate.bbox, page.width, page.height)
    st.session_state.pan_x = round(fx * 100)
    st.session_state.pan_y = round(fy * 100)


def _pdf_viewer(doc: dict, extraction: ExtractionResult, selected: RawCandidate | None) -> None:
    page_count = extraction.page_count
    st.session_state.viewer_page = min(max(1, st.session_state.get("viewer_page", 1)), page_count)
    # Keep zoom and position when the sliders are hidden or the user switches pages.
    st.session_state.viewer_zoom = st.session_state.get("viewer_zoom", "100%")
    st.session_state.pan_x = st.session_state.get("pan_x", 50)
    st.session_state.pan_y = st.session_state.get("pan_y", 50)
    page = st.session_state.viewer_page
    on_this_page = selected is not None and selected.page_index == page - 1

    with st.container(border=True):
        st.markdown("**PDF Viewer**")
        st.caption(doc["name"])
        prev_col, page_col, next_col, zoom_col = st.columns([1, 2, 1, 4], vertical_alignment="center")
        prev_col.button("", icon=":material/chevron_left:", on_click=_go_to, args=(page - 1,), disabled=page <= 1, key="prev_page")
        page_col.number_input("Page", min_value=1, max_value=page_count, key="viewer_page", label_visibility="collapsed")
        next_col.button("", icon=":material/chevron_right:", on_click=_go_to, args=(page + 1,), disabled=page >= page_count, key="next_page")
        zoom_label = zoom_col.select_slider("Zoom", options=list(ZOOM_LEVELS), key="viewer_zoom", label_visibility="collapsed")
        view_zoom = ZOOM_LEVELS[zoom_label]

        if view_zoom > 1:
            x_col, y_col, centre_col = st.columns([3, 3, 2], vertical_alignment="bottom")
            x_col.slider("Left ↔ Right", 0, 100, key="pan_x", format="%d%%")
            y_col.slider("Top ↕ Bottom", 0, 100, key="pan_y", format="%d%%")
            centre_col.button(
                "Figure", icon=":material/center_focus_strong:", key="centre_on_figure",
                on_click=_centre_on, args=(selected, extraction), disabled=not on_this_page,
                help="Centre the view on the selected figure", width="stretch",
            )
        st.caption(f"Page {page} of {page_count} · zoom {zoom_label}")

        thumbs_col, page_view_col = st.columns([1, 5])
        with thumbs_col:
            for p in range(max(1, page - 1), min(page_count, page + 1) + 1):
                st.image(_render(doc["sha"], p - 1, None, 0.25, 1.0, (0.5, 0.5), doc["bytes"]), width="stretch")
                st.button(f"{p}", key=f"thumb_{p}", on_click=_go_to, args=(p,), type="primary" if p == page else "secondary", width="stretch")
        with page_view_col:
            highlight = selected.bbox if on_this_page else None
            focus = (st.session_state.pan_x / 100, st.session_state.pan_y / 100)
            png = _render(doc["sha"], page - 1, highlight, BASE_RESOLUTION, view_zoom, focus, doc["bytes"])
            with st.container(height=640, border=False):
                st.image(png, width="stretch")


# --- right: details ----------------------------------------------------------------

def _details_panel(extraction: ExtractionResult, analysis: AnalysisResult, selected: RawCandidate | None) -> None:
    with st.container(border=True):
        st.markdown("**Metric Details**" if st.session_state.get("detail_mode") == "metric" else "**Figure Details**")
        metric_id = st.session_state.get("selected_metric")
        if st.session_state.get("detail_mode") == "metric" and metric_id in {m.metric.id for m in analysis.metrics}:
            result = analysis.metric(metric_id)
            key = st.session_state.get("selected_key")
            shown = next((mc for mc in result.candidates if mc.mapped.key == key), result.primary)
            metric_view.metric_details(
                result, shown,
                on_view=lambda c: _show_candidate(c, extraction),
                on_show_page=_go_to,
            )
        elif selected is not None:
            _figure_details(extraction, selected)
        else:
            st.caption("Select a metric (or a figure under All Figures) to see its source and details.")


def _figure_details(extraction: ExtractionResult, selected: RawCandidate) -> None:
    st.badge("Raw figure · not matched to a metric here", color="gray")
    if selected.extraction_method == TABLE_METHOD:
        st.markdown(f"#### {metric_view.esc(selected.raw_label or '(no label)')}")
    st.markdown(f"## {metric_view.esc(selected.raw_value_text)}")
    _field("Source", f"PDF page {selected.page_index + 1} (printed page number not detected)")
    if selected.extraction_method == TABLE_METHOD:
        _field("Original row", selected.row_text)
        _field("Extraction method", f"Table row · value {selected.column_index + 1} of {selected.row_value_count} in the row")
    else:
        _field("Sentence", f"…{selected.text_before} [{selected.raw_value_text}] {selected.text_after}…")
        _field("Extraction method", "Sentence (number with nearby words)")

    same_label = same_label_elsewhere(extraction.candidates, selected)
    if same_label:
        st.caption("Same label on other pages")
        for c in same_label:
            note = "same value" if c.raw_value_text == selected.raw_value_text else "**different value**"
            st.markdown(f"- p{c.page_index + 1}: `{c.raw_value_text}` ({note})")
    same_value = same_value_elsewhere(extraction.candidates, selected)
    if same_value:
        _field("Same number also on pages", ", ".join(str(p) for p in sorted({c.page_index + 1 for c in same_value})[:12]))
    with st.expander("Surrounding text"):
        st.text(selected.context_text)
    st.button("Show on page", icon=":material/description:", width="stretch",
              on_click=_go_to, args=(selected.page_index + 1,), key="show_on_page")


def _field(name: str, value: str) -> None:
    st.caption(name)
    st.markdown(metric_view.esc(value))
