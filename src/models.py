"""Records produced by mapping and validation. Raw candidates are wrapped, never changed."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from src.candidate_queries import CandidateKey, candidate_key
from src.config_loader import MetricDefinition
from src.metric_mapper import MetricMatch
from src.number_parser import ParsedNumber
from src.pdf_extractor import RawCandidate
from src.unit_normalizer import Normalized, UnitEvidence

# Periods relative to the report year: "current" (e.g. 2026 for 2025/26), "prior",
# "other" (a different year) or "unknown" (no column year found, or a sentence).
PERIOD_ORDER = {"current": 0, "unknown": 1, "prior": 2, "other": 3}

SEVERITIES = ("info", "warning", "error")
# Warnings from these rules flag the whole metric, whichever candidate they mention.
METRIC_LEVEL_RULES = ("CONFLICT", "MAP-AMBIGUOUS")


@dataclass(frozen=True)
class MappedCandidate:
    candidate: RawCandidate  # unchanged extraction evidence
    matches: tuple[MetricMatch, ...]
    parsed: ParsedNumber
    unit: UnitEvidence
    period_year: int | None
    period: str
    duplicate_of: CandidateKey | None = None  # same printed number already caught by a table row
    in_chart_row: bool = False  # table row that also holds a year: label/value pairing unreliable

    @property
    def key(self) -> CandidateKey:
        return candidate_key(self.candidate)

    @property
    def is_ambiguous(self) -> bool:
        return len(self.matches) > 1


@dataclass(frozen=True)
class MetricCandidate:
    """A mapped candidate seen from one metric, with the value converted to its unit."""

    mapped: MappedCandidate
    metric_id: str
    normalized: Normalized
    tolerance: Decimal  # rounding tolerance of the printed value, in normalized units


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str  # info | warning | error
    message: str
    metric_id: str | None = None
    candidate_keys: tuple[CandidateKey, ...] = ()


@dataclass
class MetricResult:
    metric: MetricDefinition
    candidates: list[MetricCandidate]  # ranked: advisory order, not a decision
    findings: list[Finding] = field(default_factory=list)

    @property
    def current_candidates(self) -> list[MetricCandidate]:
        return [c for c in self.candidates if c.mapped.period in ("current", "unknown")]

    @property
    def primary(self) -> MetricCandidate | None:
        current = self.current_candidates
        return current[0] if current else None

    @property
    def status(self) -> str:
        """'not_found', 'flagged' or 'needs_review'. Never 'approved': that needs a reviewer.

        Flagged when figures conflict, wording is ambiguous, or the suggested (first)
        candidate has a warning. Issues on weaker alternatives are shown with those
        alternatives but do not flag the metric.
        """
        primary = self.primary
        if primary is None:
            return "not_found"
        for f in self.findings:
            if f.severity not in ("warning", "error"):
                continue
            if f.rule_id in METRIC_LEVEL_RULES or not f.candidate_keys or primary.mapped.key in f.candidate_keys:
                return "flagged"
        return "needs_review"

    def findings_for(self, key: CandidateKey) -> list[Finding]:
        return [f for f in self.findings if key in f.candidate_keys]


@dataclass
class AnalysisResult:
    metrics: list[MetricResult]
    mapped: list[MappedCandidate]
    findings: list[Finding]

    def metric(self, metric_id: str) -> MetricResult:
        return next(m for m in self.metrics if m.metric.id == metric_id)
