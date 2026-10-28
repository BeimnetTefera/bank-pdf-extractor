"""Extract & Review screen. Layout follows docs/ui-mockup.png.

An uploaded PDF is held in memory until "Save report" stores it with its candidates; review
actions and the Excel export work only on a saved report.

Selection state (st.session_state):
  selected_key     candidate shown in the viewer and details (a CandidateKey)
  selected_metric  metric whose details are shown, when detail_mode == "metric"
  detail_mode      "metric" (picked in the metric table) or "figure" (picked in All Figures)
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image
from streamlit_image_coordinates import streamlit_image_coordinates

from src import alias_learning, bulk_review, cross_checks, ocr, page_marks, review_service, value_input
from src.analysis import analyse, column_year, report_end_year
from src.candidate_queries import (
    NO_YEAR,
    CandidateKey,
    candidate_key,
    context_line,
    display_label,
    filter_candidates,
    find_by_key,
    page_of_rows,
    same_label_elsewhere,
    same_value_elsewhere,
)
from src.config_loader import AppConfig
from src.database import connect
from src.document_manager import DocumentError, compute_sha256, store_document, validate_pdf
from src.models import AnalysisResult
from src.number_parser import parse_number
from src.pdf_extractor import (
    EXTRACTOR_VERSION,
    NARRATIVE_METHOD,
    TABLE_METHOD,
    ExtractionResult,
    RawCandidate,
    extract_candidates,
)
from src.pdf_render import focus_of, render_band_png, render_page_png, screen_box_to_page
from src.project_service import (
    NewFigures,
    find_documents_by_hash,
    get_document,
    load_candidates,
    new_figures,
    save_analysis,
    saved_candidate_ids,
)
from src.report_period import canonical_period, suggested_period
from src.review_service import ReviewError, ReviewState
from src.sector_data import without_sector_metrics
from src.calculated_entries import number_text, record_input_figure, saved_calculation
from src.evidence import NoteCalculation, PdfWords, typed_calculation
from src.excel_exporter import fiscal_year_label
from src.ui import calc_panel, export_panel, metric_view, review_panel, style
from src.ui.review_panel import ReviewContext
from src.unit_normalizer import Normalized

METHOD_FILTERS = {"All": None, "Table rows": TABLE_METHOD, "Sentences": NARRATIVE_METHOD}
SHORT_METHOD_LABELS = {TABLE_METHOD: "Table", NARRATIVE_METHOD: "Text"}
ZOOM_LEVELS = {"100%": 1.0, "150%": 1.5, "200%": 2.0, "300%": 3.0, "400%": 4.0}
BASE_RESOLUTION = 1.5
FULL_SCREEN_RESOLUTION = 2.5
AUTO_ZOOM = "200%"  # selecting a figure or metric zooms in on it, until the user picks a zoom level
VIEW_HEIGHT = 640  # pixels of page shown in the viewer before it scrolls
FIT_WIDTH, FIT_PAGE = "Fit width", "Fit page"
# Alt + arrow keys step through the metrics; plain letters would fire while typing a note.
PREVIOUS_SHORTCUT, NEXT_SHORTCUT = "Alt+Up", "Alt+Down"


@st.cache_data(show_spinner=False, max_entries=3)
def _extract(sha256: str, ocr_pages: tuple[int, ...], _pdf_bytes: bytes, _ocr_words: dict,
             ocr_engine: str) -> ExtractionResult:
    return extract_candidates(_pdf_bytes, _ocr_words, ocr_engine)


@st.cache_data(show_spinner=False, max_entries=10)
def _pages_needing_ocr(sha256: str, _pdf_bytes: bytes) -> list[int]:
    return ocr.pages_needing_ocr(_pdf_bytes)


@st.cache_resource(show_spinner=False)
def _ocr_engine(configured_dir: str | None) -> ocr.OcrEngine | None:
    return ocr.find_engine(Path(configured_dir) if configured_dir else None)


@st.cache_data(show_spinner=False, max_entries=10)
def _analyse(sha256: str, period: str, bank: str, metric_ids: tuple[str, ...], config_fingerprint: str,
             _extraction: ExtractionResult, _config: AppConfig) -> AnalysisResult:
    return analyse(_extraction, _config, list(metric_ids), period, bank or None)


@st.cache_data(show_spinner=False, max_entries=200)
def _render(sha256: str, page_index: int, highlight: tuple | None, zoom: float, view_zoom: float,
            focus: tuple[float, float], boxes: tuple, _pdf_bytes: bytes) -> bytes:
    return render_page_png(_pdf_bytes, page_index, highlight, zoom=zoom, view_zoom=view_zoom, focus=focus, boxes=boxes)


def render(config: AppConfig, open_calculations: Callable[[str], None] | None = None) -> None:
    style.wide_page()  # the review needs the whole width: queue, page and decision side by side (D-091)
    title_slot, progress_slot, tools_slot = st.columns([5, 2.2, 1.6], vertical_alignment="bottom")
    doc = _document_bar(config)
    saved = bool(doc and doc.get("document_id"))
    with title_slot:
        if saved:
            st.markdown(f"## {metric_view.esc(st.session_state.bank or '')} · {_report_year_label()}")
        else:
            st.markdown("## Extract & Review")
            st.caption("Review the extracted figures, check each against its source page, and approve the final values.")
    if doc is None:
        st.info(
            "Upload an annual report to start. The PDF is read on this computer only and is not "
            "saved yet. Every figure shown is an unverified candidate until a reviewer approves it."
        )
        return

    ocr_words, engine = _ocr_step(config, doc, tools_slot)
    with st.spinner("Reading the PDF (first time only)..."):
        extraction = _extract(doc["sha"], tuple(sorted(ocr_words)), doc["bytes"], ocr_words,
                              engine.version if engine else "")
    if saved:
        title_slot.caption(f"{doc['name']} · {extraction.page_count} pages")
    metric_ids = tuple(st.session_state.metric_selection)
    fingerprint = hashlib.sha256(repr((config.metrics, config.mappings, config.learned_aliases)).encode()).hexdigest()
    analysis = _analyse(doc["sha"], st.session_state.period, st.session_state.bank or "", metric_ids, fingerprint,
                        extraction, config)

    if st.session_state.pop("save_requested", False):
        _save_report(config, doc, analysis, extraction)
    _duplicate_prompt(config, doc, analysis, extraction)
    _reopened_notice(config, doc, analysis, extraction)

    ctx, states = _review_context(config, doc, analysis, extraction)
    if ctx is not None:
        approved = sum(s.status in ("approved", "not_reported") for s in states.values())
        progress_slot.progress(approved / len(states) if states else 0.0,
                               text=f"{approved} of {len(states)} approved or not reported")
    marked_values = _marked_values(states)
    calc_boxes = _calculation_boxes(ctx, states)
    if ctx is not None:
        ctx = replace(ctx, note_calcs={metric_id: _named_inputs(extraction, calc)
                                       for metric_id, calc in _note_calculations(doc, states).items()})
        for metric_id, calc in ctx.note_calcs.items():  # its box holds one input, not the value
            marked_values.pop(metric_id, None)
            inputs = [(f.page - 1, tuple(f.box), f.label or "input") for f in calc.figures if f.page and f.box]
            if inputs:
                calc_boxes[metric_id] = inputs
    for metric_id, inputs in calc_boxes.items():  # a calculated metric shows its first input when picked
        marked_values.setdefault(metric_id, (inputs[0][0] + 1, inputs[0][1]))
    opened = st.session_state.pop("open_metric", None)  # Open next to a metric on Bank Reports
    if opened in {m.metric.id for m in analysis.metrics}:
        _select_metric(opened, analysis, extraction, marked_values, ctx)
    # Lets a figure be recorded before the report is saved: one press saves it and records the value.
    store = None if ctx is not None else (lambda: _store_report(config, doc, analysis, extraction))
    with closing(connect(config.settings.database_path)) as conn:
        check_results = cross_checks.run_checks(conn, config.checks, analysis, states,
                                                ctx.candidate_ids if ctx else {}, st.session_state.bank or "",
                                                st.session_state.period or "")
    # The queue of metrics, the page, the decision: left to right, as in the redesign (D-091).
    table_col, viewer_col, detail_col = st.columns([8, 11, 7], gap="small")
    # Fill the table first so a newly selected row moves the viewer in the same run.
    with table_col:
        shown_ids = _middle_panel(config, extraction, analysis, doc, _review_displays(analysis, ctx, states), ctx,
                                  open_calculations, marked_values, check_results)
    selected = find_by_key(extraction.candidates, st.session_state.get("selected_key"))
    recorded = _recorded_boxes(ctx, analysis, states)
    for calculated in (ctx.note_calcs if ctx is not None else {}):
        recorded.pop(calculated, None)  # shown by its inputs in green instead
    metric_id = st.session_state.get("selected_metric")
    focus_metric = metric_id if st.session_state.get("detail_mode") == "metric" else None
    if (selected is not None and focus_metric in recorded
            and metric_id in {m.metric.id for m in analysis.metrics}
            and analysis.metric(metric_id).primary is not None
            and candidate_key(selected) == analysis.metric(metric_id).primary.mapped.key):
        selected = None  # its recorded box is shown instead; a replaced suggestion is no longer boxed
    with viewer_col:
        if calc_boxes.get(focus_metric):
            _inputs_strip(doc, calc_boxes[focus_metric], analysis.metric(focus_metric).metric.display_name)
        _pdf_viewer(doc, extraction, selected, recorded, focus_metric, ocr_words, analysis, ctx, store,
                    calc_boxes.get(focus_metric, []))
    with detail_col:
        _metric_steps(shown_ids, analysis, extraction, marked_values, ctx)  # always in view, above the decision
        _details_panel(extraction, analysis, selected, ctx, states, store)


@dataclass
class Prepared:
    """Everything the Review page draws from, worked out without drawing anything (D-092)."""

    doc: dict
    extraction: ExtractionResult
    analysis: AnalysisResult
    ocr_words: ocr.OcrWords
    ocr_needed: list[int]
    ocr_missing: list[int]
    ocr_engine: ocr.OcrEngine | None
    ocr_path: Path
    ctx: ReviewContext
    states: dict[str, ReviewState]
    marked_values: dict[str, tuple[int, tuple]]
    calc_boxes: dict[str, list[tuple[int, tuple, str]]]
    recorded: dict[str, tuple[int, tuple, str]]
    check_results: list[cross_checks.CheckResult]


def prepare(config: AppConfig, doc: dict) -> Prepared:
    """The saved report's figures, decisions and boxes, as on this page, for the Review page."""
    needed = _pages_needing_ocr(doc["sha"], doc["bytes"])
    tesseract_dir = config.settings.tesseract_dir
    engine = _ocr_engine(str(tesseract_dir) if tesseract_dir else None) if needed else None
    path = ocr.cache_path(config.settings, doc["sha"])
    ocr_words = {p: w for p, w in ocr.load_cache(path).items() if p in needed}
    extraction = _extract(doc["sha"], tuple(sorted(ocr_words)), doc["bytes"], ocr_words,
                          engine.version if engine else "")
    metric_ids = tuple(st.session_state.metric_selection)
    fingerprint = hashlib.sha256(repr((config.metrics, config.mappings, config.learned_aliases)).encode()).hexdigest()
    analysis = _analyse(doc["sha"], st.session_state.period, st.session_state.bank or "", metric_ids, fingerprint,
                        extraction, config)
    ctx, states = _review_context(config, doc, analysis, extraction, prompt=False)
    marked_values = _marked_values(states)
    calc_boxes = _calculation_boxes(ctx, states)
    ctx = replace(ctx, note_calcs={metric_id: _named_inputs(extraction, calc)
                                   for metric_id, calc in _note_calculations(doc, states).items()})
    for metric_id, calc in ctx.note_calcs.items():
        marked_values.pop(metric_id, None)
        inputs = [(f.page - 1, tuple(f.box), f.label or "input") for f in calc.figures if f.page and f.box]
        if inputs:
            calc_boxes[metric_id] = inputs
    for metric_id, inputs in calc_boxes.items():
        marked_values.setdefault(metric_id, (inputs[0][0] + 1, inputs[0][1]))
    recorded = _recorded_boxes(ctx, analysis, states)
    for calculated in ctx.note_calcs:
        recorded.pop(calculated, None)
    with closing(connect(config.settings.database_path)) as conn:
        check_results = cross_checks.run_checks(conn, config.checks, analysis, states, ctx.candidate_ids,
                                                st.session_state.bank or "", st.session_state.period or "")
    return Prepared(doc, extraction, analysis, ocr_words, needed, [p for p in needed if p not in ocr_words], engine,
                    path, ctx, states, marked_values, calc_boxes, recorded, check_results)


