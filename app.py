"""Streamlit entry point. Run with: streamlit run app.py

Only navigation lives here; each screen is in src/ui/.
"""

import streamlit as st

from src.config_loader import ConfigError, load_config
from src.seed_data import restore_seed
from src.ui import calculations, evidence_page, extract_review, pages, routes, sector_page, style

# "expanded" rather than the default "auto": with "auto" the browser re-decides on window
# width each time the sidebar is redrawn, so it could close by itself after a rerun.
st.set_page_config(
    page_title="Bank PDF Extractor",
    page_icon=":material/account_balance:",
    layout="wide",
    initial_sidebar_state="expanded",
)
style.apply()

try:
    config = load_config()
except ConfigError as exc:
    st.error("The configuration could not be loaded. Fix the file below and reload the page.")
    st.code(str(exc), language="text")
    st.stop()

restore_seed(config.settings.data_dir)  # hosted copy: reload the reviewed reports after a restart


def home_page() -> None:
    pages.home()


def extract_review_page() -> None:
    extract_review.render(config, open_calculations=_open_calculations)


def calculations_page() -> None:
    calculations.render(config, open_report=_open_report)


def bank_reports_page() -> None:
    pages.bank_reports(config, open_report=_open_report, new_report=_new_report)


def sector_data_page() -> None:
    sector_page.render(config)


def templates_page() -> None:
    pages.templates(config)


def settings_page() -> None:
    pages.settings(config)


def evidence() -> None:
    evidence_page.render_evidence(config)


def evidence_summary() -> None:
    evidence_page.render_summary(config)


app_pages = [
    st.Page(home_page, title="Getting started", icon=":material/school:", url_path="home"),
    st.Page(extract_review_page, title="Extract & Review", icon=":material/fact_check:", url_path="extract_review", default=True),
    st.Page(calculations_page, title="Calculations", icon=":material/calculate:", url_path="calculations"),
    st.Page(bank_reports_page, title="Reports", icon=":material/description:", url_path="bank_reports"),
    st.Page(sector_data_page, title="Sector data", icon=":material/public:", url_path="sector_data"),
    st.Page(templates_page, title="Templates", icon=":material/description:", url_path="templates"),
    st.Page(settings_page, title="Settings", icon=":material/settings:", url_path="settings"),
]
# Reached by links that name a report (and metric), so they stay out of the menu.
evidence_pages = {
    "evidence": st.Page(evidence, title="Evidence", icon=":material/verified:", url_path="evidence",
                        visibility="hidden"),
    "evidence_summary": st.Page(evidence_summary, title="Evidence summary", icon=":material/table_view:",
                                url_path="evidence_summary", visibility="hidden"),
}
routes.register(extract_review=app_pages[1], bank_reports=app_pages[3], sector_data=app_pages[4], **evidence_pages)


def _open_report(document_id: str) -> str | None:
    error = extract_review.open_saved(config, document_id)
    if error is None:
        st.switch_page(app_pages[1])
    return error


def _new_report() -> None:
    """Extract & Review with no report open, ready for an upload."""
    for key in ("doc", "selected_key", "selected_metric", "detail_mode", "marked"):
        st.session_state.pop(key, None)
    st.switch_page(app_pages[1])


def _open_calculations(document_id: str) -> None:
    st.session_state.calc_document = document_id
    st.switch_page(app_pages[2])


# Hidden built-in menu so the app title can sit above our own menu, as in the mockup.
navigation = st.navigation([*app_pages, *evidence_pages.values()], position="hidden")

# The address form ?page=evidence&report=<id>&metric=<id> leads to the Evidence pages too.
if st.query_params.get("page") in evidence_pages:
    target = evidence_pages[st.query_params["page"]]
    st.switch_page(target, query_params={k: v for k, v in st.query_params.items() if k != "page"})

# The menu in two groups, as in the redesign (D-090): the daily work, then the setup.
WORKSPACE = (app_pages[3], app_pages[1], app_pages[2], app_pages[4])  # Reports, Extract & Review, Calculations, Sector
SETUP = (app_pages[5], app_pages[6], app_pages[0])  # Templates, Settings, Getting started

with st.sidebar:
    style.brand()
    for label, group in (("Workspace", WORKSPACE), ("Setup", SETUP)):
        style.group_label(label)
        for page in group:
            st.page_link(page)
    st.divider()
    st.session_state.reviewer = st.session_state.get("reviewer", config.settings.default_reviewer)
    st.text_input("Reviewer", key="reviewer", help="Saved with every review decision. No login in this prototype.")
    st.caption("Pilot version · use public annual reports only")

navigation.run()
