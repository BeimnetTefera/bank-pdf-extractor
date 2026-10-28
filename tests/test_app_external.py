"""External sources, cross-checks and Sector data through Streamlit (synthetic PDFs, temporary data folder)."""

from contextlib import closing
from decimal import Decimal

import streamlit as st
from streamlit.testing.v1 import AppTest

from src import external_sources as ext
from src import review_service, sector_data
from src.database import connect
from src.external_sources import SourceInput
from tests.conftest import html_text, make_pdf
from tests.test_app_calculate import REVIEWER, Upload, app, open_metric  # noqa: F401 (app is a fixture)
from tests.test_app_evidence import screen

SOURCE_PDF = make_pdf([[("National Bank of Ethiopia annual report", [])],
                       [("Employees of Awash Bank", ["12,345"])]])
QUOTE = "Awash Bank had 12,345 employees at the end of June 2023"


class SourceUpload:
    name = "nbe_annual_report.pdf"
    file_id = "nbe-source"

    def getvalue(self) -> bytes:
        return SOURCE_PDF


def upload_sources(monkeypatch) -> None:
    """The report upload as in the app fixture; every source upload field gets the NBE copy."""
    def uploader(*args, key=None, **kwargs):
        if key == "upload_main":
            return Upload()
        return SourceUpload() if key and key.endswith("_file") else None

    monkeypatch.setattr(st, "file_uploader", uploader)


def fill_source(at: AppTest, key: str, printed: str, quote: str = QUOTE, page: int | None = None) -> None:
    at.text_input(key=f"{key}_title").input("NBE Annual Report 2022/23")
    at.text_input(key=f"{key}_publisher").input("National Bank of Ethiopia")
    at.text_area(key=f"{key}_quote").input(quote)
    at.text_input(key=f"{key}_printed").input(printed)
    if page is not None:
        next(n for n in at.number_input if n.label.startswith("Page of the uploaded file")).set_value(page)
    at.run()


def test_value_from_an_external_source_in_metric_details(app, monkeypatch):
    upload_sources(monkeypatch)
    open_metric(app, "employees")
    fill_source(app, "ext_value_employees", "12,345", page=2)
    assert any("12,345 = 12,345" in c.value for c in app.caption)  # the conversion is shown
    app.text_area(key="ext_value_employees_note").input("The report does not give staff numbers").run()
    next(b for b in app.button if b.label == "Save value from external source").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        state = review_service.review_state(conn, app.document_id, "employees")
        source = ext.source_for_decision(conn, state.pending.id)
    assert state.pending.entry_kind == "external" and state.pending.reviewed_value == Decimal(12345)
    assert (source.file_name, source.page, source.quote) == ("nbe_annual_report.pdf", 2, QUOTE)
    assert any("External source awaiting approval" in i.value for i in app.info)
    next(b for b in app.button if b.label == "Approve external source").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        assert review_service.review_state(conn, app.document_id, "employees").status == "approved"

    page = screen(app, report=app.document_id, metric="employees")
    assert not page.exception
    shown = " ".join(m.value for m in page.markdown) + " ".join(c.value for c in page.caption) + html_text(page)
    assert "External source" in shown and "NBE Annual Report 2022/23" in shown
    assert f"> {QUOTE}" in shown.replace("\\", "")  # the quote as a blockquote
    assert "12,345 = 12,345" in shown
    assert "No box was marked on the uploaded page." in shown  # the uploaded page was drawn (no box on it)
    assert not any("cannot be shown" in w.value for w in page.warning)


def test_external_value_in_brackets_offers_the_sign_choice(app):
    open_metric(app, "employees")
    fill_source(app, "ext_value_employees", "(12,345)")
    app.text_input(key="ext_value_employees_url").input("https://nbe.gov.et/report.pdf").run()
    sign = app.radio(key="ext_value_employees_sign")
    assert sign.value == "positive" and sign.options == ["Record as positive (default)", "Keep negative"]
    assert any("Will store 12,345" in c.value and "recorded as a positive amount" in c.value for c in app.caption)
    sign.set_value("as_printed").run()
    assert any("Will store \\-12,345" in c.value for c in app.caption)  # Markdown-escaped minus
    app.text_area(key="ext_value_employees_note").input("The report does not give staff numbers").run()
    next(b for b in app.button if b.label == "Save value from external source").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        pending = review_service.review_state(conn, app.document_id, "employees").pending
    assert (pending.reviewed_value, pending.sign_choice) == (Decimal(-12345), "as_printed")


