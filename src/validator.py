"""Validation rules. Each produces findings for the reviewer; none changes a value.

Rule IDs:
  MAP-AMBIGUOUS   wording matches more than one metric
  VALUE-PARSE     printed value is not a safely readable number
  VALUE-UNIT      unit missing, different or not convertible to the metric's unit
  CHART-ROW       table row also holds a year: label and value may come from a chart
  PERIOD-UNKNOWN  no year column found above a table value
  SENTENCE        value comes from running text; its year must be checked
  DUPLICATE       the same printed number was also caught as a table row
  NOT-FOUND       no current-year figure found for a metric (not the same as zero)
  CONFLICT        figures for the same metric and year disagree beyond rounding
"""

from __future__ import annotations

from src.candidate_queries import CandidateKey
from src.config_loader import MetricDefinition
from src.models import Finding, MetricCandidate
from src.pdf_extractor import NARRATIVE_METHOD


def candidate_findings(mc: MetricCandidate, metric_names: dict[str, str]) -> list[Finding]:
    mapped, c = mc.mapped, mc.mapped.candidate
    where = f"PDF page {c.page_index + 1}"
    keys = (mapped.key,)
    findings: list[Finding] = []

    def add(rule_id: str, severity: str, message: str) -> None:
        findings.append(Finding(rule_id, severity, message, mc.metric_id, keys))

    if mapped.is_ambiguous:
        names = ", ".join(metric_names.get(m.metric_id, m.metric_id) for m in mapped.matches)
        add("MAP-AMBIGUOUS", "warning", f"{where}: '{c.raw_label or c.raw_value_text}' matches several metrics ({names}). Choose the right one.")
    if mapped.parsed.issue:
        add("VALUE-PARSE", "warning", f"{where}: {mapped.parsed.issue}")
    elif mc.normalized.issue:
        add("VALUE-UNIT", "warning", f"{where}: {mc.normalized.issue}")
    if mapped.in_chart_row:
        add("CHART-ROW", "info", f"{where}: this row also contains a year, so it is probably a chart; the label and value may not belong together. Not used in conflict checks.")
    if c.extraction_method != NARRATIVE_METHOD and mapped.period == "unknown":
        add("PERIOD-UNKNOWN", "info", f"{where}: no year heading found above this column; check which year it is.")
    if c.extraction_method == NARRATIVE_METHOD:
        add("SENTENCE", "info", f"{where}: found in running text; check that it refers to the report year.")
    if mapped.duplicate_of is not None:
        add("DUPLICATE", "info", f"{where}: the same printed number was also found as a table row; counted once.")
    return findings


def not_found_finding(metric: MetricDefinition) -> Finding:
    return Finding(
        "NOT-FOUND", "warning",
        f"No figure found for {metric.display_name}. This is not the same as zero; check the report and enter it manually if shown.",
        metric.id,
    )


def conflict_findings(metric: MetricDefinition, candidates: list[MetricCandidate]) -> list[Finding]:
    """Compare normalized values per year; rounded figures agree within their rounding.

    This year: table values under a this-year column, plus sentence figures. Table values
    with no year column (e.g. note breakdowns) and chart rows are listed as alternatives
    but not compared, because they often measure something different.
    """
    findings = []
    for period, severity, label in (("current", "warning", "this year"), ("prior", "info", "the prior year")):
        group = [
            mc for mc in candidates
            if _comparable(mc) and (
                mc.mapped.period == period
                or (period == "current" and mc.mapped.candidate.extraction_method == NARRATIVE_METHOD)
            )
        ]
        if _all_agree(group):
            continue
        values = "; ".join(
            f"{mc.mapped.candidate.raw_value_text} (p{mc.mapped.candidate.page_index + 1})" for mc in group
        )
        keys: tuple[CandidateKey, ...] = tuple(mc.mapped.key for mc in group)
        findings.append(Finding(
            "CONFLICT", severity,
            f"{metric.display_name}: figures for {label} disagree: {values}. Check which is right.",
            metric.id, keys,
        ))
    return findings


def _comparable(mc: MetricCandidate) -> bool:
    return mc.normalized.value is not None and mc.mapped.duplicate_of is None and not mc.mapped.in_chart_row


def _all_agree(group: list[MetricCandidate]) -> bool:
    for i, a in enumerate(group):
        for b in group[i + 1 :]:
            if abs(a.normalized.value - b.normalized.value) > max(a.tolerance, b.tolerance):
                return False
    return True
