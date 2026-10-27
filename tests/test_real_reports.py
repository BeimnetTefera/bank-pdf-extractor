"""Checks against real, public annual reports on this computer (never copied into the repository).

Each test is skipped when its PDF is not in the reports folder. The expected figures were
read from the printed pages by a person (Birr'000, this year's column).
"""

from decimal import Decimal
from pathlib import Path

import pytest

from src import identities
from src.analysis import analyse
from src.config_loader import load_config
from src.pdf_extractor import extract_candidates

REPORTS = Path.home() / "OneDrive" / "Desktop" / "Bank Reports"
ZEMEN = REPORTS / "Zemen_Bank_2025_26.pdf"
BOA_2023 = REPORTS / "BoA Annual Report 2022_23.pdf"
BOA_2026 = REPORTS / "BOA Annual Report 2025-26 Digital Version 1.pdf"


def analysed(path: Path, period: str):
    if not path.is_file():
        pytest.skip(f"{path.name} is not on this computer")
    config = load_config()
    return analyse(extract_candidates(path.read_bytes()), config, [m.id for m in config.active_metrics()], period), config


@pytest.fixture(scope="module")
def zemen():
    return analysed(ZEMEN, "2025/26")


@pytest.fixture(scope="module")
def boa_2023():
    return analysed(BOA_2023, "2022/23")


@pytest.fixture(scope="module")
def boa_2026():
    return analysed(BOA_2026, "2025/26")


def suggested(analysis, metric_id: str) -> tuple[Decimal, int, str]:
    """(value in Birr'000 or as printed, PDF page, period) of the suggested figure."""
    primary = analysis.metric(metric_id).primary
    assert primary is not None, f"{metric_id} not found"
    value = primary.normalized.value
    if primary.normalized.unit == "ETB":
        value = abs(value) / 1000
    return value, primary.mapped.candidate.page_index + 1, primary.mapped.period


def check_statement(analysis, expected: dict[str, tuple[int, int]]) -> None:
    for metric_id, (value, page) in expected.items():
        assert suggested(analysis, metric_id) == (Decimal(value), page, "current"), metric_id


ZEMEN_P22 = {
    "total_liabilities_etb": (90_590_750, 22), "total_equity_etb": (25_956_044, 22),
    "paid_up_capital_etb": (14_287_986, 22), "total_deposits_etb": (84_850_013, 22),
    "loans_and_advances_etb": (51_727_717, 22), "total_assets_etb": (116_546_794, 22),
}


def test_zemen_landscape_spread_gives_this_years_financial_position(zemen):
    analysis, _ = zemen
    check_statement(analysis, ZEMEN_P22)
    # Last year's statement left underneath in the text layer (labels mixed with the profit or loss
    # lines) gives no this-year figure: its numbers line up with the 2025 heading, not 2026.
    for metric_id in ZEMEN_P22:
        values = {abs(mc.normalized.value) / 1000 for mc in analysis.metric(metric_id).candidates
                  if mc.mapped.period == "current" and mc.mapped.candidate.page_index == 21}
        assert values == {Decimal(ZEMEN_P22[metric_id][0])}, metric_id


def test_zemen_balance_sheet_identity_holds_for_the_suggested_figures(zemen):
    analysis, config = zemen
    check = next(c for c in config.checks if c.id == "balance_sheet")
    ranked = {m.metric.id: m.candidates for m in analysis.metrics}
    outcome = identities.solve(check, ranked)
    assert outcome.unique == {"total_assets_etb": Decimal("116546794000"),
                              "total_liabilities_etb": Decimal("90590750000"),
                              "total_equity_etb": Decimal("25956044000")}


def test_zemen_kpi_sentences(zemen):
    analysis, _ = zemen
    assert suggested(analysis, "roa_percent") == (Decimal("0.066"), 3, "unknown")
    assert suggested(analysis, "roe_percent") == (Decimal("0.378"), 3, "unknown")
    eps = [mc.mapped for mc in analysis.metric("eps_etb").candidates
           if mc.mapped.candidate.page_index == 2 and mc.mapped.candidate.raw_value_text == "53.9"]
    assert eps and eps[0].parsed.is_percent  # "earnings per share at 53.9 percent", converted later (par value)


BOA_2023_STATEMENTS = {
    "total_assets_etb": (189_512_266, 46), "total_liabilities_etb": (170_036_970, 46),
    "total_equity_etb": (19_475_296, 46), "paid_up_capital_etb": (11_898_416, 46),
    "loans_and_advances_etb": (143_796_168, 46), "total_deposits_etb": (158_536_872, 46),
    "profit_before_tax_etb": (5_230_027, 45), "profit_tax_etb": (1_357_194, 45),
    "profit_after_tax_etb": (3_872_832, 45),
}


def test_boa_2023_statement_values(boa_2023):
    analysis, _ = boa_2023
    check_statement(analysis, BOA_2023_STATEMENTS)
    # Note rows (current tax only; total comprehensive income under a PAT label) no longer flag them.
    for metric_id in ("profit_tax_etb", "profit_after_tax_etb"):
        assert analysis.metric(metric_id).status == "needs_review", metric_id


def test_boa_2023_deposits_branches_and_employees(boa_2023):
    analysis, _ = boa_2023
    assert suggested(analysis, "total_deposits_etb")[:2] == (Decimal(158_536_872), 46)  # not "IFB deposits 18.07 bn"
    assert suggested(analysis, "branches")[:2] == (Decimal(849), 18)  # not "opened 123 additional"
    assert suggested(analysis, "employees")[:2] == (Decimal(11_508), 25)  # not "recruited 2,145"


BOA_2026_STATEMENTS = {
    "total_assets_etb": (381_497_212, 111), "total_liabilities_etb": (341_985_031, 111),
    "total_equity_etb": (39_512_182, 111), "paid_up_capital_etb": (17_481_120, 111),
    "loans_and_advances_etb": (248_997_369, 111), "profit_before_tax_etb": (15_849_406, 110),
    "profit_tax_etb": (4_313_629, 110), "profit_after_tax_etb": (11_535_777, 110),
}


def test_boa_2026_statement_values_are_unchanged(boa_2026):
    analysis, _ = boa_2026
    check_statement(analysis, BOA_2026_STATEMENTS)


def test_boa_2026_deposits_point_at_the_interest_free_line(boa_2026):
    analysis, _ = boa_2026
    deposits = analysis.metric("total_deposits_etb")
    assert suggested(analysis, "total_deposits_etb")[:2] == (Decimal(277_982_151), 111)
    assert deposits.status == "flagged"  # the bank's total (318.79 bn) adds the interest-free deposits line
    assert any(f.rule_id == "RELATED-LINE" and "40,803,442" in f.message for f in deposits.findings)
