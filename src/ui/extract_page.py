"""Extract: did the app read this report well? As in the redesign, option F refined (D-094).

On top, the PDF page, opened by itself at the figure of the metric picked below, with the figure
boxed. Underneath, a drawer of fixed height: one row per metric (value the app picked, page,
one plain status), six rows a page, or the cross-checks; beside it, what to know about the
picked metric. Deciding (approve, mark, not reported) happens on Review.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from html import escape

import streamlit as st
from PIL import Image

from src import ocr, page_search
from src.candidate_queries import find_by_key
from src.config_loader import AppConfig
from src.database import connect
from src.models import METRIC_LEVEL_RULES, MetricCandidate, MetricResult
from src.pdf_render import focus_of
from src.project_service import get_document, new_figures
from src.review_service import ReviewState
from src.ui import extract_review, review_page, style
from src.ui.extract_review import Prepared

ROWS_PER_PAGE = 6  # the drawer has a fixed height: it never scrolls, it pages
FILTERS = ("all", "needs", "found", "not_found")
FILTER_LABELS = {"all": "All", "needs": "Needs a look", "found": "Found", "not_found": "Not found"}
TABS = ("metrics", "checks")
ZOOMS = list(extract_review.ZOOM_LEVELS)
STRIP_ASPECT = 2.6  # width / height of the close-up strip beside the whole page
HITS_SHOWN = 10  # pages listed for a search; more are counted


@dataclass(frozen=True)
class Status:
    """What the Extract list says about one metric: a filter group, a chip and, for a warning,
    the finding behind it."""

    group: str  # needs | found | not_found
    label: str
    kind: str  # chip colour, see style.chip
    finding: object | None = None


def metric_status(result: MetricResult, state: ReviewState | None) -> Status:
    """One plain status per metric, from the value the app picked only.

    Warnings about backup readings the app did not pick are left out, and so are notes
    (severity info, such as "no year heading"): Review shows those on the figure itself.
    """
    if state is not None and state.status == "approved":
        return Status("found", "Approved", "ok")
    if state is not None and state.status == "not_reported":
        return Status("found", "Not reported", "muted")
    primary = result.primary
    if primary is None:
        return Status("not_found", "Not found", "muted")
    for f in result.findings:
        if f.severity not in ("warning", "error"):
            continue
        if f.rule_id in METRIC_LEVEL_RULES or not f.candidate_keys or primary.mapped.key in f.candidate_keys:
            return Status("needs", _warning_label(f), "bad" if f.rule_id == "VALUE-UNIT" else "warn", f)
    return Status("found", "Found", "ok")


def _warning_label(finding) -> str:
    message = finding.message.lower()
    if finding.rule_id == "CONFLICT":
        return f"{max(2, len(finding.candidate_keys))} values disagree"
    if finding.rule_id == "VALUE-UNIT" and "percentage" in message:
        return "Percentage, not Birr" if "birr" in message else "Printed as a percentage"
    if finding.rule_id == "VALUE-UNIT":
        return "Check the unit"
    if finding.rule_id == "VALUE-PARSE":
        return "Printed as a dash" if "dash" in message else "Number not readable"
    if finding.rule_id == "MAP-AMBIGUOUS":
        return "Wording unclear"
    return "Needs a look"


def _plain(message: str) -> str:
    """A finding's message without its "PDF page n: " lead, which the page column already shows."""
    head, sep, rest = message.partition(": ")
    text = rest if sep and head.startswith("PDF page") else message
    return text[:1].upper() + text[1:]


