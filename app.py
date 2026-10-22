"""Streamlit entry point. Run with: streamlit run app.py

Only navigation lives here; each screen is in src/ui/.
"""

import streamlit as st

from src.config_loader import ConfigError, load_config
from src.ui import extract_review, pages

st.set_page_config(page_title="Bank PDF Extractor", page_icon=":material/account_balance:", layout="wide")

try:
    config = load_config()
except ConfigError as exc:
    st.error("The configuration could not be loaded. Fix the file below and reload the page.")
    st.code(str(exc), language="text")
    st.stop()


def home_page() -> None:
    pages.home()


def extract_review_page() -> None:
    extract_review.render(config)


def bank_reports_page() -> None:
    pages.bank_reports()


def templates_page() -> None:
    pages.templates(config)


def settings_page() -> None:
    pages.settings(config)


app_pages = [
    st.Page(home_page, title="Home", icon=":material/home:", url_path="home"),
    st.Page(extract_review_page, title="Extract & Review", icon=":material/fact_check:", url_path="extract_review", default=True),
    st.Page(bank_reports_page, title="Bank Reports", icon=":material/account_balance:", url_path="bank_reports"),
    st.Page(templates_page, title="Templates", icon=":material/description:", url_path="templates"),
    st.Page(settings_page, title="Settings", icon=":material/settings:", url_path="settings"),
]
# Hidden built-in menu so the app title can sit above our own menu, as in the mockup.
navigation = st.navigation(app_pages, position="hidden")

with st.sidebar:
    st.markdown("### :material/account_balance: Bank PDF Extractor")
    for page in app_pages:
        st.page_link(page)
    st.divider()
    st.caption(f"Reviewer: {config.settings.default_reviewer}")
    st.caption("Local prototype · documents stay on this computer")

navigation.run()
