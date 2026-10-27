"""Phase 3 pipeline: raw candidates -> metric candidates with units, periods and findings.

Ranking puts the most direct evidence first, but it is advisory: every candidate is kept
and nothing is approved here. Order, most important first:
  1. period: this year's column (or a sentence) before last year's;
  2. the figure balances an accounting identity when exactly one combination does
     (src/identities.py);
  3. no warning;
  4. a table row before a sentence, and a primary statement before a note;
  5. a sentence that states a total ("bringing the total to 849") before one that states
     a change ("opened 123 additional"); not used for percentages;
  6. first page, row and column.
"""

from __future__ import annotations

import re
from decimal import Decimal

from src import identities
from src.config_loader import AppConfig
from src.metric_mapper import MetricMapper
from src.models import PERIOD_ORDER, AnalysisResult, Finding, MappedCandidate, MetricCandidate, MetricResult
from src.narrative_cues import cue_rank
from src.number_parser import parse_number, with_percent_word
from src.pdf_extractor import NARRATIVE_METHOD, TABLE_METHOD, ExtractionResult, PageReport, RawCandidate
from src.related_lines import related_line_findings
from src.unit_normalizer import find_unit, normalize, rounding_tolerance
from src.validator import candidate_findings, conflict_findings, not_found_finding

# Horizontal distance (points) between a value's centre and a column heading's centre.
YEAR_COLUMN_TOLERANCE = 45.0
# A heading is aligned with a value when their right edges (numbers are right-aligned) or
# centres are this close. Used to tell apart two header rows above the same numbers.
YEAR_ALIGNED = 12.0
YEAR_ROW_TOLERANCE = 3.0  # year words this close vertically form one header row
NOTE_COLUMN_TOLERANCE = 25.0
# A block of a page is a primary statement when it holds this many different metrics' rows,
# or a statement title and at least STATEMENT_TITLED_METRICS of them.
STATEMENT_MIN_METRICS = 4
STATEMENT_TITLED_METRICS = 2
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
    """Year printed above the value's column, in the value's own column block.

    The nearest header row above that is aligned with the value wins (numbers are
    right-aligned under their year). This keeps apart two header rows printed above the same
    area, e.g. last year's statement left in the PDF underneath this year's. Without an
    aligned header, the nearest year above within YEAR_COLUMN_TOLERANCE is used.
    """
    x_mid = (candidate.bbox[0] + candidate.bbox[2]) / 2
    y_mid = (candidate.bbox[1] + candidate.bbox[3]) / 2
    right = candidate.bbox[2]
    block = page.block_of(x_mid)
    above = [
        (y, year, abs(x - x_mid), abs(x1 - right))
        for year, x, y, x1 in page.year_markers
        if y < y_mid - 2 and page.block_of(x) == block and abs(x - x_mid) <= YEAR_COLUMN_TOLERANCE
    ]
    if not above:
        return None
    for row_y in sorted({y for y, *_ in above}, reverse=True):
        row = [a for a in above if abs(a[0] - row_y) <= YEAR_ROW_TOLERANCE]
        best = min(row, key=lambda a: min(a[2], a[3]))
        if min(best[2], best[3]) <= YEAR_ALIGNED:
            return best[1]
    return max(above)[1]


def is_note_reference(candidate: RawCandidate, page: PageReport) -> bool:
    """True when the value sits under a 'Note(s)' column heading (a note number, not an amount)."""
    x_mid = (candidate.bbox[0] + candidate.bbox[2]) / 2
    y_mid = (candidate.bbox[1] + candidate.bbox[3]) / 2
    block = page.block_of(x_mid)
    return any(y < y_mid and abs(x - x_mid) <= NOTE_COLUMN_TOLERANCE and page.block_of(x) == block
               for x, y in page.note_headers)


def period_of(year: int | None, end_year: int | None) -> str:
    if year is None or end_year is None:
        return "unknown"
    if year == end_year:
        return "current"
    return "prior" if year == end_year - 1 else "other"