def test_evidence_names_the_external_source_of_a_calculation_input(app):
    import src.config_loader
    from src.calculated_entries import save_calculation

    config = src.config_loader.load_config()  # with the temporary data folder (app fixture)
    source = SourceInput(title="NBE Annual Report 2022/23", publisher="National Bank of Ethiopia",
                         quote="Total assets of Awash Bank: Birr 200 billion", printed_value="200", scale="billion",
                         url="https://nbe.gov.et/report.pdf")
    with closing(connect(app.db_path)) as conn:
        ext.record_external_value(conn, config.settings, app.document_id, "total_assets_etb", "ETB", source,
                                  "Not in this synthetic report", REVIEWER)
        review_service.approve_correction(conn, app.document_id, "total_assets_etb", REVIEWER)
        save_calculation(conn, config, app.document_id, "total_liabilities_etb", "total_assets_etb - total_equity_etb",
                         "Not printed", REVIEWER)
    page = screen(app, report=app.document_id, metric="total_liabilities_etb")
    assert not page.exception
    shown = " ".join(m.value for m in page.markdown).replace("\\", "")
    assert "from an external source" in shown and "NBE Annual Report 2022/23" in shown
    assert "> Total assets of Awash Bank: Birr 200 billion" in shown


def test_cross_check_added_in_metric_details_and_withdrawn_on_the_evidence_page(app, monkeypatch):
    open_metric(app, "total_equity_etb")  # approved in the fixture: 19,475,296 thousand
    fill_source(app, "xcheck_total_equity_etb", "25.1", quote="Total equity of Awash Bank reached Birr 25.1 billion")
    next(b for b in app.button if b.key == "xcheck_total_equity_etb_save").click().run()
    assert any("address (URL)" in e.value for e in app.error)  # neither an address nor a copy yet
    app.text_input(key="xcheck_total_equity_etb_url").input("https://nbe.gov.et/report-2022-23.pdf").run()
    next(b for b in app.button if b.key == "xcheck_total_equity_etb_save").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        (check,) = ext.cross_checks(conn, app.document_id, "total_equity_etb")
    assert check.source.url == "https://nbe.gov.et/report-2022-23.pdf"
    assert any("mismatch" in e.label for e in app.expander)

    page = screen(app, report=app.document_id, metric="total_equity_etb")
    assert not page.exception
    assert any("Does not match" in m.value for m in page.markdown)
    page.text_area(key=f"withdraw_reason_{check.id}").input("The release counts the group, not the bank").run()
    next(b for b in page.button if b.label == "Confirm withdraw").click().run()
    assert not page.exception
    with closing(connect(app.db_path)) as conn:
        (check,) = ext.cross_checks(conn, app.document_id, "total_equity_etb")
    assert not check.active and check.withdrawal.reason.startswith("The release")


def test_cross_check_added_on_the_evidence_page(app, monkeypatch):
    upload_sources(monkeypatch)
    page = screen(app, report=app.document_id, metric="profit_after_tax_etb")
    key = "ev_xcheck_profit_after_tax_etb"
    fill_source(page, key, "3,872,832", quote="Profit after tax Birr 3,872,832 thousand", page=1)
    page.selectbox(key=f"{key}_scale").set_value("birr_thousand").run()
    next(b for b in page.button if b.key == f"{key}_save").click().run()
    assert not page.exception
    with closing(connect(app.db_path)) as conn:
        (check,) = ext.cross_checks(conn, app.document_id, "profit_after_tax_etb")
    assert check.source.value == Decimal("3872832000") and check.source.has_file
    assert any(m.value.startswith("Matches") for m in page.markdown)


