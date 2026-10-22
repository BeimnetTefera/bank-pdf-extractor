"""Extract & Review screen. Layout follows docs/ui-mockup.png.

Current scope: read-only. The PDF is held in memory for this session only, figures are
raw (not yet matched to metrics), and review actions are shown but disabled until
persistence exists (Phase 4).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

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
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, ExtractionResult, RawCandidate, extract_candidates
from src.pdf_render import focus_of, render_page_png

METHOD_FILTERS = {"All": None, "Table rows": TABLE_METHOD, "Sentences": NARRATIVE_METHOD}
SHORT_METHOD_LABELS = {TABLE_METHOD: "Table", NARRATIVE_METHOD: "Text"}
NOT_REVIEWED = "Not reviewed"
_MARKDOWN_SPECIAL = set("\\`*_{}[]()#+-.!|<>~$")


@st.cache_data(show_spinner=False, max_entries=3)
def _extract(sha256: str, _pdf_bytes: bytes) -> ExtractionResult:
    return extract_candidates(_pdf_bytes)


@st.cache_data(show_spinner=False, max_entries=200)
def _render(
    sha256: str,
    page_index: int,
    highlight: tuple | None,
    zoom: float,
    view_zoom: float,
    focus: tuple[float, float],
    _pdf_bytes: bytes,
) -> bytes:
    return render_page_png(_pdf_bytes, page_index, highlight, zoom=zoom, view_zoom=view_zoom, focus=focus)


def render(config: AppConfig) -> None:
    st.markdown("## Extract & Review")
    st.caption("Check each extracted figure against its source page in the PDF.")

    doc = _document_bar(config)
    if doc is None:
        st.info(
            "Upload an annual report to start. The PDF is read on this computer only and is not "
            "saved yet. Figures shown are raw text from the PDF: unverified and not yet matched to metrics."
        )
        return

    with st.spinner("Reading the PDF (first time only)..."):
        result = _extract(doc["sha"], doc["bytes"])

    viewer_col, table_col, detail_col = st.columns([5, 5, 3], gap="small")
    # Fill the table first so a newly selected row moves the viewer in the same run.
    with table_col:
        _figures_panel(result, doc["sha"])
    selected = find_by_key(result.candidates, st.session_state.get("selected_key"))
    with viewer_col:
        _pdf_viewer(doc, result, selected)
    with detail_col:
        _details_panel(result, selected)


# --- top bar -------------------------------------------------------------------

def _document_bar(config: AppConfig) -> dict | None:
    for key in ("bank", "period"):  # keep typed values when switching pages
        st.session_state[key] = st.session_state.get(key, "")
    doc = st.session_state.get("doc")

    with st.container(border=True):
        bank_col, period_col, status_col, file_col = st.columns([3, 2, 2, 3], vertical_alignment="bottom")
        bank_col.text_input("Bank", key="bank", placeholder="e.g. Bank of Abyssinia")
        period_col.text_input("Report year", key="period", placeholder="e.g. 2025/26")
        with status_col:
            if doc is None:
                st.badge("No report loaded", color="gray")
            else:
                st.badge("Preview · not saved", color="orange", icon=":material/visibility:")
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
        st.session_state.selected_key = None
        st.session_state.viewer_page = 1
    st.session_state.doc = {"name": uploaded.name, "bytes": data, "sha": sha, "file_id": uploaded.file_id}
    st.rerun()


# --- middle: figures table ---------------------------------------------------------

def _figures_panel(result: ExtractionResult, sha: str) -> None:
    with st.container(border=True):
        data_tab, log_tab = st.tabs(["Extracted Data", "Validation Log"])
        with data_tab:
            search_col, filter_col = st.columns([4, 1], vertical_alignment="bottom")
            search = search_col.text_input(
                "Search", placeholder="Search labels and nearby words, e.g. branches", label_visibility="collapsed"
            )
            with filter_col.popover("", icon=":material/filter_list:", help="Filters", width="stretch"):
                method_name = st.radio("Found by", list(METHOD_FILTERS), key="method_filter")
                page = st.number_input("PDF page (0 = all)", min_value=0, max_value=result.page_count, key="page_filter")
                hide_unlabelled = st.checkbox(
                    "Hide table numbers without a label", value=True, key="hide_unlabelled",
                    help="Hides page numbers, year headers and similar. Nothing is deleted.",
                )

            shown = filter_candidates(
                result.candidates,
                search=search,
                method=METHOD_FILTERS[method_name],
                page_index=page - 1 if page else None,
            )
            if hide_unlabelled:
                shown = [c for c in shown if c.extraction_method != TABLE_METHOD or c.raw_label]
            st.markdown(f"**Extracted figures ({len(shown):,}** of {len(result.candidates):,})")
            # A Status column returns in Phase 4, when review decisions are saved.
            table = pd.DataFrame(
                {
                    "Figure": [display_label(c) for c in shown],
                    "Value": [c.raw_value_text for c in shown],
                    "Page": [c.page_index + 1 for c in shown],
                    "Type": [SHORT_METHOD_LABELS[c.extraction_method] for c in shown],
                }
            )
            event = st.dataframe(
                table,
                hide_index=True,
                width="stretch",
                height=620,
                on_select="rerun",
                selection_mode="single-row",
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
            if rows and rows[0] < len(shown):
                _select(shown[rows[0]], result)

        with log_tab:
            st.markdown("**Extraction notes**")
            for message in result.messages:
                st.write(f"- {message}")
            st.info(
                "Automatic checks (same metric with different values on different pages, unit "
                "mismatches, metrics not found) are added in Phase 3.",
                icon=":material/schedule:",
            )


def _select(candidate: RawCandidate, result: ExtractionResult) -> None:
    key = candidate_key(candidate)
    if st.session_state.get("selected_key") != key:
        st.session_state.selected_key = key
        st.session_state.viewer_page = candidate.page_index + 1
        _centre_on(candidate, result)  # the zoom level the user chose is kept


# --- left: PDF viewer --------------------------------------------------------------

ZOOM_LEVELS = {"100%": 1.0, "150%": 1.5, "200%": 2.0, "300%": 3.0, "400%": 4.0}
BASE_RESOLUTION = 1.5


def _go_to(page: int) -> None:
    st.session_state.viewer_page = page


def _centre_on(candidate: RawCandidate, result: ExtractionResult) -> None:
    page = result.pages[candidate.page_index]
    fx, fy = focus_of(candidate.bbox, page.width, page.height)
    st.session_state.pan_x = round(fx * 100)
    st.session_state.pan_y = round(fy * 100)


def _pdf_viewer(doc: dict, result: ExtractionResult, selected: RawCandidate | None) -> None:
    page_count = result.page_count
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
                on_click=_centre_on, args=(selected, result), disabled=not on_this_page,
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

def _details_panel(result: ExtractionResult, selected: RawCandidate | None) -> None:
    with st.container(border=True):
        st.markdown("**Figure Details**")
        if selected is None:
            st.caption("Select a figure in the table to see its source and details.")
            return

        st.badge(NOT_REVIEWED, color="orange", icon=":material/pending:")
        if selected.extraction_method == TABLE_METHOD:
            st.markdown(f"#### {_esc(selected.raw_label or '(no label)')}")
        st.markdown(f"## {_esc(selected.raw_value_text)}")
        st.caption("Unit: not detected yet (Phase 3)")

        _field("Source", f"PDF page {selected.page_index + 1} (printed page number not detected)")
        if selected.extraction_method == TABLE_METHOD:
            _field("Original row", selected.row_text)
            _field("Extraction method", f"Table row · value {selected.column_index + 1} of {selected.row_value_count} in the row")
        else:
            _field("Sentence", f"…{selected.text_before} [{selected.raw_value_text}] {selected.text_after}…")
            _field("Extraction method", "Sentence (number with nearby words)")

        same_label = same_label_elsewhere(result.candidates, selected)
        if same_label:
            st.caption("Same label on other pages")
            for c in same_label:
                note = "same value" if c.raw_value_text == selected.raw_value_text else "**different value**"
                st.markdown(f"- p{c.page_index + 1}: `{c.raw_value_text}` ({note})")
        same_value = same_value_elsewhere(result.candidates, selected)
        if same_value:
            pages = ", ".join(str(p) for p in sorted({c.page_index + 1 for c in same_value})[:12])
            _field("Same number also on pages", pages)

        with st.expander("Surrounding text"):
            st.text(selected.context_text)

        st.button(
            "Show on page", icon=":material/description:", width="stretch",
            on_click=_go_to, args=(selected.page_index + 1,), key="show_on_page",
        )
        approve_col, reject_col = st.columns(2)
        phase4 = "Saving review decisions arrives in Phase 4."
        approve_col.button("Approve", icon=":material/check:", type="primary", disabled=True, help=phase4, width="stretch")
        reject_col.button("Reject", icon=":material/close:", disabled=True, help=phase4, width="stretch")


def _field(name: str, value: str) -> None:
    st.caption(name)
    st.markdown(_esc(value))


def _esc(text: str) -> str:
    """Escape Markdown so text from the PDF is shown literally."""
    return "".join(f"\\{ch}" if ch in _MARKDOWN_SPECIAL else ch for ch in text)
