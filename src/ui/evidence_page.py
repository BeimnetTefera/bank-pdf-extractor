"""Evidence page (one metric of one report) and Evidence summary (every metric of a report).

Addresses: /evidence?report=<id>&metric=<metric id>, /evidence?sector=<YYYY/YY>&metric=<metric id>
(a sector value, D-056) and /evidence_summary?report=<id>. They load everything from the
database, so a link from Excel or the evidence PDF (a new session) shows the same as a link
inside the app. The logic is in src/evidence.py; cross-checks can be added and withdrawn here.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from decimal import Decimal

import pandas as pd
import pymupdf
import streamlit as st

from src.calculated_entries import number_text
from src.config_loader import AppConfig
from src.database import connect
from src.evidence import SECTOR_ACTIONS, EvidenceError, MetricEvidence, PdfWords, calculated_columns, evidence_url
from src.evidence import format_value, load_report_pdf, metric_evidence, report_evidence, sector_evidence
from src.evidence_pdf import summary_pdf
from src.external_sources import SavedSource, add_cross_check, source_crop_png, source_page_png, withdraw_cross_check
from src.pdf_render import focus_of, render_crop_png, render_page_png
from src.project_service import get_document, list_documents
from src.review_service import ReviewError
from src.ui import extract_review, routes, source_form, style
from src.ui.metric_view import esc

STATUS_COLORS = {"approved": "green", "pending": "blue", "needs_review": "orange", "not_found": "gray",
                 "not_reported": "violet"}


@st.cache_data(show_spinner=False, max_entries=50)
def _page_png(sha256: str, page_index: int, boxes: tuple, _pdf: bytes) -> bytes:
    """boxes: ((box, kind, label), ...) with kind 'focus' (blue: the value) or 'input' (green: a calculation input)."""
    return render_page_png(_pdf, page_index, zoom=1.2, boxes=boxes)


@st.cache_data(show_spinner=False, max_entries=200)
def _crop_png(sha256: str, page_index: int, box: tuple, _pdf: bytes, scale: float = 2.5, kind: str = "focus") -> bytes:
    return render_crop_png(_pdf, page_index, box, scale=scale, kind=kind)


# --- Evidence page ------------------------------------------------------------------------

def render_evidence(config: AppConfig) -> None:
    if st.query_params.get("sector"):
        st.markdown("## Evidence")
        _render_sector(config, st.query_params["sector"], st.query_params.get("metric", ""))
        return
    report_id, metric_id = st.query_params.get("report"), st.query_params.get("metric")
    with closing(connect(config.settings.database_path)) as conn:
        named = report_id and metric_id and get_document(conn, report_id) is not None
    if not named:  # the record itself has no pickers; they help only when the address names no value
        st.markdown("## Evidence")
        report_id, metric_id = _pickers(config, with_metric=True)
    if report_id is None or metric_id is None:
        return
    with closing(connect(config.settings.database_path)) as conn:
        pdf, words = _report_pdf(config, get_document(conn, report_id))
        try:
            ev = metric_evidence(conn, config, report_id, metric_id, words)
        except EvidenceError as exc:
            st.error(str(exc), icon=":material/error:")
            return
        finally:
            if words is not None:
                words.close()

    # Laid out as the redesign mockup's evidence record (screen 6, D-092).
    _record_header(config, ev, pdf)
    with st.container(key="panel_ev_how"):
        if ev.note_calc is not None or (ev.source_type == "calculated" and ev.calculation is not None):
            _worked_out(config, ev, pdf)
        else:
            st.markdown("#### Where the value is printed")
            _source_block(config, ev, pdf)
            _note_block(ev)
    history_col, cross_col = st.columns([3, 2], gap="medium")
    with history_col, st.container(key="panel_ev_history"):
        _history_block(ev)
    with cross_col, st.container(key="panel_ev_cross"):
        _cross_check_block(config, ev)


def _report_pdf(config: AppConfig, document) -> tuple[bytes | None, PdfWords | None]:
    """The stored PDF and its words, or (None, None) with a warning when it cannot be read."""
    if document is None:
        return None, None
    try:
        pdf = load_report_pdf(config.settings, document)
    except EvidenceError as exc:
        st.warning(f"The page image cannot be shown. {exc}", icon=":material/warning:")
        return None, None
    return pdf, PdfWords(pdf)


def _worked_out(config: AppConfig, ev: MetricEvidence, pdf: bytes | None) -> None:
    """A calculated value as in the mockup: the arithmetic and its check, the note, the input lines
    boxed in green, and a table of inputs named by their printed row and year column."""
    st.markdown("#### How the value was worked out")
    if ev.calculation is not None and ev.source_type == "calculated":
        arithmetic, ok = ev.calculation.arithmetic, True
        stated = ev.value
        figures = [(i.label, i.printed or number_text(i.value, i.unit), number_text(i.value, i.unit), i.page, i.box,
                    i.column_year) for i in ev.calc_inputs]
    else:
        c = ev.note_calc
        arithmetic, ok, stated = c.arithmetic, c.matches, c.stated
        figures = [(f.label, f.printed, number_text(f.value, f.unit), f.page, f.box, None) for f in c.figures]
    names = _row_names(config, ev.document["id"], [(pg, box) for _, _, _, pg, box, _ in figures])
    st.html(f'<div class="bpe-arithbox">{escape(arithmetic)}</div>')
    if ok:
        st.html(f'<span class="bpe-pass">✓ The arithmetic gives the saved {escape(f"{stated:f}")} (within rounding).</span>')
    else:
        st.html(f'<span class="bpe-fail">✗ The arithmetic does not give the saved {escape(f"{stated:f}")}. '
                'Check the note and the figures.</span>')
    note = ev.entry_reason or ev.approval_note
    if note:
        st.html(f'<blockquote class="bpe-quote">Reviewer\'s note: {escape(note)}</blockquote>')
    pages: dict[int, list[tuple]] = {}
    for _, _, _, page, box, _ in figures:
        if page and box is not None:
            pages.setdefault(page, []).append((tuple(box), ""))
    if pdf is not None:
        for page, boxes in sorted(pages.items()):
            st.image(extract_review._band(ev.document["sha256"], page - 1, tuple(boxes), pdf), width="stretch")
    rows = []
    for (label, printed, full, page, box, year), name in zip(figures, names):
        shown = name or (f"{label} {year}" if year else label) or "Figure"
        where = f"PDF page {page}" if page else "not found on the pages the note names"
        rows.append(f'<tr><td>{escape(shown)}</td><td class="r bpe-num">{escape(printed)}</td>'
                    f'<td class="r bpe-num">{escape(full)}</td><td>{escape(where)}</td></tr>')
    st.html('<table class="bpe-itable"><thead><tr><th>Input</th><th class="r">As printed</th>'
            '<th class="r">Full value</th><th>Where</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")
    for calc_input, source in ev.calc_input_sources:  # an input taken from another document says which (D-081)
        st.markdown(f"**{esc(calc_input.label)}** comes from an external source:")
        _external_source_block(config, source, calc_input.label)


def _row_names(config: AppConfig, document_id: str, places: list[tuple]) -> list[str | None]:
    """Each input named by the row it is printed in and its year column ("Total equity 2023")."""
    names = []
    with closing(connect(config.settings.database_path)) as conn:
        for page, box in places:
            if not page or box is None:
                names.append(None)
                continue
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            found = None
            for r in conn.execute("SELECT raw_label, period_year, bbox FROM candidates WHERE document_id = ? AND "
                                  "page_index = ?", (document_id, page - 1)):
                x0, y0, x1, y1 = json.loads(r["bbox"])
                if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
                    found = " ".join(str(v) for v in (r["raw_label"], r["period_year"]) if v) or None
                    break
            names.append(found)
    return names


EV_CHIPS = {"approved": "ok", "pending": "warn", "needs_review": "warn", "not_found": "muted", "not_reported": "muted",
            "rejected": "bad"}


def _record_header(config: AppConfig, ev: MetricEvidence, pdf: bytes | None) -> None:
    """Crumb, status and source, the metric's name, the actions, then the four facts."""
    st.html(f'<div class="bpe-crumb">Reports / {escape(ev.report_label)} / Evidence</div>')
    title_col, actions_col = st.columns([3, 2], vertical_alignment="bottom")
    with title_col:
        chips = style.chip(ev.status_label, EV_CHIPS.get(ev.status, "muted"))
        if ev.source_type == "calculated" or ev.note_calc is not None:
            chips += " " + style.chip("Calculated", "calc")
        st.html(f'<div style="display:flex;gap:6px">{chips}</div>')
        st.markdown(f"## {esc(ev.display_name)}")
        st.html(f'<p class="bpe-sub">Excel column: {escape(ev.export_column or "none (Source Evidence only)")} · '
                f'{escape(ev.report_label)}</p>')
    with actions_col:
        copy_col, pdf_col, open_col = st.columns([1, 1.5, 1.2])
        with copy_col.popover("Copy link", width="stretch"):
            st.code(evidence_url(config.settings.app_url, ev.document["id"], ev.metric_id), language=None)
        if pdf_col.button("Download evidence PDF", key="ev_pdf", width="stretch"):
            generated = datetime.now(timezone.utc).isoformat(timespec="minutes").replace("+00:00", "")
            st.session_state._ev_pdf = (ev.metric_id, summary_pdf(
                [([ev], {})], config.settings.app_url, f"Evidence: {ev.display_name} · {ev.report_label}",
                generated.replace("T", " ")))
        ready = st.session_state.get("_ev_pdf")
        if ready and ready[0] == ev.metric_id:
            pdf_col.download_button("Save PDF", ready[1], file_name=f"evidence_{ev.metric_id}.pdf",
                                    mime="application/pdf", key="ev_pdf_save", width="stretch")
        if open_col.button("Open in review", key="ev_open_review", width="stretch"):
            _open_in_viewer(config, ev, pdf, target="review")
    if ev.value is None:
        value, note = ("Not reported", "no value, not zero") if ev.source_type == "not_reported" else ("—", "no value")
    elif ev.unit == "fraction":
        value, note = f"{ev.value.normalize():f}", f"{(ev.value * 100).normalize():f}%"
    else:
        value, note = ev.value_text, ev.unit or ""
    pages = ([f"PDF page {i.page}" for i in ev.calc_inputs if i.page] if ev.calc_inputs else
             [f"PDF page {f.page}" for f in ev.note_calc.figures if f.page] if ev.note_calc else
             [f"PDF page {ev.page}"] if ev.page else [])
    approved = ev.approved_by or ev.entered_by or "not approved yet"
    inputs = len(ev.calc_inputs) if ev.calc_inputs else len(ev.note_calc.figures) if ev.note_calc else 0
    source = f"Calculated from {inputs} printed figure{'s' if inputs != 1 else ''}" if inputs else ev.source_label
    when = (ev.approved_at or ev.entered_at or "")[:16].replace("T", " ")
    style.stat_strip([
        ("Value", value, note),
        ("Source", source, " and ".join(dict.fromkeys(pages)) or ""),
        ("Approved by" if ev.approved_by else "Entered by", approved, f"{when} UTC" if when else ""),
        ("Report file", ev.document["original_filename"], f"{ev.document['page_count']} pages"),
    ])


