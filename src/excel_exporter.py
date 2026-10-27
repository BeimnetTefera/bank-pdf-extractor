"""Approved-only Excel export, built from saved review decisions (never from screen state).

One workbook per fiscal year (`approved_2025-26.xlsx`) holding every bank saved for that
year. Exporting a year again rebuilds its file from the database.

Sheets:
  Approved Data    one row per report, in the column layout of the Shega bank timeseries
                   spreadsheet (config/export_template.yaml), so rows paste straight in.
                   Extracted columns hold approved figures; calculated columns hold the
                   values an analyst entered. A blank cell means "no value", never zero.
                   Sector columns (level: sector, D-056) hold the fiscal year's value from
                   the Sector data page in every bank row, falling back to a value approved
                   in the report itself (with a warning).
                   Every value cell links to its Evidence page and carries a comment with
                   its source, note and formula (spec section 1).
  Evidence         one row per analysed metric per report: status, value (approved only),
                   source type, page, note, external source, cross-check result (D-057) and
                   the Evidence page address; plus one row per sector value used.
  Source Evidence  one row per value: document, page, printed figure or formula, who entered it.
  Export Metadata  when, by whom, with which extractor, what was left out, and warnings.

Rejected figures, corrections still awaiting approval and unreviewed metrics are excluded.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from src.analysis import report_end_year
from src.calculated_entries import number_text
from src.calculation_service import CalculatedValue, current_values
from src.candidate_queries import METHOD_LABELS
from src.config_loader import AppConfig
from src.evidence import EvidenceError, MetricEvidence, PdfWords, SectorEvidence, evidence_url, format_value
from src.evidence import load_report_pdf, report_evidence, sector_evidence, sector_evidence_url
from src.external_sources import SavedSource, mismatch_text
from src.pdf_extractor import NARRATIVE_METHOD
from src.review_service import ApprovedResult, approved_results, extraction_status
from src.sector_data import sector_metrics

APPROVED_SHEET = "Approved Data"
METRIC_EVIDENCE_SHEET = "Evidence"
EVIDENCE_SHEET = "Source Evidence"
METADATA_SHEET = "Export Metadata"

EVIDENCE_COLUMNS = [
    "bank", "fiscal_year", "metric", "export_column", "approved_value", "approved_value_exact", "unit",
    "value_origin", "pdf_page", "box_on_page", "box_marked_by_reviewer", "original_label_or_sentence", "printed_value", "unit_evidence", "column_year",
    "extraction_method", "matched_by", "sign_choice", "approved_by", "approved_at_utc", "warnings_acknowledged",
    "approval_note", "entered_by", "entered_at_utc", "entry_reason", "metric_id", "report_year_as_saved",
    "source_file", "source_sha256", "source_document_id", "external_source", "evidence_url",
]
METRIC_EVIDENCE_COLUMNS = [
    "bank", "fiscal_year", "metric_id", "metric", "export_column", "status", "value", "unit", "as_printed",
    "source_type", "pdf_page", "label_or_sentence", "formula", "note", "entered_by", "approved_by",
    "approved_at_utc", "external_source", "cross_check", "evidence_url",
]
LINK_FONT = Font(color="0563C1", underline="single")
VALUE_ORIGINS = {"approved": "extracted", "corrected": "corrected by reviewer", "manual_entry": "entered manually"}
CALCULATED_ORIGIN = "calculated by analyst"
EXTERNAL_ORIGIN = "from an external source (see external_source)"
SECTOR_ORIGIN = "sector value for the fiscal year (Sector data page, external source)"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExportError(RuntimeError):
    """The export could not be produced; the message says why."""


@dataclass(frozen=True)
class YearExport:
    path: Path
    fiscal_year: str
    reports: int
    values: int  # approved extracted values
    calculated: int  # calculated values entered by analysts
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Report:
    """Everything exported for one saved report."""

    document: sqlite3.Row
    approved: list[ApprovedResult]
    calculated: dict[str, CalculatedValue]
    extraction_finished: bool
    evidence: list[MetricEvidence]  # every analysed metric, approved or not
    sector: dict[str, SectorEvidence] = field(default_factory=dict)  # sector metric id -> the year's sector value

    def report_evidence_approved(self) -> list[MetricEvidence]:
        return [e for e in self.evidence if e.status == "approved"]

    def sector_values(self) -> list[SectorEvidence]:
        """Sector values approved for the report's fiscal year (they fill the sector columns)."""
        return [s for s in self.sector.values() if s.status == "approved"]