def _report_year_label() -> str:
    """The report year as YYYY/YY ("2025" reads as 2024/25), or as typed when it cannot be read."""
    end_year = report_end_year(st.session_state.period or "")
    return fiscal_year_label(end_year) if end_year else (st.session_state.period or "")


# --- OCR for picture pages -------------------------------------------------------------

def _ocr_step(config: AppConfig, doc: dict, tools_slot) -> tuple[ocr.OcrWords, ocr.OcrEngine | None]:
    """OCR words already read for this PDF; offers to read the rest. Sets st.session_state.ocr_missing."""
    needed = _pages_needing_ocr(doc["sha"], doc["bytes"])
    tesseract_dir = config.settings.tesseract_dir
    engine = _ocr_engine(str(tesseract_dir) if tesseract_dir else None) if needed else None
    path = ocr.cache_path(config.settings, doc["sha"])
    words = {p: w for p, w in ocr.load_cache(path).items() if p in needed}
    missing = [p for p in needed if p not in words]
    st.session_state.ocr_missing = len(missing)
    if doc.get("document_id"):  # a saved report keeps OCR in its Report tools menu, in the header
        with tools_slot:
            _report_tools(doc, needed, missing, engine, path)
        return words, engine
    if not missing:
        return words, engine

    with st.container(border=True):
        st.markdown(f":material/image: **{len(missing)} of {len(needed)} picture page(s) not read yet.** "
                    f"This PDF has {len(needed)} page(s) that are pictures (scanned or photographed), so "
                    "their figures can only be read with OCR (text recognition).")
        if engine is None:
            st.warning("Tesseract OCR is not installed, so these pages cannot be read. Install it (Windows "
                       "installer from UB Mannheim, github.com/UB-Mannheim/tesseract), or set `tesseract_dir` "
                       "in config/settings.yaml, then reload.", icon=":material/warning:")
            return words, engine
        minutes = max(1, round(len(missing) * 10 / 60))
        st.caption(f"About 10 seconds per page (≈{minutes} min) with {engine.version}, on this computer. "
                   "The result is saved, so each PDF is read once; an interrupted run continues where it stopped.")
        label = "Read with OCR" if len(missing) == len(needed) else f"Continue OCR ({len(needed) - len(missing)} done)"
        if st.button(label, icon=":material/document_scanner:", type="primary", key="run_ocr"):
            bar = st.progress(0.0, text="Starting OCR...")
            ocr.run_ocr(doc["bytes"], missing, engine, path,
                        on_page=lambda done, total: bar.progress(done / total, text=f"OCR: page {done} of {total}"))
            st.rerun()
    return words, engine


def picture_pages_summary(needed: list[int], missing: list[int]) -> str:
    """Which picture pages (zero-based) were read with OCR and which were not, in plain words."""
    if not needed:
        return "Every page of this PDF has a text layer, so no page needs OCR."
    def pages(indexes: list[int]) -> str:
        return ", ".join(str(i + 1) for i in indexes)

    read = [p for p in needed if p not in missing]
    text = f"This PDF has {len(needed)} picture page(s): PDF page(s) {pages(needed)}."
    if read:
        text += f" Read with OCR: {pages(read)}."
    if missing:
        text += f" **Not read yet: {pages(missing)}.**"
    return text


def _report_tools(doc: dict, needed: list[int], missing: list[int], engine: ocr.OcrEngine | None, path: Path) -> None:
    """Report tools menu of a saved report: OCR for picture pages stays available after saving."""
    label = f"Report tools · {len(missing)} picture page(s) not read" if missing else "Report tools"
    with st.popover(label, icon=":material/build:", width="stretch"):
        st.markdown(picture_pages_summary(needed, missing))
        if not missing:
            return
        if engine is None:
            st.warning("Tesseract OCR is not installed, so these pages cannot be read. Install it, or set "
                       "`tesseract_dir` in config/settings.yaml, then reload.", icon=":material/warning:")
            return
        st.caption(f"About 10 seconds per page with {engine.version}, on this computer. Figures on these pages are "
                   "not part of the saved report yet; after reading, the review screen offers to add them.")
        if st.button(f"Read {len(missing)} page(s) with OCR", icon=":material/document_scanner:", type="primary",
                     key="run_ocr_saved"):
            bar = st.progress(0.0, text="Starting OCR...")
            ocr.run_ocr(doc["bytes"], missing, engine, path,
                        on_page=lambda done, total: bar.progress(done / total, text=f"OCR: page {done} of {total}"))
            st.rerun()


# --- saving and reopening ------------------------------------------------------------

def _save_report(config: AppConfig, doc: dict, analysis: AnalysisResult, extraction: ExtractionResult,
                 allow_duplicate: bool = False) -> None:
    if _store_report(config, doc, analysis, extraction, allow_duplicate) is None:
        return
    period = canonical_period(st.session_state.period) or st.session_state.period  # saved as YYYY/YY (D-085)
    st.toast(f"Saved {st.session_state.bank} {period}. You can now approve, reject or correct "
             "figures.", icon=":material/save:")
    st.rerun()


def _store_report(config: AppConfig, doc: dict, analysis: AnalysisResult, extraction: ExtractionResult,
                  allow_duplicate: bool = False) -> ReviewContext | None:
    """Save the report; return its review context, or None after showing why it was not saved."""
    bank, period = (st.session_state.bank or "").strip(), st.session_state.period.strip()
    if not bank:
        st.error("Choose the bank before saving.", icon=":material/error:")
        return None
    if st.session_state.get("ocr_missing"):
        st.error("Some pages are pictures that have not been read yet. Read them with OCR before saving, "
                 "so their figures are part of the saved report.", icon=":material/error:")
        return None
    if report_end_year(period) is None:
        st.error("Enter the report year like 2025/26 before saving, so this year's column can be identified.",
                 icon=":material/error:")
        return None
    with closing(connect(config.settings.database_path)) as conn:
        if not allow_duplicate and find_documents_by_hash(conn, doc["sha"]):
            st.session_state.duplicate_pending = True
            st.rerun()  # the message at the top offers to open the saved report
        try:
            document = store_document(doc["bytes"], doc["name"], bank, period, config.settings)
            ids = save_analysis(conn, document, analysis, extraction.extractor_version or EXTRACTOR_VERSION)
        except DocumentError as exc:
            st.error(str(exc), icon=":material/error:")
            return None
    st.session_state.doc = {**doc, "document_id": document.id}
    st.session_state.pop("duplicate_pending", None)
    return ReviewContext(config.settings.database_path, document.id, ids, st.session_state.reviewer,
                         extraction.page_count, st.session_state.get("viewer_page", 1), config=config)


def _duplicate_prompt(config: AppConfig, doc: dict, analysis: AnalysisResult, extraction: ExtractionResult) -> None:
    if not st.session_state.get("duplicate_pending") or doc.get("document_id"):
        return
    with closing(connect(config.settings.database_path)) as conn:
        existing = find_documents_by_hash(conn, doc["sha"])
    if not existing:
        st.session_state.pop("duplicate_pending", None)
        return
    latest = existing[-1]
    with st.container(border=True):
        st.warning(
            f"This exact PDF was already saved as **{latest['bank_name']} {latest['reporting_period']}** "
            f"on {latest['uploaded_at'][:10]} ({len(existing)} saved cop{'y' if len(existing) == 1 else 'ies'}). "
            "Open it to continue that review, or save a separate copy.",
            icon=":material/content_copy:",
        )
        if error := st.session_state.pop("open_error", None):
            st.error(error, icon=":material/error:")
        open_col, copy_col, cancel_col = st.columns(3)
        # A callback runs before the page is drawn, so it may change the (already drawn) Bank and year fields.
        open_col.button("Open saved report", type="primary", width="stretch",
                        on_click=_open_from_prompt, args=(config, latest["id"]))
        if copy_col.button("Save a separate copy", width="stretch"):
            _save_report(config, doc, analysis, extraction, allow_duplicate=True)
        if cancel_col.button("Cancel", width="stretch"):
            st.session_state.pop("duplicate_pending", None)
            st.rerun()


