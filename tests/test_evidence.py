"""Evidence for one metric of a saved report (spec section 1): status, source, history, links."""

from dataclasses import replace
from decimal import Decimal

import pytest

from src import review_service as review
from src.analysis import analyse
from src.config_loader import LabelMapping, load_config
from src.database import connect
from src.document_manager import store_document
from src.evidence import (
    EvidenceError,
    PdfWords,
    evidence_url,
    format_value,
    load_report_pdf,
    metric_evidence,
    note_calculation,
    pages_named_in,
    report_evidence,
    typed_calculation,
)
from src.pdf_extractor import EXTRACTOR_VERSION, extract_candidates
from src.pdf_render import render_crop_png
from src.project_service import save_analysis
from tests.conftest import make_pdf

STATEMENT = [
    ("SYNTHETIC BANK S.C. STATEMENT OF FINANCIAL POSITION", []),
    ("", ["Notes", "2026", "2025"]),
    ("Birr'000", []),
    ("Total assets", ["", "1,234,567", "987,654"]),
    ("Income tax expense", ["15", "(4,313,629)", "(2,802,590)"]),
    ("Total equity", ["", "500,000", "400,000"]),
]
MAPPINGS = (
    LabelMapping("total_assets_etb", aliases=("Total assets",)),
    LabelMapping("profit_tax_etb", aliases=("Income tax expense",)),
    LabelMapping("total_equity_etb", aliases=("Total equity",)),
)
METRICS = ["total_assets_etb", "profit_tax_etb", "total_equity_etb", "employees"]
REVIEWER = "Analyst A"


@pytest.fixture
def report(settings):
    pdf = make_pdf([STATEMENT])
    config = replace(load_config(), mappings=MAPPINGS, settings=settings)
    document = store_document(pdf, "synthetic.pdf", "Synthetic Bank", "2025/26", settings)
    analysis = analyse(extract_candidates(pdf), config, METRICS, "2025/26")
    conn = connect(settings.database_path)
    ids = save_analysis(conn, document, analysis, EXTRACTOR_VERSION)

    def candidate(metric_id: str, printed: str) -> int:
        mc = next(m for m in analysis.metric(metric_id).candidates if m.mapped.candidate.raw_value_text == printed)
        return ids[(metric_id, mc.mapped.key)]

    yield conn, config, document, candidate, pdf
    conn.close()


def test_unreviewed_figure_is_shown_as_the_suggestion(report):
    conn, config, document, _, _ = report
    ev = metric_evidence(conn, config, document.id, "total_assets_etb")
    assert (ev.status, ev.status_label, ev.source_type) == ("needs_review", "Not reviewed yet", "pdf_figure")
    assert (ev.value, ev.page, ev.printed, ev.label) == (Decimal("1234567000"), 1, "1,234,567", "Total assets")
    assert ev.conversion.startswith("1,234,567 (") and ev.conversion.endswith("= 1,234,567,000 ETB")
    assert [(o.printed, o.why) for o in ev.other_candidates] == [("987,654", "Prior-year column (2025)")]


def test_approved_extracted_figure_with_who_when_and_sign(report):
    conn, config, document, candidate, _ = report
    review.approve(conn, candidate("profit_tax_etb", "(4,313,629)"), REVIEWER, sign="positive", note="Statement p1")
    ev = metric_evidence(conn, config, document.id, "profit_tax_etb")
    assert (ev.status, ev.source_type, ev.value) == ("approved", "pdf_figure", Decimal("4313629000"))
    assert (ev.approved_by, ev.approval_note) == (REVIEWER, "Statement p1")
    assert "recorded as a positive amount" in ev.sign_text
    assert ev.box is not None and ev.page == 1
    assert ev.history[-1].action == "Approved the extracted figure" and ev.history[-1].new == "4,313,629,000 ETB"