def _header(ev: MetricEvidence) -> None:
    title_col, link_col = st.columns([4, 1], vertical_alignment="center")
    with title_col:
        st.markdown(f"### {esc(ev.display_name)}")
        st.caption(f"{esc(ev.report_label)} · {esc(ev.document['original_filename'])}")
    with link_col:
        routes.link("evidence_summary", "Evidence summary", ":material/table_view:", report=ev.document["id"])
    badge_col, source_col, _ = st.columns([1, 1, 3])
    with badge_col:
        st.badge(ev.status_label, color=STATUS_COLORS[ev.status])
    with source_col:
        st.badge(ev.source_label, color="violet", icon=":material/source:")
    with st.container(border=True):
        if ev.source_type == "not_reported":
            pending = " (awaiting approval)" if ev.status == "pending" else ""
            st.markdown(f"**Not reported{pending}:** the report does not disclose this figure. No value, and not "
                        "the same as zero. The reason is below.")
        elif ev.value is None:
            st.markdown("**No value.** This is not the same as zero." if ev.status == "not_found" else "**No value.**")
        else:
            suffix = " · awaiting approval" if ev.status == "pending" else (
                " · suggested, not reviewed" if ev.status == "needs_review" else "")
            st.markdown(f"## {esc(ev.value_text)}")
            st.caption(f"Unit: {ev.unit}{suffix}")
        if ev.conversion:
            st.markdown(f"As printed: `{esc(ev.conversion)}`")
        if ev.sign_text:
            st.caption(ev.sign_text)
        if ev.read_by_ocr:
            st.warning("Read by OCR from a picture of the page: check every digit.", icon=":material/document_scanner:")
        if ev.approved_by:
            st.caption(f"Approved by **{esc(ev.approved_by)}** on {ev.approved_at[:16].replace('T', ' ')} UTC")
        if ev.entered_by:
            st.caption(f"Entered by **{esc(ev.entered_by)}** on {ev.entered_at[:16].replace('T', ' ')} UTC")