def _reopened_notice(config: AppConfig, doc: dict, analysis: AnalysisResult, extraction: ExtractionResult) -> None:
    """Says that an upload opened the saved review of the same PDF; offers a separate copy instead."""
    note = doc.get("notice")
    if not note:
        return
    with st.container(border=True):
        text_col, copy_col, close_col = st.columns([6, 2, 1], vertical_alignment="center")
        text_col.markdown(f":material/history: {note}")
        if copy_col.button("Save a separate copy instead", key="separate_copy", width="stretch",
                           help="Starts a new, empty review of this PDF with the bank and year shown above."):
            if _store_report(config, doc, analysis, extraction, allow_duplicate=True) is not None:
                st.session_state.doc.pop("notice", None)
                st.toast("Saved a separate copy. Its review starts empty.", icon=":material/save:")
                st.rerun()
        if close_col.button("OK", key="close_notice", width="stretch"):
            st.session_state.doc.pop("notice", None)
            st.rerun()


def _open_from_prompt(config: AppConfig, document_id: str) -> None:
    error = open_saved(config, document_id)
    if error:
        st.session_state.open_error = error


def open_saved(config: AppConfig, document_id: str) -> str | None:
    """Load a saved report into the review screen. Returns an error message, or None."""
    with closing(connect(config.settings.database_path)) as conn:
        row = get_document(conn, document_id)
    if row is None:
        return "That report is no longer in the database."
    path = config.settings.data_dir / row["stored_path"]
    if not path.is_file():
        return f"The stored PDF is missing: {path}"
    data = path.read_bytes()
    if compute_sha256(data) != row["sha256"]:
        return "The stored PDF does not match its saved fingerprint (it was changed or damaged); it was not opened."
    for key in ("selected_key", "selected_metric", "detail_mode", "_picks", "duplicate_pending", "marked"):
        st.session_state.pop(key, None)
    st.session_state.viewer_page = 1
    st.session_state.bank = row["bank_name"]
    st.session_state.period = row["reporting_period"]
    st.session_state.metric_selection = json.loads(row["metric_ids"])
    st.session_state.doc = {"name": row["original_filename"], "bytes": data, "sha": row["sha256"],
                            "file_id": f"saved-{document_id}", "document_id": document_id}
    return None


def _review_context(config: AppConfig, doc: dict, analysis: AnalysisResult,
                    extraction: ExtractionResult, prompt: bool = True) -> tuple[ReviewContext | None, dict[str, ReviewState]]:
    document_id = doc.get("document_id")
    if not document_id:
        return None, {}
    with closing(connect(config.settings.database_path)) as conn:
        saved = load_candidates(conn, document_id)
        states = {m.metric.id: review_service.review_state(conn, document_id, m.metric.id) for m in analysis.metrics}
        finished = review_service.extraction_status(conn, document_id).finished
        new = new_figures(conn, document_id, analysis)
        saved_version = get_document(conn, document_id)["extractor_version"]
    ids = saved_candidate_ids(saved, analysis)

    def show_box(page: int, bbox: tuple) -> None:
        st.session_state.viewer_page = page
        size = extraction.pages[page - 1]
        fx, fy = focus_of(bbox, size.width, size.height)
        st.session_state.pan_x, st.session_state.pan_y = round(fx * 100), round(fy * 100)
        _auto_zoom()

    ctx = ReviewContext(config.settings.database_path, document_id, ids, st.session_state.reviewer,
                        extraction.page_count, st.session_state.get("viewer_page", 1), finished, show_box,
                        config=config)
    if prompt:
        _new_figures_prompt(ctx, new, analysis, extraction, saved_version)
    return ctx, states


def _new_figures_prompt(ctx: ReviewContext, new: NewFigures, analysis: AnalysisResult, extraction: ExtractionResult,
                        saved_version: str) -> None:
    """Offer to add figures the current wording finds but the saved report does not have (D-044)."""
    if not new.candidates:
        return
    names = {m.metric.id: m.metric.display_name for m in analysis.metrics}
    metrics = ", ".join(sorted({names.get(metric_id, metric_id) for metric_id, _, _ in new.candidates}))
    with st.expander(f"{len(new.candidates)} figure(s) found with the current wording are not in this saved report",
                     icon=":material/sync_problem:"):
        st.markdown(f"**{len(new.candidates)} figure(s) found with the current wording are not part of this saved "
                    f"report** ({metrics}). They can be viewed, but approved only once added. Adding them changes "
                    "nothing already saved or reviewed.")
        if ctx.extraction_finished:
            st.caption("The extraction review is finished. Reopen it to add them.")
        elif not review_service.same_text_reader(saved_version, extraction.extractor_version):
            st.caption(f"This report was saved with {saved_version}; the app now reads PDFs with "
                       f"{extraction.extractor_version}, so figures cannot be added safely.")
        elif st.button("Add them to the saved report", icon=":material/library_add:", key="add_new_figures"):
            review_panel.run_action(ctx, lambda conn: review_service.add_new_figures(
                conn, ctx.document_id, analysis, extraction.extractor_version, ctx.reviewer),
                f"Added {len(new.candidates)} figure(s) to the saved report.")


def _recorded_boxes(ctx: ReviewContext | None, analysis: AnalysisResult,
                    states: dict[str, ReviewState]) -> dict[str, tuple[int, tuple, str]]:
    """metric -> (zero-based page, box, label) for every approved value and every marked value
    waiting for approval, so whoever checks sees which number each value came from."""
    if ctx is None:
        return {}
    names = {m.metric.id: m.metric.display_name for m in analysis.metrics}
    with closing(connect(ctx.db_path)) as conn:
        results = review_service.approved_results(conn, ctx.document_id)
    boxes = {r.metric_id: (r.source_page - 1, tuple(r.evidence_box), names.get(r.metric_id, r.metric_id))
             for r in results if r.evidence_box is not None}
    boxes |= {metric_id: (s.pending.source_page - 1, tuple(s.pending.source_bbox),
                          f"{names.get(metric_id, metric_id)} (awaiting approval)")
              for metric_id, s in states.items() if s.pending is not None and s.pending.source_bbox is not None}
    return boxes


def _calculation_boxes(ctx: ReviewContext | None, states: dict[str, ReviewState]) -> dict[str, list[tuple[int, tuple, str]]]:
    """metric -> (zero-based page, box, label) of each input of its calculation on this report (D-052)."""
    if ctx is None:
        return {}
    found: dict[str, list[tuple[int, tuple, str]]] = {}
    with closing(connect(ctx.db_path)) as conn:
        for metric_id, s in states.items():
            decision = s.pending if s.status == "corrected" else None
            if s.status == "approved" and s.accepted_decision.approves_decision_id is not None:
                decision = next((d for d in s.history if d.id == s.accepted_decision.approves_decision_id), None)
            if decision is None or decision.entry_kind != "calculated":
                continue
            calc = saved_calculation(conn, decision.id)
            items = [(i.page - 1, tuple(i.box), i.label) for i in (calc.inputs if calc else [])
                     if i.page and i.box is not None and i.source_document_id == ctx.document_id]
            if items:
                found[metric_id] = items
    return found


def _note_calculations(doc: dict, states: dict[str, ReviewState]) -> dict[str, NoteCalculation]:
    """Typed values worked out in their note (entered before Calculate existed, e.g. ROE from profit and
    equity): metric -> the note's arithmetic. Their marked box holds one input, not the value (D-089)."""
    found = {}
    for metric_id, s in states.items():
        if s.status == "corrected":
            decision = proposal = s.pending
        elif s.status == "approved" and s.accepted_decision.approves_decision_id is not None:
            decision = s.accepted_decision
            proposal = next((d for d in s.history if d.id == decision.approves_decision_id), None)
        else:
            continue
        if proposal is None or proposal.entry_kind is not None or "=" not in (proposal.reason or ""):
            continue
        note = " ".join(dict.fromkeys(r for r in (proposal.reason, decision.reason) if r))
        calc = _typed_calculation(doc["sha"], doc["bytes"], decision.reviewed_value, decision.reviewed_unit,
                                  decision.source_page, decision.source_bbox is not None, note)
        if calc is not None:
            found[metric_id] = calc
    return found


def _named_inputs(extraction: ExtractionResult, calc: NoteCalculation) -> NoteCalculation:
    """Each figure of the note named by the row it is printed in and its year column ("Total equity
    2023"), as the reviewer's note seldom names every number; the note's own word when no row is found."""
    return replace(calc, figures=tuple(replace(f, label=_row_name(extraction, f.page, f.box) or f.label)
                                       if f.page and f.box else f for f in calc.figures))


def _row_name(extraction: ExtractionResult, page: int, box: tuple) -> str | None:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for c in extraction.candidates:
        x0, y0, x1, y1 = c.bbox
        if c.page_index == page - 1 and x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
            year = column_year(c, extraction.pages[page - 1]) if c.extraction_method == TABLE_METHOD else None
            name = " ".join(str(part) for part in ((c.raw_label or "").strip(), year) if part)
            return name or None
    return None


def _inputs_strip(doc: dict, inputs: list[tuple[int, tuple, str]], metric_name: str) -> None:
    """The selected calculated metric's inputs, every page side by side as a close-up of the rows
    that hold them, each input boxed in green: what the value was worked out from, at a glance."""
    pages: dict[int, list[tuple[tuple, str]]] = {}
    for page_index, box, label in inputs:
        pages.setdefault(page_index, []).append((tuple(box), label))  # named under the picture, not on it
    with st.container(border=True):
        st.markdown(f":green[:material/calculate:] **{metric_view.esc(metric_name)} is calculated** · "
                    f"its {len(inputs)} input(s), boxed in green")
        ordered = sorted(pages.items())
        for start in range(0, len(ordered), 2):  # two pages per row keeps the rows readable
            columns = st.columns(2)
            for column, (page_index, boxes) in zip(columns, ordered[start:start + 2]):
                with column:
                    st.caption(f"PDF page {page_index + 1}")
                    st.image(_band(doc["sha"], page_index, tuple((box, "") for box, _ in boxes), doc["bytes"]),
                             width="stretch")
                    st.caption(" · ".join(metric_view.esc(label) for _, label in boxes))
                    st.button(f"Open page {page_index + 1}", icon=":material/open_in_full:", width="stretch",
                              key=f"inputs_open_{page_index}", on_click=_go_to, args=(page_index + 1,))


@st.cache_data(show_spinner=False, max_entries=200)
def _band(sha256: str, page_index: int, boxes: tuple, _pdf_bytes: bytes) -> bytes:
    return render_band_png(_pdf_bytes, page_index, boxes)


