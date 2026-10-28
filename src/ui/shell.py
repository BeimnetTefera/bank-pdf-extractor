"""The frame around every page, as in the redesign mockup (D-092): a top bar with the product name,
the search box and the reviewer, then the menu on the left (two groups split by a line, with counts;
icons only as a rail) and the page itself.

Streamlit's own sidebar is not used, so the top bar can run across the whole window.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from html import escape

import streamlit as st

from src import bulk_review, search
from src.config_loader import AppConfig
from src.database import connect
from src.project_service import list_documents
from src.ui import style

SEARCH_KEY = "global_search"


def top_bar(config: AppConfig) -> str:
    """Brand, search box and reviewer; returns the search text."""
    with st.container(key="topbar"):
        brand_col, search_col, me_col = st.columns([2.2, 4, 2], vertical_alignment="center")
        brand_col.html(f'<div class="bpe-brand"><span class="bpe-mark">{style.mark_svg()}</span>'
                       'Bank PDF Extractor</div>')
        query = search_col.text_input("Search", key=SEARCH_KEY, placeholder="Search reports, metrics or a printed figure",
                                      label_visibility="collapsed", icon=":material/search:")
        name = st.session_state.get("reviewer", "") or "no name"
        me_col.html(f'<div class="bpe-me"><span class="bpe-avatar-lg">{escape(style.initials(name))}</span>'
                    f'{escape(name)} · Reviewer{style.icon("expand_more")}</div>')
    return query or ""


def menu_counts(config: AppConfig) -> tuple[int, int]:
    """(saved reports, metrics waiting for review across them)."""
    with closing(connect(config.settings.database_path)) as conn:
        documents = list_documents(conn)
        waiting = sum(bulk_review.outstanding_work(conn, d["id"], config).count for d in documents)
    return len(documents), waiting


def menu(groups: Sequence[tuple[str, Sequence[tuple[object, str | None]]]], rail: bool, current: object = None,
         titles: bool = False) -> None:
    """The left menu: (group label, [(page, count text)]); the current page is marked. With titles each
    group is named, else the groups are split by a thin line. As a rail: icons only, no counts."""
    with st.container(key="nav_rail" if rail else "nav"):
        for at, (label, pages) in enumerate(groups):
            if titles and not rail:
                st.html(f'<div class="bpe-group">{escape(label)}</div>')
            elif at:
                st.html('<div class="bpe-navsep"></div>')
            for page, count in pages:
                on = current is not None and page.url_path == current.url_path
                with st.container(key=f"navitem_{page.url_path or 'home'}{'_on' if on else ''}"):
                    if rail:
                        st.page_link(page, label=" ", help=page.title, width="stretch")
                        continue
                    link_col, count_col = st.columns([5, 1], vertical_alignment="center", gap=None)
                    link_col.page_link(page, width="stretch")
                    if count:
                        count_col.html(f'<span class="bpe-count">{escape(count)}</span>')


def search_results(config: AppConfig, query: str, open_hit: Callable[[search.Hit], None]) -> None:
    """The hits for the search box, each with Open; nothing when the box is empty."""
    if not query.strip():
        return
    with closing(connect(config.settings.database_path)) as conn:
        hits = search.search(conn, config, query)
    with st.container(key="panel_search"):
        st.markdown(f"**Search** · “{escape(query.strip())}”")
        if not hits:
            st.caption("Nothing found. Try a bank, a fiscal year, a metric name or the digits of a printed figure.")
        icons = {"report": ":material/description:", "metric": ":material/check_box:", "figure": ":material/pin:"}
        for i, hit in enumerate(hits):
            text_col, open_col = st.columns([6, 1], vertical_alignment="center")
            text_col.markdown(f"{icons[hit.kind]} **{escape(hit.title)}**  \n:gray[{escape(hit.detail)}]")
            open_col.button("Open", key=f"search_open_{i}", width="stretch", on_click=open_hit, args=(hit,))


def pager(key: str, total: int, size: int) -> tuple[int, int]:
    """Page through a long list: draws "‹ Page n of N ›" when it needs more than one page and
    returns the (start, end) slice of the page shown. The page is kept in st.session_state[key]."""
    pages = max(1, -(-total // size))
    page = min(max(0, st.session_state.get(key, 0)), pages - 1)
    st.session_state[key] = page
    if pages > 1:
        with st.container(key=f"pager_{key}"):
            prev_col, text_col, next_col = st.columns([1, 2, 1], vertical_alignment="center")
            prev_col.button("‹", key=f"{key}_prev", disabled=page == 0, width="stretch", help="Previous page",
                            on_click=st.session_state.__setitem__, args=(key, page - 1))
            text_col.html(f'<div class="bpe-minor" style="text-align:center;white-space:nowrap" '
                          f'title="{total} items">Page {page + 1} of {pages}</div>')
            next_col.button("›", key=f"{key}_next", disabled=page >= pages - 1, width="stretch", help="Next page",
                            on_click=st.session_state.__setitem__, args=(key, page + 1))
    return page * size, min(total, (page + 1) * size)