def fiscal_year_label(end_year: int) -> str:
    """2026 -> '2025/26', the Shega fiscal_year form (July 2025 to June 2026)."""
    return f"{end_year - 1}/{end_year % 100:02d}"


def report_years(conn: sqlite3.Connection) -> list[int]:
    """Fiscal year-end years of the saved reports, newest first."""
    years = {report_end_year(d["reporting_period"]) for d in conn.execute("SELECT reporting_period FROM documents")}
    return sorted((y for y in years if y is not None), reverse=True)


def export_year(conn: sqlite3.Connection, config: AppConfig, end_year: int, *, exported_by: str) -> YearExport:
    """Write approved_<YYYY-YY>.xlsx for every bank saved for that fiscal year, replacing the previous copy."""
    label = fiscal_year_label(end_year)
    document_ids = [d["id"] for d in conn.execute("SELECT id, reporting_period FROM documents ORDER BY uploaded_at")
                    if report_end_year(d["reporting_period"]) == end_year]
    if not document_ids:
        raise ExportError(f"No reports are saved for {label}.")
    reports = _reports(conn, config, document_ids)
    if not reports:
        raise ExportError(f"Nothing is approved yet for {label}, so there is nothing to export. "
                          "Approve at least one metric first.")
    warnings = _warnings(reports, config)
    wb = _workbook(config, reports, exported_by, datetime.now(timezone.utc).isoformat(timespec="seconds"), warnings)

    target = config.settings.exports_dir / f"approved_{label.replace('/', '-')}.xlsx"
    temp = target.with_suffix(".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        wb.save(temp)
        os.replace(temp, target)
    except PermissionError as exc:
        temp.unlink(missing_ok=True)
        raise ExportError(f"{target.name} could not be replaced. If it is open in Excel, close it and export again.") from exc
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise ExportError(f"Could not write the Excel file in {target.parent}: {exc.strerror or exc}") from exc
    return YearExport(target, label, len(reports), sum(len(r.approved) for r in reports),
                      sum(len(r.calculated) for r in reports), warnings)


def build_workbook(conn: sqlite3.Connection, config: AppConfig, document_ids: list[str] | None = None, *,
                   exported_by: str, exported_at: str | None = None) -> Workbook:
    """Workbook of approved and calculated values for the given reports (all saved reports if None)."""
    reports = _reports(conn, config, document_ids)
    exported_at = exported_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return _workbook(config, reports, exported_by, exported_at, _warnings(reports, config))


# --- sheets ---------------------------------------------------------------------------

def _workbook(config: AppConfig, reports: list[_Report], exported_by: str, exported_at: str,
              warnings: tuple[str, ...]) -> Workbook:
    if not reports:
        raise ExportError("Nothing is approved yet, so there is nothing to export. Approve at least one metric first.")
    reports = sorted(reports, key=lambda r: (r.document["bank_name"].casefold(), r.document["uploaded_at"]))
    wb = Workbook()
    _approved_sheet(wb.active, config, reports)
    _metric_evidence_sheet(wb.create_sheet(METRIC_EVIDENCE_SHEET), config, reports)
    _evidence_sheet(wb.create_sheet(EVIDENCE_SHEET), config, reports)
    _metadata_sheet(wb.create_sheet(METADATA_SHEET), reports, exported_by, exported_at, warnings, config.settings.app_url)
    return wb


def _approved_sheet(ws, config: AppConfig, reports: list[_Report]) -> None:
    ws.title = APPROVED_SHEET
    template = config.export_template
    base = config.settings.app_url
    ws.append(list(template.columns))
    for report in reports:
        doc = report.document
        evidence = {e.metric_id: e for e in report.evidence}
        # Approved metrics without an export column appear only in Source Evidence.
        values: dict[str, tuple[Decimal, str | None]] = {}
        links: dict[str, tuple[str, str]] = {}  # column -> (Evidence page address, comment)
        for r in report.approved:
            if column := _export_column(config, r.metric_id):
                values[column] = (r.value, r.unit)
                links[column] = (evidence_url(base, doc["id"], r.metric_id),
                                 _value_comment(evidence[r.metric_id]) if r.metric_id in evidence else "")
        for column, v in report.calculated.items():
            if column in template.columns:
                values[column] = (v.value, None)
                links[column] = (evidence_url(base, doc["id"]), _calculated_comment(v))
        # Sector columns (D-056): the fiscal year's sector value, the same in every bank row. Without one,
        # a value approved in the report stays (a warning says so); a sector "not reported" is a blank cell.
        for s in report.sector.values():
            if s.export_column not in template.columns:
                continue
            url = sector_evidence_url(base, s.fiscal_year, s.metric_id)
            if s.status == "approved":
                values[s.export_column] = (s.value, s.unit)
                links[s.export_column] = (url, _sector_comment(s))
            elif s.status == "not_reported" and s.export_column not in values:
                links[s.export_column] = (url, f"Sector value not reported for {s.fiscal_year}: "
                                               f"{s.proposal.reason if s.proposal else ''}")
        # Not reported (D-053): the cell stays blank, with the reason as its comment and a link to the evidence.
        for e in report.evidence:
            if e.status == "not_reported" and e.export_column in template.columns and e.export_column not in values:
                links[e.export_column] = (evidence_url(base, doc["id"], e.metric_id),
                                          f"Not reported in this report: {e.entry_reason}\n"
                                          f"Approved by {e.approved_by} on {e.approved_at[:16].replace('T', ' ')} UTC")
        row = {template.bank_column: doc["bank_name"], template.year_column: _fiscal_year(doc)}
        ws.append([_cell_value(values[c][0]) if c in values else row.get(c) for c in template.columns])
        for index, column in enumerate(template.columns, start=1):
            if column in links:
                cell = ws.cell(ws.max_row, index)
                if column in values:
                    _number_format(cell, *values[column])
                url, comment = links[column]
                cell.hyperlink = url
                cell.font = LINK_FONT
                if column not in values:
                    cell.value = None  # openpyxl writes the address into an empty linked cell; keep it blank
                if comment:
                    cell.comment = Comment(comment, "Bank PDF Extractor", width=360, height=180)
    widths = {template.columns.index(template.bank_column) + 1: 28, template.columns.index(template.year_column) + 1: 12}
    _finish(ws, widths=widths, default_width=18)


def _value_comment(e: MetricEvidence) -> str:
    """What a reader needs without leaving Excel: source, page, as printed, who approved, the note."""
    lines = [f"{e.source_label}" + (f", PDF page {e.page}" if e.page else "")
             + (" (number found on the page)" if e.box_found else "")
             + (" (the box marks a figure used, not the value)" if e.box_is_input else "")]
    if e.calculation is not None:
        lines += [f"Formula: {e.display_name} = {e.calculation.words}", f"Arithmetic: {e.calculation.arithmetic}"]
        lines += [f"  {i.label} = {number_text(i.value, i.unit)} ({i.where})" for i in e.calc_inputs]
        if e.stale:
            lines.append("STALE: " + " ".join(e.stale))
    elif e.conversion:
        lines.append(f"As printed: {e.conversion}")
    if e.external is not None:
        lines.append(f"Source: {e.external.citation}")
    if e.note_calc is not None:
        check = "agrees with" if e.note_calc.matches else "does NOT agree with"
        lines.append(f"Calculation in the note: {e.note_calc.arithmetic} ({check} the stated {e.note_calc.stated:f})")
        lines += [f"  {f.label or f.printed} = {number_text(f.value, f.unit)}"
                  + (f" (PDF page {f.page})" if f.page else " (not found on the page)") for f in e.note_calc.figures]
    if e.label:
        lines.append(f"Quote: \"{e.label}\"" if e.external is not None else f"Label: {e.label}")
    if e.sign_text:
        lines.append(e.sign_text)
    if e.approved_by:
        lines.append(f"Approved by {e.approved_by} on {e.approved_at[:16].replace('T', ' ')} UTC")
    if e.entry_reason:
        lines.append(f"Reason: {e.entry_reason}")
    if e.approval_note:
        lines.append(f"Note: {e.approval_note}")
    for c in e.cross_checks:
        if c.check.active:
            flag = "MISMATCH" if c.comparison.status == "mismatch" else c.comparison.label
            lines.append(f"Cross-check ({flag}): {mismatch_text(c)}")
    lines.append("Click the cell for the Evidence page (the app must be running).")
    return "\n".join(lines)


def _sector_comment(s: SectorEvidence) -> str:
    lines = [f"Sector value for {s.fiscal_year} (the same in every bank row), from the Sector data page"]
    if s.source is not None:
        lines += [f"Source: {s.source.citation}", f"As printed: {s.source.conversion}", f"Quote: \"{s.source.quote}\""]
    if s.approval is not None:
        lines.append(f"Approved by {s.approval.reviewer} on {s.approval.created_at[:16].replace('T', ' ')} UTC")
    if s.proposal is not None and s.proposal.reason:
        lines.append(f"Note: {s.proposal.reason}")
    lines.append("Click the cell for the Evidence page (the app must be running).")
    return "\n".join(lines)


def _source_text(source: SavedSource | None) -> str:
    """The external_source cell: who published what, where, and the exact quote."""
    if source is None:
        return ""
    return f"{source.citation} | \"{source.quote}\" | {source.conversion}"


def _cross_check_text(e: MetricEvidence) -> str:
    """The cross_check cell: each active cross-check's result, MISMATCH first in capitals."""
    parts = [("MISMATCH: " if c.comparison.status == "mismatch" else f"{c.comparison.label}: ") + mismatch_text(c)
             for c in e.cross_checks if c.check.active]
    return "\n".join(parts)


def _calculated_comment(v: CalculatedValue) -> str:
    return (f"Calculated by {v.reviewer} on {v.created_at[:16].replace('T', ' ')} UTC (Calculations page)\n"
            f"Formula: {v.formula_note}\nClick the cell for the report's evidence summary.")


def _metric_evidence_sheet(ws, config: AppConfig, reports: list[_Report]) -> None:
    """One row per analysed metric per report; the value only when approved (D-032)."""
    ws.append(METRIC_EVIDENCE_COLUMNS)
    value_column = METRIC_EVIDENCE_COLUMNS.index("value") + 1
    url_column = METRIC_EVIDENCE_COLUMNS.index("evidence_url") + 1
    for report in reports:
        doc = report.document
        common = {"bank": doc["bank_name"], "fiscal_year": _fiscal_year(doc)}
        for e in report.evidence:
            approved = e.status == "approved"
            row = common | {
                "metric_id": e.metric_id, "metric": e.display_name, "export_column": e.export_column or "",
                "status": e.status_label, "value": _cell_value(e.value) if approved else "",
                "unit": e.unit if approved else "", "source_type": e.source_label, "pdf_page": e.page or "",
                "as_printed": "" if e.calculation else e.conversion,
                "label_or_sentence": "" if e.calculation else e.label,
                "formula": (f"{e.metric_id} = {e.calculation.expression} | {e.calculation.words} | "
                            f"{e.calculation.arithmetic}" + (" | STALE" if e.stale else "")) if e.calculation else "",
                "note": "\n".join(n for n in (e.entry_reason, e.approval_note) if n),
                "entered_by": e.entered_by, "approved_by": e.approved_by, "approved_at_utc": e.approved_at,
                "external_source": _source_text(e.external), "cross_check": _cross_check_text(e),
                "evidence_url": evidence_url(config.settings.app_url, doc["id"], e.metric_id),
            }
            ws.append([None if row.get(c) in ("", None) else row.get(c) for c in METRIC_EVIDENCE_COLUMNS])
            if approved:
                _number_format(ws.cell(ws.max_row, value_column), e.value, e.unit)
            _link(ws.cell(ws.max_row, url_column))
        for s in report.sector_values():
            row = common | {
                "metric_id": s.metric_id, "metric": f"{s.display_name} (sector, {s.fiscal_year})",
                "export_column": s.export_column or "", "status": "Sector value approved", "value": _cell_value(s.value),
                "unit": s.unit, "as_printed": s.source.conversion if s.source else "", "source_type": "External source "
                "(Sector data)", "label_or_sentence": s.source.quote if s.source else "",
                "note": s.proposal.reason if s.proposal else "", "entered_by": s.proposal.reviewer if s.proposal else "",
                "approved_by": s.approval.reviewer, "approved_at_utc": s.approval.created_at,
                "external_source": _source_text(s.source),
                "evidence_url": sector_evidence_url(config.settings.app_url, s.fiscal_year, s.metric_id),
            }
            ws.append([None if row.get(c) in ("", None) else row.get(c) for c in METRIC_EVIDENCE_COLUMNS])
            _number_format(ws.cell(ws.max_row, value_column), s.value, s.unit)
            _link(ws.cell(ws.max_row, url_column))
        for column, v in report.calculated.items():
            row = common | {
                "metric": column, "export_column": column, "status": "Calculated by analyst",
                "value": _cell_value(v.value), "source_type": "Calculated (typed on the Calculations page)",
                "formula": v.formula_note, "approved_by": v.reviewer, "approved_at_utc": v.created_at,
                "evidence_url": evidence_url(config.settings.app_url, doc["id"]),
            }
            ws.append([None if row.get(c) in ("", None) else row.get(c) for c in METRIC_EVIDENCE_COLUMNS])
            _number_format(ws.cell(ws.max_row, value_column), v.value, None)
            _link(ws.cell(ws.max_row, url_column))
    _finish(ws, widths={METRIC_EVIDENCE_COLUMNS.index("label_or_sentence") + 1: 40,
                        METRIC_EVIDENCE_COLUMNS.index("note") + 1: 50,
                        METRIC_EVIDENCE_COLUMNS.index("external_source") + 1: 50,
                        METRIC_EVIDENCE_COLUMNS.index("cross_check") + 1: 50,
                        url_column: 60}, default_width=16)


def _link(cell) -> None:
    if isinstance(cell.value, str) and cell.value.startswith("http"):
        cell.hyperlink = cell.value
        cell.font = LINK_FONT


def _evidence_sheet(ws, config: AppConfig, reports: list[_Report]) -> None:
    ws.append(EVIDENCE_COLUMNS)
    value_column = EVIDENCE_COLUMNS.index("approved_value") + 1
    url_column = EVIDENCE_COLUMNS.index("evidence_url") + 1
    base = config.settings.app_url
    for report in reports:
        doc = report.document
        source = {
            "source_document_id": doc["id"], "bank": doc["bank_name"], "fiscal_year": _fiscal_year(doc),
            "report_year_as_saved": doc["reporting_period"], "source_file": doc["original_filename"],
            "source_sha256": doc["sha256"],
        }
        external = {e.metric_id: e.external for e in report.evidence if e.external is not None}
        for r in report.approved:
            row = source | _approved_evidence(config, r) | {"evidence_url": evidence_url(base, doc["id"], r.metric_id)}
            if r.metric_id in external:
                ext = external[r.metric_id]
                row |= {"value_origin": EXTERNAL_ORIGIN, "pdf_page": "", "original_label_or_sentence": ext.quote,
                        "printed_value": ext.printed_value, "unit_evidence": ext.conversion,
                        "external_source": _source_text(ext)}
            _append_evidence(ws, row)
            _number_format(ws.cell(ws.max_row, value_column), r.value, r.unit)
            _link(ws.cell(ws.max_row, url_column))
        for s in report.sector_values():
            _append_evidence(ws, source | {
                "metric_id": s.metric_id, "metric": s.display_name, "export_column": s.export_column or "",
                "approved_value": _cell_value(s.value), "approved_value_exact": str(s.value), "unit": s.unit,
                "value_origin": SECTOR_ORIGIN, "original_label_or_sentence": s.source.quote if s.source else "",
                "printed_value": s.source.printed_value if s.source else "",
                "unit_evidence": s.source.conversion if s.source else "", "approved_by": s.approval.reviewer,
                "approved_at_utc": s.approval.created_at, "entered_by": s.proposal.reviewer if s.proposal else "",
                "entered_at_utc": s.proposal.created_at if s.proposal else "",
                "entry_reason": s.proposal.reason if s.proposal else "", "external_source": _source_text(s.source),
                "evidence_url": sector_evidence_url(base, s.fiscal_year, s.metric_id)})
            _number_format(ws.cell(ws.max_row, value_column), s.value, s.unit)
            _link(ws.cell(ws.max_row, url_column))
        for column, v in report.calculated.items():
            _append_evidence(ws, source | {
                "metric": column, "export_column": column, "approved_value": _cell_value(v.value),
                "approved_value_exact": str(v.value), "value_origin": CALCULATED_ORIGIN, "approval_note": v.formula_note,
                "approved_by": v.reviewer, "approved_at_utc": v.created_at, "evidence_url": evidence_url(base, doc["id"]),
            })
            _number_format(ws.cell(ws.max_row, value_column), v.value, None)
            _link(ws.cell(ws.max_row, url_column))
    _finish(ws, widths={EVIDENCE_COLUMNS.index("original_label_or_sentence") + 1: 50}, default_width=16)


def _approved_evidence(config: AppConfig, r: ApprovedResult) -> dict:
    c, a, p = r.candidate, r.approval, r.proposal
    row = {
        "metric_id": r.metric_id, "export_column": _export_column(config, r.metric_id) or "",
        "metric": _display_name(config, r.metric_id), "approved_value": _cell_value(r.value),
        "approved_value_exact": str(r.value), "unit": r.unit,
        "value_origin": ("calculated with a formula (see the Evidence sheet)" if p and p.entry_kind == "calculated"
                         else VALUE_ORIGINS[p.decision if p else "approved"]), "pdf_page": r.source_page,
        "box_on_page": ", ".join(f"{v:.1f}" for v in r.evidence_box) if r.evidence_box else "",
        "box_marked_by_reviewer": "yes" if r.box_marked_by_reviewer else "no",
        "sign_choice": a.sign_choice or "", "approved_by": a.reviewer, "approved_at_utc": a.created_at,
        "warnings_acknowledged": "yes" if a.warnings_acknowledged else "no", "approval_note": a.reason,
        "entered_by": p.reviewer if p else "", "entered_at_utc": p.created_at if p else "",
        "entry_reason": p.reason if p else "",
    }
    if c is not None:
        row |= {
            "original_label_or_sentence": (f"…{c.text_before} [{c.raw_value_text}] {c.text_after}…"
                                           if c.extraction_method == NARRATIVE_METHOD else c.raw_label),
            "printed_value": c.raw_value_text, "unit_evidence": c.conversion_basis or c.raw_unit,
            "column_year": c.period_year,
            "extraction_method": METHOD_LABELS.get(c.extraction_method, c.extraction_method)
                                 + (" (OCR from page image)" if c.read_by_ocr else ""),
            "matched_by": f"{c.match_basis}: {c.matched_text}",
        }
    return row


def _append_evidence(ws, row: dict) -> None:
    ws.append([None if row.get(column) == "" else row.get(column) for column in EVIDENCE_COLUMNS])  # blank cells


def _metadata_sheet(ws, reports: list[_Report], exported_by: str, exported_at: str, warnings: tuple[str, ...],
                    app_url: str) -> None:
    versions = sorted({r.document["extractor_version"] or "unknown" for r in reports})
    rows = [
        ("Field", "Value"),
        ("Fiscal year", ", ".join(sorted({_fiscal_year(r.document) for r in reports}))),
        ("Contents", "Approved figures and analysts' calculated values, one row per saved bank report."),
        ("Excluded", "Rejected figures, corrections or manual entries not yet approved, and unreviewed metrics."),
        ("Blank cells", "A blank cell in Approved Data means no value was approved or entered. It is not zero."),
        ("Units", "ETB amounts in full Birr (not thousands). Percentages as decimal fractions: 0.29 = 29% "
                  "(shown with % formatting). Counts as whole numbers. Calculated values exactly as entered."),
        ("Layout", "Approved Data uses the columns of the Shega bank timeseries spreadsheet. Extracted columns come "
                   "from the reviewed report; calculated columns were worked out and entered by an analyst "
                   "(formula in Source Evidence). The app itself calculates nothing."),
        ("bank", "As chosen by the reviewer when the report was saved."),
        ("fiscal_year", "Ethiopian fiscal year (July to June) in YYYY/YY form; the year as typed is in Source Evidence."),
        ("Tracing a value", "Find the bank, fiscal_year and export_column in Source Evidence for the PDF, page, "
                            "printed figure or formula, and who entered it. box_on_page is the value's position "
                            "on that page (x0, y0, x1, y1 in PDF points from the top-left corner); "
                            "box_marked_by_reviewer = yes when a reviewer drew it around the number."),
        ("Evidence links", f"Every value cell in Approved Data links to its Evidence page (page image, box, note, "
                           f"history) and its comment gives the source and note. The links open where the app "
                           f"is running at {app_url}. The Evidence sheet lists every analysed metric."),
        ("Exported at (UTC)", exported_at),
        ("Exported by", exported_by),
        ("Application", "Bank PDF Extractor (local prototype)"),
        ("Extractor version", "; ".join(versions)),
        ("Reports", len(reports)),
        ("Approved values", sum(len(r.approved) for r in reports)),
        ("Calculated values", sum(len(r.calculated) for r in reports)),
        ("Sector values", "Sector columns (one figure for the whole banking sector) come from the Sector data page "
                          "and are the same in every bank row of the fiscal year; the Evidence sheet names their "
                          "external source. External sources are cited by title, publisher and address (never "
                          "opened by the app)."),
        ("Cross-checks", "cross_check in the Evidence sheet compares an approved value with an external figure: "
                         "Match within 0.5% (or 2 thousand Birr of rounding for Birr'000 figures), else MISMATCH."),
    ]
    rows += [("Warning", w) for w in warnings]
    for row in rows:
        ws.append(row)
    _finish(ws, widths={1: 32, 2: 100})


# --- helpers --------------------------------------------------------------------------

def _reports(conn: sqlite3.Connection, config: AppConfig, document_ids: list[str] | None) -> list[_Report]:
    """Reports with at least one approved or calculated value."""
    if document_ids is None:
        document_ids = [d["id"] for d in conn.execute("SELECT id FROM documents ORDER BY uploaded_at")]
    reports = []
    sector_by_year: dict[str, dict[str, SectorEvidence]] = {}
    for document_id in document_ids:
        document = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if document is None:
            raise ExportError(f"Report {document_id} is not in the database.")
        approved, calculated = approved_results(conn, document_id), current_values(conn, document_id)
        if approved or calculated:
            words = _pdf_words(config, document)
            try:
                evidence = report_evidence(conn, config, document_id, words)
            finally:
                if words is not None:
                    words.close()
            year = _fiscal_year(document)
            if year not in sector_by_year:
                sector_by_year[year] = _sector_values(conn, config, year)
            reports.append(_Report(document, approved, calculated, extraction_status(conn, document_id).finished,
                                   evidence, sector_by_year[year]))
    return reports


def _sector_values(conn: sqlite3.Connection, config: AppConfig, fiscal_year: str) -> dict[str, SectorEvidence]:
    """The year's sector value of each sector metric (none when the year is not a fiscal year)."""
    if report_end_year(fiscal_year) is None:
        return {}
    return {m.id: sector_evidence(conn, config, fiscal_year, m.id) for m in sector_metrics(config)}


def _pdf_words(config: AppConfig, document: sqlite3.Row) -> PdfWords | None:
    """The report's words, to give typed values the box of their printed number; None if the PDF is unreadable."""
    try:
        return PdfWords(load_report_pdf(config.settings, document))
    except EvidenceError:
        return None


def _warnings(reports: list[_Report], config: AppConfig) -> tuple[str, ...]:
    """Things the reader must know before loading the file into the dataset."""
    ids: dict[tuple[str, str], list[str]] = defaultdict(list)
    names: dict[tuple[str, str], str] = {}
    for r in reports:
        key = (r.document["bank_name"].casefold(), _fiscal_year(r.document))
        ids[key].append(r.document["id"])
        names[key] = r.document["bank_name"]
    warnings = [
        f"{names[key]} has {len(found)} saved reports with approved values for {key[1]}, so it has {len(found)} rows. "
        "Keep one before loading the file into the dataset."
        for key, found in sorted(ids.items()) if len(found) > 1
    ]
    no_column = sorted({_display_name(config, a.metric_id) for r in reports for a in r.approved
                        if not _export_column(config, a.metric_id)})
    if no_column:
        warnings.append(f"Approved but not in the spreadsheet layout, so only in Source Evidence: {', '.join(no_column)}.")
    for r in reports:
        if r.calculated and not r.extraction_finished:
            warnings.append(f"{r.document['bank_name']} {_fiscal_year(r.document)}: the extraction review was "
                            "reopened after calculated values were entered. Check the calculated values.")
    for r in reports:
        warnings += _report_warnings(r)
    return tuple(warnings)


def _report_warnings(r: _Report) -> list[str]:
    """Cross-check mismatches and sector values taken from the report instead of the Sector data page."""
    where = f"{r.document['bank_name']} {_fiscal_year(r.document)}"
    warnings = [f"{where}: the approved {e.display_name} ({e.value_text}) does not match its external cross-check "
                f"({mismatch_text(c)}). Check it before loading the file."
                for e in r.report_evidence_approved() for c in e.mismatches]
    for a in r.approved:
        s = r.sector.get(a.metric_id)
        if s is None:
            continue
        if s.status != "approved":
            warnings.append(f"{where}: {s.display_name} has no approved sector value for {s.fiscal_year} on the "
                            f"Sector data page, so the value approved in this report ({format_value(a.value, a.unit)}) "
                            "is exported instead.")
        elif s.value != a.value:
            warnings.append(f"{where}: the value approved in this report for {s.display_name} "
                            f"({format_value(a.value, a.unit)}) differs from the sector value "
                            f"({s.value_text}); the sector value is exported.")
    return warnings


def _fiscal_year(document: sqlite3.Row) -> str:
    end_year = report_end_year(document["reporting_period"])
    return fiscal_year_label(end_year) if end_year else document["reporting_period"]


def _cell_value(value: Decimal) -> int | float | str:
    """A number Excel stores exactly; otherwise the exact text, so no digit is silently lost."""
    as_float = float(value)
    if Decimal(repr(as_float)) != value:
        return str(value)
    return int(value) if value == value.to_integral_value() else as_float


def _number_format(cell, value: Decimal, unit: str | None) -> None:
    """unit None: a calculated value, shown as entered (ratios such as 6.44 are not percentages)."""
    if isinstance(cell.value, str):
        return
    if unit == "fraction":
        cell.number_format = "0.00%"
    elif value == value.to_integral_value():
        cell.number_format = "#,##0"
    elif unit is not None:
        cell.number_format = "#,##0.00"


def _export_column(config: AppConfig, metric_id: str) -> str | None:
    try:
        return config.metric(metric_id).export_column
    except KeyError:
        return None  # metric removed from metrics.yaml after it was approved


def _display_name(config: AppConfig, metric_id: str) -> str:
    try:
        return config.metric(metric_id).display_name
    except KeyError:
        return metric_id


def _finish(ws, widths: dict[int, int], default_width: int = 14) -> None:
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for column in range(1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(column)].width = widths.get(column, default_width)