def render(config: AppConfig, open_review: Callable[[], None], open_reports: Callable[[], None]) -> None:
    doc = st.session_state.get("doc")
    if not doc or not doc.get("document_id"):
        st.markdown("## Extract")
        st.html('<p class="bpe-sub">Open a saved report to see what the app read from it, page by page.</p>')
        st.button("Open Reports", icon=":material/description:", type="primary", on_click=open_reports)
        return
    if not review_page._saved_fields(config, doc["document_id"]):
        st.error("That report is no longer in the database.", icon=":material/error:")
        return
    with st.spinner("Reading the report..."):
        p = extract_review.prepare(config, doc)
    statuses = {m.metric.id: metric_status(m, p.states.get(m.metric.id)) for m in p.analysis.metrics}
    order = _ordered(p)
    with st.container(key="extract_work"):
        _work_bar(p, statuses, open_review)
        if st.session_state.get("selected_metric") not in statuses and order:
            needs = [m for m in order if statuses[m].group == "needs"]
            _pick(p, (needs or order)[0])
        selected = st.session_state.get("selected_metric")
        _upper(p, selected)
        _drawer(p, statuses, order, selected, open_review)


def _ordered(p: Prepared) -> list[str]:
    """Metrics in statement order, as on Review."""
    ids = [m.metric.id for m in p.analysis.metrics]
    grouped = [m for members in review_page.GROUPS.values() for m in members if m in ids]
    return grouped + [m for m in ids if m not in grouped]


def _pick(p: Prepared, metric_id: str) -> None:
    """Open the metric's page with its figure boxed (the page opens by itself)."""
    extract_review._select_metric(metric_id, p.analysis, p.extraction, p.marked_values, p.ctx)
    st.session_state.ex_focus = "metric"


# --- the bar ----------------------------------------------------------------------------------

def _work_bar(p: Prepared, statuses: dict[str, Status], open_review: Callable[[], None]) -> None:
    found = sum(s.group != "not_found" for s in statuses.values())
    bank = st.session_state.bank or ""
    with st.container(key="extract_bar"):
        title_col, note_col, tools_col, go_col = st.columns([3.2, 2.6, 1.3, 1.2], vertical_alignment="center")
        title_col.html(f'<div class="bpe-crumb">Reports / {escape(bank)} · '
                       f'{escape(extract_review._report_year_label())}</div>'
                       '<h1 style="font-size:18px;font-weight:600;margin:0">Extract</h1>')
        note_col.html(f'<span class="bpe-note">{p.extraction.page_count} pages read · '
                      f'<span class="bpe-num">{found} of {len(statuses)}</span> metrics found</span>')
        with tools_col:
            _report_tools(p)
        go_col.button("Go to Review", key="ex_go_review", type="primary", width="stretch", on_click=open_review)


def _report_tools(p: Prepared) -> None:
    """Report tools: picture pages (OCR), figures found with the current wording, learn wording."""
    missing = p.ocr_missing
    with st.popover("Report tools", icon=":material/build:", width="stretch"):
        doc = p.doc
        st.html(f'<div class="bpe-kv"><span>File</span><span>{escape(doc["name"])}</span></div>'
                f'<div class="bpe-kv"><span>Pages</span><span>{p.extraction.page_count}</span></div>')
        st.markdown(extract_review.picture_pages_summary(p.ocr_needed, missing))
        if missing and p.ocr_engine is not None and st.button(
                f"Read {len(missing)} page(s) with OCR", icon=":material/document_scanner:", key="ex_run_ocr"):
            bar = st.progress(0.0, text="Starting OCR...")
            ocr.run_ocr(doc["bytes"], missing, p.ocr_engine, p.ocr_path,
                        on_page=lambda done, total: bar.progress(done / total, text=f"OCR: page {done} of {total}"))
            st.rerun()
        _new_figures(p)
        config = p.ctx.config
        if config is not None:
            extract_review._learn_wording(config, p.ctx, p.analysis, p.extraction)


def _new_figures(p: Prepared) -> None:
    with closing(connect(p.ctx.db_path)) as conn:
        new = new_figures(conn, p.ctx.document_id, p.analysis)
        saved_version = get_document(conn, p.ctx.document_id)["extractor_version"]
    extract_review._new_figures_prompt(p.ctx, new, p.analysis, p.extraction, saved_version)


# --- on top: the whole page and a close-up of the boxed row (option H, D-095) ---------------------