def _note_block(ev: MetricEvidence) -> None:
    if not (ev.entry_reason or ev.approval_note):
        return
    st.markdown("#### Note")
    if ev.entry_reason:
        st.caption("Reason given when the value was entered")
        st.text(ev.entry_reason)
    if ev.approval_note:
        st.caption("Note at approval")
        st.text(ev.approval_note)


def _source_block(config: AppConfig, ev: MetricEvidence, pdf: bytes | None) -> None:
    if ev.source_type == "calculated":
        _calculation_block(config, ev, pdf)
        return
    if ev.external is not None:
        st.markdown("#### Source: external document")
        _external_source_block(config, ev.external, ev.display_name)
        return
    st.markdown("#### Source")
    if ev.page is None:
        st.caption("No source page: no figure was found or entered for this metric.")
        return
    if ev.note_calc is not None:  # a worked-out value: the calculation and every figure it used, in green
        marked = (" The box the reviewer marked (PDF page " + str(ev.page) + ") is one of those figures, "
                  "not the value." if ev.box_is_input else "")
        st.info(f"{ev.value_text} is not printed in the report: it was worked out from other figures.{marked} The "
                "calculation is below, with every figure it used boxed in green on its page.",
                icon=":material/calculate:")
        _note_calculation_block(ev, pdf)
        if st.button("Open in viewer at this page", icon=":material/open_in_new:", key="evidence_open_viewer"):
            _open_in_viewer(config, ev, pdf)
        return
    st.markdown(f"PDF page {ev.page} · {esc(box_origin(ev))}" + (f" · label: {esc(ev.label)}" if ev.label else ""))
    if ev.typed_page:
        st.caption(f"The value was saved with page {ev.typed_page}; the number is printed on page {ev.page}, "
                   "the page named in the note.")
    if pdf is not None:
        page_col, crop_col = st.columns([3, 2])
        kind = "input" if ev.box_is_input else "focus"
        label = "marked figure (not the value)" if ev.box_is_input else ev.display_name
        with page_col:
            boxes = ((tuple(ev.box), kind, label),) if ev.box is not None else ()
            st.image(_page_png(ev.document["sha256"], ev.page - 1, boxes, pdf), width="stretch")
        with crop_col:
            if ev.box_is_input:
                st.warning(f"The marked box holds a different number from {ev.value_text}: the value was worked "
                           "out, but the note does not show how. Record it with Calculate so each figure used is "
                           "marked.", icon=":material/warning:")
            if ev.box is not None:
                st.caption("Close-up of the line")
                st.image(_crop_png(ev.document["sha256"], ev.page - 1, tuple(ev.box), pdf, kind=kind), width="stretch")
            else:
                st.warning(f"{ev.value_text} is not printed on page {ev.page} (as printed or in thousands, millions "
                           "or billions), so there is no box. If it was worked out from other figures, record it "
                           "with Calculate so each figure used is marked.", icon=":material/search_off:")
    if st.button("Open in viewer at this page", icon=":material/open_in_new:", key="evidence_open_viewer"):
        _open_in_viewer(config, ev, pdf)