@st.cache_data(show_spinner=False, max_entries=500)
def _typed_calculation(sha256: str, _pdf: bytes, value: Decimal | None, unit: str | None, page: int | None,
                       boxed: bool, note: str) -> NoteCalculation | None:
    pdf = PdfWords(_pdf)
    try:
        return typed_calculation(pdf, value, unit, page, boxed, note)
    finally:
        pdf.close()


def _marked_values(states: dict[str, ReviewState]) -> dict[str, tuple[int, tuple]]:
    """Metrics whose current value comes from a box the reviewer marked: metric -> (1-based page, box).
    For these the old suggested figure is no longer boxed; the marked box is the evidence."""
    marked = {}
    for metric_id, s in states.items():
        decision = s.pending or s.accepted_decision
        if decision is not None and decision.source_bbox is not None and decision.source_page:
            marked[metric_id] = (decision.source_page, tuple(decision.source_bbox))
    return marked


def _review_displays(analysis: AnalysisResult, ctx: ReviewContext | None,
                     states: dict[str, ReviewState]) -> dict[str, metric_view.ReviewDisplay]:
    """Table text for reviewed metrics: the recorded full value (e.g. 381,497,212,000 ETB) next to
    the figure as printed (381,497,212 Birr'000), so nobody corrects a value approving already
    converted. A value not taken from an extracted figure says where it came from instead."""
    if ctx is None:
        return {}
    key_by_id = {candidate_id: key for (_, key), candidate_id in ctx.candidate_ids.items()}
    displays = {}
    for m in analysis.metrics:
        state = states.get(m.metric.id)
        if state is None or state.status == "open":
            continue
        if state.status == "not_reported":
            displays[m.metric.id] = metric_view.ReviewDisplay("not_reported", "—", "")
            continue
        decision = state.pending if state.status == "corrected" else state.accepted_decision
        value, unit = decision.reviewed_value, decision.reviewed_unit
        full = metric_view.full_value_text(value, unit) if value is not None else "—"
        proposal = decision if state.status == "corrected" else next(
            (d for d in state.history if d.id == decision.approves_decision_id), None)
        figure = next((mc for mc in m.candidates if mc.mapped.key == key_by_id.get(decision.candidate_id)), None)
        if proposal is not None and proposal.entry_kind in ENTRY_ORIGINS:
            printed = ENTRY_ORIGINS[proposal.entry_kind]
        elif m.metric.id in ctx.note_calcs:
            printed = "calculated (in the note)"
        elif figure is not None:
            printed = metric_view.printed_text(figure, m.metric.expected_unit)
            if proposal is not None:
                printed = f"{printed} (before correction)"
        else:
            printed = "typed by the reviewer" if proposal is not None else ""
        displays[m.metric.id] = metric_view.ReviewDisplay(state.status, full, printed)
    return displays


ENTRY_ORIGINS = {"calculated": "calculated", "external": "external source"}


# --- top bar -------------------------------------------------------------------

def _document_bar(config: AppConfig) -> dict | None:
    # An upload of a PDF that is already saved opens that saved review. It is done here, before the
    # Bank and year fields are drawn, because they take the saved report's values.
    pending = st.session_state.pop("open_pending", None)
    if pending is not None:
        document_id, note = pending
        error = open_saved(config, document_id)
        if error:
            st.error(error, icon=":material/error:")
        else:
            st.session_state.doc["notice"] = note
    _open_from_link(config)
    _restore_fields(config)
    doc = st.session_state.get("doc")
    saved = bool(doc and doc.get("document_id"))
    names = {m.id: m.display_name for m in config.metrics}
    locked = "Saved with the report; open a new upload to change it." if saved else None
    period_help = locked or _suggest_period(doc)

    # Once saved, the fields are locked: they fold away under the report's title (D-091).
    frame = (st.expander("Report details · bank, year, metrics, PDF", icon=":material/description:") if saved
             else st.container(border=True))
    with frame:
        bank_col, period_col, metrics_col, status_col, file_col = st.columns([3, 2, 2, 2, 3], vertical_alignment="bottom")
        if saved:  # shown as saved, even if it was typed before the bank list existed
            bank_col.text_input("Bank", key="bank", disabled=True, help=locked)
        else:
            if st.session_state.bank not in config.banks:
                st.session_state.bank = None
            bank_col.selectbox("Bank", config.banks, key="bank", index=None, placeholder="Choose a bank",
                               help="From config/banks.yaml. Missing a bank? Add it there.")
        period_col.text_input("Report year", key="period", placeholder="e.g. 2025/26", disabled=saved, help=period_help)
        with metrics_col.popover(f"Metrics ({len(st.session_state.metric_selection)})", icon=":material/checklist:",
                                 width="stretch", disabled=saved, help=locked):
            st.multiselect(
                "Metrics to extract", options=[m.id for m in config.active_metrics()], key="metric_selection",
                format_func=lambda metric_id: names[metric_id],
                help="From config/metrics.yaml (active metrics). Inactive ones need a definition first.",
            )
        with status_col:
            if doc is None:
                st.badge("No report loaded", color="gray")
            elif saved:
                st.badge("Saved · in review", color="green", icon=":material/save:")
            elif st.button("Save report", icon=":material/save:", type="primary", width="stretch",
                           help="Saves the PDF and its figures locally so review decisions can be recorded."):
                st.session_state.save_requested = True
        with file_col:
            if doc is None:
                uploaded = st.file_uploader("Annual report PDF", type=["pdf"], key="upload_main")
            else:
                with st.popover(f"{doc['name'][:38]}", icon=":material/picture_as_pdf:", width="stretch"):
                    uploaded = st.file_uploader("Replace with another PDF", type=["pdf"], key="upload_replace")
        _remember_fields()
        if uploaded is not None:
            _load_upload(uploaded, config)
    _link_to_report(st.session_state.get("doc"))
    return st.session_state.get("doc")


def _suggest_period(doc: dict | None) -> str | None:
    """Fill an empty Report year box of an unsaved upload with the fiscal year its first pages state
    (D-085), once per PDF. Only a suggestion: the reviewer can change it, and Save stores it."""
    if doc is None or doc.get("document_id"):
        return None
    if st.session_state.get("_period_suggested") != doc["sha"]:
        st.session_state._period_suggested = doc["sha"]
        st.session_state._period_suggestion = None
        if not (st.session_state.get("period") or "").strip():
            suggestion = suggested_period(doc["bytes"])
            if suggestion:
                st.session_state.period = suggestion
                st.session_state._period_suggestion = suggestion
    suggestion = st.session_state.get("_period_suggestion")
    if suggestion and st.session_state.get("period") == suggestion:
        return f"Filled from the period end stated in the PDF ({suggestion}). Check it before saving."
    return None


# Streamlit drops a widget's value whenever a page without that widget is shown, so the Bank,
# Report year and Metrics fields keep a copy under keys no widget uses.
FIELD_COPIES = {"bank": "_bank_value", "period": "_period_value", "metric_selection": "_metric_selection_value"}
REPORT_PARAM = "report"  # ?report=<document id> in the address keeps the open saved report on reload


def _restore_fields(config: AppConfig) -> None:
    """Put the Bank, Report year and Metrics values back before their widgets are drawn.

    A saved report's own values are the truth; for an unsaved upload, the values last typed."""
    doc = st.session_state.get("doc")
    if doc and doc.get("document_id"):
        with closing(connect(config.settings.database_path)) as conn:
            row = get_document(conn, doc["document_id"])
        if row is not None:
            st.session_state.bank = row["bank_name"]
            st.session_state.period = row["reporting_period"]
            st.session_state.metric_selection = json.loads(row["metric_ids"])
            return
    defaults = {"bank": "", "period": "", "metric_selection": [m.id for m in config.active_metrics()]}
    for key, default in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = st.session_state.get(FIELD_COPIES[key], default)


def _remember_fields() -> None:
    for key, copy_key in FIELD_COPIES.items():
        value = st.session_state.get(key)
        st.session_state[copy_key] = list(value) if isinstance(value, list) else value


def _open_from_link(config: AppConfig) -> None:
    """A reload or a new tab starts an empty session: reopen the saved report named in the address.

    Tried once per session and report, so a failure never repeats and never replaces an upload."""
    if st.session_state.get("doc") is not None:
        return
    document_id = st.query_params.get(REPORT_PARAM)
    if not document_id or st.session_state.get("_link_opened") == document_id:
        return
    st.session_state._link_opened = document_id
    error = open_saved(config, document_id)
    if error:
        st.query_params.pop(REPORT_PARAM, None)
        st.error(f"The report in the link could not be opened. {error}", icon=":material/error:")


def _link_to_report(doc: dict | None) -> None:
    """Keep ?report=<id> in the address while a saved report is open, so a reload reopens it."""
    document_id = doc.get("document_id") if doc else None
    if document_id:
        if st.query_params.get(REPORT_PARAM) != document_id:
            st.query_params[REPORT_PARAM] = document_id
    elif REPORT_PARAM in st.query_params:
        del st.query_params[REPORT_PARAM]


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
    if current is None or current["sha"] != sha or current.get("document_id"):
        for key in ("selected_key", "selected_metric", "detail_mode", "_picks", "duplicate_pending", "marked"):
            st.session_state.pop(key, None)
        st.session_state.viewer_page = 1
    st.session_state.doc = {"name": uploaded.name, "bytes": data, "sha": sha, "file_id": uploaded.file_id}
    with closing(connect(config.settings.database_path)) as conn:
        existing = find_documents_by_hash(conn, sha)
    if existing:  # continue the saved review instead of starting a second one
        latest = existing[-1]
        copies = f"the newest of {len(existing)} saved copies" if len(existing) > 1 else "saved"
        note = (f"This PDF was already saved as **{latest['bank_name']} {latest['reporting_period']}** "
                f"({copies}, {latest['uploaded_at'][:10]}), so that review is open: your recorded values are here.")
        st.session_state.open_pending = (latest["id"], note)
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
    _centre_on(candidate, extraction)
    _auto_zoom()  # a zoom level the user chose is kept


def _select_metric(metric_id: str, analysis: AnalysisResult, extraction: ExtractionResult,
                   marked_values: dict[str, tuple[int, tuple]], ctx: ReviewContext | None) -> None:
    """Show a metric's details and move the page viewer to its figure."""
    st.session_state.selected_metric = metric_id
    st.session_state.detail_mode = "metric"
    primary = analysis.metric(metric_id).primary
    if primary is not None:
        _show_candidate(primary.mapped.candidate, extraction)
    if metric_id in marked_values and ctx is not None and ctx.show_box is not None:
        ctx.show_box(*marked_values[metric_id])  # go to the reviewer's box, not the old suggestion


