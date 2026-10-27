"""Not reported (D-053): the report does not disclose a metric. Not Not found, not zero."""

from contextlib import closing

import pytest
from streamlit.testing.v1 import AppTest  # noqa: F401 (used through the app fixture)

from src import review_service as review
from src.cross_checks import current_figures
from src.database import connect
from src.evidence import metric_evidence
from src.excel_exporter import APPROVED_SHEET, METRIC_EVIDENCE_SHEET, build_workbook
from src.review_service import ReviewError
from tests.test_app_calculate import REVIEWER, app, open_metric  # noqa: F401 (app is a fixture)
from tests.test_evidence import report  # noqa: F401 (report is a fixture)

REASON = "The report does not disclose the number of shareholders; searched notes 27-28."


def test_not_reported_needs_a_reason_and_approval(report):
    conn, config, document, _, _ = report
    with pytest.raises(ReviewError, match="Say why"):
        review.not_reported(conn, document.id, "employees", "Analyst A", "n/a")
    review.not_reported(conn, document.id, "employees", "Analyst A", REASON)
    state = review.review_state(conn, document.id, "employees")
    assert state.status == "corrected" and state.pending.entry_kind == "not_reported"
    review.approve_correction(conn, document.id, "employees", "Analyst A")
    state = review.review_state(conn, document.id, "employees")
    assert (state.status, state.accepted_value) == ("not_reported", None)
    assert review.approved_results(conn, document.id) == []  # no value is exported
    assert "employees" not in review.not_approved_metrics(conn, document.id)  # but it is done
    review.reopen(conn, document.id, "employees", "Analyst A", "Found it on page 25 after all")
    assert review.review_state(conn, document.id, "employees").status == "open"


def test_evidence_says_not_reported_with_the_reason(report):
    conn, config, document, _, _ = report
    review.not_reported(conn, document.id, "employees", "Analyst A", REASON)
    pending = metric_evidence(conn, config, document.id, "employees")
    assert (pending.status, pending.source_type, pending.value) == ("pending", "not_reported", None)
    review.approve_correction(conn, document.id, "employees", "Analyst A")
    ev = metric_evidence(conn, config, document.id, "employees")
    assert (ev.status_label, ev.source_label, ev.entry_reason) == ("Not reported", "Not reported", REASON)
    assert [h.action for h in ev.history[1:]] == ["Marked Not reported", "Approved: not reported"]
    assert ev.history[-1].new == "Not reported"
    assert "employees" not in current_figures(type("A", (), {"metrics": []})(), {}, {})  # nothing to check


def test_exported_as_a_blank_cell_with_the_reason(report):
    conn, config, document, candidate, _ = report
    review.approve(conn, candidate("total_assets_etb", "1,234,567"), "Analyst A")
    review.not_reported(conn, document.id, "employees", "Analyst A", REASON)
    review.approve_correction(conn, document.id, "employees", "Analyst A")
    wb = build_workbook(conn, config, [document.id], exported_by="Analyst A")
    ws = wb[APPROVED_SHEET]
    header = [c.value for c in ws[1]]
    cell = ws.cell(2, header.index(config.metric("employees").export_column) + 1)
    assert cell.value is None  # blank, never zero
    assert cell.comment.text.startswith("Not reported in this report: The report does not disclose")
    assert cell.hyperlink.target.endswith("metric=employees")
    rows = list(wb[METRIC_EVIDENCE_SHEET].iter_rows(values_only=True))
    row = next(dict(zip(rows[0], r)) for r in rows[1:] if r[2] == "employees")
    assert (row["status"], row["value"], row["note"]) == ("Not reported", None, REASON)


def test_not_reported_from_metric_details(app):
    open_metric(app, "shareholders")
    next(t for t in app.text_area if t.key == "not_reported_reason_shareholders").input(REASON).run()
    next(b for b in app.button if b.label == "Record as not reported").click().run()
    assert not app.exception
    next(b for b in app.button if b.label == "Approve not reported").click().run()
    assert not app.exception
    with closing(connect(app.db_path)) as conn:
        assert review.review_state(conn, app.document_id, "shareholders").status == "not_reported"
    table = app.dataframe[0].value
    assert table[table["Metric"] == "Shareholders"].iloc[0]["Status"].endswith("Not reported")