def _external_source_block(config: AppConfig, source: SavedSource, label: str) -> None:
    """Title, publisher, address as text, the quote, the conversion, and the uploaded page with its box in blue."""
    st.markdown(source_form.source_lines(source))
    st.markdown(f"> {esc(source.quote)}")
    st.markdown(f"Value as printed → stored: `{esc(source.conversion)}`")
    st.caption(f"Recorded by {source.reviewer} on {source.created_at[:16].replace('T', ' ')} UTC")
    if not source.has_file:
        st.caption("No copy was uploaded: find the document at the address above.")
        return
    try:
        page = _source_png(source.file_sha256, source.page, source.box, label, config.settings.data_dir, source)
        crop = _source_crop(source.file_sha256, source.page, source.box, config.settings.data_dir, source)
    except ReviewError as exc:
        st.warning(f"The uploaded copy cannot be shown. {exc}", icon=":material/warning:")
        return
    page_col, crop_col = st.columns([3, 2])
    page_col.image(page, width="stretch", caption=f"Page {source.page} of {source.file_name or 'the uploaded copy'}")
    with crop_col:
        if crop is not None:
            st.caption("Close-up of the line")
            st.image(crop, width="stretch")
        else:
            st.caption("No box was marked on the uploaded page.")


@st.cache_data(show_spinner=False, max_entries=50)
def _source_png(sha256: str, page: int, box: tuple | None, label: str, _data_dir, _source: SavedSource) -> bytes:
    """The uploaded page with the figure's box in blue (cached by file, page and box)."""
    return source_page_png(_data_dir, _source, label=label)


@st.cache_data(show_spinner=False, max_entries=100)
def _source_crop(sha256: str, page: int, box: tuple | None, _data_dir, _source: SavedSource) -> bytes | None:
    return source_crop_png(_data_dir, _source)


def _cross_check_block(config: AppConfig, ev: MetricEvidence) -> None:
    """External figures the approved value is checked against: flag, source, quote; withdraw or add one."""
    active = [c for c in ev.cross_checks if c.check.active]
    st.markdown(f"#### Cross-checks ({len(active)})")
    if not ev.cross_checks:
        st.caption("None. A cross-check compares the approved value with a figure from another document.")
    for c in ev.cross_checks:
        with st.container(border=True):
            if not c.check.active:
                w = c.check.withdrawal
                st.badge("Withdrawn", color="gray")
                st.caption(f"Withdrawn by {w.reviewer} on {w.created_at[:16].replace('T', ' ')} UTC: {w.reason}")
            else:
                st.badge(c.comparison.label, color={"match": "green", "mismatch": "red"}.get(c.comparison.status,
                                                                                         "gray"))
                st.markdown(esc(c.comparison.text))
            if c.check.note:
                st.caption(f"Note: {c.check.note}")
            _external_source_block(config, c.check.source, f"cross-check: {ev.display_name}")
            if c.check.active:
                _withdraw(config, c.check.id)
    with st.expander("Add a cross-check", icon=":material/add:"):
        key = f"ev_xcheck_{ev.metric_id}"
        source = source_form.source_form(key, ev.expected_unit, config.settings)
        note = st.text_input("Note (optional)", key=f"{key}_note")
        if st.button("Add cross-check", key=f"{key}_save"):
            _run(config, lambda conn: add_cross_check(conn, config.settings, ev.document["id"], ev.metric_id,
                                                      ev.expected_unit, source, note, _reviewer()),
                 "Cross-check added", lambda: source_form.clear_form(key))


def _withdraw(config: AppConfig, cross_check_id: int) -> None:
    with st.popover("Withdraw", icon=":material/undo:"):
        st.caption("The cross-check stays in the record, marked withdrawn with your reason.")
        reason = st.text_area("Why is it withdrawn?", key=f"withdraw_reason_{cross_check_id}",
                              placeholder="e.g. the source counts the group, not the bank")
        if st.button("Confirm withdraw", type="primary", key=f"withdraw_{cross_check_id}"):
            _run(config, lambda conn: withdraw_cross_check(conn, cross_check_id, _reviewer(), reason),
                 "Cross-check withdrawn")


