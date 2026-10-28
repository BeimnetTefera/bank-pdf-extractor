"""Streamlit entry point. Run with: streamlit run app.py

Only navigation lives here; each screen is in src/ui/.
"""

import streamlit as st

from src.config_loader import ConfigError, load_config
from src.seed_data import restore_seed
from src.ui import (calculations, evidence_page, extract_review, new_report, pages, review_page, routes,
                    sector_page, shell, style)

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


def extract_page() -> None:
    extract_review.render(config, open_calculations=_open_calculations)


def review() -> None:
    review_page.render(config, open_calculations=_open_calculations, open_reports=lambda: st.switch_page(app_pages[3]))


def calculations_page() -> None:
    calculations.render(config, open_report=_open_report)


def bank_reports_page() -> None:
    pages.bank_reports(config, open_report=_open_report, new_report=_new_report, open_calculations=_open_calculations)


def new_report_page() -> None:
    new_report.render(config, open_review=lambda: st.switch_page(app_pages[7]))


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
    st.Page(home_page, title="Getting started", icon=":material/school:", url_path="home", visibility="hidden"),
    st.Page(extract_page, title="Extract", icon=":material/fact_check:", url_path="extract_review", default=True),
    st.Page(calculations_page, title="Calculations", icon=":material/calculate:", url_path="calculations"),
    st.Page(bank_reports_page, title="Reports", icon=":material/description:", url_path="bank_reports"),
    st.Page(sector_data_page, title="Sector data", icon=":material/public:", url_path="sector_data"),
    st.Page(templates_page, title="Templates", icon=":material/notes:", url_path="templates"),
    st.Page(settings_page, title="Settings", icon=":material/settings:", url_path="settings"),
    st.Page(review, title="Review", icon=":material/check_box:", url_path="review"),
    st.Page(new_report_page, title="New report", icon=":material/add:", url_path="new_report", visibility="hidden"),
]
# Reached by links that name a report (and metric), so they stay out of the menu.
evidence_pages = {
    "evidence": st.Page(evidence, title="Evidence", icon=":material/verified:", url_path="evidence",
                        visibility="hidden"),
    "evidence_summary": st.Page(evidence_summary, title="Evidence summary", icon=":material/table_view:",
                                url_path="evidence_summary", visibility="hidden"),
}
routes.register(extract_review=app_pages[1], review=app_pages[7], bank_reports=app_pages[3], sector_data=app_pages[4],
                calculations=app_pages[2], settings=app_pages[6], templates=app_pages[5], **evidence_pages)


def _open_report(document_id: str) -> str | None:
    """Open a saved report on the Review page."""
    error = extract_review.open_saved(config, document_id)
    if error is None:
        st.switch_page(app_pages[7])
    return error


def _new_report() -> None:
    """The New report page, with no report open."""
    for key in ("doc", "selected_key", "selected_metric", "detail_mode", "marked"):
        st.session_state.pop(key, None)
    st.switch_page(app_pages[8])


def _open_calculations(document_id: str) -> None:
    st.session_state.calc_document = document_id
    st.switch_page(app_pages[2])


def _open_hit(hit) -> None:
    """A search result: open its report on the Review page, at the metric or page it found."""
    if extract_review.open_saved(config, hit.document_id) is not None:
        return
    if hit.metric_id:
        st.session_state.selected_metric = hit.metric_id
        st.session_state.detail_mode = "metric"
    if hit.page:
        st.session_state.viewer_page = hit.page
    st.session_state[shell.SEARCH_KEY] = ""
    st.session_state.goto_review = True


# Our own frame (top bar and menu, D-092), so Streamlit's menu is hidden.
navigation = st.navigation([*app_pages, *evidence_pages.values()], position="hidden")

# The address form ?page=evidence&report=<id>&metric=<id> leads to the Evidence pages too.
if st.query_params.get("page") in evidence_pages:
    target = evidence_pages[st.query_params["page"]]
    st.switch_page(target, query_params={k: v for k, v in st.query_params.items() if k != "page"})
if st.session_state.pop("goto_review", False):
    st.switch_page(app_pages[7])

st.session_state.reviewer = st.session_state.get("reviewer", config.settings.default_reviewer)
reports_count, waiting_count = shell.menu_counts(config)
# The menu in two groups, as in the redesign mockup: the daily work, then the setup.
MENU = (("Workspace", ((app_pages[3], str(reports_count)), (app_pages[1], None), (app_pages[7], str(waiting_count)),
                       (app_pages[2], None), (app_pages[4], None))),
        ("Setup", ((app_pages[5], None), (app_pages[6], None))))

query = shell.top_bar(config)
nav_col, page_col = st.columns([1, 6], gap=None)
with nav_col:
    shell.menu(MENU, rail=False)  # the full menu on every page, Review too (user feedback 2026-10-28)
with page_col, st.container(key="page"):
    shell.search_results(config, query, _open_hit)
    navigation.run()