def _upper(p: Prepared, metric_id: str | None) -> None:
    page_count = p.extraction.page_count
    st.session_state.viewer_page = min(max(1, st.session_state.get("viewer_page", 1)), page_count)
    st.session_state.setdefault("ex_zoom", "100%")
    page = st.session_state.viewer_page
    box, kind = _box_on(p, page)
    with st.container(key="ex_upper"):
        whole_col, close_col = st.columns([1, 4.3], gap=None)
        with whole_col, st.container(key="ex_whole"):
            st.html('<div class="bpe-cap">Whole page</div>')
            st.image(_page_png(p, page, box, kind, 1.0), width="stretch")
            if st.button("See the whole page", icon=":material/fullscreen:", key="ex_full", width="stretch",
                         help="Open this page large, in a window over the app"):
                highlight, boxes = _boxes(p, page, box, kind)
                extract_review._full_screen(p.doc, page, highlight, boxes)
        with close_col, st.container(key="ex_page"):
            _toolbar(p, metric_id, page, box is not None and kind == "selected")
            _find_results(p, page)
            with st.container(key="ex_sheet"):
                _close_up(p, page, box, kind)


def _box_on(p: Prepared, page: int) -> tuple[tuple | None, str]:
    """The box to show on this page: the word just found, else the picked metric's figure."""
    found = st.session_state.get("ex_found")
    if st.session_state.get("ex_focus") == "find" and found and found[0] == page - 1:
        return tuple(found[1]), "marked"
    selected = find_by_key(p.extraction.candidates, st.session_state.get("selected_key"))
    if selected is not None and selected.page_index == page - 1:
        return tuple(selected.bbox), "selected"
    return None, "selected"


def _boxes(p: Prepared, page: int, box: tuple | None, kind: str) -> tuple[tuple | None, tuple]:
    """The red box (the metric's figure) and the other boxes drawn on this page."""
    highlight = box if kind == "selected" else None
    boxes = tuple((b, "recorded", name) for _, (pg, b, name) in p.recorded.items() if pg == page - 1)
    if box is not None and kind == "marked":
        boxes += ((box, "marked"),)
    return highlight, boxes


def _page_png(p: Prepared, page: int, box: tuple | None, kind: str, scale_zoom: float) -> bytes:
    highlight, boxes = _boxes(p, page, box, kind)
    return extract_review._render(p.doc["sha"], page - 1, highlight, extract_review.BASE_RESOLUTION * scale_zoom, 1.0,
                                  (0.5, 0.5), boxes, p.doc["bytes"])


def _toolbar(p: Prepared, metric_id: str | None, page: int, opened: bool) -> None:
    page_count = p.extraction.page_count
    name = p.analysis.metric(metric_id).metric.display_name if metric_id else None
    with st.container(key="ex_opened"):
        cols = st.columns([2.6, 2.4, 0.32, 0.62, 0.55, 0.32, 0.2, 0.32, 0.55, 0.32], vertical_alignment="center",
                          gap=None)
        if opened and name:
            cols[0].html(f'<span class="bpe-opened">→ Opened for <b>{escape(name)}</b></span>')
        elif st.session_state.get("ex_focus") == "find" and st.session_state.get("ex_find"):
            cols[0].html(f'<span class="bpe-opened">“{escape(st.session_state.ex_find)}” on PDF page {page}</span>')
        cols[1].text_input("Find in this report", key="ex_find", placeholder="Find a word or number in this report",
                           label_visibility="collapsed", icon=":material/search:")
        cols[2].button("", icon=":material/chevron_left:", key="ex_prev_page", disabled=page <= 1,
                       on_click=extract_review._go_to, args=(page - 1,), help="Previous page")
        cols[3].number_input("Page", min_value=1, max_value=page_count, key="viewer_page", label_visibility="collapsed",
                             help="Type a page number to go to it")
        cols[4].html(f'<span class="bpe-num bpe-minor" style="white-space:nowrap">of {page_count}</span>')
        cols[5].button("", icon=":material/chevron_right:", key="ex_next_page", disabled=page >= page_count,
                       on_click=extract_review._go_to, args=(page + 1,), help="Next page")
        at = ZOOMS.index(st.session_state.ex_zoom) if st.session_state.ex_zoom in ZOOMS else 0
        cols[7].button("", icon=":material/remove:", key="ex_zoom_out", disabled=at <= 0, on_click=_zoom,
                       args=(at - 1,), help="Zoom out")
        cols[8].html(f'<span class="bpe-num" style="display:block;text-align:center">{escape(ZOOMS[at])}</span>')
        cols[9].button("", icon=":material/add:", key="ex_zoom_in", disabled=at >= len(ZOOMS) - 1, on_click=_zoom,
                       args=(at + 1,), help="Zoom in")


