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
  NOTE-DIFFERS    a note table prints another figure than the primary statement under the same wording
  OCR             read by OCR from a picture of the page: digits may be misread
  SENTENCE-CHANGE the sentence states a change or comparison ("opened 123 additional"), not a total
  APPROXIMATE     the sentence gives an estimate ("nearly 64,000")
  IDENTITY        an accounting identity does not balance with this figure (src/identities.py)
  COUNT-SCALE     a count read from a table whose heading gives a scale (Birr'000): taken as printed
  RELATED-LINE    the statement prints a second line that may belong in the total (src/related_lines.py)
"""

from __future__ import annotations

from src.candidate_queries import CandidateKey
from src.config_loader import MetricDefinition
from src.models import Finding, MetricCandidate
from src.narrative_cues import CHANGE, approximate_word, is_change_or_estimate, sentence_cue
from src.pdf_extractor import NARRATIVE_METHOD


def candidate_findings(mc: MetricCandidate, metric_names: dict[str, str]) -> list[Finding]:
    mapped, c = mc.mapped, mc.mapped.candidate
    where = f"PDF page {c.page_index + 1}"
    keys = (mapped.key,)
    findings: list[Finding] = []

    def add(rule_id: str, severity: str, message: str) -> None:
        findings.append(Finding(rule_id, severity, message, mc.metric_id, keys))

    if c.ocr:
        add("OCR", "warning", f"{where}: read by OCR from a picture of the page. OCR can misread digits "
                              "(e.g. 8 and 3, 1 and 7) or drop commas: check every digit against the page.")
    if mapped.is_ambiguous:
        names = ", ".join(metric_names.get(m.metric_id, m.metric_id) for m in mapped.matches)
        add("MAP-AMBIGUOUS", "warning", f"{where}: '{c.raw_label or c.raw_value_text}' matches several metrics ({names}). Choose the right one.")
    if mapped.parsed.issue:
        add("VALUE-PARSE", "warning", f"{where}: {mapped.parsed.issue}")
    elif mc.normalized.issue:
        add("VALUE-UNIT", "warning", f"{where}: {mc.normalized.issue}")
    if mc.normalized.unit == "count" and mapped.unit.from_heading and mapped.unit.scale != 1:
        add("COUNT-SCALE", "warning", f"{where}: a count in a table headed {mapped.unit.source}. It is taken as "
                                      "printed, but some banks print share counts in thousands too (BoA in full, "
                                      "Zemen in thousands): check, e.g. profit / shares should give the EPS.")
    if mapped.in_chart_row:
        add("CHART-ROW", "info", f"{where}: this row also contains a year, so it is probably a chart; the label and value may not belong together. Not used in conflict checks.")
    if c.extraction_method != NARRATIVE_METHOD and mapped.period == "unknown":
        add("PERIOD-UNKNOWN", "info", f"{where}: no year heading found above this column; check which year it is.")
    if c.extraction_method == NARRATIVE_METHOD:
        add("SENTENCE", "info", f"{where}: found in running text; check that it refers to the report year.")
        if sentence_cue(c) == CHANGE:
            words = " ".join(c.text_before.split()[-2:] + [c.raw_value_text] + c.text_after.split()[:2])
            add("SENTENCE-CHANGE", "warning", f"{where}: the sentence describes a change or a comparison "
                                              f"('…{words}…'), not the year-end total. Look for the total.")
        approximate = approximate_word(c)
        if approximate:
            add("APPROXIMATE", "info", f"{where}: printed as an estimate ('{approximate} {c.raw_value_text}'), "
                                       "not an exact figure.")
    if mapped.duplicate_of is not None:
        add("DUPLICATE", "info", f"{where}: the same printed number was also found as a table row; counted once.")
    return findings


def not_found_finding(metric: MetricDefinition) -> Finding:
    return Finding(
        "NOT-FOUND", "warning",
        f"No figure found for {metric.display_name}. This is not the same as zero; check the report and enter it manually if shown.",
        metric.id,
    )


def conflict_findings(metric: MetricDefinition, candidates: list[MetricCandidate],
                      statement_keys: frozenset[CandidateKey] = frozenset()) -> list[Finding]:
    """Compare normalized values per year; rounded figures agree within their rounding.

    This year: table values under a this-year column, plus sentence figures. Table values
    with no year column (e.g. note breakdowns) and chart rows are listed as alternatives
    but not compared, because they often measure something different. So are sentence
    figures that state a change or an estimate ("opened 123 additional", "over 230").

    statement_keys: candidates from a primary statement. When this year's statement figure
    exists, note tables that print a different figure under the same label (often a part, or
    another basis) get an info note (NOTE-DIFFERS) instead of a conflict.
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
        if period == "current" and any(mc.mapped.key in statement_keys for mc in group):
            group, findings_for_notes = _split_note_rows(metric, group, statement_keys)
            findings += findings_for_notes
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


def _split_note_rows(metric: MetricDefinition, group: list[MetricCandidate],
                     statement_keys: frozenset[CandidateKey]) -> tuple[list[MetricCandidate], list[Finding]]:
    """Keep statement rows and sentences for the conflict check; note rows that differ from
    every statement figure get an info finding instead."""
    statement = [mc for mc in group if mc.mapped.key in statement_keys]
    kept, findings = [], []
    for mc in group:
        c = mc.mapped.candidate
        is_note_row = c.extraction_method != NARRATIVE_METHOD and mc.mapped.key not in statement_keys
        if not is_note_row:
            kept.append(mc)
            continue
        if any(_agree(mc, s) for s in statement):
            continue
        shown = "; ".join(f"{s.mapped.candidate.raw_value_text} (p{s.mapped.candidate.page_index + 1})" for s in statement)
        findings.append(Finding(
            "NOTE-DIFFERS", "info",
            f"PDF page {c.page_index + 1}: this table prints {c.raw_value_text} under the same wording, but the "
            f"{metric.display_name} line of the primary statement is {shown}. Notes often show a part or another "
            "basis; the statement figure is suggested.",
            metric.id, (mc.mapped.key,)))
    return kept, findings


def _comparable(mc: MetricCandidate) -> bool:
    c = mc.mapped.candidate
    if c.extraction_method == NARRATIVE_METHOD and is_change_or_estimate(c):
        return False
    return mc.normalized.value is not None and mc.mapped.duplicate_of is None and not mc.mapped.in_chart_row


def _agree(a: MetricCandidate, b: MetricCandidate) -> bool:
    return abs(a.normalized.value - b.normalized.value) <= max(a.tolerance, b.tolerance)


def _all_agree(group: list[MetricCandidate]) -> bool:
    return all(_agree(a, b) for i, a in enumerate(group) for b in group[i + 1 :])
