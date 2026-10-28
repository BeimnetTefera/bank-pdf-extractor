"""Finding a word or a printed number in a report's pages (D-095). Synthetic PDF."""

from src.page_search import find_in_report
from tests.test_app_smoke import PDF


def test_a_word_is_found_on_its_page_with_a_box():
    hits = find_in_report(PDF, "total assets")
    assert [page for page, _ in hits] == [0, 1]  # the synthetic report prints it on both pages
    x0, y0, x1, y1 = hits[0][1]
    assert x1 > x0 and y1 > y0


def test_a_number_is_found_however_it_is_typed():
    assert find_in_report(PDF, "1234567") == find_in_report(PDF, "1,234,567") != []


def test_nothing_is_searched_for_one_letter_and_ocr_words_count():
    assert find_in_report(PDF, "a") == []
    ocr = {0: [[1.0, 2.0, 30.0, 9.0, "Shareholders", 0, 0, 0]]}
    assert find_in_report(PDF, "shareholders", ocr) == [(0, (1.0, 2.0, 30.0, 9.0))]