def _zoom(at: int) -> None:
    st.session_state.ex_zoom = ZOOMS[at]


@st.cache_data(show_spinner=False, max_entries=30)
def _find(sha256: str, query: str, _pdf_bytes: bytes, _ocr_words: dict) -> list[tuple[int, tuple]]:
    return page_search.find_in_report(_pdf_bytes, query, _ocr_words)


def _go_hit(page_index: int, box: tuple) -> None:
    st.session_state.viewer_page = page_index + 1
    st.session_state.ex_found = (page_index, box)
    st.session_state.ex_focus = "find"


def _find_results(p: Prepared, page: int) -> None:
    """The pages that hold what was typed in the search box, each one click away."""
    query = (st.session_state.get("ex_find") or "").strip()
    if len(query) < 2:
        return
    hits = _find(p.doc["sha"], query, p.doc["bytes"], p.ocr_words)
    with st.container(key="ex_hits"):
        st.html("<style>.st-key-ex_sheet { height: calc(100vh - 556px) !important; } "  # room for this row
                ".st-key-ex_sheet img { max-height: calc(100vh - 584px); }</style>")
        if not hits:
            st.html(f'<span class="bpe-minor">“{escape(query)}” is not on any page of this report.</span>')
            return
        shown = hits[:HITS_SHOWN]
        cols = st.columns([2.2] + [0.42] * len(shown) + [1.4 + 0.42 * (HITS_SHOWN - len(shown))],
                          vertical_alignment="center", gap="small")
        cols[0].html(f'<span class="bpe-minor">“{escape(query)}” is on {len(hits)} page{"s" if len(hits) != 1 else ""}:'
                     '</span>')
        for col, (page_index, box) in zip(cols[1:], shown):
            col.button(str(page_index + 1), key=f"ex_hit_{page_index}", width="stretch",
                       type="primary" if page_index == page - 1 else "secondary",
                       on_click=_go_hit, args=(page_index, box))
        if len(hits) > len(shown):
            cols[-1].html(f'<span class="bpe-minor">and {len(hits) - len(shown)} more; type more words to narrow it'
                          '</span>')


def _close_up(p: Prepared, page: int, box: tuple | None, kind: str) -> None:
    """The boxed row and the rows around it, large. With nothing boxed, the page at full width."""
    view_zoom = extract_review.ZOOM_LEVELS.get(st.session_state.ex_zoom, 1.0)
    png = _page_png(p, page, box, kind, view_zoom * 1.4)
    if box is None:
        st.html("<style>.st-key-ex_sheet { overflow-y: auto !important; }</style>")
        st.image(png, width="stretch")
        return
    size = p.extraction.pages[page - 1]
    st.image(_strip(png, view_zoom, focus_of(box, size.width, size.height)), width="stretch")


@st.cache_data(show_spinner=False, max_entries=50)
def _strip(png: bytes, view_zoom: float, focus: tuple[float, float]) -> bytes:
    """A wide strip of the page around focus. At 100% the strip is the page's full width;
    zooming in narrows it, so the rows around the box get larger."""
    image = Image.open(io.BytesIO(png))
    width = image.width / view_zoom
    height = min(image.height, width / STRIP_ASPECT)
    left = min(max(0.0, focus[0] * image.width - width / 2), image.width - width)
    top = min(max(0.0, focus[1] * image.height - height / 2), image.height - height)
    out = io.BytesIO()
    image.crop((round(left), round(top), round(left + width), round(top + height))).save(out, format="PNG")
    return out.getvalue()


# --- underneath: the drawer ----------------------------------------------------------------------

