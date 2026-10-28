"""D-086: the same PDF saved twice, and merging the copies. Synthetic PDFs and temporary databases only."""

from contextlib import closing
from dataclasses import replace

import pytest
from streamlit.testing.v1 import AppTest

from src import report_merge
from src import review_service as review
from src.analysis import analyse
from src.calculated_entries import record_input_figure
from src.config_loader import load_config
from src.database import connect
from src.document_manager import store_document
from src.pdf_extractor import EXTRACTOR_VERSION, extract_candidates
from src.project_service import get_document, list_documents, load_candidates, save_analysis
from src.review_service import ReviewError
from tests.conftest import make_pdf
from tests.test_app_review import bank_reports_page
from tests.test_persistence_and_review import MAPPINGS, METRICS, NOTES, STATEMENT

REVIEWER, MERGER = "Analyst A", "Analyst B"


@pytest.fixture
def copies(settings):
    """The same synthetic PDF saved twice: (db path, first id, second id)."""
    pdf = make_pdf([STATEMENT, NOTES])
    config = replace(load_config(), mappings=MAPPINGS)
    analysis = analyse(extract_candidates(pdf), config, METRICS, "2025/26")
    ids = []
    with closing(connect(settings.database_path)) as conn:
        for _ in range(2):
            document = store_document(pdf, "synthetic.pdf", "Bank of Abyssinia", "2024/25", settings)
            save_analysis(conn, document, analysis, EXTRACTOR_VERSION)
            ids.append(document.id)
    return settings.database_path, ids[0], ids[1]


@pytest.fixture
def conn(copies):
    with closing(connect(copies[0])) as connection:
        yield connection


def first_candidate(conn, document_id: str, metric_id: str) -> int:
    return min(load_candidates(conn, document_id, metric_id), key=lambda c: c.rank).id


def decide(conn, document_id: str) -> None:
    """Six decisions of every kind: approval, correction + its approval, manual entry, reopen, rejection."""
    review.approve(conn, first_candidate(conn, document_id, "total_assets_etb"), REVIEWER,
                   acknowledge_warnings=True, note="checked p1")
    review.correct(conn, first_candidate(conn, document_id, "profit_tax_etb"), REVIEWER, "4313629", "ETB",
                   "printed in brackets", sign="positive")
    review.approve_correction(conn, document_id, "profit_tax_etb", REVIEWER, acknowledge_warnings=True)
    review.manual_entry(conn, document_id, "employees", REVIEWER, "120", "count", 1, "typed from p1",
                        source_bbox=(10.0, 20.0, 30.0, 40.0))
    review.reopen(conn, document_id, "employees", REVIEWER, "wrong page")
    review.reject(conn, first_candidate(conn, document_id, "total_equity_etb"), REVIEWER, "a note, not the statement")


def semantics(conn, document_id: str) -> list[tuple]:
    """Every decision as it means something: the figure by position key, value, sign, box, approvals."""
    rows = conn.execute("SELECT * FROM review_decisions WHERE document_id = ? ORDER BY id", (document_id,)).fetchall()
    position = {r["id"]: (r["metric_id"], r["extraction_method"], r["page_index"], r["row_index"], r["column_index"])
                for r in conn.execute("SELECT * FROM candidates WHERE document_id = ?", (document_id,))}
    order = {r["id"]: i for i, r in enumerate(rows)}
    return [(r["metric_id"], r["decision"], position.get(r["candidate_id"]), r["reviewed_value"], r["reviewed_unit"],
             r["sign_choice"], r["source_page"], r["source_bbox"], r["warnings_acknowledged"], r["entry_kind"],
             None if r["approves_decision_id"] is None else order[r["approves_decision_id"]]) for r in rows]


def test_duplicate_group_defaults_to_the_copy_with_most_decisions(conn, copies):
    _, first, second = copies
    decide(conn, second)  # as in the real data: one copy with no decisions, one with several
    (group,) = report_merge.duplicate_groups(conn)
    assert {d["id"] for d in group.documents} == {first, second}
    assert group.decision_counts == {first: 0, second: 6}
    assert group.default_keep == second


def test_merge_keeping_the_decided_copy_copies_nothing_and_deletes_the_other(conn, copies, settings):
    _, first, second = copies
    decide(conn, second)
    before = semantics(conn, second)
    plan = report_merge.plan_merge(conn, second, first)
    assert plan.allowed and plan.decisions_to_copy == 0 and not plan.conflicts
    result = report_merge.merge_reports(conn, second, first, MERGER, settings.data_dir)
    assert result.decisions_copied == 0
    assert [d["id"] for d in list_documents(conn)] == [second]
    assert semantics(conn, second) == before
    (log,) = review.deletion_log(conn)
    assert (log["document_id"], log["reason"], log["reviewer"]) == (first, f"Merged into report {second}", MERGER)
    assert (settings.data_dir / get_document(conn, second)["stored_path"]).is_file()  # the kept copy uses the PDF
    assert report_merge.duplicate_groups(conn) == []


