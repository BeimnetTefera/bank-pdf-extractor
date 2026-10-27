"""The app's pages by name, so a screen can link to another without importing app.py.

app.py registers its st.Page objects at start-up. When a screen runs on its own (in tests),
nothing is registered and links are left out instead of failing.
"""

from __future__ import annotations

import streamlit as st

PAGES: dict[str, object] = {}


def register(**pages: object) -> None:
    PAGES.update(pages)


def link(name: str, label: str, icon: str | None = None, **query_params: str) -> None:
    """An in-app link that keeps the session (open report, reviewer); nothing when the page is unknown."""
    page = PAGES.get(name)
    if page is not None:
        st.page_link(page, label=label, icon=icon, query_params=query_params or None)


def go(name: str, **query_params: str) -> None:
    """Switch to a page in this session; nothing when the page is unknown."""
    page = PAGES.get(name)
    if page is not None:
        st.switch_page(page, query_params=query_params or None)