def _reviewer() -> str:
    return st.session_state.get("reviewer", "")


def _run(config: AppConfig, action, success: str, on_success=None) -> None:
    """Run an action against a fresh connection; show the rule that blocked it, if any."""
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


def _render_sector(config: AppConfig, fiscal_year: str, metric_id: str) -> None:
    """Evidence for a sector value (one per fiscal year for every bank, D-056)."""
    with closing(connect(config.settings.database_path)) as conn:
        try:
            ev = sector_evidence(conn, config, fiscal_year, metric_id)
        except EvidenceError as exc:
            st.error(str(exc), icon=":material/error:")
            return
    title_col, link_col = st.columns([4, 1], vertical_alignment="center")
    with title_col:
        st.markdown(f"### {esc(ev.display_name)}")
        st.caption(f"Sector value for {ev.fiscal_year} · the same for every bank")
    with link_col:
        routes.link("sector_data", "Sector data", ":material/public:")
    badge_col, source_col, _ = st.columns([1, 1, 3])
    badge_col.badge(ev.status_label, color={"approved": "green", "pending": "blue", "not_reported": "violet"}.get(
        ev.status, "gray"))
    source_col.badge("External source", color="violet", icon=":material/source:")
    with st.container(border=True):
        not_reported = ev.proposal is not None and ev.proposal.entry_kind == "not_reported"
        if ev.status == "open":
            st.markdown("**No value** is recorded for this year. This is not the same as zero.")
        elif not_reported:
            st.markdown("**Not reported**" + (" (awaiting approval)" if ev.status == "pending" else "")
                        + f": no source gives this figure for {ev.fiscal_year}. Not zero.")
        else:
            st.markdown(f"## {esc(format_value(ev.shown_value, ev.proposal.unit))}")
            st.caption(f"Unit: {ev.proposal.unit}" + (" · awaiting approval" if ev.status == "pending" else ""))
        if ev.proposal is not None:
            st.caption(f"Entered by **{esc(ev.proposal.reviewer)}** on "
                       f"{ev.proposal.created_at[:16].replace('T', ' ')} UTC")
        if ev.approval is not None:
            st.caption(f"Approved by **{esc(ev.approval.reviewer)}** on "
                       f"{ev.approval.created_at[:16].replace('T', ' ')} UTC")
    if ev.proposal is not None and ev.proposal.reason:
        st.markdown("#### Note")
        st.text(ev.proposal.reason)
    if ev.source is not None:
        st.markdown("#### Source: external document")
        _external_source_block(config, ev.source, ev.display_name)
    st.markdown("#### Used for")
    st.caption("Banks with a saved report for this year (the export fills their rows): "
               + (", ".join(ev.banks) if ev.banks else "none yet"))
    st.markdown("#### History")
    st.dataframe(pd.DataFrame([{"When (UTC)": d.created_at[:16].replace("T", " "), "Who": d.reviewer,
                                "Action": "Marked Not reported" if d.entry_kind == "not_reported"
                                else SECTOR_ACTIONS[d.decision],
                                "Value": format_value(d.value, d.unit) if d.value is not None else "",
                                "Reason": d.reason} for d in ev.history]), hide_index=True, width="stretch")
    with st.expander("Metric definition (Templates)"):
        st.markdown(f"**{esc(ev.display_name)}** · Excel column `{ev.export_column or 'none'}`")
        st.write(ev.definition or "No definition in config/metrics.yaml.")