def _drawer(p: Prepared, statuses: dict[str, Status], order: list[str], selected: str | None,
            open_review: Callable[[], None]) -> None:
    counts = {f: sum(f == "all" or s.group == f for s in statuses.values()) for f in FILTERS}
    passed = sum(r.outcome == "pass" for r in p.check_results)
    tab_labels = {"metrics": f"Metrics {len(statuses)}",
                  "checks": f"Checks · {passed} of {len(p.check_results)} passed"}
    with st.container(key="ex_drawer"):
        list_col, side_col = st.columns([3.3, 1.2], gap=None)
        with list_col, st.container(key="ex_list"):
            with st.container(key="ex_tabs"):
                tab_col, filter_col = st.columns([1, 1.3], vertical_alignment="center")
                st.session_state.setdefault("ex_tab", "metrics")
                tab = tab_col.pills("View", TABS, key="ex_tab", format_func=tab_labels.get,
                                    label_visibility="collapsed") or "metrics"
                chosen = "all"
                if tab == "metrics":
                    st.session_state.setdefault("ex_filter", "needs" if counts["needs"] else "all")
                    with filter_col, st.container(key="ex_filters"):
                        chosen = st.pills("Show", FILTERS, key="ex_filter",
                                          format_func=lambda f: f"{FILTER_LABELS[f]} {counts[f]}",
                                          label_visibility="collapsed") or "all"
            if tab == "checks":
                _checks(p)
            else:
                _metric_rows(p, statuses, [m for m in order if chosen == "all" or statuses[m].group == chosen],
                             selected)
        with side_col, st.container(key="ex_side"):
            if selected:
                _about(p, p.analysis.metric(selected), statuses[selected], open_review)


def _metric_rows(p: Prepared, statuses: dict[str, Status], shown: list[str], selected: str | None) -> None:
    st.html('<div class="bpe-exhead"><span>Metric</span><span class="r">Value the app picked</span>'
            '<span class="r">Page</span><span>Status</span></div>')
    if selected in shown and st.session_state.get("_ex_follows") != selected:
        st.session_state.ex_page = shown.index(selected) // ROWS_PER_PAGE  # the page of the metric picked
        st.session_state._ex_follows = selected
    with st.container(key="ex_rows"):
        start, end = (0, 0) if not shown else _page_slice(len(shown))
        for metric_id in shown[start:end]:
            _row(p, metric_id, statuses[metric_id], metric_id == selected)
        if not shown:
            st.html('<div class="bpe-qmore">Nothing in this list. Choose another filter.</div>')
    _list_pager(len(shown))