def analyse(extraction: ExtractionResult, config: AppConfig, metric_ids: list[str], report_period: str,
            bank: str | None = None) -> AnalysisResult:
    """bank: whose learned wording to use besides mappings.yaml (D-046); None for none."""
    mapped = map_candidates(extraction, config, report_period, bank)
    names = {m.id: m.display_name for m in config.metrics}
    statements = statement_blocks(extraction, mapped)

    ranked: dict[str, list[MetricCandidate]] = {}
    per_candidate: dict[tuple[str, tuple], list[Finding]] = {}
    for metric_id in metric_ids:
        metric = config.metric(metric_id)
        candidates = [
            MetricCandidate(m, metric_id, normalize(m.parsed, m.unit, metric.expected_unit,
                                                    count_in_thousands_below=metric.count_in_thousands_below),
                            rounding_tolerance(m.parsed, m.unit))
            for m in mapped
            if any(match.metric_id == metric_id for match in m.matches)
        ]
        for mc in candidates:
            per_candidate[(metric_id, mc.mapped.key)] = candidate_findings(mc, names)
        ranked[metric_id] = sorted(candidates, key=lambda mc: _rank(mc, per_candidate[(metric_id, mc.mapped.key)],
                                                                     False, statements))

    preferred = _apply_identities(config, ranked, per_candidate, names)

    results: list[MetricResult] = []
    all_findings: list[Finding] = []
    for metric_id in metric_ids:
        metric = config.metric(metric_id)
        candidates = sorted(ranked[metric_id], key=lambda mc: _rank(
            mc, per_candidate[(metric_id, mc.mapped.key)], (metric_id, mc.mapped.key) in preferred, statements))
        # After ranking: a warning about a line beside the statement figure must not demote the figure.
        this_year = [mc for mc in candidates if mc.mapped.period == "current" and in_statement(mc, statements)]
        for f in related_line_findings(extraction, metric_id, metric.display_name, this_year):
            per_candidate[(metric_id, f.candidate_keys[0])].append(f)
        findings = [f for mc in candidates for f in per_candidate[(metric_id, mc.mapped.key)]]
        statement_keys = frozenset(mc.mapped.key for mc in candidates if in_statement(mc, statements))
        findings += conflict_findings(metric, candidates, statement_keys)
        result = MetricResult(metric, candidates, findings)
        if result.primary is None:
            result.findings.append(not_found_finding(metric))
        results.append(result)
        all_findings += result.findings
    return AnalysisResult(results, mapped, all_findings)


def _apply_identities(config: AppConfig, ranked: dict[str, list[MetricCandidate]],
                      per_candidate: dict[tuple[str, tuple], list[Finding]], names: dict[str, str]) -> set[tuple[str, tuple]]:
    """Try each sum check of checks.yaml on the analysed metrics. Adds IDENTITY findings to
    per_candidate; returns the (metric, candidate key) pairs of the one balancing set, if any."""
    preferred: set[tuple[str, tuple]] = set()
    pinned: dict[str, Decimal] = {}
    for check in config.checks:
        if not all(m in ranked for m in (check.total, *check.parts)):
            continue
        outcome = identities.solve(check, ranked, pinned)
        if outcome is None:
            continue
        for f in identities.findings(outcome, ranked, names):
            per_candidate[(f.metric_id, f.candidate_keys[0])].append(f)
        if outcome.unique is not None:
            pinned.update(outcome.unique)
            preferred |= {(m, mc.mapped.key) for m in outcome.unique for mc in ranked[m]
                          if identities.matches_solution(mc, outcome.unique)}
    return preferred


def statement_blocks(extraction: ExtractionResult, mapped: list[MappedCandidate]) -> set[tuple[int, int]]:
    """(page, column block) pairs that look like a primary statement.

    A block qualifies when its table rows match STATEMENT_MIN_METRICS different metrics
    (statements carry many headline lines; a note carries one or two), or when it has a
    statement title and at least STATEMENT_TITLED_METRICS of them.
    """
    metrics: dict[tuple[int, int], set[str]] = {}
    for m in mapped:
        if m.candidate.extraction_method == TABLE_METHOD:
            metrics.setdefault((m.candidate.page_index, m.block), set()).update(match.metric_id for match in m.matches)
    titled = {(p.page_index, p.block_of(x)) for p in extraction.pages for _, x, _ in p.statement_titles}
    return {where for where, ids in metrics.items()
            if len(ids) >= STATEMENT_MIN_METRICS or (where in titled and len(ids) >= STATEMENT_TITLED_METRICS)}


def in_statement(mc: MetricCandidate, statements: set[tuple[int, int]]) -> bool:
    """A table row of a primary statement (see statement_blocks)."""
    c = mc.mapped.candidate
    return c.extraction_method == TABLE_METHOD and (c.page_index, mc.mapped.block) in statements


def map_candidates(extraction: ExtractionResult, config: AppConfig, report_period: str,
                   bank: str | None = None) -> list[MappedCandidate]:
    mapper = MetricMapper(config.mappings_for(bank))
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
        parsed = parse_number(c.raw_value_text)
        if not is_table:
            parsed = with_percent_word(parsed, c.text_after)  # "6.6 percent" is 6.6%
        mapped.append(
            MappedCandidate(
                candidate=c,
                matches=matches,
                parsed=parsed,
                unit=find_unit(c, page),
                period_year=year,
                period=period_of(year, end_year),
                duplicate_of=None if is_table else table_locations.get((c.page_index, c.bbox)),
                in_chart_row=is_table and (c.page_index, c.row_index) in chart_rows,
                block=page.block_of((c.bbox[0] + c.bbox[2]) / 2),
            )
        )
    return mapped


def _rank(mc: MetricCandidate, findings: list[Finding], balances: bool, statements: set[tuple[int, int]]) -> tuple:
    c = mc.mapped.candidate
    narrative = c.extraction_method == NARRATIVE_METHOD
    return (
        PERIOD_ORDER[mc.mapped.period],
        not balances,
        any(f.severity in ("warning", "error") for f in findings),
        narrative,
        not in_statement(mc, statements),
        cue_rank(c) if narrative and mc.normalized.unit != "fraction" else 1,
        mc.mapped.duplicate_of is not None,
        c.page_index,
        c.row_index,
        c.column_index,
    )