def _calculation_block(config: AppConfig, ev: MetricEvidence, pdf: bytes | None) -> None:
    """The formula, the arithmetic, and every input boxed in green on its own page."""
    calc = ev.calculation
    st.markdown("#### Calculation")
    if ev.stale:
        st.error("**Stale:** an input changed after this was calculated, so the value may be out of date. "
                 "Calculate again.  \n" + "  \n".join(f"• {esc(r)}" for r in ev.stale), icon=":material/history:")
    st.caption("Formula (as used)")
    st.code(f"{ev.metric_id} = {calc.expression}", language=None, wrap_lines=True)
    st.caption("In words")
    st.markdown(f"**{esc(ev.display_name)}** = {esc(calc.words)}")
    st.caption("With the numbers used")
    st.code(calc.arithmetic, language=None, wrap_lines=True)
    st.caption(f"Stored: {ev.value_text} (rounded) · calculated {calc.created_at[:16].replace('T', ' ')} UTC")

    st.markdown(f"##### Inputs ({len(ev.calc_inputs)})")
    external = {i.variable: f" · external source: {s.citation}" for i, s in ev.calc_input_sources}
    st.dataframe(pd.DataFrame([{
        "Input": i.label, "Name in formula": i.variable, "Value": number_text(i.value, i.unit),
        "Where it is": i.where + external.get(i.variable, ""), "Box": "yes" if i.box is not None else "no"}
        for i in ev.calc_inputs]),
        hide_index=True, width="stretch", column_config={"Where it is": st.column_config.TextColumn(width="large")})
    for i in ev.calc_inputs:
        if i.source_metric_id and i.source_document_id:
            routes.link("evidence", f"Evidence for {i.label}", ":material/verified:",
                        report=i.source_document_id, metric=i.source_metric_id)
    for i, source in ev.calc_input_sources:  # not printed in the report: the external document is the evidence
        with st.container(border=True):
            st.markdown(f"**{esc(i.label)}: {esc(number_text(i.value, i.unit))}** · from an external source")
            _external_source_block(config, source, i.label)

    st.markdown("##### Where each input is printed")
    st.caption(":green[■] green boxes: the figures this calculation used, each named on the page.")
    pages: dict[tuple[str, int], list] = {}
    for i in ev.calc_inputs:
        if i.page and i.source_document_id:
            pages.setdefault((i.source_document_id, i.page), []).append(i)
    if not pages:
        st.caption("No input has a page (all were typed without one).")
    for (document_id, page), inputs in pages.items():
        report_pdf, title = _input_pdf(config, ev, document_id, pdf)
        st.markdown(f"**PDF page {page}** · {esc(title)} · " + ", ".join(esc(i.label) for i in inputs))
        if report_pdf is None:
            st.caption("The PDF of that report cannot be read, so the page is not shown.")
            continue
        sha = ev.document["sha256"] if document_id == ev.document["id"] else document_id
        boxes = tuple((tuple(i.box), "input", i.label) for i in inputs if i.box is not None)
        page_col, crop_col = st.columns([3, 2])
        page_col.image(_page_png(sha, page - 1, boxes, report_pdf), width="stretch")
        with crop_col:
            for i in inputs:
                st.caption(f"{i.label}: {number_text(i.value, i.unit)}")
                if i.box is not None:
                    st.image(_crop_png(sha, page - 1, tuple(i.box), report_pdf, kind="input"), width="stretch")
                else:
                    st.caption("Typed without a box, and the number was not found on this page.")


def _note_calculation_block(ev: MetricEvidence, pdf: bytes | None) -> None:
    """The arithmetic a reviewer wrote in the note of a typed value, checked, with its figures in green."""
    calc = ev.note_calc
    st.markdown("#### Calculation written in the note")
    st.caption("Read from the reviewer's note, not recorded with Calculate. To store it with its inputs, open the "
               "metric in Extract & Review and use Calculate from other figures.")
    st.caption("As written")
    st.markdown(f"> {esc(calc.written)}")
    st.caption("Worked out again with the full numbers")
    st.code(calc.arithmetic, language=None, wrap_lines=True)
    if calc.matches:
        st.success(f"The arithmetic gives the stated {calc.stated:f} (within rounding).", icon=":material/check_circle:")
    else:
        st.error(f"The arithmetic gives {calc.result.quantize(Decimal('0.0001')).normalize():f}, not the stated "
                 f"{calc.stated:f}. Check the note and the figures.", icon=":material/error:")
    st.dataframe(pd.DataFrame([{
        "Figure": f.label or "(unnamed)", "As written in the note": f.printed, "Full value": number_text(f.value, f.unit),
        "Printed on": f"PDF page {f.page}" if f.page else "not found on the pages the note names"}
        for f in calc.figures]), hide_index=True, width="stretch")
    st.caption(":green[■] green boxes: the figures this calculation used, each named on the page.")
    pages: dict[int, list] = {}
    for f in calc.figures:
        if f.page and f.box is not None:
            pages.setdefault(f.page, []).append(f)
    if pdf is None or not pages:
        return
    for page, figures in pages.items():
        st.markdown(f"**PDF page {page}** · this report · " + ", ".join(esc(f.label or f.printed) for f in figures))
        boxes = tuple((tuple(f.box), "input", f.label or f.printed) for f in figures)
        page_col, crop_col = st.columns([3, 2])
        page_col.image(_page_png(ev.document["sha256"], page - 1, boxes, pdf), width="stretch")
        with crop_col:
            for f in figures:
                st.caption(f"{f.label or f.printed}: {number_text(f.value, f.unit)}")
                st.image(_crop_png(ev.document["sha256"], page - 1, tuple(f.box), pdf, kind="input"), width="stretch")


def _input_pdf(config: AppConfig, ev: MetricEvidence, document_id: str, pdf: bytes | None) -> tuple[bytes | None, str]:
    """The PDF holding an input: this report's, or another report's (a previous-year input)."""
    if document_id == ev.document["id"]:
        return pdf, "this report"
    with closing(connect(config.settings.database_path)) as conn:
        other = get_document(conn, document_id)
    if other is None:
        return None, "a report that was deleted"
    title = f"{other['bank_name']} {other['reporting_period']} report"
    try:
        return load_report_pdf(config.settings, other), title
    except EvidenceError:
        return None, title


def box_origin(ev: MetricEvidence) -> str:
    """Where the box on the page comes from, in a reader's words."""
    if ev.box is None:
        return "no box"
    if ev.box_found:
        return "box found automatically: the reviewer typed this value and the same number is printed here"
    if ev.box_is_input:
        return "box marked by the reviewer around a figure used to work the value out (not the value)"
    if ev.source_type == "pdf_marked_box":
        return "box marked by the reviewer"
    return "box of the extracted figure"


