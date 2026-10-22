import pymupdf
import pytest

from src.pdf_extractor import EXTRACTION_METHOD, extract_candidates, is_value_token
from tests.conftest import make_pdf


def values_by_label(result, label):
    return [c.raw_value_text for c in result.candidates if c.raw_label == label]


def test_table_row_becomes_one_candidate_per_value():
    result = extract_candidates(make_pdf([[("Total assets", ["381,497,212", "286,226,976"])]]))
    first, second = [c for c in result.candidates if c.raw_label == "Total assets"]
    assert (first.raw_value_text, first.column_index) == ("381,497,212", 0)
    assert (second.raw_value_text, second.column_index) == ("286,226,976", 1)
    assert first.row_value_count == 2
    assert first.row_text == "Total assets 381,497,212 286,226,976"
    assert first.extraction_method == EXTRACTION_METHOD


def test_raw_value_text_is_kept_exactly_as_printed():
    result = extract_candidates(make_pdf([[("Impairment charge", ["(12,801)", "-", "4.75%"])]]))
    assert values_by_label(result, "Impairment charge") == ["(12,801)", "-", "4.75%"]


def test_page_index_is_zero_based_and_preserved():
    pdf = make_pdf([[("Cover", [])], [("Other", ["1"])], [("Total assets", ["100"])]])
    (candidate,) = [c for c in extract_candidates(pdf).candidates if c.raw_label == "Total assets"]
    assert candidate.page_index == 2


def test_repeated_label_on_different_pages_keeps_every_occurrence():
    pdf = make_pdf([[("Profit before tax", ["10,101,095"])], [("Profit before tax", ["10,101,094"])]])
    found = [(c.page_index, c.raw_value_text) for c in extract_candidates(pdf).candidates if c.raw_label == "Profit before tax"]
    assert found == [(0, "10,101,095"), (1, "10,101,094")]


def test_digits_inside_a_label_stay_in_the_label():
    result = extract_candidates(make_pdf([[("Due within 1 year", ["500", "400"])]]))
    assert values_by_label(result, "Due within 1 year") == ["500", "400"]


def test_numbers_only_row_is_kept_with_empty_label():
    result = extract_candidates(make_pdf([[("Statement of financial position", []), ("", ["2026", "2025"])]]))
    assert values_by_label(result, "") == ["2026", "2025"]


def test_text_only_rows_produce_no_candidates_but_appear_in_context():
    pdf = make_pdf([[("ASSETS", []), ("Birr'000", []), ("Cash and cash equivalents", ["76,416,523"])]])
    (candidate,) = extract_candidates(pdf).candidates
    assert candidate.context_text == "ASSETS\nBirr'000\nCash and cash equivalents 76,416,523"


def test_values_slightly_off_the_label_baseline_join_the_same_row():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 100), "Total equity", fontsize=10)
    page.insert_text((300, 101.5), "39,512,182", fontsize=10)
    page.insert_text((400, 99), "28,841,931", fontsize=10)
    result = extract_candidates(doc.tobytes())
    assert values_by_label(result, "Total equity") == ["39,512,182", "28,841,931"]


def test_printed_page_number_is_never_guessed():
    result = extract_candidates(make_pdf([[("Total assets", ["1"])]]))
    assert all(c.printed_page_number is None for c in result.candidates)


def test_page_without_text_is_reported():
    pdf = make_pdf([[("Total assets of the bank", ["1,000"])], None, [("Total equity of the bank", ["500"])]])
    result = extract_candidates(pdf)
    assert result.page_count == 3
    assert result.text_status == "partial_text"
    assert result.pages_without_text == [1]
    assert "PDF pages 2" in result.messages[0]


def test_low_text_page_is_flagged_but_its_values_are_kept():
    result = extract_candidates(make_pdf([[("Total assets", ["100"])]]))
    assert result.pages_without_text == [0]  # under MIN_TEXT_CHARS: may be scanned
    assert values_by_label(result, "Total assets") == ["100"]


def test_pdf_with_no_text_reports_ocr_not_implemented():
    result = extract_candidates(make_pdf([None, None]))
    assert result.text_status == "no_text"
    assert result.candidates == []
    assert "OCR is not implemented" in result.messages[0]


def test_results_are_described_as_unverified(simple_pdf):
    result = extract_candidates(simple_pdf)
    assert result.text_status == "ok"
    assert "unverified" in result.messages[-1]


@pytest.mark.parametrize(
    "token,expected",
    [
        ("381,497,212", True),
        ("(18,576)", True),
        ("286.23", True),
        ("77%", True),
        ("-", True),
        ("–", True),
        ("2026", True),
        ("15d", False),
        ("Birr'000", False),
        ("2025/26", False),
        ("assets", False),
        ("", False),
    ],
)
def test_is_value_token(token, expected):
    assert is_value_token(token) is expected
