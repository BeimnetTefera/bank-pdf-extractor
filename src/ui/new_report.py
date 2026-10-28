"""New report, as in the redesign mockup (screen 2, D-092): upload the PDF, give the bank, fiscal
year and metrics, then Read report reads it, saves it and opens it on the Review page.

The reading and saving are the Extract page's own (extract_review); a PDF already saved opens its
saved review instead of starting a second one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import closing
from html import escape

import streamlit as st

from src import ocr
from src.analysis import report_end_year
from src.config_loader import AppConfig
from src.database import connect
from src.document_manager import DocumentError, compute_sha256, validate_pdf
from src.project_service import find_documents_by_hash
from src.report_period import suggested_period
from src.ui import extract_review

STEPS = ("Upload PDF", "Report details", "Read the report", "Review figures")


def render(config: AppConfig, open_review: Callable[[], None]) -> None:
    st.html('<div class="bpe-crumb">Reports / New report</div>')
    st.markdown("## New report")
    st.html('<p class="bpe-sub">The PDF is read on this computer. Nothing is approved until a reviewer approves it.</p>')
    upload = st.session_state.get("nr_upload_data")
    with st.container(key="panel_newreport"):
        st.html(_steps(1 if upload else 0))
        form_col, side_col = st.columns([3, 1.4], gap="large")
        with form_col:
            _form(config, upload, open_review)
        with side_col:
            st.markdown("**What happens next**")
            for n, (title, text) in enumerate((
                    ("The app reads every page", "Tables and sentences are searched for each metric. Scanned "
                                                 "pages are read with OCR on this computer."),
                    ("Each metric gets a suggestion", "With its page, row, year column and unit, and any warnings."),
                    ("You approve or correct it", "Every decision is saved with your name and the time. Nothing "
                                                  "is overwritten.")), start=1):
                st.html(f'<div class="bpe-next"><span class="bpe-stepnum">{n}</span><span><b>{escape(title)}</b>'
                        f'{escape(text)}</span></div>')


def _steps(done: int) -> str:
    items = []
    for i, label in enumerate(STEPS):
        state = "done" if i < done else "on" if i == done else ""
        mark = "✓" if i < done else str(i + 1)
        items.append(f'<li class="{state}"><b>{mark}</b>{escape(label)}</li>')
    return f'<ol class="bpe-steps">{"".join(items)}</ol>'


def _form(config: AppConfig, upload: dict | None, open_review: Callable[[], None]) -> None:
    if upload is None:
        uploaded = st.file_uploader("Annual report PDF", type=["pdf"], key="nr_upload",
                                    help="One annual report; it is read on this computer only.")
        if uploaded is not None:
            _take_upload(config, uploaded.getvalue(), uploaded.name)
        return
    name_col, replace_col = st.columns([5, 1], vertical_alignment="center")
    name_col.html(f'<div class="bpe-filerow"><span class="bpe-pdf">PDF</span><div><div style="font-weight:500;'
                  f'overflow-wrap:anywhere">{escape(upload["name"])}</div><div class="bpe-minor bpe-num">'
                  f'{upload["pages"]} pages</div></div></div>')
    replace_col.button("Replace", key="nr_replace", type="tertiary", on_click=_clear)
    bank_col, year_col = st.columns(2)
    with bank_col:
        st.selectbox("Bank", config.banks, key="nr_bank", index=None, placeholder="Choose a bank")
        st.caption("From the list of banks in the Shega dataset.")
    with year_col:
        st.text_input("Fiscal year", key="nr_year", placeholder="2025/26")
        end_year = report_end_year(st.session_state.get("nr_year") or "")
        if upload.get("suggested") and st.session_state.get("nr_year") == upload["suggested"]:
            st.html('<span style="font-size:12px;color:#17663F">Suggested from the period stated in the PDF</span>')
        st.caption("Format YYYY/YY." + (f" Figures are taken from the {end_year} year column." if end_year else ""))
    names = {m.id: m.display_name for m in config.active_metrics()}
    choice = st.radio("Metrics to find", ["all", "choose"], key="nr_metrics_choice",
                      format_func={"all": f"All bank metrics · {len(names)} metrics of the Shega bank table",
                                   "choose": "Choose metrics · only the metrics you pick are searched"}.get)
    if choice == "choose":
        st.multiselect("Metrics", list(names), key="nr_metrics", format_func=names.get)
    cancel_col, read_col = st.columns([4, 1.3])
    cancel_col.button("Cancel", key="nr_cancel", on_click=_clear)
    if read_col.button("Read report", key="nr_read", type="primary", width="stretch"):
        _read(config, upload, list(names) if choice == "all" else st.session_state.get("nr_metrics") or [], open_review)


def _take_upload(config: AppConfig, data: bytes, name: str) -> None:
    try:
        pages = validate_pdf(data, name, config.settings)
    except DocumentError as exc:
        st.error(str(exc), icon=":material/error:")
        return
    sha = compute_sha256(data)
    suggestion = suggested_period(data)
    st.session_state.nr_upload_data = {"name": name, "bytes": data, "sha": sha, "suggested": suggestion,
                                       "pages": pages}
    if suggestion and not st.session_state.get("nr_year"):
        st.session_state.nr_year = suggestion
    st.rerun()


def _clear() -> None:
    for key in ("nr_upload_data", "nr_bank", "nr_year", "nr_metrics", "nr_metrics_choice", "nr_upload"):
        st.session_state.pop(key, None)


def _read(config: AppConfig, upload: dict, metric_ids: list[str], open_review: Callable[[], None]) -> None:
    """Read the PDF (OCR for picture pages), save the report, and open it on the Review page."""
    with closing(connect(config.settings.database_path)) as conn:
        existing = find_documents_by_hash(conn, upload["sha"])
    if existing:
        error = extract_review.open_saved(config, existing[-1]["id"])
        if error:
            st.error(error, icon=":material/error:")
            return
        st.toast(f"This PDF is already saved as {existing[-1]['bank_name']} {existing[-1]['reporting_period']}: "
                 "its review is open.", icon=":material/content_copy:")
        _clear()
        open_review()
        return
    if not st.session_state.get("nr_bank"):
        st.error("Choose the bank.", icon=":material/error:")
        return
    if report_end_year(st.session_state.get("nr_year") or "") is None:
        st.error("Enter the fiscal year like 2025/26.", icon=":material/error:")
        return
    if not metric_ids:
        st.error("Choose at least one metric.", icon=":material/error:")
        return
    doc = {"name": upload["name"], "bytes": upload["bytes"], "sha": upload["sha"], "file_id": f"new-{upload['sha'][:12]}"}
    if not _read_picture_pages(config, doc):
        return
    st.session_state.doc = doc
    st.session_state.bank = st.session_state.nr_bank
    st.session_state.period = st.session_state.nr_year.strip()
    st.session_state.metric_selection = metric_ids
    st.session_state.viewer_page = 1
    for key in ("selected_key", "selected_metric", "detail_mode", "marked"):
        st.session_state.pop(key, None)
    st.session_state.ocr_missing = 0
    with st.spinner("Reading the report..."):
        p_extraction, p_analysis = _extract_and_analyse(config, doc, metric_ids)
        ctx = extract_review._store_report(config, doc, p_analysis, p_extraction)
    if ctx is None:
        return
    _clear()
    st.toast(f"Saved {st.session_state.bank} {st.session_state.period}.", icon=":material/save:")
    open_review()


def _read_picture_pages(config: AppConfig, doc: dict) -> bool:
    """Read scanned pages with OCR first, so their figures are part of the saved report."""
    needed = extract_review._pages_needing_ocr(doc["sha"], doc["bytes"])
    if not needed:
        return True
    path = ocr.cache_path(config.settings, doc["sha"])
    missing = [p for p in needed if p not in ocr.load_cache(path)]
    if not missing:
        return True
    tesseract_dir = config.settings.tesseract_dir
    engine = extract_review._ocr_engine(str(tesseract_dir) if tesseract_dir else None)
    if engine is None:
        st.error(f"{len(missing)} page(s) are scanned pictures and Tesseract OCR is not installed, so they cannot be "
                 "read. Install it (see Settings), then press Read report again.", icon=":material/error:")
        return False
    bar = st.progress(0.0, text="Reading scanned pages with OCR...")
    ocr.run_ocr(doc["bytes"], missing, engine, path,
                on_page=lambda done, total: bar.progress(done / total, text=f"OCR: page {done} of {total}"))
    return True


def _extract_and_analyse(config: AppConfig, doc: dict, metric_ids: list[str]):
    needed = extract_review._pages_needing_ocr(doc["sha"], doc["bytes"])
    tesseract_dir = config.settings.tesseract_dir
    engine = extract_review._ocr_engine(str(tesseract_dir) if tesseract_dir else None) if needed else None
    words = {p: w for p, w in ocr.load_cache(ocr.cache_path(config.settings, doc["sha"])).items() if p in needed}
    extraction = extract_review._extract(doc["sha"], tuple(sorted(words)), doc["bytes"], words,
                                         engine.version if engine else "")
    fingerprint = hashlib.sha256(repr((config.metrics, config.mappings, config.learned_aliases)).encode()).hexdigest()
    analysis = extract_review._analyse(doc["sha"], st.session_state.period, st.session_state.bank or "",
                                       tuple(metric_ids), fingerprint, extraction, config)
    return extraction, analysis