def _open_in_viewer(config: AppConfig, ev: MetricEvidence, pdf: bytes | None, target: str = "extract_review") -> None:
    """Extract & Review on this report, with this metric selected and the viewer on its page and box."""
    error = extract_review.open_saved(config, ev.document["id"])
    if error:
        st.error(error, icon=":material/error:")
        return
    st.session_state.selected_metric, st.session_state.detail_mode = ev.metric_id, "metric"
    if ev.candidate_key is not None:
        st.session_state.selected_key = ev.candidate_key
    st.session_state.viewer_page = ev.page
    if ev.box is not None and pdf is not None:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            rect = doc[ev.page - 1].rect
        fx, fy = focus_of(ev.box, rect.width, rect.height)
        st.session_state.pan_x, st.session_state.pan_y = round(fx * 100), round(fy * 100)
        st.session_state.viewer_zoom = "200%"
    routes.go(target)


def _checks_block(ev: MetricEvidence) -> None:
    if not ev.checks:
        return
    st.markdown("#### Checks recorded when the report was saved")
    for c in ev.checks:
        text = f"`{c.rule_id}` · {esc(c.message)}"
        if c.severity == "info":
            st.caption(text)
        else:
            st.warning(text, icon=":material/warning:")


def _candidates_block(ev: MetricEvidence) -> None:
    st.markdown(f"#### Other figures found in the PDF ({len(ev.other_candidates)})")
    if not ev.other_candidates:
        st.caption("None.")
        return
    st.dataframe(pd.DataFrame([{"Page": o.page, "Value": o.printed, "Year column": str(o.year_column or ""),
                                "Label / sentence": o.label, "Found by": o.method, "Why not used": o.why}
                               for o in ev.other_candidates]), hide_index=True, width="stretch")


def _history_block(ev: MetricEvidence) -> None:
    st.markdown("#### History")
    st.html('<ol class="bpe-timeline">' + "".join(
        f'<li><time>{escape(h.at[:19].replace("T", " "))}</time><span class="dot{" g" if i < len(ev.history) - 1 else ""}">'
        f'</span><span><b>{escape(h.action)}</b> · {escape(h.who)}{" · " + escape(h.new) if h.new else ""}</span></li>'
        for i, h in enumerate(ev.history)) + "</ol>")
    with st.expander("Every step in full"):
        st.dataframe(pd.DataFrame([{"When (UTC)": h.at[:16].replace("T", " "), "Who": h.who, "Action": h.action,
                                    "Before": h.old, "After": h.new, "Reason": h.reason} for h in ev.history]),
                     hide_index=True, width="stretch",
                     column_config={"Reason": st.column_config.TextColumn(width="large")})


# --- Evidence summary ----------------------------------------------------------------------

def render_summary(config: AppConfig) -> None:
    st.markdown("## Evidence summary")
    report_id, _ = _pickers(config, with_metric=False)
    if report_id is None:
        return
    with closing(connect(config.settings.database_path)) as conn:
        _, words = _report_pdf(config, get_document(conn, report_id))
        try:
            evidences = report_evidence(conn, config, report_id, words)
        except EvidenceError as exc:
            st.error(str(exc), icon=":material/error:")
            return
        finally:
            if words is not None:
                words.close()
        calculated = calculated_columns(conn, report_id)
    doc = evidences[0].document if evidences else None
    if doc is None:
        st.info("This report has no metrics.")
        return
    counts = {s: sum(e.status == s for e in evidences) for s in STATUS_COLORS}
    st.markdown(f"**{esc(evidences[0].report_label)}** · {esc(doc['original_filename'])}  \n"
                f"{counts['approved']} approved · {counts['not_reported']} not reported · "
                f"{counts['pending']} correction pending · {counts['needs_review']} not reviewed · "
                f"{counts['not_found']} not found")

    base = config.settings.app_url
    st.dataframe(
        pd.DataFrame([{"Metric": e.display_name, "Status": e.status_label, "Value": e.value_text,
                       "Source": e.source_label + (" · box found" if e.box_found else "")
                       + (" · worked out (calculation in the note)" if e.note_calc else ""),
                       "Cross-check": _cross_check_flag(e), "Page": e.page or None,
                       "Evidence": evidence_url(base, report_id, e.metric_id)} for e in evidences]),
        hide_index=True, width="stretch", height=min(40 + 35 * len(evidences), 900),
        column_config={"Evidence": st.column_config.LinkColumn("Evidence", display_text="Open"),
                       "Page": st.column_config.NumberColumn(format="%d")})
    st.caption(f"The Open links use the app's address ({base}) and open in a new tab.")
    if calculated:
        st.markdown("#### Calculated by an analyst (Calculations page)")
        st.dataframe(pd.DataFrame([{"Column": c, "Value": f"{v.value:f}", "Formula note": v.formula_note,
                                    "By": v.reviewer, "When (UTC)": v.created_at[:16].replace("T", " ")}
                                   for c, v in calculated.items()]), hide_index=True, width="stretch")

    names = {e.metric_id: e.display_name for e in evidences}
    pick_col, open_col = st.columns([3, 1], vertical_alignment="bottom")
    metric_id = pick_col.selectbox("Open a metric's evidence here", list(names), format_func=names.get,
                                   key="summary_metric")
    with open_col:
        routes.link("evidence", "Open evidence", ":material/verified:", report=report_id, metric=metric_id)
    _downloads(config, evidences, calculated, doc)