def test_a_typed_value_in_brackets_recorded_as_positive_says_so(report):
    conn, config, document, candidate, _ = report
    tax = candidate("profit_tax_etb", "(4,313,629)")
    review.correct(conn, tax, REVIEWER, Decimal("4313630000"), "ETB", "last digit", sign="positive")
    pending = metric_evidence(conn, config, document.id, "profit_tax_etb")
    assert pending.sign_text == "Typed in brackets (negative); recorded as a positive amount, as the reviewer chose."
    review.approve_correction(conn, document.id, "profit_tax_etb", REVIEWER)
    approved = metric_evidence(conn, config, document.id, "profit_tax_etb")
    assert (approved.source_type, approved.sign_text) == ("typed", pending.sign_text)
    review.correct(conn, candidate("total_assets_etb", "1,234,567"), REVIEWER, Decimal("1234568000"), "ETB", "typo")
    assert metric_evidence(conn, config, document.id, "total_assets_etb").sign_text == ""
    review.correct(conn, candidate("total_equity_etb", "500,000"), REVIEWER, Decimal("-500000000"), "ETB",
                   "a deficit", sign="as_printed")
    assert metric_evidence(conn, config, document.id, "total_equity_etb").sign_text == (
        "Typed in brackets (negative); kept negative, as the reviewer chose.")


def test_correction_with_a_marked_box_then_approval(report):
    conn, config, document, candidate, _ = report
    box = (300.0, 100.0, 360.0, 112.0)
    review.correct(conn, candidate("total_assets_etb", "1,234,567"), REVIEWER, "1234568000", "ETB",
                   "Last digit misread; the note on page 1 says 1,234,568", source_page=1, source_bbox=box)
    pending = metric_evidence(conn, config, document.id, "total_assets_etb")
    assert (pending.status, pending.status_label, pending.source_type) == ("pending", "Correction pending", "pdf_marked_box")
    assert pending.entry_reason == "Last digit misread; the note on page 1 says 1,234,568"  # in full, not cut

    review.approve_correction(conn, document.id, "total_assets_etb", REVIEWER, note="Checked")
    ev = metric_evidence(conn, config, document.id, "total_assets_etb")
    assert (ev.status, ev.value, ev.box, ev.page) == ("approved", Decimal("1234568000"), box, 1)
    assert (ev.entered_by, ev.approved_by, ev.approval_note) == (REVIEWER, REVIEWER, "Checked")
    steps = [(h.action, h.old, h.new) for h in ev.history[1:]]
    assert steps == [
        ("Corrected (PDF page 1, box marked)", "", "1,234,568,000 ETB (awaiting approval)"),
        ("Approved the correction (PDF page 1, box marked)", "1,234,568,000 ETB (awaiting approval)", "1,234,568,000 ETB"),
    ]
    # The extracted figure it replaced is listed with the alternatives.
    assert any(o.printed == "1,234,567" for o in ev.other_candidates)


def test_rejected_figure_gives_its_reason(report):
    conn, config, document, candidate, _ = report
    review.reject(conn, candidate("total_equity_etb", "500,000"), REVIEWER, "This is the parent-only equity")
    ev = metric_evidence(conn, config, document.id, "total_equity_etb")
    assert ev.status == "not_found"  # the only this-year figure was rejected
    assert ("500,000", "Rejected: This is the parent-only equity") in [(o.printed, o.why) for o in ev.other_candidates]
    assert ev.history[-1].action == "Rejected the figure 500,000 on PDF page 1"


def test_manual_entry_without_a_box_is_typed(report):
    conn, config, document, _, _ = report
    review.manual_entry(conn, document.id, "employees", REVIEWER, "10627", "count", 1, "From the HR section")
    review.approve_correction(conn, document.id, "employees", REVIEWER)
    ev = metric_evidence(conn, config, document.id, "employees")
    assert (ev.status, ev.source_type, ev.page, ev.box, ev.value_text) == ("approved", "typed", 1, None, "10,627")


def test_not_found_metric(report):
    conn, config, document, _, _ = report
    ev = metric_evidence(conn, config, document.id, "employees")
    assert (ev.status, ev.source_type, ev.value) == ("not_found", "none", None)
    assert any(c.rule_id == "NOT-FOUND" for c in ev.checks)


def test_report_evidence_lists_every_analysed_metric(report):
    conn, config, document, _, _ = report
    assert [e.metric_id for e in report_evidence(conn, config, document.id)] == METRICS


def test_missing_report_is_explained(report):
    conn, config, _, _, _ = report
    with pytest.raises(EvidenceError, match="not in the database"):
        metric_evidence(conn, config, "nope", "total_assets_etb")


def test_evidence_links():
    assert evidence_url("http://localhost:8501/", "abc", "total_assets_etb") == \
        "http://localhost:8501/evidence?report=abc&metric=total_assets_etb"
    assert evidence_url("http://localhost:8501", "abc") == "http://localhost:8501/evidence_summary?report=abc"


