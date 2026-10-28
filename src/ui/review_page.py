"""Review: one saved report, metric by metric, as in the redesign mockup (screens 3 and 4, D-092).

Left: the metric queue, grouped by statement, with filters. Middle: the PDF page (and, for a
calculated value, close-ups of its inputs). Right: the decision for the selected metric, then
Previous / Next. All rules are the review engine's (review_service, calculated_entries); the
figures and boxes come from extract_review.prepare, the same as the Extract page.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from html import escape

import streamlit as st
from PIL import Image
from streamlit_image_coordinates import streamlit_image_coordinates

from src import review_service
from src.calculated_entries import number_text, saved_calculation
from src.config_loader import AppConfig
from src.database import connect
from src.models import MetricCandidate, MetricResult
from src.project_service import get_document
from src.review_service import ReviewError, ReviewState
from src.ui import calc_panel, extract_review, metric_view, review_panel, routes, shell, style
from src.ui.extract_review import Prepared
from src.unit_normalizer import unit_label
from src.value_input import stored_on_approval

# Where each metric sits in the queue (statement order); anything else goes under "Other".
GROUPS = {
    "Financial position": ("total_assets_etb", "total_assets_previous_year_etb", "total_liabilities_etb",
                           "total_equity_etb", "paid_up_capital_etb", "total_deposits_etb", "loans_and_advances_etb",
                           "non_performing_loans_etb", "foreign_assets_etb", "nbe_bills_etb",
                           "deposits_with_foreign_banks_etb", "foreign_liabilities_etb", "current_assets_etb",
                           "current_liabilities_etb", "total_regulatory_capital_etb", "risk_weighted_assets_etb"),
    "Profit or loss": ("total_revenue_etb", "total_expense_etb", "operating_income_etb", "operating_expense_etb",
                       "net_interest_income_etb", "profit_before_tax_etb", "profit_tax_etb", "profit_after_tax_etb",
                       "dividends_paid_etb"),
    "Profitability": ("roe_percent", "roa_percent", "eps_etb", "weighted_average_shares"),
    "Network and customers": ("branches", "atms", "pos_machines", "agents", "employees", "shareholders",
                              "deposit_account_holders", "mobile_banking_users", "internet_banking_users",
                              "mobile_and_internet_banking_users", "cardholders"),
    "Sector": ("sector_total_deposits_etb",),
}
TO_REVIEW = ("needs_review", "flagged", "not_found", "corrected")
FILTERS = ("to_review", "warnings", "calculated", "approved", "all")
FILTER_LABELS = {"to_review": "To review", "warnings": "Warnings", "calculated": "Calculated",
                 "approved": "Approved", "all": "All"}
DOTS = {"needs_review": ":orange[●]", "flagged": ":red[●]", "not_found": ":gray[○]", "corrected": ":blue[●]",
        "approved": ":green[●]", "not_reported": ":gray[●]", "calculated": ":blue[●]"}
ZOOMS = list(extract_review.ZOOM_LEVELS)
QUEUE_PAGE = 12  # metrics per page of the queue


def render(config: AppConfig, open_calculations: Callable[[str], None] | None = None,
           open_reports: Callable[[], None] | None = None) -> None:
    doc = st.session_state.get("doc")
    if not doc or not doc.get("document_id"):
        st.markdown("## Review")
        st.html('<p class="bpe-sub">Open a saved report to review its figures one by one.</p>')
        if open_reports is not None:
            st.button("Open Reports", icon=":material/description:", type="primary", on_click=open_reports)
        return
    if not _saved_fields(config, doc["document_id"]):
        st.error("That report is no longer in the database.", icon=":material/error:")
        return
    with st.spinner("Reading the report..."):
        p = extract_review.prepare(config, doc)
    reviews = extract_review._review_displays(p.analysis, p.ctx, p.states)
    statuses = metric_view.table_statuses(p.analysis, reviews)
    with st.container(key="review_work"):
        _work_bar(p, open_calculations)
        queue_col, viewer_col, insp_col = st.columns([2.5, 5.6, 3.1], gap=None)
        with queue_col:
            shown_ids = _queue(p, statuses, reviews)
        selected = st.session_state.get("selected_metric")
        if selected not in {m.metric.id for m in p.analysis.metrics}:
            selected = None
        with viewer_col:
            _viewer(p, selected)
        with insp_col:
            _inspector(p, selected, statuses, shown_ids)


def _saved_fields(config: AppConfig, document_id: str) -> bool:
    """Bank, year and metrics from the saved report itself. They are the Extract page's field values,
    which Streamlit forgets while that page is not shown, so Review never relies on them."""
    with closing(connect(config.settings.database_path)) as conn:
        row = get_document(conn, document_id)
    if row is None:
        return False
    values = {"bank": row["bank_name"], "period": row["reporting_period"],
              "metric_selection": json.loads(row["metric_ids"])}
    for key, value in values.items():
        st.session_state[key] = value
        st.session_state[extract_review.FIELD_COPIES[key]] = value  # so Extract shows the same report
    return True


# --- the bar over the panes ----------------------------------------------------------------

def _work_bar(p: Prepared, open_calculations: Callable[[str], None] | None) -> None:
    approved = sum(s.status == "approved" for s in p.states.values())
    total = len(p.states)
    bank = st.session_state.bank or ""
    with st.container(key="review_bar"):
        title_col, progress_col, tools_col, finish_col = st.columns([4, 2.4, 1.2, 1.4], vertical_alignment="center")
        title_col.html(f'<div class="bpe-crumb">Reports / {escape(bank)}</div>'
                       f'<h1 style="font-size:18px;font-weight:600;margin:0">{escape(bank)} · '
                       f'{escape(extract_review._report_year_label())}</h1>')
        progress_col.html(f'<div class="bpe-progress"><span class="bpe-track"><b style="width:'
                          f'{100 * approved / total if total else 0:.0f}%"></b></span>'
                          f'<span class="bpe-num">{approved} of {total} approved</span></div>')
        with tools_col:
            extract_review._report_tools(p.doc, p.ocr_needed, p.ocr_missing, p.ocr_engine, p.ocr_path)
        with finish_col:
            extract_review._finish_extraction(p.ctx, p.analysis, open_calculations, p.check_results)


# --- left: the queue -----------------------------------------------------------------------

def _queue(p: Prepared, statuses: dict[str, str], reviews: dict[str, metric_view.ReviewDisplay]) -> list[str]:
    calculated = set(p.calc_boxes)
    counts = {
        "to_review": sum(s in TO_REVIEW for s in statuses.values()),
        "warnings": sum(s == "flagged" for s in statuses.values()),
        "calculated": len(calculated),
        "approved": sum(s == "approved" for s in statuses.values()),
        "all": len(statuses),
    }
    with st.container(key="queue"):
        with st.container(key="queue_head"):
            find = st.text_input("Find a metric", key="review_find", placeholder="Find a metric",
                                 label_visibility="collapsed", icon=":material/search:")
            st.session_state.setdefault("review_filter", "to_review" if counts["to_review"] else "all")
            chosen = st.pills("Show", [f for f in FILTERS if counts[f] or f in ("to_review", "all")],
                              key="review_filter", format_func=lambda f: f"{FILTER_LABELS[f]} {counts[f]}",
                              label_visibility="collapsed") or "all"

        def keep(metric_id: str) -> bool:
            status = statuses[metric_id]
            return {"to_review": status in TO_REVIEW, "warnings": status == "flagged",
                    "calculated": metric_id in calculated, "approved": status == "approved"}.get(chosen, True)

        names = {m.metric.id: m.metric.display_name for m in p.analysis.metrics}
        shown = [m.metric.id for m in p.analysis.metrics
                 if keep(m.metric.id) and (not find or find.casefold() in names[m.metric.id].casefold())]
        grouped = [(group, [m for m in members if m in shown]) for group, members in GROUPS.items()]
        grouped.append(("Other", [m for m in shown if not any(m in members for members in GROUPS.values())]))
        selected = st.session_state.get("selected_metric")
        order = [m for _, members in grouped for m in members]  # Previous / Next go through all of them
        group_of = {m: group for group, members in grouped for m in members}
        if selected in order and st.session_state.get("_queue_follows") != selected:
            st.session_state.review_qpage = order.index(selected) // QUEUE_PAGE  # show the page of the metric picked
            st.session_state._queue_follows = selected
        start, end = shell.pager("review_qpage", len(order), QUEUE_PAGE)
        last_group = None
        for metric_id in order[start:end]:
            if group_of[metric_id] != last_group:
                last_group = group_of[metric_id]
                st.html(f'<div class="bpe-qgroup">{escape(last_group)}</div>')
            _queue_row(p, metric_id, names[metric_id], statuses[metric_id], metric_id in calculated,
                       reviews.get(metric_id), metric_id == selected)
        if not order:
            st.html('<div class="bpe-qmore">No metric matches. Choose another filter.</div>')
    return order


def _queue_row(p: Prepared, metric_id: str, name: str, status: str, calculated: bool,
               review: metric_view.ReviewDisplay | None, selected: bool) -> None:
    dot = DOTS["calculated"] if calculated and status == "approved" else DOTS.get(status, ":gray[●]")
    with st.container(key=f"qrow_{metric_id}"):
        if selected:
            st.html(f"<style>.st-key-qrow_{metric_id} {{ background: #F5F7FE; border-left-color: #2747C9 !important; }}"
                    "</style>")
        name_col, value_col = st.columns([3, 2], vertical_alignment="center", gap=None)
        name_col.button(f"{dot} {name}", key=f"q_{metric_id}", type="tertiary", width="stretch",
                        on_click=_select, args=(p, metric_id))
        value_col.html(f'<div class="bpe-qval">{escape(_queue_value(p, metric_id, status, review))}</div>')


def _queue_value(p: Prepared, metric_id: str, status: str, review: metric_view.ReviewDisplay | None) -> str:
    state = p.states.get(metric_id)
    if status == "approved" and state is not None and state.accepted_value is not None:
        return _short(state.accepted_value, state.accepted_unit)
    if status == "not_reported":
        return "not reported"
    primary = p.analysis.metric(metric_id).primary
    if primary is None:
        return "not found"
    if primary.normalized.value is None:
        return "check unit"
    return primary.mapped.candidate.raw_value_text


def _short(value: Decimal, unit: str | None) -> str:
    """A value in the queue: 22.99% · 849 · 189,512,266 (ETB in thousands, as statements print them)."""
    if unit == "fraction":
        return f"{(value * 100).normalize():f}%"
    if unit == "ETB" and value == value.to_integral_value() and abs(value) >= 1000 and value % 1000 == 0:
        return f"{value / 1000:,.0f}"
    return f"{value.normalize():,f}"


def _select(p: Prepared, metric_id: str) -> None:
    extract_review._select_metric(metric_id, p.analysis, p.extraction, p.marked_values, p.ctx)
    st.session_state.review_action = None


# --- middle: the page ------------------------------------------------------------------------

def _viewer(p: Prepared, metric_id: str | None) -> None:
    doc, extraction = p.doc, p.extraction
    page_count = extraction.page_count
    st.session_state.viewer_page = min(max(1, st.session_state.get("viewer_page", 1)), page_count)
    st.session_state.setdefault("viewer_zoom", "100%")
    st.session_state.setdefault("pan_x", 50)
    st.session_state.setdefault("pan_y", 50)
    page = st.session_state.viewer_page
    inputs = p.calc_boxes.get(metric_id, []) if metric_id else []
    with st.container(key="viewer"):
        if inputs:
            _inputs_strip(p, inputs)
        with st.container(key="vbar"):
            cols = st.columns([0.55, 0.8, 0.85, 0.55, 0.55, 0.8, 0.55, 1.4, 2.7], vertical_alignment="center")
            cols[0].button("", icon=":material/chevron_left:", key="rv_prev_page", disabled=page <= 1,
                           on_click=extract_review._go_to, args=(page - 1,), help="Previous page")
            cols[1].number_input("Page", min_value=1, max_value=page_count, key="viewer_page",
                                 label_visibility="collapsed")
            cols[2].html(f'<span class="bpe-num bpe-minor">of {page_count}</span>')
            cols[3].button("", icon=":material/chevron_right:", key="rv_next_page", disabled=page >= page_count,
                           on_click=extract_review._go_to, args=(page + 1,), help="Next page")
            at = ZOOMS.index(st.session_state.viewer_zoom) if st.session_state.viewer_zoom in ZOOMS else 0
            cols[4].button("", icon=":material/remove:", key="rv_zoom_out", disabled=at <= 0, on_click=_zoom, args=(at - 1,), help="Zoom out")
            cols[5].html(f'<span class="bpe-num">{escape(ZOOMS[at])}</span>')
            cols[6].button("", icon=":material/add:", key="rv_zoom_in", disabled=at >= len(ZOOMS) - 1, on_click=_zoom, args=(at + 1,),
                           help="Zoom in")
            cols[7].button("Fit width", key="rv_fit", on_click=_zoom, args=(0,))
            if st.session_state.pop("turn_on_marking", False):
                st.session_state.mark_mode = True
            marking = cols[8].toggle("Mark a number", key="mark_mode",
                                     help="Drag a box around the right number on the page to record it.")
        with st.container(key="vpage"):
            _page_image(p, metric_id, page, marking)
            st.html('<p class="bpe-minor" style="text-align:center;margin:8px 0 0">Red: the suggestion you are '
                    'checking · thin blue: figures already found on this page</p>')
            extract_review._mark_summary()
            mark = st.session_state.get("marked")
            if mark is not None and not mark.from_figure:
                extract_review._mark_form(mark, p.analysis, p.ctx, None)


def _zoom(at: int) -> None:
    st.session_state.viewer_zoom = ZOOMS[at]
    st.session_state.zoom_chosen = True


def _page_image(p: Prepared, metric_id: str | None, page: int, marking: bool) -> None:
    doc = p.doc
    primary = p.analysis.metric(metric_id).primary if metric_id else None
    state = p.states.get(metric_id) if metric_id else None
    selected = None
    if primary is not None and metric_id not in p.recorded and (state is None or state.status != "approved"):
        selected = primary.mapped.candidate
    highlight = selected.bbox if selected is not None and selected.page_index == page - 1 else None
    boxes = tuple((box, "focus" if m == metric_id else "recorded", name)
                  for m, (pg, box, name) in p.recorded.items() if pg == page - 1)
    boxes += tuple((box, "input", label) for pg, box, label in p.calc_boxes.get(metric_id, []) if pg == page - 1)
    mark = st.session_state.get("marked")
    if mark is not None and mark.page_index == page - 1:
        boxes += ((mark.bbox, "marked"),)
    zoom_label = st.session_state.viewer_zoom
    view_zoom = extract_review.ZOOM_LEVELS.get(zoom_label, 1.0)
    focus = (st.session_state.pan_x / 100, st.session_state.pan_y / 100)
    png = extract_review._render(doc["sha"], page - 1, highlight, extract_review.BASE_RESOLUTION, view_zoom, focus,
                                 boxes, doc["bytes"])
    if marking:
        drag = streamlit_image_coordinates(Image.open(io.BytesIO(png)), width="stretch",
                                           click_and_drag=True, cursor="crosshair",
                                           key=f"rv_mark_{doc['sha'][:12]}_{page}_{zoom_label}_{focus}")
        extract_review._take_mark(drag, doc, p.extraction, page, view_zoom, focus, p.ocr_words)
    else:
        st.image(png, width="stretch")


def _inputs_strip(p: Prepared, inputs: list[tuple[int, tuple, str]]) -> None:
    pages: dict[int, list[tuple[tuple, str]]] = {}
    for page_index, box, label in inputs:
        pages.setdefault(page_index, []).append((tuple(box), label))
    with st.container(key="strip"):
        st.markdown(f":green[:material/calculate:] **Worked out from {len(inputs)} printed figure"
                    f"{'s' if len(inputs) != 1 else ''}**")
        for page_index, boxes in sorted(pages.items()):  # one page per row: a full page width stays readable
            st.image(extract_review._band(p.doc["sha"], page_index, tuple((b, "") for b, _ in boxes),
                                          p.doc["bytes"]), width="stretch")
            name_col, open_col = st.columns([3, 2], vertical_alignment="center")
            name_col.html(f'<span style="color:#17663F;font-size:12px">'
                          f'{escape(" · ".join(label for _, label in boxes))}</span>')
            open_col.button(f"Open page {page_index + 1}", key=f"rv_open_{page_index}", width="stretch",
                            on_click=extract_review._go_to, args=(page_index + 1,))


# --- right: the decision ----------------------------------------------------------------------

def _inspector(p: Prepared, metric_id: str | None, statuses: dict[str, str], shown_ids: list[str]) -> None:
    with st.container(key="insp"):
        if metric_id is None:
            with st.container(key="isec_empty"):
                st.markdown("**Pick a metric**")
                st.caption("Choose a metric on the left, or start with the first one to review.")
            _steps(p, None, shown_ids)
            return
        result = p.analysis.metric(metric_id)
        state = p.states[metric_id]
        status = statuses[metric_id]
        calculated = metric_id in p.calc_boxes
        shown = result.primary
        if p.ctx.extraction_finished:
            _head(result, state, status, calculated, shown)
            if state.status == "approved":
                _approved_detail(p, result, state, calculated)
            with st.container(key="isec_locked"):
                st.caption(":material/lock: The extraction review is finished, so figures are locked.")
                review_panel.reopen_extraction(p.ctx, key="review")
        elif state.status == "approved":
            _head(result, state, status, calculated, shown)
            _approved_detail(p, result, state, calculated)
            with st.container(key="isec_actions"):
                routes.link("evidence", "Open evidence record", ":material/verified:",
                            report=p.ctx.document_id, metric=metric_id)
                recalc_col, reopen_col = st.columns(2)
                if recalc_col.button("Recalculate", key=f"rv_recalc_{metric_id}", width="stretch"):
                    st.session_state.calc_open = metric_id
                with reopen_col:
                    review_panel._reopen(p.ctx, metric_id, "Reopen")
                if st.session_state.get("calc_open") == metric_id:
                    calc_panel.calculate_section(p.ctx, result, state)
        elif state.status == "corrected":
            _head(result, state, status, calculated, shown)
            with st.container(key="isec_pending"):
                review_panel._pending_box(p.ctx, result, state)
        elif state.status == "not_reported":
            _head(result, state, status, calculated, shown)
            with st.container(key="isec_not_reported"):
                review_panel._not_reported_box(state)
                review_panel._reopen(p.ctx, metric_id, "Reopen")
        else:
            _head(result, state, status, calculated, shown)
            _open_detail(p, result, shown)
            _decide(p, result, state, shown)
        _steps(p, metric_id, shown_ids)


def _head(result: MetricResult, state: ReviewState, status: str, calculated: bool,
          shown: MetricCandidate | None) -> None:
    chips = {"needs_review": ("Needs review", "warn"), "flagged": ("Check warnings", "bad"),
             "not_found": ("Not found", "muted"), "corrected": ("Awaiting approval", "warn"),
             "approved": ("Approved", "ok"), "not_reported": ("Not reported", "muted")}
    label, kind = chips.get(status, (status.replace("_", " ").capitalize(), "muted"))
    html = style.chip(label, kind) + (" " + style.chip("Calculated", "calc") if calculated else "")
    with st.container(key="isec_head"):
        value_html = ""
        if state.status in ("approved", "corrected") and (state.accepted_value if state.status == "approved"
                                                           else state.pending.reviewed_value) is not None:
            value = state.accepted_value if state.status == "approved" else state.pending.reviewed_value
            unit = state.accepted_unit if state.status == "approved" else state.pending.reviewed_unit
            if unit == "fraction":
                value_html = (f'<div><span class="bpe-big">{value.normalize():f}</span> '
                              f'<span class="bpe-unit">{(value * 100).normalize():f}%</span></div>')
            else:
                value_html = f'<div><span class="bpe-big">{escape(metric_view.full_value_text(value, unit))}</span></div>'
        elif shown is not None and shown.normalized.value is not None:
            printed = shown.mapped.candidate.raw_value_text
            unit_text = unit_label(shown.mapped.unit) if shown.normalized.unit == "ETB" else ""
            value_html = (f'<div><span class="bpe-big">{escape(printed)}</span> <span class="bpe-unit">'
                          f'{escape(unit_text)}</span></div><div class="bpe-minor bpe-num">'
                          f'= {escape(metric_view.full_value_text(shown.normalized.value, shown.normalized.unit))}</div>')
        st.html(f'<div style="display:flex;gap:6px;flex-wrap:wrap">{html}</div>'
                f'<div style="font-size:17px;font-weight:600;margin:10px 0 6px">{escape(result.metric.display_name)}'
                f'</div>{value_html}')
        if state.status == "approved" and state.accepted_decision is not None:
            a = state.accepted_decision
            st.html(f'<div class="bpe-minor">Approved by {escape(a.reviewer)} · {escape(_when(a.created_at))}</div>')


def _when(iso: str) -> str:
    when = datetime.fromisoformat(iso)
    return f"{when.day} {when:%b %Y}, {when:%H:%M} UTC"


def _open_detail(p: Prepared, result: MetricResult, shown: MetricCandidate | None) -> None:
    with st.container(key="isec_source"):
        if shown is None:
            st.caption("No figure was found in this report. This is not the same as zero.")
            return
        c = shown.mapped.candidate
        year = shown.mapped.period_year
        end_year = extract_review.report_end_year(st.session_state.period or "")
        column = (f"{year}, the year this report ends" if year and year == end_year
                  else f"{year} (not the report year)" if year else "year not identified")
        unit = unit_label(shown.mapped.unit)
        rows = [("Page", f"PDF page {c.page_index + 1}"), ("Row", c.raw_label or "—"), ("Column", column),
                ("Unit", f"{unit}, from the page heading" if shown.normalized.unit == "ETB" else unit)]
        st.html('<dl class="bpe-dl">' + "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in rows)
                + "</dl>")
        with closing(connect(p.ctx.db_path)) as conn:
            cid = p.ctx.candidate_ids.get((result.metric.id, shown.mapped.key))
            warnings = review_service.relevant_warnings(conn, p.ctx.document_id, result.metric.id, cid)
        if warnings:
            for w in warnings:
                st.html(f'<div class="bpe-checkwarn">⚠ {escape(w["message"])}</div>')
        else:
            right = year == end_year
            st.html(f'<div class="bpe-check">✓ {"Right year column · " if right else ""}no warnings</div>')


def _decide(p: Prepared, result: MetricResult, state: ReviewState, shown: MetricCandidate | None) -> None:
    metric_id = result.metric.id
    with st.container(key="isec_decide"):
        candidate_id = p.ctx.candidate_ids.get((metric_id, shown.mapped.key)) if shown is not None else None
        if shown is not None and candidate_id is None:
            st.warning("This figure is not part of the saved report; record the value another way below.",
                       icon=":material/warning:")
        elif shown is not None and candidate_id in state.rejected_candidate_ids:
            st.info("You rejected this figure. Correct it, or record the value another way below.",
                    icon=":material/block:")
            review_panel._correct(p.ctx, result, shown, candidate_id)
        elif shown is not None and shown.normalized.value is not None:
            _approve(p, result, shown, candidate_id)
            correct_col, reject_col = st.columns(2)
            with correct_col:
                review_panel._correct(p.ctx, result, shown, candidate_id)
            with reject_col:
                review_panel._reject(p.ctx, metric_id, candidate_id)
        elif shown is not None:
            st.warning(f"Cannot approve as printed: {shown.normalized.issue} Use Correct.", icon=":material/warning:")
            review_panel._correct(p.ctx, result, shown, candidate_id)
        st.html('<div class="bpe-otherways">Other ways to record it</div>')
        with st.container(key="other_ways"):
            if st.button("Mark the number on the page ›", key=f"rv_mark_{metric_id}", type="tertiary",
                         width="stretch"):
                st.session_state.mark_mode = True
                st.rerun()
            calc_panel.calculate_section(p.ctx, result, state)
            review_panel._external_value(p.ctx, result)
            review_panel._not_reported(p.ctx, result, label="Not reported in this report")


def _approve(p: Prepared, result: MetricResult, shown: MetricCandidate, candidate_id: int) -> None:
    """Approve, named with the printed figure; warnings and a negative sign are asked first."""
    metric_id = result.metric.id
    with closing(connect(p.ctx.db_path)) as conn:
        warnings = review_service.relevant_warnings(conn, p.ctx.document_id, metric_id, candidate_id)
    acknowledged = True
    if warnings:
        acknowledged = st.checkbox(f"I have checked the {len(warnings)} warning(s)", key=f"rv_ack_{metric_id}")
    sign = "as_printed"
    if shown.normalized.value < 0:
        choice = st.radio("Record the sign", ["As printed (negative)", "As a positive amount"], horizontal=True,
                          key=f"rv_sign_{metric_id}", index=1 if result.metric.sign == "positive" else 0)
        sign = "positive" if choice.startswith("As a positive") else "as_printed"
    printed = shown.mapped.candidate.raw_value_text
    stored = stored_on_approval(shown.normalized.value, sign)
    if st.button(f"Approve {printed}", key="approve_main", type="primary", width="stretch",
                 help=f"Records {number_text(stored, shown.normalized.unit)}"):
        review_panel.run_action(p.ctx, lambda conn: review_service.approve(
            conn, candidate_id, p.ctx.reviewer, sign=sign, acknowledge_warnings=acknowledged),
            f"{result.metric.display_name} approved")


def _approved_detail(p: Prepared, result: MetricResult, state: ReviewState, calculated: bool) -> None:
    """How an approved value came about: the calculation card, or where it is printed."""
    metric_id = result.metric.id
    note_calc = p.ctx.note_calcs.get(metric_id)
    saved = None
    proposal = next((d for d in state.history if d.id == state.accepted_decision.approves_decision_id), None)
    if proposal is not None and proposal.entry_kind == "calculated":
        with closing(connect(p.ctx.db_path)) as conn:
            saved = saved_calculation(conn, proposal.id)
    with st.container(key="isec_how"):
        if note_calc is not None or saved is not None:
            words = saved.words if saved is not None else note_calc.written
            arithmetic = saved.arithmetic if saved is not None else note_calc.arithmetic
            check = ('<span class="bpe-pass">✓ Gives the saved '
                     f'{escape(f"{state.accepted_value.normalize():f}")}</span>')
            if note_calc is not None and not note_calc.matches:
                check = (f'<span class="bpe-fail">✗ Gives {note_calc.result.quantize(Decimal("0.0001")).normalize():f}'
                         f', not the saved {note_calc.stated:f}</span>')
            figures = ([(i.label, number_text(i.value, i.unit), f"p{i.page}" if i.page else "") for i in saved.inputs]
                       if saved is not None else [(f.label or "Figure", f.printed, f"p{f.page}" if f.page else "")
                                                  for f in note_calc.figures])
            st.html(f'<div class="bpe-calcbox"><span class="lbl">Calculated</span>'
                    f'<span style="font-size:14px">{escape(words)}</span>'
                    f'<span class="bpe-arith">{escape(arithmetic)}</span>{check}</div>'
                    '<table class="bpe-itable"><thead><tr><th>Input</th><th class="r">Value</th>'
                    '<th class="r">Page</th></tr></thead><tbody>'
                    + "".join(f'<tr><td>{escape(n)}</td><td class="r bpe-num">{escape(v)}</td>'
                              f'<td class="r">{escape(pg)}</td></tr>' for n, v, pg in figures)
                    + "</tbody></table>")
            if proposal is not None and proposal.reason and saved is None:
                st.html(f'<p class="bpe-minor" style="margin:0">Reviewer\'s note: “{escape(proposal.reason)}”</p>')
            return
        a = state.accepted_decision
        rows = [("Source", "human-entered value" if state.is_human_entered else "extracted figure")]
        if a.source_page:
            rows.append(("Page", f"PDF page {a.source_page}"))
        st.html('<dl class="bpe-dl">' + "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in rows)
                + "</dl>")


def _steps(p: Prepared, metric_id: str | None, shown_ids: list[str]) -> None:
    """‹ Previous and Next: <metric> through the queue as filtered, with Alt+↑ / Alt+↓."""
    if not shown_ids:
        return
    at = shown_ids.index(metric_id) if metric_id in shown_ids else -1
    following = shown_ids[min(at + 1, len(shown_ids) - 1)]
    names = {m.metric.id: m.metric.display_name for m in p.analysis.metrics}
    with st.container(key="isec_steps"):
        prev_col, next_col = st.columns([1.6, 4], vertical_alignment="center")
        prev_col.button("‹", key="metric_prev", help="Previous metric (Alt+↑)", disabled=at <= 0, width="stretch",
                        on_click=_select, args=(p, shown_ids[max(at - 1, 0)]),
                        shortcut=extract_review.PREVIOUS_SHORTCUT)
        next_col.button(f"Next: {names[following]} ›" if at >= 0 else f"Start: {names[following]} ›",
                        key="metric_next", disabled=at >= len(shown_ids) - 1 and at >= 0, width="stretch",
                        on_click=_select, args=(p, following), shortcut=extract_review.NEXT_SHORTCUT)