def test_merge_copies_decisions_with_their_meaning_and_order(conn, copies, settings):
    _, first, second = copies
    decide(conn, second)
    expected = semantics(conn, second)
    plan = report_merge.plan_merge(conn, first, second)  # the reviewer keeps the copy with no decisions
    assert plan.allowed and plan.decisions_to_copy == 6
    assert sorted(plan.copy_metrics) == ["employees", "profit_tax_etb", "total_assets_etb", "total_equity_etb"]
    report_merge.merge_reports(conn, first, second, MERGER, settings.data_dir)
    assert [d["id"] for d in list_documents(conn)] == [first]
    assert sorted(semantics(conn, first)) == sorted(expected)
    rows = conn.execute("SELECT * FROM review_decisions WHERE document_id = ? ORDER BY id", (first,)).fetchall()
    assert {r["reviewer"] for r in rows} == {MERGER}
    assert rows[0]["reason"] == f"Merged from report {second}: checked p1"
    manual = next(r for r in rows if r["decision"] == "manual_entry")
    assert manual["candidate_id"] is None and manual["source_bbox"] == "[10.0, 20.0, 30.0, 40.0]"
    approval = next(r for r in rows if r["metric_id"] == "profit_tax_etb" and r["decision"] == "approved")
    corrected = next(r for r in rows if r["decision"] == "corrected")
    assert approval["approves_decision_id"] == corrected["id"] and corrected["id"] < approval["id"]
    assert review.review_state(conn, first, "profit_tax_etb").status == "approved"
    assert review.review_state(conn, first, "employees").status == "open"
    assert review.review_state(conn, first, "total_equity_etb").rejected_candidate_ids == {
        first_candidate(conn, first, "total_equity_etb")}


def test_metric_decided_differently_is_listed_and_not_copied(conn, copies, settings):
    _, first, second = copies
    review.approve(conn, first_candidate(conn, first, "total_assets_etb"), REVIEWER, acknowledge_warnings=True)
    review.approve(conn, first_candidate(conn, second, "total_assets_etb"), REVIEWER, acknowledge_warnings=True,
                   note="same value")
    review.approve(conn, first_candidate(conn, first, "total_equity_etb"), REVIEWER, acknowledge_warnings=True)
    review.reject(conn, first_candidate(conn, second, "total_equity_etb"), REVIEWER, "wrong figure")
    plan = report_merge.plan_merge(conn, first, second)
    assert plan.same_metrics == ["total_assets_etb"] and plan.conflicts == ["total_equity_etb"]
    assert plan.decisions_to_copy == 0 and plan.allowed
    report_merge.merge_reports(conn, first, second, MERGER, settings.data_dir)
    assert review.review_state(conn, first, "total_equity_etb").status == "approved"  # kept copy's own decision


def test_merge_is_refused_when_the_other_copy_has_work_it_cannot_copy(conn, copies, settings):
    _, first, second = copies
    record_input_figure(conn, second, "par_value", "Par value", "1000", "ETB", 1, (1.0, 2.0, 3.0, 4.0), "1,000",
                        "printed on p1", REVIEWER)
    plan = report_merge.plan_merge(conn, first, second)
    assert not plan.allowed and any("1 input figure(s)" in r for r in plan.refusals)
    with pytest.raises(ReviewError, match="input figure"):
        report_merge.merge_reports(conn, first, second, MERGER, settings.data_dir)
    assert len(list_documents(conn)) == 2 and review.deletion_log(conn) == []
    assert report_merge.plan_merge(conn, second, first).allowed  # the other way round is fine


def test_merge_is_refused_into_a_finished_copy_and_for_different_pdfs(conn, copies, settings):
    _, first, second = copies
    decide(conn, second)
    review.finish_extraction(conn, first, REVIEWER)
    assert any("finished" in r for r in report_merge.plan_merge(conn, first, second).refusals)
    assert report_merge.plan_merge(conn, first, first).refusals
    other = store_document(make_pdf([STATEMENT]), "other.pdf", "Bank of Abyssinia", "2024/25", settings)
    analysis = analyse(extract_candidates(make_pdf([STATEMENT])), replace(load_config(), mappings=MAPPINGS),
                       METRICS, "2025/26")
    save_analysis(conn, other, analysis, EXTRACTOR_VERSION)
    assert any("same PDF" in r for r in report_merge.plan_merge(conn, first, other.id).refusals)


def test_merge_is_all_or_nothing(conn, copies, settings, monkeypatch):
    _, first, second = copies
    decide(conn, second)

    def failing_delete(*args, **kwargs):
        raise ReviewError("disk full")

    monkeypatch.setattr(report_merge, "delete_report_rows", failing_delete)
    with pytest.raises(ReviewError, match="disk full"):
        report_merge.merge_reports(conn, first, second, MERGER, settings.data_dir)
    assert len(list_documents(conn)) == 2
    assert semantics(conn, first) == []  # no decision was copied


def test_bank_reports_shows_the_duplicate_banner_and_merges_on_press(copies, settings):
    db_path, first, second = copies
    with closing(connect(db_path)) as conn:
        decide(conn, second)
    page = AppTest.from_function(bank_reports_page, args=(str(settings.data_dir),), default_timeout=60)
    page.session_state["reviewer"] = MERGER
    page.run()
    assert not page.exception
    assert any("The same PDF is saved twice" in m.value for m in page.markdown)
    next(b for b in page.button if b.label == "Review duplicates").click().run()  # shows the merge
    assert next(r for r in page.radio if r.label == "Copy to keep").value == second
    with closing(connect(db_path)) as conn:
        assert len(list_documents(conn)) == 2  # shown, nothing changed
    next(r for r in page.radio if r.label == "Copy to keep").set_value(first).run()
    assert any("Will copy 6 decision(s)" in m.value for m in page.markdown)
    next(b for b in page.button if b.label == "Merge").click().run()
    assert not page.exception
    with closing(connect(db_path)) as conn:
        assert [d["id"] for d in list_documents(conn)] == [first]
        assert len(semantics(conn, first)) == 6
    assert not any("The same PDF is saved" in m.value for m in page.markdown)