def sector_screen(data_dir: str) -> None:
    """The Sector data page on its own, with a given data folder (run by AppTest.from_function)."""
    from dataclasses import replace
    from pathlib import Path

    from src.config_loader import load_config
    from src.ui import sector_page

    config = load_config()
    sector_page.render(replace(config, settings=replace(config.settings, data_dir=Path(data_dir))))


def test_sector_value_recorded_and_approved_on_the_sector_page(tmp_path):
    data_dir = tmp_path / "appdata"
    at = AppTest.from_function(sector_screen, args=(str(data_dir),), default_timeout=60)
    at.session_state["reviewer"] = REVIEWER
    at.run()
    assert not at.exception
    at.text_input(key="sector_year_typed").input("2026").run()  # the form is keyed by the fiscal year
    key = "sector_sector_total_deposits_etb_2025_26"
    fill_source(at, key, "2,345.6", quote="Deposits of the banking system reached Birr 2,345.6 billion")
    at.text_input(key=f"{key}_url").input("https://nbe.gov.et/annual-report-2025-26.pdf").run()
    assert any("2,345.6 billion Birr = 2,345,600,000,000 ETB" in c.value.replace("\\", "") for c in at.caption)
    next(b for b in at.button if b.label == "Save for approval").click().run()
    assert not at.exception
    next(b for b in at.button if b.label == "Approve").click().run()
    assert not at.exception
    with closing(connect(data_dir / "app.db")) as conn:
        state = sector_data.sector_state(conn, "2025/26", "sector_total_deposits_etb")
    assert (state.status, state.value) == ("approved", Decimal("2345600000000"))
    assert at.query_params["year"] == ["2025/26"] or at.query_params["year"] == "2025/26"

    page = AppTest.from_function(sector_evidence_screen, args=(str(data_dir),), default_timeout=60)
    page.query_params["sector"] = "2025/26"
    page.query_params["metric"] = "sector_total_deposits_etb"
    page.run()
    assert not page.exception
    shown = " ".join(m.value for m in page.markdown) + " ".join(c.value for c in page.caption)
    assert "2,345,600,000,000 ETB" in shown and "Sector value for 2025/26" in shown
    assert "nbe.gov.et/annual-report-2025-26.pdf" in shown
    assert list(page.dataframe[-1].value["Action"]) == ["Value from an external source", "Approved"]


def test_sector_not_reported_and_reopen(tmp_path):
    data_dir = tmp_path / "appdata"
    with closing(connect(data_dir / "app.db")) as conn:
        from dataclasses import replace

        from src.config_loader import load_config

        config = load_config()
        config = replace(config, settings=replace(config.settings, data_dir=data_dir))
        sector_data.record_value(conn, config, config.settings, "2025/26", "sector_total_deposits_etb",
                                 SourceInput("T", "P", "q", "1", "billion", url="https://x.org"), "", REVIEWER)
    at = AppTest.from_function(sector_screen, args=(str(data_dir),), default_timeout=60)
    at.session_state["reviewer"] = REVIEWER
    at.run()
    key = "sector_sector_total_deposits_etb_2025_26"
    assert any("(awaiting approval)" in m.value for m in at.markdown)
    at.text_area(key=f"{key}_reopen_reason").input("Wrong table in the NBE report").run()
    next(b for b in at.button if b.key == f"{key}_reopen").click().run()
    assert not at.exception
    with closing(connect(data_dir / "app.db")) as conn:  # Not reported is parked off the page (redesign backlog)
        sector_data.record_not_reported(conn, config, "2025/26", "sector_total_deposits_etb",
                                        "NBE has not published 2025/26 yet", REVIEWER)
    at.run()
    next(b for b in at.button if b.label == "Approve").click().run()
    assert not at.exception
    with closing(connect(data_dir / "app.db")) as conn:
        assert sector_data.sector_state(conn, "2025/26", "sector_total_deposits_etb").status == "not_reported"


def sector_evidence_screen(data_dir: str) -> None:
    from dataclasses import replace
    from pathlib import Path

    from src.config_loader import load_config
    from src.ui import evidence_page

    config = load_config()
    evidence_page.render_evidence(replace(config, settings=replace(config.settings, data_dir=Path(data_dir))))
