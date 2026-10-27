import pytest

from src.config_loader import LabelMapping
from src.metric_mapper import MetricMapper, normalize_text, strip_note_reference
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, RawCandidate

MAPPINGS = (
    LabelMapping("total_assets_etb", aliases=("Total assets",)),
    LabelMapping("loans_and_advances_etb", aliases=("Loans , advances and financing to customers(Net)",)),
    LabelMapping("profit_before_tax_etb", aliases=("Profit before tax",)),
    LabelMapping("branches", text_after=("branches",)),
    LabelMapping("atms", text_after=("ATMs",)),
    LabelMapping("employees", text_after=("employees",)),
    LabelMapping("roe_percent", text_before=("Return on equity", "ROE")),
    LabelMapping("shareholders", text_before=("number of shareholders",)),
)


def table(label: str, value: str = "1") -> RawCandidate:
    return RawCandidate(0, 0, 0, 1, label, value, f"{label} {value}", "", (0, 0, 1, 1), TABLE_METHOD)


def sentence(value: str, before: str = "", after: str = "") -> RawCandidate:
    return RawCandidate(0, 0, 0, 1, "", value, "", "", (0, 0, 1, 1), NARRATIVE_METHOD, before, after)


def metric_ids(candidate: RawCandidate, mappings=MAPPINGS) -> list[str]:
    return [m.metric_id for m in MetricMapper(mappings).match(candidate)]


@pytest.mark.parametrize("label", ["Total assets", "TOTAL ASSETS", "Total  assets", "Total assets:"])
def test_exact_alias_ignores_case_spacing_and_colon(label):
    assert metric_ids(table(label)) == ["total_assets_etb"]


def test_ligature_and_odd_spacing_are_normalised():
    assert metric_ids(table("Proﬁt Before Tax")) == ["profit_before_tax_etb"]
    assert metric_ids(table("Loans, advances and financing to customers (Net)")) == []  # space before "(" differs
    assert metric_ids(table("Loans , advances and financing to customers(Net)")) == ["loans_and_advances_etb"]
    assert normalize_text("Loans , advances") == "loans, advances"


def test_partial_label_does_not_match():
    assert metric_ids(table("Total assets held for sale")) == []
    assert metric_ids(table("Average total assets")) == []


@pytest.mark.parametrize("label", ["Total assets 16", "Total assets 16 (c)", "Total assets 31(c)", "Total assets »",
                                   "Profit before tax 3 *%"])
def test_trailing_note_reference_is_ignored(label):
    expected = "profit_before_tax_etb" if label.startswith("Profit") else "total_assets_etb"
    assert metric_ids(table(label)) == [expected]


def test_only_the_end_of_a_label_is_stripped():
    assert metric_ids(table("Total assets 2026")) == []  # a year is not a note reference
    assert metric_ids(table("Total 16 assets")) == []  # words inside the label stay
    assert strip_note_reference("note 16") == "note"


def test_unknown_label_stays_unmapped():
    assert metric_ids(table("Cash and cash equivalents")) == []


def test_alias_listed_under_two_metrics_returns_both():
    mappings = MAPPINGS + (LabelMapping("total_equity_etb", aliases=("Total assets",)),)
    assert sorted(metric_ids(table("Total assets"), mappings)) == ["total_assets_etb", "total_equity_etb"]


def test_original_candidate_is_unchanged():
    c = table("Total  ASSETS", "381,497,212")
    MetricMapper(MAPPINGS).match(c)
    assert (c.raw_label, c.raw_value_text) == ("Total  ASSETS", "381,497,212")


# --- sentences ---------------------------------------------------------------------

def test_word_right_after_the_number():
    assert metric_ids(sentence("981", "network comprising", "branches, 1,761 ATMs, 6,045 Point")) == ["branches"]


def test_only_the_nearest_words_count_in_a_list():
    # "ATMs" is in the window after 981, but behind another number: not a match for 981.
    assert metric_ids(sentence("981", "comprising", "branches, 1,761 ATMs")) == ["branches"]
    assert metric_ids(sentence("1,761", "comprising 981 branches,", "ATMs, 6,045 Point")) == ["atms"]


def test_one_extra_word_allowed_after():
    assert metric_ids(sentence("10,627", "With", "permanent employees serving")) == ["employees"]
    assert metric_ids(sentence("10,627", "With", "very senior permanent employees")) == []


def test_phrase_before_with_filler_words():
    assert metric_ids(sentence("29%", "year. Return on equity has jumped to", "from 25.3% a")) == ["roe_percent"]
    assert metric_ids(sentence("5,011", "The number of shareholders has grown to", "with a total")) == ["shareholders"]
    assert metric_ids(sentence("29.0%", "Return on equity (ROE) reached", "its highest")) == ["roe_percent"]


def test_phrase_before_is_blocked_by_another_number():
    # 25.3% follows "29% from": the ROE phrase belongs to 29%, not to the prior-year figure.
    assert metric_ids(sentence("25.3%", "Return on equity has jumped to 29% from", "a year ago")) == []


def test_phrase_before_too_far_away():
    assert metric_ids(sentence("7", "Return on equity was discussed at length in the meeting on", "May")) == []


def test_table_aliases_do_not_apply_to_sentences():
    assert metric_ids(sentence("100", "Total assets", "")) == []