def _downloads(config: AppConfig, evidences: list[MetricEvidence], calculated: dict, doc) -> None:
    stem = f"evidence_{doc['bank_name']}_{doc['reporting_period']}".replace(" ", "-").replace("/", "-")
    pdf_col, csv_col = st.columns(2)
    try:
        report_pdf = load_report_pdf(config.settings, doc)
    except EvidenceError:
        report_pdf = None

    def crop(e: MetricEvidence) -> bytes | None:
        if e.external is not None:
            try:
                return source_crop_png(config.settings.data_dir, e.external, scale=1.6)
            except ReviewError:
                return None
        if report_pdf is None or e.box is None or e.page is None:
            return None
        return _crop_png(doc["sha256"], e.page - 1, tuple(e.box), report_pdf, scale=1.6)

    if pdf_col.button("Prepare evidence PDF", icon=":material/picture_as_pdf:", width="stretch", key="evidence_pdf"):
        generated = datetime.now(timezone.utc).isoformat(timespec="minutes").replace("+00:00", "")
        st.session_state._evidence_pdf_ready = (doc["id"], summary_pdf(
            [(evidences, calculated)], config.settings.app_url, f"Evidence: {evidences[0].report_label}",
            generated.replace("T", " "), crop))
    ready = st.session_state.get("_evidence_pdf_ready")
    if ready and ready[0] == doc["id"]:
        pdf_col.download_button("Download evidence PDF", ready[1], file_name=f"{stem}.pdf", mime="application/pdf",
                                icon=":material/download:", width="stretch", key="evidence_pdf_download")
    rows = [{"metric_id": e.metric_id, "metric": e.display_name, "status": e.status_label,
             "value": "" if e.value is None else str(e.value), "unit": e.unit or "", "source": e.source_label,
             "pdf_page": e.page or "", "as_printed": e.conversion, "note": e.entry_reason or e.approval_note,
             "external_source": e.external.citation if e.external else "", "cross_check": _cross_check_flag(e),
             "evidence_url": evidence_url(config.settings.app_url, doc["id"], e.metric_id)} for e in evidences]
    csv_col.download_button("Download CSV", pd.DataFrame(rows).to_csv(index=False).encode("utf-8"),
                            file_name=f"{stem}.csv", mime="text/csv", icon=":material/download:", width="stretch",
                            key="evidence_csv")


# --- shared ----------------------------------------------------------------------------------

def _cross_check_flag(e: MetricEvidence) -> str:
    """'Mismatch', 'Match' or 'Not compared' for the active cross-checks; '' when there are none."""
    statuses = [c.comparison.status for c in e.cross_checks if c.check.active]
    for status, label in (("mismatch", "Mismatch"), ("match", "Match"), ("not_compared", "Not compared")):
        if status in statuses:
            return label
    return ""


def _pickers(config: AppConfig, with_metric: bool) -> tuple[str | None, str | None]:
    """Report (and metric) from the address, changeable here; the address follows the choice."""
    with closing(connect(config.settings.database_path)) as conn:
        documents = list_documents(conn)
        current = get_document(conn, st.query_params.get("report", "")) if st.query_params.get("report") else None
    if not documents:
        st.info("No reports are saved yet.", icon=":material/inbox:")
        return None, None
    labels = {d["id"]: f"{d['bank_name']} · {d['reporting_period']} · saved {d['uploaded_at'][:10]}" for d in documents}
    report_id = st.query_params.get("report")
    if report_id and current is None:
        st.error("The report in the address is not in the database (it may have been deleted). Choose another.",
                 icon=":material/error:")
        report_id = None
    columns = st.columns([3, 2]) if with_metric else [st.container()]
    chosen = columns[0].selectbox("Report", list(labels), format_func=labels.get, key="evidence_report",
                                  index=list(labels).index(report_id) if report_id in labels else None,
                                  placeholder="Choose a report")
    if chosen != report_id:
        st.query_params["report"] = chosen
        if "metric" in st.query_params and chosen is not None:
            del st.query_params["metric"]
        report_id = chosen
    if not with_metric or report_id is None:
        return report_id, None
    metric_ids = json.loads(next(d for d in documents if d["id"] == report_id)["metric_ids"])
    names = _metric_names(config, metric_ids)
    metric_id = st.query_params.get("metric")
    picked = columns[1].selectbox("Metric", metric_ids, format_func=names.get, key="evidence_metric",
                                  index=metric_ids.index(metric_id) if metric_id in metric_ids else None,
                                  placeholder="Choose a metric")
    if picked != metric_id and picked is not None:
        st.query_params["metric"] = picked
    return report_id, picked


def _metric_names(config: AppConfig, metric_ids: list[str]) -> dict[str, str]:
    names = {}
    for m in metric_ids:
        try:
            names[m] = config.metric(m).display_name
        except KeyError:
            names[m] = m
    return names
