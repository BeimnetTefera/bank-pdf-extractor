"""Extraction rules added for side-by-side tables, year headers, identities, statement-first
ranking and sentence cues (D-060 to D-064). Synthetic PDFs only."""

from dataclasses import replace
from decimal import Decimal

import pymupdf
import pytest

from src.analysis import analyse, column_year
from src.config_loader import LabelMapping, load_config
from src.metric_mapper import MetricMapper
from src.narrative_cues import CHANGE, NEUTRAL, TOTAL, approximate_word, sentence_cue
from src.number_parser import parse_number, with_percent_word
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, RawCandidate, extract_candidates, words_in_area
from tests.conftest import make_pdf

MAPPINGS = (
    LabelMapping("total_assets_etb", aliases=("Total assets",)),
    LabelMapping("total_liabilities_etb", aliases=("Total liabilities",)),
    LabelMapping("total_equity_etb", aliases=("Total equity",)),
    LabelMapping("total_deposits_etb", aliases=("Deposits from customers",), text_before=("deposits to Birr",)),
    LabelMapping("profit_before_tax_etb", aliases=("Profit before tax",)),
    LabelMapping("profit_tax_etb", aliases=("Income tax expense",)),
    LabelMapping("profit_after_tax_etb", aliases=("Profit after tax",)),
    LabelMapping("weighted_average_shares", aliases=("Weighted average number of ordinary shares",)),
    LabelMapping("branches", text_after=("branches",), text_before=("total number of branches",)),
    LabelMapping("employees", text_after=("employees",), text_before=("total number of employees",)),
    LabelMapping("pos_machines", text_before=("POS terminals",)),
    LabelMapping("internet_banking_users", text_before=("internet banking users",)),
    LabelMapping("mobile_banking_users", text_after=("mobile banking users",)),
    LabelMapping("roa_percent", text_before=("Return on Average Assets",)),
)
BALANCE = ["total_assets_etb", "total_liabilities_etb", "total_equity_etb"]
PROFIT = ["profit_before_tax_etb", "profit_tax_etb", "profit_after_tax_etb"]


@pytest.fixture(scope="module")
def config():
    return replace(load_config(), mappings=MAPPINGS)


def draw_table(page: pymupdf.Page, rows: list[tuple[str, list[str]]], label_x: float, value_xs: tuple[float, ...],
               top: float = 80) -> None:
    for i, (label, values) in enumerate(rows):
        y = top + i * 20
        if label:
            page.insert_text((label_x, y), label, fontsize=10)
        for x, value in zip(value_xs, values):
            page.insert_text((x, y), value, fontsize=10)


def pdf_of(draw) -> bytes:
    doc = pymupdf.open()
    draw(doc)
    data = doc.tobytes()
    doc.close()
    return data


PROFIT_OR_LOSS = [
    ("STATEMENT OF PROFIT OR LOSS", []), ("", ["2026", "2025"]), ("Birr'000", []),
    ("Profit before tax", ["5,230,027", "4,654,545"]),
    ("Income tax expense", ["(1,357,194)", "(1,418,721)"]),
    ("Profit after tax", ["3,872,832", "3,235,824"]),
]
FINANCIAL_POSITION = [
    ("STATEMENT OF FINANCIAL POSITION", []), ("", ["2026", "2025"]), ("Birr'000", []),
    ("Total assets", ["116,546,794", "88,366,839"]),
    ("Total liabilities", ["90,590,750", "69,932,721"]),
    ("Total equity", ["25,956,044", "18,434,118"]),
]


def spread() -> bytes:
    """A landscape page holding two report pages: profit or loss left, financial position right,
    with rows on the same lines (they would merge into one row without the split)."""
    def draw(doc):
        page = doc.new_page(width=842, height=595)
        draw_table(page, PROFIT_OR_LOSS, 30, (230, 320))
        draw_table(page, FINANCIAL_POSITION, 450, (650, 740))
    return pdf_of(draw)


# --- column blocks -------------------------------------------------------------------------