def _step_to(metric_id: str, analysis: AnalysisResult, extraction: ExtractionResult,
             marked_values: dict[str, tuple[int, tuple]], ctx: ReviewContext | None) -> None:
    # Runs as a button callback, before the page is drawn, so the viewer already shows the new figure.
    st.session_state._metric_nav = st.session_state.get("_metric_nav", 0) + 1
    st.session_state.setdefault("_picks", {})["metrics"] = None
    _select_metric(metric_id, analysis, extraction, marked_values, ctx)


def _metric_steps(shown_ids: list[str], analysis: AnalysisResult, extraction: ExtractionResult,
                  marked_values: dict[str, tuple[int, tuple]], ctx: ReviewContext | None) -> None:
    """Previous / Next through the metrics the table shows (after its filter)."""
    if not shown_ids:
        return
    current = st.session_state.get("selected_metric")
    at = shown_ids.index(current) if current in shown_ids else -1
    args = (analysis, extraction, marked_values, ctx)
    following = shown_ids[min(at + 1, len(shown_ids) - 1)]
    names = {m.metric.id: m.metric.display_name for m in analysis.metrics}
    with st.container(key="metric_steps"):
        st.caption(f"Metric {at + 1} of {len(shown_ids)}" if at >= 0 else f"{len(shown_ids)} metrics shown")
        prev_col, next_col = st.columns([1, 4], vertical_alignment="center")
        prev_col.button("", icon=":material/arrow_upward:", key="metric_prev", width="stretch",
                        help=f"Previous metric ({PREVIOUS_SHORTCUT})",
                        disabled=at <= 0, on_click=_step_to, args=(shown_ids[max(at - 1, 0)], *args),
                        shortcut=PREVIOUS_SHORTCUT)
        next_col.button(f"Next: {names.get(following, following)}" if at >= 0 else "Start with the first",
                        icon=":material/arrow_downward:", key="metric_next", width="stretch",
                        disabled=at >= len(shown_ids) - 1, on_click=_step_to, args=(following, *args),
                        shortcut=NEXT_SHORTCUT)


def _approve_all_verified(ctx: ReviewContext, analysis: AnalysisResult) -> None:
    """Approve, in one go, the suggested figures a reviewer would approve without a second look (D-072)."""
    with closing(connect(ctx.db_path)) as conn:
        ready, left = bulk_review.verified_figures(conn, ctx.document_id, analysis, ctx.candidate_ids)
    if not ready:
        return
    with st.popover(f"Approve all verified ({len(ready)})", icon=":material/done_all:", width="stretch",
                    help="Report-year figures, converted, positive and with no warning."):
        st.markdown("These suggested figures come from the report year's column, are converted to the "
                    "metric's unit, are positive and have no warning:")
        st.dataframe(pd.DataFrame([{"Metric": f.metric_name, "As printed": f.printed,
                                    "Value": number_text(f.value, f.unit), "Page": f.page} for f in ready]),
                     hide_index=True, width="stretch")
        if left:
            with st.expander(f"Left for you to review ({len(left)})"):
                for item in left:
                    st.markdown(f"- **{item.metric_name}**: {item.reason}")
        checked = st.checkbox("I checked this list against the report", key="bulk_checked")
        if st.button(f"Approve {len(ready)} figure{'s' if len(ready) != 1 else ''}", icon=":material/done_all:", type="primary",
                     disabled=not checked, key="bulk_approve"):
            try:
                with closing(connect(ctx.db_path)) as conn:
                    result = bulk_review.approve_verified(conn, ctx.document_id, ready, ctx.reviewer)
            except ReviewError as exc:
                st.error(str(exc))
                return
            st.session_state.bulk_message = f"Approved {len(result.approved)} figures."
            st.session_state.bulk_skipped = [f"{s.metric_name}: {s.reason}" for s in result.skipped]
            st.session_state.pop("bulk_checked", None)
            st.rerun()


# --- middle: tables ----------------------------------------------------------------

def _middle_panel(config: AppConfig, extraction: ExtractionResult, analysis: AnalysisResult, doc: dict,
                  reviews: dict[str, metric_view.ReviewDisplay], ctx: ReviewContext | None,
                  open_calculations: Callable[[str], None] | None,
                  marked_values: dict[str, tuple[int, tuple]],
                  check_results: list[cross_checks.CheckResult]) -> list[str]:
    """The metric table and its tools; returns the metric ids it shows, for Previous / Next."""
    sha = doc["sha"]
    with st.container(border=True):
        metrics_tab, figures_tab, log_tab = st.tabs(["Extracted Data", "All Figures", "Validation Log"])
        with metrics_tab:
            # A step with Next / Previous starts a new table, so the old row is no longer highlighted.
            table_key = f"metrics_{sha[:12]}_{len(analysis.metrics)}_{st.session_state.get('_metric_nav', 0)}"
            metric_id, shown_ids = metric_view.metric_table(analysis, table_key, reviews)
            if _is_new_pick("metrics", metric_id):
                _select_metric(metric_id, analysis, extraction, marked_values, ctx)
            if bulk_message := st.session_state.pop("bulk_message", None):
                st.success(bulk_message, icon=":material/done_all:")
            for skipped in st.session_state.pop("bulk_skipped", []):
                st.warning(f"Not approved: {skipped}", icon=":material/warning:")
            if ctx is not None and not ctx.extraction_finished:
                _approve_all_verified(ctx, analysis)
            _checks_panel(check_results)
            end_year = report_end_year(st.session_state.period or "")
            if ctx is not None and end_year:
                _finish_extraction(ctx, analysis, open_calculations, check_results)
                _learn_wording(config, ctx, analysis, extraction)
                export_panel.export_button(config, end_year, key=f"export_{end_year}")
            else:
                st.caption("Save the report to approve figures and export them to Excel.")
        with figures_tab:
            candidate = _figures_table(extraction, sha)
            if candidate is not None and _is_new_pick("figures", candidate_key(candidate)):
                st.session_state.detail_mode = "figure"
                _show_candidate(candidate, extraction)
        with log_tab:
            metric_view.validation_log(analysis, extraction.messages, report_end_year(st.session_state.period) is not None)
    return shown_ids


CHECK_ICONS = {"pass": ":green[:material/check_circle:]", "fail": ":red[:material/cancel:]",
               "skipped": ":gray[:material/do_not_disturb_on:]"}


def _checks_panel(results: list[cross_checks.CheckResult]) -> None:
    """Cross-checks between figures (D-045); open by itself when one fails."""
    if not results:
        return
    count = {o: sum(r.outcome == o for r in results) for o in CHECK_ICONS}
    label = f"Cross-checks · {count['pass']} passed · {count['fail']} failed · {count['skipped']} not checked"
    with st.expander(f":material/rule: {label}", expanded=count["fail"] > 0):
        for r in results:
            st.markdown(f"{CHECK_ICONS[r.outcome]} **{metric_view.esc(r.name)}** · {metric_view.esc(r.message)}")
        st.caption("Checks point out where to look. They never change, approve or reject a figure.")


def _learn_wording(config: AppConfig, ctx: ReviewContext, analysis: AnalysisResult,
                   extraction: ExtractionResult) -> None:
    """Offer the labels of values approved from unrecognised rows as new wording for this bank (D-043, D-046)."""
    bank = st.session_state.bank
    mappings = config.mappings_for(bank)
    with closing(connect(ctx.db_path)) as conn:
        results = review_service.approved_results(conn, ctx.document_id)
    suggestions = alias_learning.suggest_aliases(results, extraction.candidates, mappings)
    if not suggestions:
        return
    names = {m.id: m.display_name for m in config.metrics}
    addable = sum(s.can_add for s in suggestions)
    # Count only what can be added, so the button never reads as if something was learned already.
    label = f"Learn wording ({addable} to add)" if addable else "Learn wording (nothing to add)"
    with st.popover(label, icon=":material/school:", width="stretch",
                    help=f"Have the app find these labels by itself in {bank}'s reports from now on."):
        st.markdown(f"You approved these values from table rows the app did not recognise. Add a label to have "
                    f"it found automatically in **{metric_view.esc(bank)}**'s reports from now on (other banks keep "
                    "their own wording). Add only labels that always mean this metric. Nothing is learned until "
                    "you press Add.")
        for i, s in enumerate(suggestions):
            text_col, button_col = st.columns([4, 1], vertical_alignment="center")
            text_col.markdown(f"**{metric_view.esc(names.get(s.metric_id, s.metric_id))}** ← "
                              f"“{metric_view.esc(s.label)}” · PDF page {s.page}")
            if not s.can_add:
                other = ", ".join(names.get(m, m) for m in s.already_used_by)
                text_col.caption(f"Not learned: “{s.label}” already means {other}. One label cannot point to two "
                                 "metrics, or every such row would become ambiguous.")
            elif button_col.button("Add", key=f"learn_{i}", type="primary", width="stretch"):
                seen_in = f"{st.session_state.bank} {st.session_state.period}, PDF page {s.page}"
                try:
                    alias_learning.add_learned_alias(config.learned_aliases_path, mappings, s.metric_id,
                                                     s.label, seen_in, ctx.reviewer, bank)
                except alias_learning.LearnError as exc:
                    st.error(str(exc), icon=":material/error:")
                    return
                st.toast(f"Learned “{s.label}” for {names.get(s.metric_id, s.metric_id)} in {bank}'s reports.",
                         icon=":material/school:")
                st.rerun()
        st.caption(f"Saved in config/{config.learned_aliases_path.name} with your name; delete an entry there "
                   "to undo it.")


