"""Par value of the bank's own shares, found in the PDF with its page and box (D-070)."""

from decimal import Decimal
from pathlib import Path

import pytest

from src.par_value import best_par_value, find_par_values
from tests.conftest import make_pdf

REPORTS = Path.home() / "OneDrive" / "Desktop" / "Bank Reports"


def test_par_value_wordings_are_found_with_page_and_box():
    pdf = make_pdf([
        [("Statement of profit or loss", [])],
        [("27. Share capital: Ordinary Shares, with par value of ETB 1,000 each", [])],
        [("Authorised: 12,500,000 ordinary shares of Birr 500 each", [])],
    ])
    found = find_par_values(pdf)
    assert [(p.value, p.page, p.printed) for p in found] == [(Decimal(1000), 2, "1,000"), (Decimal(500), 3, "500")]
    x0, y0, x1, y1 = found[0].bbox
    assert x0 < x1 and y0 < y1 and found[0].text == "par value of ETB 1,000 each"


@pytest.mark.parametrize("line,printed", [
    ("Share capital: par value per share Birr 1,000", "1,000"),
    ("Ordinary shares with a par value of Birr 500 per share", "500"),
    ("Each share has a nominal value of Birr 1,000.", "1,000"),
    ("Although the par value is 100 birr per share, shares were sold at a premium", "100"),
    ("28. Share capital: ordinary shares, Birr 1,000 per share", "1,000"),
])
def test_other_par_value_wordings(line, printed):
    found = find_par_values(make_pdf([[(line, [])]]))
    assert [p.printed for p in found] == [printed]


@pytest.mark.parametrize("line", [
    "Share capital increased; dividend of Birr 250 per share was paid",  # a dividend, not the par value
    "Ordinary shares were sold at a price of Birr 1,500 per share",  # a price
    "Basic earnings per share Birr 390 per share",  # no share capital wording
    "Par value of Birr 1,000 million in total",  # a scale word: an amount, not per share
    "Investment in Eth Switch S.Co: par value per share Birr 1,000",  # another company's shares
])
def test_per_share_figures_that_are_not_the_par_value(line):
    assert find_par_values(make_pdf([[(line, [])]])) == []


def test_investee_par_values_and_tables_are_skipped():
    pdf = make_pdf([[
        ("Investment in Eth Switch S.Co ordinary shares of Birr 1,000 each", []),
        ("Fair Value per share Par value (Birr) Cost", []),
        ("Eth Switch S.Co", ["52,211", "1,000"]),
    ]])
    assert find_par_values(pdf) == []


def test_the_most_stated_par_value_wins():
    pdf = make_pdf([[("shares of Birr 100 each", [])], [("shares of Birr 1000 each", [])],
                     [("shares of Birr 1000 each", [])]])
    best = best_par_value(find_par_values(pdf))
    assert (best.value, best.page) == (Decimal(1000), 2)
    assert best_par_value([]) is None


@pytest.mark.parametrize("name,page", [
    ("Zemen_Bank_2025_26.pdf", 18),
    ("BoA Annual Report 2022_23.pdf", 113),  # not p102: that is the par value of investee companies
    ("BOA Annual Report 2025-26 Digital Version 1.pdf", 216),
    ("2024-25 BOA Annual report final print ready 2.pdf", 143),
    ("OromiaBank_2025_26_FY.pdf", 133),
    ("Wegagen_Bank_Annual_Report_2025_26_eca31dfb7c.pdf", 121),
])
def test_real_reports(name, page):
    path = REPORTS / name
    if not path.is_file():
        pytest.skip(f"{name} is not on this computer")
    best = best_par_value(find_par_values(path.read_bytes()))
    assert (best.value, best.page) == (Decimal(1000), page)