def test_spread_is_split_and_labels_stay_with_their_own_numbers():
    result = extract_candidates(spread())
    page = result.pages[0]
    assert len(page.column_blocks) == 2 and page.column_blocks[0][1] < 450 <= page.column_blocks[1][0]
    assert any("side by side (PDF pages 1)" in m for m in result.messages)
    by_value = {c.raw_value_text: c for c in result.candidates if c.extraction_method == TABLE_METHOD}
    assert by_value["116,546,794"].raw_label == "Total assets"
    assert by_value["5,230,027"].raw_label == "Profit before tax"
    assert by_value["116,546,794"].row_text == "Total assets 116,546,794 88,366,839"
    # The evidence box is the printed number itself.
    x0, _, x1, _ = by_value["116,546,794"].bbox
    assert 649 <= x0 < 652 and x1 < 740


def test_spread_gives_the_right_figures_and_years(config):
    analysis = analyse(extract_candidates(spread()), config, BALANCE + PROFIT, "2025/26")
    values = {m.metric.id: m.primary.mapped.candidate.raw_value_text for m in analysis.metrics}
    assert values == {"total_assets_etb": "116,546,794", "total_liabilities_etb": "90,590,750",
                      "total_equity_etb": "25,956,044", "profit_before_tax_etb": "5,230,027",
                      "profit_tax_etb": "(1,357,194)", "profit_after_tax_etb": "3,872,832"}
    assert all(m.primary.mapped.period == "current" for m in analysis.metrics)


def test_one_statement_with_a_wide_gap_before_its_numbers_is_not_split():
    result = extract_candidates(make_pdf([FINANCIAL_POSITION]))
    assert result.pages[0].column_blocks == ()
    rows = [c.row_index for c in result.candidates if c.raw_label == "Total equity"]
    assert rows == [5, 5]  # row numbers as before: one block, counted down the page


def test_words_in_a_box_across_the_gutter_are_read_per_side():
    rows = words_in_area(spread(), 0, (0, 130, 842, 150))
    assert ["Profit", "before", "tax", "5,230,027", "4,654,545"] in rows
    assert ["Total", "assets", "116,546,794", "88,366,839"] in rows


# --- year headers --------------------------------------------------------------------------

def test_value_takes_the_year_of_the_header_it_is_aligned_with():
    """Two header rows above the same numbers (an old statement left under a new one): the
    number belongs to the header it lines up with, not simply the nearest one above."""
    def draw(doc):
        page = doc.new_page()
        page.insert_text((300, 100), "2025", fontsize=10)  # aligned with the numbers below
        page.insert_text((275, 115), "2026", fontsize=10)  # nearer, but 25 points to the left
        page.insert_text((50, 140), "Total assets", fontsize=10)
        page.insert_text((300, 140), "1,234,567", fontsize=10)
    result = extract_candidates(pdf_of(draw))
    value = next(c for c in result.candidates if c.raw_value_text == "1,234,567")
    assert column_year(value, result.pages[0]) == 2025


def test_statement_titles_are_found_but_not_sentences_mentioning_them():
    def draw(doc):
        page = doc.new_page()
        page.insert_text((50, 60), "SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION", fontsize=10)
        page.insert_text((50, 90), "The amounts are presented in the statement of financial position at cost.",
                         fontsize=10)
    titles = extract_candidates(pdf_of(draw)).pages[0].statement_titles
    assert [t[0].upper() for t in titles] == ["SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION"]


# --- identities and statement-first ranking ---------------------------------------------------

def test_the_one_balancing_set_is_suggested_and_breakers_are_flagged(config):
    misprint = [r if r[0] != "Total assets" else ("Total assets", ["1,100,000", "900,000"]) for r in FINANCIAL_POSITION]
    misprint = [r if r[0] != "Total liabilities" else ("Total liabilities", ["600,000", "500,000"]) for r in misprint]
    misprint = [r if r[0] != "Total equity" else ("Total equity", ["400,000", "400,000"]) for r in misprint]
    balanced = [r if r[0] != "Total assets" else ("Total assets", ["1,000,000", "900,000"]) for r in misprint]
    analysis = analyse(extract_candidates(make_pdf([misprint, balanced])), config, BALANCE, "2025/26")
    assets = analysis.metric("total_assets_etb")
    assert (assets.primary.mapped.candidate.raw_value_text, assets.primary.mapped.candidate.page_index) == ("1,000,000", 1)
    page_one = next(mc for mc in assets.candidates if mc.mapped.candidate.raw_value_text == "1,100,000")
    (identity,) = [f for f in assets.findings_for(page_one.mapped.key) if f.rule_id == "IDENTITY"]
    assert identity.severity == "warning" and "1,000,000,000 = " in identity.message


