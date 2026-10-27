from decimal import Decimal

from src.candidate_queries import (
    NO_YEAR,
    candidate_key,
    display_label,
    filter_candidates,
    find_by_key,
    page_of_rows,
    same_label_elsewhere,
    same_value_elsewhere,
)
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, RawCandidate


def make(page=0, row=0, col=0, label="Total assets", value="100", method=TABLE_METHOD, bbox=None, before="", after=""):
    return RawCandidate(
        page_index=page,
        row_index=row,
        column_index=col,
        row_value_count=1,
        raw_label=label,
        raw_value_text=value,
        row_text=f"{label} {value}",
        context_text="",
        bbox=bbox or (float(col), float(row), float(col) + 1, float(row) + 1),
        extraction_method=method,
        text_before=before,
        text_after=after,
    )


def test_candidate_key_is_unique_per_location_and_method():
    a, b = make(), make(method=NARRATIVE_METHOD, label="")
    assert candidate_key(a) != candidate_key(b)
    assert find_by_key([a, b], candidate_key(b)) is b
    assert find_by_key([a], None) is None


def test_filter_by_search_method_and_page():
    table = make(page=1, label="Total assets")
    sentence = make(page=2, label="", method=NARRATIVE_METHOD, value="981", after="branches nationwide")
    items = [table, sentence]
    assert filter_candidates(items, search="BRANCHES") == [sentence]
    assert filter_candidates(items, search="total assets") == [table]
    assert filter_candidates(items, search="981") == [sentence]
    assert filter_candidates(items, method=TABLE_METHOD) == [table]
    assert filter_candidates(items, page_index=2) == [sentence]
    assert filter_candidates(items) == items


def test_number_search_ignores_thousands_commas():
    deposits = make(value="189,512,266")
    other = make(row=1, value="(18,951,226)")
    assert filter_candidates([deposits, other], search="189512266") == [deposits]
    assert filter_candidates([deposits, other], search="189,512,266") == [deposits]
    assert filter_candidates([deposits, other], search="18951") == [deposits, other]
    assert filter_candidates([deposits, other], search="512 266") == [deposits]


def test_filter_by_page_range_year_and_value():
    a = make(page=0, value="1,000")
    b = make(page=4, value="(2,500)", row=1)
    c = make(page=9, value="-", row=2)
    years = {id(a): 2026, id(b): 2025}
    items = [a, b, c]
    assert filter_candidates(items, pages=(3, 9)) == [b, c]
    assert filter_candidates(items, year=2026, year_of=lambda x: years.get(id(x))) == [a]
    assert filter_candidates(items, year=NO_YEAR, year_of=lambda x: years.get(id(x))) == [c]
    assert filter_candidates(items, min_value=Decimal("0")) == [a]  # the dash has no number
    assert filter_candidates(items, max_value=Decimal("0")) == [b]  # printed in brackets: negative
    assert filter_candidates(items, min_value=Decimal("-3000"), max_value=Decimal("1000")) == [a, b]


def test_page_of_rows_reports_the_true_count_of_pages():
    rows = list(range(250))
    assert page_of_rows(rows, 1, 100) == (list(range(100)), 3)
    assert page_of_rows(rows, 3, 100) == (list(range(200, 250)), 3)
    assert page_of_rows(rows, 9, 100)[0] == list(range(200, 250))  # past the end: the last page
    assert page_of_rows([], 1, 100) == ([], 1)


def test_display_label_for_sentence_shows_nearby_words():
    c = make(label="", method=NARRATIVE_METHOD, value="981", before="network comprising", after="branches, 1,761 ATMs")
    assert display_label(c) == "…network comprising [981] branches, 1,761 ATMs…"
    assert display_label(make(label="")) == "(no label)"


def test_same_label_on_other_pages_shows_differing_values():
    statement = make(page=109, label="Profit before tax", value="10,101,095", col=1)
    note = make(page=219, label="Profit  before tax", value="10,101,094", col=1)
    other_column = make(page=219, label="Profit before tax", value="15,849,406", col=0)
    assert same_label_elsewhere([statement, note, other_column], statement) == [note]


def test_same_label_ignores_sentences():
    sentence = make(label="", method=NARRATIVE_METHOD)
    assert same_label_elsewhere([sentence], sentence) == []


def test_same_value_counts_one_printed_number_once():
    target = make(page=5, value="381,497,212", bbox=(1, 1, 2, 2))
    elsewhere = make(page=110, value="381,497,212", bbox=(5, 5, 6, 6))
    same_spot_other_method = make(page=110, value="381,497,212", bbox=(5, 5, 6, 6), method=NARRATIVE_METHOD)
    assert same_value_elsewhere([target, elsewhere, same_spot_other_method], target) == [elsewhere]