def test_values_read_as_expected():
    assert format_value(Decimal("0.29"), "fraction") == "0.29 (29%)"
    assert format_value(Decimal("1234567000"), "ETB") == "1,234,567,000 ETB"
    assert format_value(Decimal("10627"), "count") == "10,627"


def test_stored_pdf_is_checked_and_a_crop_rendered(report):
    conn, config, document, _, pdf = report
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (document.id,)).fetchone()
    assert load_report_pdf(config.settings, row) == pdf
    png = render_crop_png(pdf, 0, (300.0, 150.0, 360.0, 162.0))
    assert png.startswith(b"\x89PNG")
    (config.settings.data_dir / row["stored_path"]).write_bytes(b"%PDF-1.4 changed")
    with pytest.raises(EvidenceError, match="fingerprint"):
        load_report_pdf(config.settings, row)


# --- a typed value gets the box of its printed number -----------------------------------------

def test_typed_value_gets_the_box_of_the_printed_number(report):
    conn, config, document, candidate, pdf = report
    review.correct(conn, candidate("total_assets_etb", "1,234,567"), REVIEWER, "1234567000", "ETB",
                   "Converted Birr'000 to full ETB")  # typed: no box saved
    review.approve_correction(conn, document.id, "total_assets_etb", REVIEWER)
    assert metric_evidence(conn, config, document.id, "total_assets_etb").box is None  # without the PDF
    words = PdfWords(pdf)
    ev = metric_evidence(conn, config, document.id, "total_assets_etb", words)
    assert (ev.source_type, ev.box_found, ev.page, ev.printed) == ("typed", True, 1, "1,234,567")
    assert ev.box is not None and ev.typed_page is None
    words.close()


def test_page_named_in_the_note_is_searched(settings):
    pdf = make_pdf([[("Highlights", [])], STATEMENT])  # the statement is on page 2
    words = PdfWords(pdf)
    assert words.locate(Decimal("1234567000"), "ETB", [1]) is None
    page, box, printed = words.locate(Decimal("1234567000"), "ETB", [1, *pages_named_in("see p2, 2025 column")])
    assert (page, printed) == (2, "1,234,567")
    assert words.locate(Decimal("4313629000"), "ETB", [2])[2] == "(4,313,629)"  # brackets: size compared
    assert words.locate(Decimal("390.58"), "ETB", [1, 2]) is None  # a calculated value is not printed
    words.close()


def test_pages_named_in_notes():
    assert pages_named_in("p46 Deposits; same on p. 92 and page 113") == [46, 92, 113]
    assert pages_named_in("p45 and p115 both 5,230,027; pages 45, 46 and 47") == [45, 115, 45, 46, 47]
    assert pages_named_in("no page here, 2023 column") == []


# --- arithmetic written in the note of a typed value ---------------------------------------

def test_arithmetic_in_a_note_is_checked_and_its_figures_found(settings):
    pdf = make_pdf([[("Highlights", [])], [("Note 28 Earnings per share", []), ("Birr'000", []),
                                           ("Profit attributable", ["3,869,598"]),
                                           ("Weighted average shares", ["9,907,298"])]])
    words = PdfWords(pdf)
    note = ("Report prints EPS as 39% of par. p2: profit 3,869,598 Birr'000 / 9,907,298 weighted shares = "
            "Birr 390.58 per share (= 39% x 1000)")
    calc = note_calculation(note, words, [*pages_named_in(note), 1])
    assert calc.written == "profit 3,869,598 Birr'000 / 9,907,298 weighted shares = 390.58"
    assert calc.arithmetic.startswith("3,869,598,000 ÷ 9,907,298 = 390.5805") and calc.matches
    profit, shares = calc.figures
    assert (profit.label, profit.value, profit.page) == ("profit", Decimal("3869598000"), 2)
    assert (shares.label, shares.value, shares.page) == ("weighted shares", Decimal("9907298"), 2)
    assert profit.box is not None and shares.box is not None
    wrong = note_calculation("profit 3,869,598 Birr'000 / 9,907,298 shares = Birr 395.00", words, [2])
    assert wrong is not None and not wrong.matches
    assert note_calculation("Converted Birr'000 to full ETB; verified p46", words, [2]) is None
    words.close()