def _finish_extraction(ctx: ReviewContext, analysis: AnalysisResult,
                       open_calculations: Callable[[str], None] | None,
                       check_results: list[cross_checks.CheckResult]) -> None:
    """Step 2 of the workflow: lock the approved figures so the analyst can calculate from them."""
    if ctx.extraction_finished:
        st.success("Extraction review finished: figures are locked. The analyst enters the calculated "
                   "columns on the Calculations page.", icon=":material/task_alt:")
        calc_col, reopen_col = st.columns(2)
        if open_calculations is not None and calc_col.button("Open Calculations", icon=":material/calculate:",
                                                             width="stretch", key="open_calculations"):
            open_calculations(ctx.document_id)
        with reopen_col:
            review_panel.reopen_extraction(ctx, key="table")
        return
    with st.popover("Finish extraction", icon=":material/task_alt:", width="stretch",
                    help="When the review is done: locks the figures and opens the calculated columns."):
        with closing(connect(ctx.db_path)) as conn:
            pending = review_service.not_approved_metrics(conn, ctx.document_id)
        names = {m.metric.id: m.metric.display_name for m in analysis.metrics}
        st.markdown("Finish when every figure you can find in the report is approved. The figures are then "
                    "locked, and the analyst can enter the calculated columns from them.")
        pending, sector = (without_sector_metrics(ctx.config, pending) if ctx.config is not None
                           else (pending, []))
        if pending:
            st.warning(f"{len(pending)} metric(s) are not approved and will stay blank: "
                       + ", ".join(names.get(m, m) for m in pending), icon=":material/warning:")
        if sector:
            st.caption(":material/public: Sector figures (" + ", ".join(names.get(m, m) for m in sector)
                       + ") are exported from the Sector data page for every bank of the year.")
        failed = [r for r in check_results if r.outcome == "fail"]
        if failed:
            st.warning(f"{len(failed)} cross-check(s) failed: " + "; ".join(r.name for r in failed)
                       + ". See Cross-checks above the button.", icon=":material/rule:")
        note = st.text_input("Note (optional)", key=f"finish_note_{ctx.document_id}",
                             placeholder="e.g. NPL not disclosed in this report")
        if st.button("Confirm: finish extraction", type="primary", key=f"finish_{ctx.document_id}"):
            review_panel.run_action(ctx, lambda conn: review_service.finish_extraction(
                conn, ctx.document_id, ctx.reviewer, note), "Extraction review finished")


FIGURES_PER_PAGE = 100
YEAR_ALL, YEAR_NONE = "All years", "No year column"


def _column_years(extraction: ExtractionResult, sha: str) -> dict[CandidateKey, int | None]:
    """Year column of every table figure (None for sentences), worked out once per report."""
    cache = st.session_state.setdefault("_column_years", {})
    if sha not in cache:
        cache.clear()  # one report at a time
        cache[sha] = {candidate_key(c): column_year(c, extraction.pages[c.page_index])
                      if c.extraction_method == TABLE_METHOD else None for c in extraction.candidates}
    return cache[sha]


def _typed_number(label: str, key: str) -> tuple[Decimal | None, str | None]:
    """A number box for a filter: (value, problem). Empty means no limit."""
    text = st.text_input(label, key=key, placeholder="e.g. 1,000,000")
    if not text.strip():
        return None, None
    value = parse_number(text).value
    return value, None if value is not None else f"“{text}” is not a number, so it is not used."


def _figures_filters(extraction: ExtractionResult, sha: str, years: dict) -> dict:
    """The All Figures filter controls; returns the arguments for filter_candidates (and the problems)."""
    found = sorted({y for y in years.values() if y is not None}, reverse=True)
    last = max(extraction.page_count, 1)
    with st.popover("Filters", icon=":material/filter_list:", help="Filter by page, type, year column and value",
                    width="stretch"):
        method_name = st.radio("Found by", list(METHOD_FILTERS), key="method_filter", horizontal=True)
        first_col, last_col = st.columns(2)
        first = first_col.number_input("From PDF page", min_value=1, max_value=last, value=1, key=f"fig_from_{sha[:12]}")
        to = last_col.number_input("To PDF page", min_value=1, max_value=last, value=last, key=f"fig_to_{sha[:12]}")
        year_name = st.selectbox("Year column", [YEAR_ALL, *map(str, found), YEAR_NONE], key=f"fig_year_{sha[:12]}",
                                 help="The year printed above the number's column. Sentences have none.")
        low_col, high_col = st.columns(2)
        with low_col:
            low, low_problem = _typed_number("Smallest value", "fig_min")
        with high_col:
            high, high_problem = _typed_number("Largest value", "fig_max")
        st.caption("Values are compared as printed (before Birr'000 or millions are applied).")
        hide_unlabelled = st.checkbox("Hide table numbers without a label", value=True, key="hide_unlabelled",
                                      help="Hides page numbers, year headers and similar. Nothing is deleted.")
    year = None if year_name == YEAR_ALL else NO_YEAR if year_name == YEAR_NONE else int(year_name)
    return {"method": METHOD_FILTERS[method_name], "pages": (min(first, to) - 1, max(first, to) - 1),
            "year": year, "min_value": low, "max_value": high, "hide_unlabelled": hide_unlabelled,
            "problems": [p for p in (low_problem, high_problem) if p]}