def _page_slice(total: int) -> tuple[int, int]:
    pages = max(1, -(-total // ROWS_PER_PAGE))
    page = min(max(0, st.session_state.get("ex_page", 0)), pages - 1)
    st.session_state.ex_page = page
    return page * ROWS_PER_PAGE, min(total, (page + 1) * ROWS_PER_PAGE)


def _list_pager(total: int) -> None:
    """A small pager on the right under the list: "1–6 of 41  ‹ 1 / 7 ›"."""
    if not total:
        return
    pages = max(1, -(-total // ROWS_PER_PAGE))
    page = st.session_state.get("ex_page", 0)
    start, end = _page_slice(total)
    with st.container(key="ex_pager"):
        cols = st.columns([6, 1.3, 0.35, 0.6, 0.35], vertical_alignment="center", gap=None)
        cols[1].html(f'<span class="bpe-num bpe-minor" style="white-space:nowrap">{start + 1}–{end} of {total}</span>')
        cols[2].button("", icon=":material/chevron_left:", key="ex_page_prev", disabled=page == 0, help="Previous rows",
                       on_click=st.session_state.__setitem__, args=("ex_page", page - 1))
        cols[3].html(f'<span class="bpe-num" style="display:block;text-align:center">{page + 1} / {pages}</span>')
        cols[4].button("", icon=":material/chevron_right:", key="ex_page_next", disabled=page >= pages - 1,
                       help="Next rows", on_click=st.session_state.__setitem__, args=("ex_page", page + 1))


def _row(p: Prepared, metric_id: str, status: Status, selected: bool) -> None:
    result = p.analysis.metric(metric_id)
    primary = result.primary
    with st.container(key=f"exrow_{metric_id}"):
        if selected:
            st.html(f"<style>.st-key-exrow_{metric_id} {{ background: #F5F7FE; box-shadow: inset 3px 0 #2747C9; }}"
                    "</style>")
        name_col, value_col, page_col, status_col = st.columns([2.2, 1.6, 0.6, 1.7], vertical_alignment="center",
                                                               gap="small")
        name_col.button(result.metric.display_name, key=f"ex_{metric_id}", type="tertiary", width="stretch",
                        on_click=_pick, args=(p, metric_id))
        value_col.html(f'<div class="bpe-qval">{escape(primary.mapped.candidate.raw_value_text) if primary else "–"}'
                       '</div>')
        page_col.html(f'<div class="bpe-qval">{primary.mapped.candidate.page_index + 1 if primary else ""}</div>')
        status_col.html(style.chip(status.label, status.kind))


def _checks(p: Prepared) -> None:
    icons = {"pass": ("Passed", "ok"), "fail": ("Failed", "bad"), "skipped": ("Not checked", "muted")}
    rows = "".join(f"<tr><td>{escape(r.name)}</td><td>{style.chip(*icons[r.outcome])}</td>"
                   f'<td class="bpe-minor">{escape(r.message)}</td></tr>' for r in p.check_results)
    st.html('<table class="bpe-itable bpe-exchecks"><thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr>'
            f'</thead><tbody>{rows or "<tr><td colspan=3>No checks are set up.</td></tr>"}</tbody></table>'
            '<p class="bpe-minor" style="padding:8px 14px;margin:0">Checks point out where to look. They never '
            'change, approve or reject a figure.</p>')


# --- beside the drawer: about the picked metric ---------------------------------------------------

def _about(p: Prepared, result: MetricResult, status: Status, open_review: Callable[[], None]) -> None:
    st.html(f'{style.chip(status.label, status.kind)}'
            f'<div style="font-size:15px;font-weight:600;margin-top:8px">{escape(result.metric.display_name)}</div>')
    primary = result.primary
    finding = status.finding
    others: list[MetricCandidate] = []
    if finding is not None and finding.rule_id == "CONFLICT":
        values = [c for c in result.candidates if c.mapped.key in finding.candidate_keys]
        others = [c for c in values if primary is None or c.mapped.key != primary.mapped.key]
        boxed = st.session_state.get("selected_key")
        lines = "".join(f'<span>PDF page {c.mapped.candidate.page_index + 1}'
                        f'{" · boxed" if c.mapped.key == boxed else ""}</span>'
                        f'<span class="bpe-num">{escape(c.mapped.candidate.raw_value_text)}</span>' for c in values)
        st.html(f'<div class="bpe-vs">{lines}</div>'
                '<div class="bpe-minor">Review will ask you which one is right.</div>')
    elif finding is not None:
        st.html(f'<div class="bpe-minor">{escape(_plain(finding.message))}</div>')
    elif primary is None and status.group == "not_found":
        st.html('<div class="bpe-minor">The app found no figure for this metric. On Review you can mark it on the '
                'page, or record that the report does not give it.</div>')
    if primary is not None and (finding is None or finding.rule_id != "CONFLICT"):
        st.html(f'<div class="bpe-vs"><span>PDF page {primary.mapped.candidate.page_index + 1} · boxed</span>'
                f'<span class="bpe-num">{escape(primary.mapped.candidate.raw_value_text)}</span></div>')
    st.button("Open in Review", key="ex_open_review", type="primary", width="stretch", on_click=open_review)
    for c in others[:2]:
        n = c.mapped.candidate.page_index + 1
        st.button(f"Box {c.mapped.candidate.raw_value_text} on page {n}", key=f"ex_show_{n}_{c.mapped.candidate.row_index}",
                  width="stretch",
                  on_click=_show_other, args=(c.mapped.candidate, p.extraction))


def _show_other(candidate, extraction) -> None:
    extract_review._show_candidate(candidate, extraction)
    st.session_state.ex_focus = "metric"
