"""Phase 3 pipeline: raw candidates -> metric candidates with units, periods and findings.

Ranking puts the most direct evidence first (current-year table rows without warnings),
but it is advisory: every candidate is kept and nothing is approved here.
"""

from __future__ import annotations

import re

from src.config_loader import AppConfig
from src.metric_mapper import MetricMapper
from src.models import PERIOD_ORDER, AnalysisResult, Finding, MappedCandidate, MetricCandidate, MetricResult
from src.number_parser import parse_number
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, ExtractionResult, PageReport, RawCandidate
from src.unit_normalizer import find_unit, normalize, rounding_tolerance
from src.validator import candidate_findings, conflict_findings, not_found_finding

# Horizontal distance (points) between a value's centre and a column heading's centre.
YEAR_COLUMN_TOLERANCE = 45.0
NOTE_COLUMN_TOLERANCE = 25.0
_BARE_YEAR = re.compile(r"^(19|20)\d\d$")


def report_end_year(period: str) -> int | None:
    """'2025/26' -> 2026, '2025-2026' -> 2026, 'FY2026' -> 2026; None if unclear."""
    match = re.search(r"(\d{4})\s*[/-]\s*(\d{4}|\d{2})\b", period)
    if match:
        start, end_text = int(match.group(1)), match.group(2)
        end = int(end_text) if len(end_text) == 4 else start // 100 * 100 + int(end_text)
        if end < start:
            end += 100
        return end if end == start + 1 else None
    match = re.fullmatch(r"\s*(?:FY)?\s*(\d{4})\s*", period, re.IGNORECASE)
    return int(match.group(1)) if match else None


def is_bare_year(text: str) -> bool:
    return bool(_BARE_YEAR.match(text))


def column_year(candidate: RawCandidate, page: PageReport) -> int | None:
    """Year printed above the value's column (the nearest heading above, horizontally aligned)."""
    x_mid = (candidate.bbox[0] + candidate.bbox[2]) / 2
    y_mid = (candidate.bbox[1] + candidate.bbox[3]) / 2
    above = [
        (y, year) for year, x, y in page.year_markers
        if y < y_mid - 2 and abs(x - x_mid) <= YEAR_COLUMN_TOLERANCE
    ]
    return max(above)[1] if above else None


def is_note_reference(candidate: RawCandidate, page: PageReport) -> bool:
    """True when the value sits under a 'Note(s)' column heading (a note number, not an amount)."""
    x_mid = (candidate.bbox[0] + candidate.bbox[2]) / 2
    y_mid = (candidate.bbox[1] + candidate.bbox[3]) / 2
    return any(y < y_mid and abs(x - x_mid) <= NOTE_COLUMN_TOLERANCE for x, y in page.note_headers)


def period_of(year: int | None, end_year: int | None) -> str:
    if year is None or end_year is None:
        return "unknown"
    if year == end_year:
        return "current"
    return "prior" if year == end_year - 1 else "other"


def analyse(extraction: ExtractionResult, config: AppConfig, metric_ids: list[str], report_period: str) -> AnalysisResult:
    mapped = map_candidates(extraction, config, report_period)
    names = {m.id: m.display_name for m in config.metrics}

    results: list[MetricResult] = []
    all_findings: list[Finding] = []
    for metric_id in metric_ids:
        metric = config.metric(metric_id)
        candidates = [
            MetricCandidate(m, metric_id, normalize(m.parsed, m.unit, metric.expected_unit), rounding_tolerance(m.parsed, m.unit))
            for m in mapped
            if any(match.metric_id == metric_id for match in m.matches)
        ]
        by_key = {mc.mapped.key: candidate_findings(mc, names) for mc in candidates}
        candidates.sort(key=lambda mc: _rank(mc, by_key[mc.mapped.key]))

        findings = [f for mc in candidates for f in by_key[mc.mapped.key]]
        findings += conflict_findings(metric, candidates)
        result = MetricResult(metric, candidates, findings)
        if result.primary is None:
            result.findings.append(not_found_finding(metric))
        results.append(result)
        all_findings += result.findings
    return AnalysisResult(results, mapped, all_findings)


def map_candidates(extraction: ExtractionResult, config: AppConfig, report_period: str) -> list[MappedCandidate]:
    mapper = MetricMapper(config.mappings)
    end_year = report_end_year(report_period)
    chart_rows = {
        (c.page_index, c.row_index)
        for c in extraction.candidates
        if c.extraction_method == TABLE_METHOD and is_bare_year(c.raw_value_text)
    }
    table_locations = {
        (c.page_index, c.bbox): (c.extraction_method, c.page_index, c.row_index, c.column_index)
        for c in extraction.candidates
        if c.extraction_method == TABLE_METHOD
    }
    mapped = []
    for c in extraction.candidates:
        if is_bare_year(c.raw_value_text):
            continue  # a year label (column heading, chart axis), not a figure
        matches = mapper.match(c)
        if not matches:
            continue
        page = extraction.pages[c.page_index]
        is_table = c.extraction_method == TABLE_METHOD
        if is_table and is_note_reference(c, page):
            continue
        year = column_year(c, page) if is_table else None
        mapped.append(
            MappedCandidate(
                candidate=c,
                matches=matches,
                parsed=parse_number(c.raw_value_text),
                unit=find_unit(c, page),
                period_year=year,
                period=period_of(year, end_year),
                duplicate_of=None if is_table else table_locations.get((c.page_index, c.bbox)),
                in_chart_row=is_table and (c.page_index, c.row_index) in chart_rows,
            )
        )
    return mapped


def _rank(mc: MetricCandidate, findings: list[Finding]) -> tuple:
    c = mc.mapped.candidate
    return (
        PERIOD_ORDER[mc.mapped.period],
        any(f.severity in ("warning", "error") for f in findings),
        c.extraction_method == NARRATIVE_METHOD,
        mc.mapped.duplicate_of is not None,
        c.page_index,
        c.row_index,
        c.column_index,
    )
