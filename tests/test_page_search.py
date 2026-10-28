"""Finding a word or a printed number in a report's pages (D-095). Synthetic PDF."""

from src.page_search import figure_for, find_in_report, value_beside
from tests.test_app_smoke import PDF


def test_a_word_is_found_on_its_page_with_a_box():
    hits = find_in_report(PDF, "total assets")
    assert [page for page, _ in hits] == [0, 1]  # the synthetic report prints it on both pages
    x0, y0, x1, y1 = hits[0][1][0]
    assert x1 > x0 and y1 > y0


def test_a_number_is_found_however_it_is_typed():
    assert find_in_report(PDF, "1234567") == find_in_report(PDF, "1,234,567") != []


def test_nothing_is_searched_for_one_letter_and_ocr_words_count():
    assert find_in_report(PDF, "a") == []
    ocr = {0: [[1.0, 2.0, 30.0, 9.0, "Shareholders", 0, 0, 0]]}
    assert find_in_report(PDF, "shareholders", ocr) == [(0, [(1.0, 2.0, 30.0, 9.0)])]


def test_searching_a_label_boxes_the_figure_beside_it_not_the_words():
    from src.pdf_extractor import extract_candidates

    numbers = [(c.bbox, c.raw_value_text) for c in extract_candidates(PDF).candidates if c.page_index == 0]
    label = find_in_report(PDF, "total assets")[0][1][0]
    this_year = next(box for box, text in numbers if text == "1,234,567")
    assert value_beside(label, numbers) == this_year  # the report year's column, not the prior year's
    number = find_in_report(PDF, "1,234,567")[0][1][0]
    assert value_beside(number, numbers) == number  # a searched number stays boxed itself
    assert value_beside((0.0, 0.0, 5.0, 5.0), numbers) is None  # nothing on that line


def test_a_heading_before_the_table_row_does_not_take_the_box():
    heading, row = (10.0, 10.0, 80.0, 20.0), (10.0, 100.0, 80.0, 110.0)
    figure = (200.0, 100.0, 260.0, 110.0)
    numbers = [(figure, "1,234,567")]
    assert figure_for([heading, row], numbers) == figure  # the row's figure, though the heading came first
    assert figure_for([heading], numbers) == heading  # no figure anywhere: the words stay boxed
