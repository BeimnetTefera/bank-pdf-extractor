"""The Calculations page as in the approved mockup (2026-10-29): tiles, filters, one statement group open at
a time with pages, accept one or all (synthetic report with total assets and equity approved)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from src import calculation_service as calc
from src import review_service as review
from src.config_loader import load_config
from src.ui.calculations import check, kind, pages_of
from tests.conftest import html_text
from tests.test_persistence_and_review import REVIEWER, conn, primary_id, saved  # noqa: F401 (fixtures)

LEVERAGE = "Leverage Ratio"


def calculations_screen(data_dir: str) -> None:
    from dataclasses import replace
    from pathlib import Path

    from src.config_loader import AppSettings, load_config
    from src.ui import calculations

    settings = AppSettings(data_dir=Path(data_dir), max_upload_mb=100, max_pages=1000, default_reviewer="tester")
    calculations.render(replace(load_config(), settings=settings), open_report=lambda document_id: None)


@pytest.fixture
def page(saved, conn, settings) -> AppTest:
    _, document, _, _ = saved
    for metric_id in ("total_assets_etb", "total_equity_etb"):
        review.approve(conn, primary_id(saved, metric_id), REVIEWER, acknowledge_warnings=True)
    review.finish_extraction(conn, document.id, REVIEWER, "done")
    at = AppTest.from_function(calculations_screen, args=(str(settings.data_dir),), default_timeout=60)
    at.session_state["reviewer"] = REVIEWER
    at.run()
    assert not at.exception
    at.document_id = document.id
    return at


def leverage_key() -> str:
    return f"accept_trow_calc_{load_config().export_template.calculated.index(LEVERAGE)}"


def open_group(at: AppTest, name: str) -> AppTest:
    next(b for b in at.button if (b.key or "").startswith("calc_grp_btn_") and b.label.startswith(name.upper())).click()
    return at.run()


def test_tiles_and_the_first_group_open(page):
    text = html_text(page)
    assert "Extraction finished" in text and "Ready to accept" in text and "Entered values" in text
    assert page.button(key="calc_accept_all").label == "Accept 3 auto values"  # average assets, leverage, equity/assets
    assert "Showing 1–4 of 5" in text and "Page 1 of 2" in text  # Profit and per share: all miss an input


def test_one_group_open_at_a_time(page):
    open_group(page, "Liquidity")
    assert any((b.key or "") == leverage_key() for b in page.button)
    assert "Showing 1–4 of 5" in html_text(page)
    open_group(page, "Liquidity")  # clicking the open group closes it
    assert not any((b.key or "") == leverage_key() for b in page.button)
    assert "Showing" not in html_text(page)


def test_accept_one_then_it_reads_entered(page, conn):
    open_group(page, "Liquidity")
    page.button(key=leverage_key()).click().run()
    assert not page.exception
    assert calc.current_values(conn, page.document_id)[LEVERAGE].formula_note.startswith("Auto (draft formula)")
    assert "✓ Entered · " + REVIEWER in html_text(page)
    assert page.button(key="calc_accept_all").label == "Accept 2 auto values"


def test_pages_inside_the_open_group(page):
    open_group(page, "Network and customers")
    assert "Showing 1–4 of 8" in html_text(page) and page.button(key="calc_prev").disabled
    page.button(key="calc_next").click().run()
    assert "Showing 5–8 of 8" in html_text(page) and page.button(key="calc_next").disabled


def test_ready_filter_shows_only_groups_with_ready_values(page):
    page.button(key="calc_chip_ready").click().run()
    groups = [b.label for b in page.button if (b.key or "").startswith("calc_grp_btn_")]
    assert [g.split(" :")[0] for g in groups] == ["BALANCE SHEET", "LIQUIDITY, CAPITAL AND LEVERAGE"]
    assert "Showing 1–1 of 1" in html_text(page)  # the first one left opens by itself
    open_group(page, "Liquidity")
    assert "Showing 1–2 of 2" in html_text(page)


def test_accept_all(page, conn):
    page.button(key="calc_accept_all").click().run()
    assert set(calc.current_values(conn, page.document_id)) == {"Average Total Asset", LEVERAGE, "Equity-to-Asset Ratio"}
    assert page.button(key="calc_accept_all").disabled


def test_kind_check_and_pages():
    auto = SimpleNamespace(value=Decimal("0.032243"), reported=Decimal("0.032"))
    assert kind(auto, None) == "diff" and check(auto) == ("+0.8% · above 0.5%", "warn")
    assert check(SimpleNamespace(value=Decimal("5"), reported=Decimal("5"))) == ("✓ Matches · 0", "ok")
    assert check(SimpleNamespace(value=Decimal("0.209213"), reported=Decimal("0.209")))[0] == "✓ Within 0.5% · 0.1%"
    assert check(SimpleNamespace(value=Decimal("1"), reported=None)) == ("Nothing to compare", "muted")
    assert kind(SimpleNamespace(value=None), None) == "missing"
    assert kind(auto, SimpleNamespace(value=Decimal("0.032"))) == "entered"
    kinds = {"a": "ready", "b": "ready", "c": "ready", "d": "diff", "e": "ready"}
    assert pages_of(list(kinds), kinds) == [["a", "b", "c"], ["d", "e"]]  # the difference needs two slots