def _figures_table(extraction: ExtractionResult, sha: str) -> RawCandidate | None:
    """Every number in the PDF: search, filters, the true count, 100 rows per page and the selected row's context."""
    years = _column_years(extraction, sha)
    search_col, filter_col = st.columns([3, 1], vertical_alignment="bottom")
    search = search_col.text_input("Search", placeholder="Search words or a number, e.g. branches or 189512266",
                                   label_visibility="collapsed")
    with filter_col:
        chosen = _figures_filters(extraction, sha, years)
    for problem in chosen.pop("problems"):
        st.warning(problem, icon=":material/warning:")
    hide_unlabelled = chosen.pop("hide_unlabelled")
    matched = filter_candidates(extraction.candidates, search=search, year_of=lambda c: years.get(candidate_key(c)),
                                **chosen)
    if hide_unlabelled:
        matched = [c for c in matched if c.extraction_method != TABLE_METHOD or c.raw_label]
    filters = hash((search, hide_unlabelled, *map(str, chosen.values())))
    pages = max(1, -(-len(matched) // FIGURES_PER_PAGE))
    count_col, page_col = st.columns([3, 1], vertical_alignment="center")
    page = page_col.number_input("Results page", min_value=1, max_value=pages, value=1,
                                 key=f"fig_page_{sha[:12]}_{filters}", label_visibility="collapsed",
                                 help=f"{FIGURES_PER_PAGE} figures per page")
    shown, pages = page_of_rows(matched, page, FIGURES_PER_PAGE)
    start = (page - 1) * FIGURES_PER_PAGE
    count_col.markdown(f"**{len(matched):,} figures match** (of {len(extraction.candidates):,} in the PDF) · "
                       + (f"rows {start + 1:,}–{start + len(shown):,}, page {page} of {pages}" if matched else "none"))
    event = st.dataframe(
        pd.DataFrame({
            "Figure": [display_label(c) for c in shown],
            "Value": [c.raw_value_text for c in shown],
            "Page": [c.page_index + 1 for c in shown],
            "Year": [str(years.get(candidate_key(c)) or "") for c in shown],
            "Type": [SHORT_METHOD_LABELS[c.extraction_method] + (" · OCR" if c.ocr else "") for c in shown],
        }),
        hide_index=True, width="stretch", height=480, on_select="rerun", selection_mode="single-row",
        # A new key when the filters or the results page change resets the selection to match the new rows.
        key=f"figures_{sha[:12]}_{filters}_{page}",
        column_config={
            "Figure": st.column_config.TextColumn(width=190),
            "Value": st.column_config.TextColumn(width=95),
            "Page": st.column_config.NumberColumn(width=45, format="%d"),
            "Year": st.column_config.TextColumn(width=45, help="Year printed above the number's column"),
            "Type": st.column_config.TextColumn(width=55, help="Table row or sentence text; OCR = read from a picture"),
        },
    )
    rows = event.selection.rows if event is not None else []
    picked = shown[rows[0]] if rows and rows[0] < len(shown) else None
    if picked is not None:
        kind = "Sentence" if picked.extraction_method == NARRATIVE_METHOD else "Whole row"
        st.caption(f"{kind} · PDF page {picked.page_index + 1}")
        st.markdown(f"> {metric_view.esc(context_line(picked))}")
    return picked


# --- left: PDF viewer --------------------------------------------------------------

def _go_to(page: int) -> None:
    st.session_state.viewer_page = page


def _centre_on(candidate: RawCandidate, extraction: ExtractionResult) -> None:
    page = extraction.pages[candidate.page_index]
    fx, fy = focus_of(candidate.bbox, page.width, page.height)
    st.session_state.pan_x = round(fx * 100)
    st.session_state.pan_y = round(fy * 100)


def _auto_zoom() -> None:
    """Zoom in on a newly selected figure, until the reviewer picks a zoom level themselves."""
    if not st.session_state.get("zoom_chosen"):
        st.session_state.viewer_zoom = AUTO_ZOOM


def _zoom_chosen() -> None:
    st.session_state.zoom_chosen = True


@st.dialog("PDF page", width="large")
def _full_screen(doc: dict, page: int, highlight: tuple | None, boxes: tuple) -> None:
    """The whole page, large and sharp, with the same boxes as the viewer."""
    st.caption(f"{doc['name']} · page {page}")
    st.image(_render(doc["sha"], page - 1, highlight, FULL_SCREEN_RESOLUTION, 1.0, (0.5, 0.5), boxes, doc["bytes"]),
             width="stretch")


def _pdf_viewer(doc: dict, extraction: ExtractionResult, selected: RawCandidate | None,
                recorded: dict[str, tuple[int, tuple, str]], focus_metric: str | None, ocr_words: ocr.OcrWords,
                analysis: AnalysisResult, ctx: ReviewContext | None,
                store: Callable[[], ReviewContext | None] | None = None,
                input_boxes: list[tuple[int, tuple, str]] = ()) -> None:
    """Page viewer. input_boxes: (zero-based page, box, label) of the selected calculated metric's
    inputs, drawn in green. Boxes: red = a suggested figure being looked at; blue (the marker's colour) =
    recorded values: the selected metric's bold, shaded and named, the others thin and unnamed
    so names never cover figures; and the reviewer's new mark. With "Mark a number" on, dragging
    on the page draws a mark, and a form below records it for a metric."""
    page_count = extraction.page_count
    st.session_state.viewer_page = min(max(1, st.session_state.get("viewer_page", 1)), page_count)
    # Keep zoom and position when the sliders are hidden or the user switches pages.
    st.session_state.viewer_zoom = st.session_state.get("viewer_zoom", "100%")
    st.session_state.pan_x = st.session_state.get("pan_x", 50)
    st.session_state.pan_y = st.session_state.get("pan_y", 50)
    page = st.session_state.viewer_page
    on_this_page = selected is not None and selected.page_index == page - 1

    with st.container(border=True):  # one toolbar row: page, zoom, full screen (the file name is in the header)
        prev_col, page_col, next_col, zoom_col, full_col = st.columns([1, 1.7, 1, 4, 1], vertical_alignment="center")
        prev_col.button("", icon=":material/chevron_left:", on_click=_go_to, args=(page - 1,), disabled=page <= 1, key="prev_page")
        page_col.number_input("Page", min_value=1, max_value=page_count, key="viewer_page", label_visibility="collapsed")
        next_col.button("", icon=":material/chevron_right:", on_click=_go_to, args=(page + 1,), disabled=page >= page_count, key="next_page")
        zoom_label = zoom_col.select_slider("Zoom", options=list(ZOOM_LEVELS), key="viewer_zoom",
                                            label_visibility="collapsed", on_change=_zoom_chosen)
        view_zoom = ZOOM_LEVELS[zoom_label]
        full_screen = full_col.button("", icon=":material/fullscreen:", key="viewer_full_screen", width="stretch",
                                      help="Full screen: show this page large, in a window over the app")
        fit_col, mark_col = st.columns([3, 2], vertical_alignment="center")
        fit = fit_col.radio("Fit", [FIT_WIDTH, FIT_PAGE], key="viewer_fit", horizontal=True,
                            label_visibility="collapsed",
                            help="Fit width: the page fills the panel width (scroll down). Fit page: all of it shows.")

        if view_zoom > 1:
            x_col, y_col, centre_col = st.columns([3, 3, 2], vertical_alignment="bottom")
            x_col.slider("Left ↔ Right", 0, 100, key="pan_x", format="%d%%")
            y_col.slider("Top ↕ Bottom", 0, 100, key="pan_y", format="%d%%")
            centre_col.button(
                "Figure", icon=":material/center_focus_strong:", key="centre_on_figure",
                on_click=_centre_on, args=(selected, extraction), disabled=not on_this_page,
                help="Centre the view on the selected figure", width="stretch",
            )
        if st.session_state.pop("turn_on_marking", False):  # asked for by Calculate (Mark it)
            st.session_state.mark_mode = True
        _calc_input_banner()
        marking = mark_col.toggle("Mark a number", key="mark_mode",
                                  help="Drag a box around the right number on the page. Correct and Enter manually "
                                       "then use it, and it is saved as the evidence for that value.")

        st.caption(f"Page {page} of {page_count} · :red[■] suggestion · :blue[■] selected · "
                   ":green[■] calculation input · thin: other recorded")
        thumbs_col, page_view_col = st.columns([1, 5])
        with thumbs_col:
            for p in range(max(1, page - 1), min(page_count, page + 1) + 1):
                st.image(_render(doc["sha"], p - 1, None, 0.25, 1.0, (0.5, 0.5), (), doc["bytes"]), width="stretch")
                st.button(f"{p}", key=f"thumb_{p}", on_click=_go_to, args=(p,), type="primary" if p == page else "secondary", width="stretch")
        with page_view_col:
            highlight = selected.bbox if on_this_page else None
            focus = (st.session_state.pan_x / 100, st.session_state.pan_y / 100)
            mark: page_marks.Mark | None = st.session_state.get("marked")
            boxes = tuple((box, "focus" if m == focus_metric else "recorded", name)
                          for m, (p, box, name) in recorded.items() if p == page - 1)
            boxes += tuple((box, "input", label) for p, box, label in input_boxes if p == page - 1)
            if mark is not None and mark.page_index == page - 1:
                boxes += ((mark.bbox, "marked"),)
            png = _render(doc["sha"], page - 1, highlight, BASE_RESOLUTION, view_zoom, focus, boxes, doc["bytes"])
            report_page = extraction.pages[page - 1]
            width = _image_width(fit, report_page.width, report_page.height)
            with st.container(height=VIEW_HEIGHT, border=False):
                if marking:
                    drag = streamlit_image_coordinates(
                        Image.open(io.BytesIO(png)), width=width, click_and_drag=True, cursor="crosshair",
                        key=f"mark_{doc['sha'][:12]}_{page}_{zoom_label}_{focus}_{fit}")
                    _take_mark(drag, doc, extraction, page, view_zoom, focus, ocr_words)
                else:
                    st.image(png, width=width)
            if full_screen:
                _full_screen(doc, page, highlight, boxes)
        _mark_summary()
        mark = st.session_state.get("marked")
        if mark is not None and not mark.from_figure:  # a figure picked in All Figures has its form there
            _mark_form(mark, analysis, ctx, store)


def _image_width(fit: str, page_width: float, page_height: float) -> int | str:
    """Fit width fills the panel; Fit page makes the whole page as tall as the viewer (zoom keeps proportions)."""
    if fit == FIT_PAGE and page_height > 0:
        return max(1, int((VIEW_HEIGHT - 16) * page_width / page_height))
    return "stretch"


def _take_mark(drag: dict | None, doc: dict, extraction: ExtractionResult, page: int, view_zoom: float,
               focus: tuple[float, float], ocr_words: ocr.OcrWords) -> None:
    """Turn a new drag on the page image into the reviewer's mark (read the number inside it)."""
    if not drag or drag.get("unix_time") == st.session_state.get("_mark_time"):
        return
    st.session_state._mark_time = drag.get("unix_time")
    report = extraction.pages[page - 1]
    bbox = screen_box_to_page(drag, (report.width, report.height), view_zoom, focus)
    if bbox is None:
        st.toast("Drag a box around the number (a click alone is not a box).", icon=":material/touch_app:")
        return
    st.session_state.marked = page_marks.read_mark(doc["bytes"], report, bbox, ocr_words)
    st.rerun()


def _mark_form(mark: page_marks.Mark, analysis: AnalysisResult, ctx: ReviewContext | None,
               store: Callable[[], ReviewContext | None] | None = None,
               column_note: tuple[str, bool] | None = None) -> None:
    """Record the marked number as the value of a metric, with the box as its evidence.

    Before the report is saved, store (if given) saves it, so one press saves and records.
    column_note: (text, is_warning) on which year column a picked figure is in.
    """
    if ctx is None and store is None:
        st.caption("Save the report first to record a marked number.")
        return
    if ctx is not None and ctx.extraction_finished:
        st.caption("The extraction review is finished; reopen it to record a marked number.")
        return
    target = st.session_state.get(calc_panel.CALC_INPUT)
    if target is not None and ctx is not None:
        _input_figure_form(mark, ctx, target)
        return
    suffix = _mark_key(mark)
    names = {m.metric.id: m.metric.display_name for m in analysis.metrics}
    with st.container(border=True):
        st.markdown("**Use this figure for a metric**" if mark.from_figure else "**Record the marked number**")
        metric_id = st.selectbox("Which figure is this?", list(names), index=None, format_func=names.get,
                                 placeholder="Type to search, e.g. assets", key=f"mark_metric{suffix}")
        if metric_id is None:
            return
        result = analysis.metric(metric_id)
        suggestion = page_marks.suggested_value(mark, result.metric.expected_unit,
                                                result.metric.count_in_thousands_below)
        converted = suggestion is not None and suggestion.value is not None
        value_col, unit_col = st.columns([3, 2], vertical_alignment="bottom")
        with unit_col:
            unit = review_panel.unit_select(result, f"mark_unit_{metric_id}{suffix}")
        value = review_panel.value_box(
            "Value", unit, f"mark_value_{metric_id}{suffix}", where=value_col, sign=result.metric.sign,
            default=value_input.input_text(suggestion.value, suggestion.unit) if converted else "")
        problem_slot = st.empty()  # a refused value is explained right under the box
        if converted:
            source = "this figure" if mark.from_figure else "your box"
            st.caption(f"Read from {source}: {mark.printed_value} ({suggestion.basis}).")
        else:
            st.caption(not_converted_reason(mark, suggestion, result.metric.display_name, result.metric.expected_unit))
        if column_note is not None:
            text, is_warning = column_note
            if is_warning:
                st.warning(text, icon=":material/calendar_month:")
            else:
                st.caption(text)
        how = "Picked from All Figures" if mark.from_figure else "Marked"
        note = st.text_area("Note", value=f"{how} on PDF page {mark.page_index + 1}", key=f"mark_note{suffix}",
                            height=80, help="Saved with the value as its reason, e.g. which table or note it is from.")
        label = "Save report and record this value" if ctx is None else "Save marked value"
        if not st.button(label, type="primary", icon=":material/save:", width="stretch", key=f"mark_save{suffix}"):
            if ctx is None:
                st.caption("The report is not saved yet: this saves it (bank, year and figures) and records the value.")
            return
        # Checked before anything is saved; on a problem every field stays as typed.
        problem = mark_input_problem(value, note)
        if problem is not None:
            problem_slot.error(problem, icon=":material/error:")
            return
        if ctx is None:
            ctx = store()
            if ctx is None:
                return
        primary = result.primary
        candidate_id = ctx.candidate_ids.get((metric_id, primary.mapped.key)) if primary is not None else None
        page, bbox, reviewer, document_id = mark.page_index + 1, mark.bbox, ctx.reviewer, ctx.document_id
        stored, sign = value.value, review_panel.typed_sign(value)
        if candidate_id is not None:  # replaces the suggested figure, which stays on record
            def action(conn):
                return review_service.correct(conn, candidate_id, reviewer, stored, unit, note,
                                              source_page=page, source_bbox=bbox, sign=sign)
        else:
            def action(conn):
                return review_service.manual_entry(conn, document_id, metric_id, reviewer, stored, unit,
                                                   page, note, source_bbox=bbox, sign=sign)
        def show_metric() -> None:  # only once saved: a refused save keeps this form on screen
            st.session_state.selected_metric, st.session_state.detail_mode = metric_id, "metric"

        review_panel.run_action(ctx, action, f"{names[metric_id]}: value recorded. Approve it to use it.",
                                on_success=show_metric)


def input_figure_sign(config: AppConfig | None, metric_id: str) -> str:
    """How a bracketed input figure is recorded: as printed when the calculated metric can be
    negative (metrics.yaml sign: as_printed, e.g. EPS printed as (5%) of par in a loss year),
    otherwise as a positive amount (D-068)."""
    if config is None:
        return "positive"
    try:
        return config.metric(metric_id).sign
    except KeyError:
        return "positive"


def _input_figure_form(mark: page_marks.Mark, ctx: ReviewContext, target: dict) -> None:
    """Record the marked number as an input of a calculation (D-052), with the box as its evidence."""
    suffix = _mark_key(mark)
    with st.container(border=True):
        st.markdown(f"**Record as calculation input: {metric_view.esc(target['label'])}**")
        unit = target.get("unit")
        if unit is None:  # a new name typed in a formula: the reviewer says what kind of figure it is
            guessed = "fraction" if mark.printed_value and parse_number(mark.printed_value).is_percent else (
                "ETB" if mark.unit.currency else "count")
            unit = st.selectbox("Kind of figure", list(review_panel.UNIT_LABELS), format_func=review_panel.UNIT_LABELS.get,
                                index=list(review_panel.UNIT_LABELS).index(guessed), key=f"input_unit{suffix}")
        suggestion = page_marks.suggested_value(mark, unit)
        converted = suggestion is not None and suggestion.value is not None
        sign = input_figure_sign(ctx.config, target["metric"])
        value = review_panel.value_box(
            "Value", unit, f"input_value_{target['variable']}{suffix}", sign=sign,
            default=value_input.input_text(value_input.stored_on_approval(suggestion.value, sign), suggestion.unit)
            if converted else "")
        if converted:
            st.caption(f"Read from the box: {mark.printed_value} ({suggestion.basis}).")
        else:
            st.caption(not_converted_reason(mark, suggestion, target["label"], unit))
        note = st.text_area("Note", value=f"Marked on PDF page {mark.page_index + 1}", key=f"input_note{suffix}",
                            height=70)
        problem_slot = st.empty()
        save_col, cancel_col = st.columns([3, 1])
        if cancel_col.button("Cancel", key=f"input_cancel{suffix}", width="stretch"):
            st.session_state.pop(calc_panel.CALC_INPUT, None)
            st.session_state.pop("marked", None)
            st.rerun()
        if not save_col.button("Save input", type="primary", icon=":material/save:", width="stretch",
                               key=f"input_save{suffix}"):
            return
        problem = mark_input_problem(value, note)
        if problem is not None:
            problem_slot.error(problem, icon=":material/error:")
            return
        page, bbox = mark.page_index + 1, mark.bbox

        def back_to_calculation() -> None:
            st.session_state.pop(calc_panel.CALC_INPUT, None)
            st.session_state.selected_metric, st.session_state.detail_mode = target["metric"], "metric"
            st.session_state.calc_open = target["metric"]

        review_panel.run_action(ctx, lambda conn: record_input_figure(
            conn, ctx.document_id, target["variable"], target["label"], value.value, unit, page, bbox,
            mark.printed_value or "", note, ctx.reviewer), f"{target['label']} recorded for the calculation.",
            on_success=back_to_calculation)


def _calc_input_banner() -> None:
    """While a calculation waits for a figure, say which one is being marked."""
    target = st.session_state.get(calc_panel.CALC_INPUT)
    if target is None:
        return
    text_col, cancel_col = st.columns([4, 1], vertical_alignment="center")
    text_col.info(f"Marking **{metric_view.esc(target['label'])}** for a calculation: drag a box around the number, "
                  "or pick it in All Figures with Use this figure.", icon=":material/calculate:")
    if cancel_col.button("Cancel", key="calc_input_cancel", width="stretch"):
        st.session_state.pop(calc_panel.CALC_INPUT, None)
        st.rerun()


UNIT_EXAMPLES = value_input.EXAMPLES
UNIT_KINDS = {"ETB": "an amount in full Birr: type it, e.g. 1,234,567,000 or 1,234.567 million",
              "count": "a count: type the whole number, e.g. 10,627",
              "fraction": "a percentage: type it, e.g. 29% or 0.29"}


def _mark_key(mark: page_marks.Mark) -> str:
    """Widget key suffix for a mark: the same for the same page and box on every rerun."""
    box = "_".join(f"{v:.1f}" for v in mark.bbox)
    return f"_{mark.page_index}_{hashlib.sha1(box.encode()).hexdigest()[:8]}"


def not_converted_reason(mark: page_marks.Mark, suggestion: Normalized | None, metric_name: str, expected_unit: str) -> str:
    """Why the Value box is empty, in plain words, and what to type instead."""
    kind = UNIT_KINDS.get(expected_unit, f"in {expected_unit}: type the value")
    if mark.printed_value is None:
        return "The box does not hold exactly one readable number: type the value."
    printed_as = None
    if parse_number(mark.printed_value).is_percent:
        printed_as = "a percentage"
    elif mark.unit.currency:
        printed_as = "in Birr" if mark.unit.currency == "ETB" else f"in {mark.unit.currency}"
    mismatch = {"fraction": printed_as not in (None, "a percentage"), "count": printed_as is not None,
                "ETB": printed_as == "a percentage"}.get(expected_unit, False)
    if mismatch:
        return f"This figure is {printed_as}; {metric_name} is {kind}."
    issue = (suggestion.issue if suggestion is not None else None) or "It could not be converted."
    return f"{issue} {metric_name} is {kind}."


def mark_input_problem(value: str | value_input.TypedValue, note: str) -> str | None:
    """What stops the marked value from being saved, or None. Nothing is saved or cleared on a problem.

    value: what the value box read (value_input, D-067), or the typed text."""
    if isinstance(value, value_input.TypedValue):
        if value.value is None:
            return value.problem or "Type the value first: the Value box is empty."
    elif not (value or "").strip():
        return "Type the value first: the Value box is empty."
    elif value_input.split_typed(value).number is None:
        return value_input.split_typed(value).problem
    if not (note or "").strip():
        return "Write a note: it is saved with the value as its reason."
    return None


def _mark_summary() -> None:
    mark: page_marks.Mark | None = st.session_state.get("marked")
    if mark is None:
        if st.session_state.get("mark_mode"):
            st.caption("Drag a box around the right number on the page.")
        return
    text_col, clear_col = st.columns([4, 1], vertical_alignment="center")
    found = (f"number **{metric_view.esc(mark.printed_value)}**" if mark.printed_value
             else f"“{metric_view.esc(mark.text)}”" if mark.text else "no readable text (type the value yourself)")
    text_col.markdown(f":blue[:material/crop_free:] Your mark on page {mark.page_index + 1}: {found}")
    if clear_col.button("Clear", key="clear_mark", width="stretch"):
        st.session_state.pop("marked", None)
        st.rerun()


# --- right: details ----------------------------------------------------------------

def _details_panel(extraction: ExtractionResult, analysis: AnalysisResult, selected: RawCandidate | None,
                   ctx: ReviewContext | None, states: dict[str, ReviewState],
                   store: Callable[[], ReviewContext | None] | None = None) -> None:
    with st.container(border=True):
        st.markdown("**Metric Details**" if st.session_state.get("detail_mode") == "metric" else "**Figure Details**")
        metric_id = st.session_state.get("selected_metric")
        if st.session_state.get("detail_mode") == "metric" and metric_id in {m.metric.id for m in analysis.metrics}:
            result = analysis.metric(metric_id)
            key = st.session_state.get("selected_key")
            shown = next((mc for mc in result.candidates if mc.mapped.key == key), result.primary)
            state = states.get(metric_id)
            review_label = None
            if state is not None and state.status in metric_view.REVIEW_LABELS:
                label, color, _ = metric_view.REVIEW_LABELS[state.status]
                review_label = (label, color)
            rejected_keys = frozenset()
            if ctx is not None and state is not None:
                rejected_keys = frozenset(k for (mid, k), cid in ctx.candidate_ids.items()
                                          if mid == metric_id and cid in state.rejected_candidate_ids)
            view_result, no_figure_text = result, None
            if shown is None and state is not None and state.status in ("approved", "corrected"):
                # a value was recorded without an extracted figure: "not found" would contradict it
                view_result = replace(result, findings=[f for f in result.findings if f.rule_id != "NOT-FOUND"])
                no_figure_text = ("Calculated by the reviewer from other figures (see below)."
                                  if ctx is not None and metric_id in ctx.note_calcs else
                                  "No printed figure was extracted for this metric; the value below was recorded "
                                  "by a reviewer.")
            metric_view.metric_details(
                view_result, shown,
                on_view=lambda c: _show_candidate(c, extraction),
                on_show_page=_go_to,
                review_label=review_label,
                rejected_keys=rejected_keys,
                review_controls=lambda: review_panel.review_section(ctx, result, shown, state),
                **({"no_figure_text": no_figure_text} if no_figure_text else {}),
            )
        elif selected is not None:
            _figure_details(extraction, selected, analysis, ctx, store)
        else:
            st.caption("Select a metric (or a figure under All Figures) to see its source and details.")


def _column_note(extraction: ExtractionResult, figure: RawCandidate) -> tuple[str, bool] | None:
    """Which year column a table figure sits in, against the report year: (text, is_warning)."""
    if figure.extraction_method != TABLE_METHOD:
        return None
    year = column_year(figure, extraction.pages[figure.page_index])
    period = (st.session_state.get("period") or "").strip()
    end = report_end_year(period)
    if year is None:
        return "No year heading found above this number: check which year it is on the page.", False
    if end is None:
        return f"This number is in the {year} column. Enter the report year to check it is the right year.", False
    if year == end:
        return f"This number is in the {year} column, the year report {period} ends: the right column.", False
    which = "last year's" if year == end - 1 else "another year's"
    return (f"This number is in the {year} column, which is {which} figure. Report {period} ends in {end}: "
            f"for {period}, use the {end} column.", True)


def _figure_details(extraction: ExtractionResult, selected: RawCandidate, analysis: AnalysisResult,
                    ctx: ReviewContext | None, store: Callable[[], ReviewContext | None] | None = None) -> None:
    st.badge("Raw figure · not matched to a metric here", color="gray")
    if selected.extraction_method == TABLE_METHOD:
        st.markdown(f"#### {metric_view.esc(selected.raw_label or '(no label)')}")
    st.markdown(f"## {metric_view.esc(selected.raw_value_text)}")
    _field("Source", f"PDF page {selected.page_index + 1} (printed page number not detected)")
    column_note = _column_note(extraction, selected)
    if selected.extraction_method == TABLE_METHOD:
        year = column_year(selected, extraction.pages[selected.page_index])
        _field("Year column", str(year) if year else "not identified")
        if column_note is not None and column_note[1]:
            st.warning(column_note[0], icon=":material/calendar_month:")
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

    mark: page_marks.Mark | None = st.session_state.get("marked")
    if (mark is not None and mark.from_figure and mark.page_index == selected.page_index
            and tuple(mark.bbox) == tuple(selected.bbox)):
        _mark_form(mark, analysis, ctx, store, column_note)
    elif st.button("Use this figure for a metric", icon=":material/add_task:", type="primary", width="stretch",
                   key="use_figure", help="Record this exact number, on this page, as the value of a metric."):
        st.session_state.marked = page_marks.mark_from_figure(selected, extraction.pages[selected.page_index])
        st.rerun()


def _field(name: str, value: str) -> None:
    st.caption(name)
    st.markdown(metric_view.esc(value))