def test_rounding_of_a_thousand_birr_still_balances(config):
    note = [("Income tax expense", ["", "1,284,232", "1,375,889"]), ("Profit after tax", ["", "3,869,598", "3,516,622"])]
    pdf = make_pdf([PROFIT_OR_LOSS[:2] + PROFIT_OR_LOSS[2:], [("", ["Notes", "2026", "2025"]), ("Birr'000", [])] + note])
    analysis = analyse(extract_candidates(pdf), config, PROFIT, "2025/26")
    assert analysis.metric("profit_after_tax_etb").primary.mapped.candidate.raw_value_text == "3,872,832"
    assert analysis.metric("profit_tax_etb").primary.mapped.candidate.raw_value_text == "(1,357,194)"
    tax = analysis.metric("profit_tax_etb")
    assert tax.status == "needs_review"  # the note's current-tax part is an info note, not a conflict
    assert {f.rule_id for f in tax.findings if f.severity == "info"} >= {"NOTE-DIFFERS"}


def test_nothing_balances_gives_an_info_note_only(config):
    broken = [r if r[0] != "Total equity" else ("Total equity", ["20,000,000", "18,434,118"]) for r in FINANCIAL_POSITION]
    analysis = analyse(extract_candidates(make_pdf([broken])), config, BALANCE, "2025/26")
    notes = [f for f in analysis.findings if f.rule_id == "IDENTITY"]
    assert notes and all(f.severity == "info" for f in notes)
    assert analysis.metric("total_assets_etb").status == "needs_review"


def test_statement_row_is_suggested_before_a_sentence_and_a_note(config):
    note = [("", ["Notes", "2026", "2025"]), ("Birr'000", []), ("Deposits from customers", ["", "50,299,409", "1"])]
    statement = FINANCIAL_POSITION + [("Deposits from customers", ["84,850,013", "64,208,501"])]

    def draw(doc):
        for rows in (note, statement):
            draw_table(doc.new_page(), rows, 50, (300, 400, 500) if rows is note else (300, 400))
        doc[0].insert_textbox(pymupdf.Rect(50, 400, 550, 500),
                              "Total IFB deposits to Birr 18.07 billion during the year.", fontsize=10)
    analysis = analyse(extract_candidates(pdf_of(draw)), config, ["total_deposits_etb"], "2025/26")
    deposits = analysis.metric("total_deposits_etb")
    assert deposits.primary.mapped.candidate.raw_value_text == "84,850,013"
    assert any(f.rule_id == "NOTE-DIFFERS" for f in deposits.findings)
    conflict = [f for f in deposits.findings if f.rule_id == "CONFLICT" and f.severity == "warning"]
    assert conflict and "18.07" in conflict[0].message and "50,299,409" not in conflict[0].message


def test_a_related_statement_line_is_pointed_out(config):
    statement = FINANCIAL_POSITION + [("Deposits from customers", ["277,982,151", "1"]),
                                      ("Interest free customers deposits", ["40,803,442", "1"])]
    analysis = analyse(extract_candidates(make_pdf([statement])), config, ["total_deposits_etb"], "2025/26")
    deposits = analysis.metric("total_deposits_etb")
    (related,) = [f for f in deposits.findings if f.rule_id == "RELATED-LINE"]
    assert "Interest free customers deposits 40,803,442" in related.message and deposits.status == "flagged"
    assert deposits.primary.mapped.candidate.raw_value_text == "277,982,151"