@pytest.mark.parametrize("note, arithmetic, labels", [
    ("Calculated (report does not print ROE): PAT 3,872,832 (p45) / average equity (19,475,296 + 14,216,185)/2 "
     "(p46) = 0.2299. Year-end basis would be 0.1989",
     "3,872,832 ÷ ((19,475,296 + 14,216,185) ÷ 2) = 0.2299", ["PAT", "average equity", ""]),
    ("Derived from p45: Net operating income 15,627,301 minus Profit before tax 5,230,027 = 10,397,274 Birr'000",
     "15,627,301 − 5,230,027 = 10,397,274", ["Net operating income", "Profit before tax"]),
    ("p18: 2.76 million mobile banking users + nearly 64,000 internet banking users = 2,824,000 (approximate; "
     "customers using both may be counted twice)",
     "2,760,000 + 64,000 = 2,824,000", ["mobile banking users", "internet banking users"]),
    ("p88 maturity table 30 June 2023 (Birr'000): assets maturing within 1 year = 57,061,160 + 16,180,700 + "
     "15,080,710 + 18,559,910 = 106,882,480. Table covers cash only",
     "57,061,160 + 16,180,700 + 15,080,710 + 18,559,910 = 106,882,480", ["", "", "", ""]),
])
def test_reviewers_ways_of_writing_a_calculation(note, arithmetic, labels):
    calc = note_calculation(note)
    assert calc.arithmetic == arithmetic and calc.matches
    assert [f.label for f in calc.figures] == labels


def test_page_reference_in_the_note_sends_each_figure_to_its_page(settings):
    pdf = make_pdf([[("Income statement", []), ("Profit after tax", ["3,872,832"])],
                    [("Financial position", []), ("Total equity", ["19,475,296", "14,216,185"])]])
    words = PdfWords(pdf)
    calc = note_calculation("PAT 3,872,832 (p1) / average equity (19,475,296 + 14,216,185)/2 (p2) = 0.2299", words, [])
    assert [f.page for f in calc.figures] == [1, 2, 2] and all(f.box for f in calc.figures)
    words.close()


def test_derived_value_marked_on_one_input_shows_the_note_calculation(report):
    conn, config, document, candidate, pdf = report
    box = next(c.bbox for c in extract_candidates(pdf).candidates if c.raw_value_text == "1,234,567")
    review.manual_entry(conn, document.id, "employees", REVIEWER, "1734567", "count", 1,
                        "Worked out: 1,234,567 + 500,000 = 1,734,567", source_bbox=box)
    review.approve_correction(conn, document.id, "employees", REVIEWER)
    words = PdfWords(pdf)
    ev = metric_evidence(conn, config, document.id, "employees", words)
    assert ev.box_is_input and ev.note_calc.matches
    assert [f.page for f in ev.note_calc.figures] == [1, 1]
    words.close()


def test_nil_marked_value_is_not_treated_as_worked_out(report):
    conn, config, document, _, pdf = report
    review.manual_entry(conn, document.id, "employees", REVIEWER, "0", "count", 1, "nil", source_bbox=(1, 1, 5, 5))
    review.approve_correction(conn, document.id, "employees", REVIEWER)
    words = PdfWords(pdf)
    assert not metric_evidence(conn, config, document.id, "employees", words).box_is_input
    words.close()


def test_typed_calculation_only_for_a_value_not_printed_where_it_was_recorded(settings):
    pdf = make_pdf([[("Income statement", []), ("Profit after tax", ["3,872,832"])],
                    [("Financial position", []), ("Total equity", ["19,475,296", "14,216,185"])]])
    words = PdfWords(pdf)
    note = "PAT 3,872,832 (p1) / average equity (19,475,296 + 14,216,185)/2 (p2) = 0.2299"
    calc = typed_calculation(words, Decimal("0.2299"), "fraction", 1, True, note)  # box marked on the profit
    assert calc.matches and [f.page for f in calc.figures] == [1, 2, 2]
    # the typed value is the boxed number itself: printed, so not a calculation
    assert typed_calculation(words, Decimal("3872832000"), "ETB", 1, True, "p1 = 3,872,832 Birr'000") is None
    assert typed_calculation(words, Decimal("0.2299"), "fraction", 1, True, "read from the chart") is None
    assert typed_calculation(words, Decimal(0), "ETB", 1, True, note) is None
    words.close()