def test_a_share_count_in_a_birr_thousand_table_is_not_scaled(config):
    rows = [("", ["2026", "2025"]), ("Birr'000", []), ("Weighted average number of ordinary shares", ["9,907,298", "1"])]
    analysis = analyse(extract_candidates(make_pdf([rows])), config, ["weighted_average_shares"], "2025/26")
    shares = analysis.metric("weighted_average_shares")
    assert shares.primary.normalized.value == Decimal("9907298") and shares.primary.normalized.issue is None
    assert any(f.rule_id == "COUNT-SCALE" and f.severity == "warning" for f in shares.findings)  # some banks print thousands


# --- sentences -----------------------------------------------------------------------------

STORY = ("During the year the Bank opened 123 additional branches, bringing the total number of branches "
         "to 849. The Bank recruited 2,145 employees raising the total number of employees of the Bank to "
         "11,508 as at June 30. The network has 6,045 PoS terminals, and 80 virtual centres. Internet "
         "banking users reached nearly 64,000 and there were 2.76 million mobile banking users. Return on "
         "Average Assets at 6.6 percent.")


@pytest.fixture(scope="module")
def story(config):
    def draw(doc):
        doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 400), STORY, fontsize=10)
    metrics = ["branches", "employees", "pos_machines", "internet_banking_users", "mobile_banking_users", "roa_percent"]
    return analyse(extract_candidates(pdf_of(draw)), config, metrics, "2025/26")


def test_stated_total_is_suggested_before_a_change(story):
    branches, employees = story.metric("branches"), story.metric("employees")
    assert branches.primary.normalized.value == Decimal(849)
    assert employees.primary.normalized.value == Decimal(11508)
    change = next(mc for mc in employees.candidates if mc.normalized.value == Decimal(2145))
    assert any(f.rule_id == "SENTENCE-CHANGE" for f in employees.findings_for(change.mapped.key))
    assert not any(f.rule_id == "CONFLICT" for f in branches.findings)  # a change is not compared with the total


def test_phrase_before_a_list_break_does_not_take_the_next_number(story):
    assert [mc.normalized.value for mc in story.metric("pos_machines").candidates] == []


def test_estimates_scales_and_percent_words(story):
    internet = story.metric("internet_banking_users")
    assert internet.primary.normalized.value == Decimal(64000)
    assert any(f.rule_id == "APPROXIMATE" for f in internet.findings)
    assert story.metric("mobile_banking_users").primary.normalized.value == Decimal(2760000)
    assert story.metric("roa_percent").primary.normalized.value == Decimal("0.066")


def sentence(before: str, value: str, after: str) -> RawCandidate:
    return RawCandidate(0, 0, 0, 1, "", value, "", "", (0, 0, 1, 1), NARRATIVE_METHOD, before, after)


@pytest.mark.parametrize("before,after,cue", [
    ("bringing the total number of branches to", "", TOTAL),
    ("the permanent workforce reached", "employees as of June", TOTAL),
    ("the Bank opened", "additional branches", CHANGE),
    ("during the year,", "employees joined the Bank", CHANGE),
    ("total assets increased by", "percent", CHANGE),
    ("customers through", "ATMs nationwide", NEUTRAL),
    ("supported by", "ATMs, 651", NEUTRAL),
])
def test_sentence_cues(before, after, cue):
    assert sentence_cue(sentence(before, "123", after)) == cue


def test_approximate_words():
    assert approximate_word(sentence("users reached nearly", "64,000", "")) == "nearly"
    assert approximate_word(sentence("there were more than", "2.76", "million")) == "more than"
    assert approximate_word(sentence("there were", "2.76", "million")) is None


def test_percent_word_makes_a_percentage():
    assert with_percent_word(parse_number("6.6"), "percent and Return").is_percent
    assert with_percent_word(parse_number("6.6"), "per cent").is_percent
    assert not with_percent_word(parse_number("6.6"), "billion").is_percent


def test_scale_word_does_not_count_as_a_gap():
    mapper = MetricMapper((LabelMapping("mobile_banking_users", text_after=("users",)),))
    assert mapper.match(sentence("there were", "2.76", "million mobile users"))  # "mobile" is the one gap word
