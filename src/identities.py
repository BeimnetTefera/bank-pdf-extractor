"""Use accounting identities to choose between candidate figures (advisory, D-007).

The sum checks of config/checks.yaml (total assets = total liabilities + total equity;
profit before tax = profit after tax + income tax) are tried with every combination of the
this-year statement figures found for their metrics. When exactly one combination of values
balances, those figures are suggested first and the other figures get a warning for breaking
the identity. When nothing balances, the suggested figures get an info note (the cross-check
panel of D-045 shows the failed check too). Nothing is approved, changed or discarded here:
a reviewer still decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import product

from src.config_loader import SumCheck
from src.models import Finding, MetricCandidate
from src.pdf_extractor import TABLE_METHOD

RULE_ID = "IDENTITY"
MAX_VALUES_PER_METRIC = 8  # distinct values tried per metric (best ranked first)


@dataclass(frozen=True)
class IdentityResult:
    check: SumCheck
    solutions: tuple[dict[str, Decimal], ...]  # distinct value sets that balance (sizes, signs ignored)

    @property
    def unique(self) -> dict[str, Decimal] | None:
        return self.solutions[0] if len(self.solutions) == 1 else None


def eligible(mc: MetricCandidate) -> bool:
    """A this-year table figure with a converted value, not a chart row."""
    m = mc.mapped
    return (m.candidate.extraction_method == TABLE_METHOD and m.period == "current"
            and mc.normalized.value is not None and not m.in_chart_row)


def solve(check: SumCheck, ranked: dict[str, list[MetricCandidate]],
          pinned: dict[str, Decimal] | None = None) -> IdentityResult | None:
    """Every distinct set of values that balances; None when a metric of the check has no figure.

    ranked: each metric's candidates in ranked order. pinned: values already chosen by an
    earlier check, kept fixed here.
    """
    pinned = pinned or {}
    metric_ids = (check.total, *check.parts)
    options: dict[str, list[MetricCandidate]] = {}
    for metric_id in metric_ids:
        figures = [mc for mc in ranked.get(metric_id, []) if eligible(mc)]
        if metric_id in pinned:
            figures = [mc for mc in figures if _same(abs(mc.normalized.value), pinned[metric_id], mc.tolerance)]
        options[metric_id] = _distinct(figures)[:MAX_VALUES_PER_METRIC]
        if not options[metric_id]:
            return None
    solutions: list[dict[str, Decimal]] = []
    for combo in product(*(options[m] for m in metric_ids)):
        total, parts = combo[0], combo[1:]
        if abs(abs(total.normalized.value) - sum(abs(p.normalized.value) for p in parts)) <= allowed_difference(combo):
            values = {m: abs(mc.normalized.value) for m, mc in zip(metric_ids, combo)}
            if values not in solutions:
                solutions.append(values)
    return IdentityResult(check, tuple(solutions))


def allowed_difference(figures: tuple[MetricCandidate, ...]) -> Decimal:
    """The printed rounding of each figure (half its last digit) plus one more unit of the
    coarsest last digit: 2.5 thousand Birr for three figures printed in Birr'000."""
    return sum((mc.tolerance for mc in figures), Decimal(0)) + 2 * max(mc.tolerance for mc in figures)


def matches_solution(mc: MetricCandidate, solution: dict[str, Decimal]) -> bool:
    value = solution.get(mc.metric_id)
    return value is not None and eligible(mc) and _same(abs(mc.normalized.value), value, mc.tolerance)


def findings(result: IdentityResult, ranked: dict[str, list[MetricCandidate]], names: dict[str, str]) -> list[Finding]:
    """Warnings for figures that break the identity (unique solution), or info notes on the
    suggested figures when no combination balances."""
    check = result.check
    unique = result.unique
    out: list[Finding] = []
    if unique is not None:
        balance = _describe(check, unique, names)
        for metric_id in (check.total, *check.parts):
            for mc in ranked.get(metric_id, []):
                if eligible(mc) and not matches_solution(mc, unique):
                    c = mc.mapped.candidate
                    out.append(Finding(
                        RULE_ID, "warning",
                        f"PDF page {c.page_index + 1}: '{check.name}' does not balance with this figure "
                        f"({c.raw_value_text}). It balances only with {balance}.",
                        metric_id, (mc.mapped.key,)))
    elif not result.solutions:
        # Info only: the cross-check panel (D-045) already shows the failed check for the suggested figures.
        tops = {m: next((mc for mc in ranked.get(m, []) if eligible(mc)), None) for m in (check.total, *check.parts)}
        if all(tops.values()):
            shown = "; ".join(f"{names.get(m, m)} {mc.mapped.candidate.raw_value_text} "
                              f"(p{mc.mapped.candidate.page_index + 1})" for m, mc in tops.items())
            for metric_id, mc in tops.items():
                out.append(Finding(
                    RULE_ID, "info",
                    f"'{check.name}' does not balance with any of this year's statement figures found "
                    f"(suggested: {shown}). One of them is probably wrong or from another table.",
                    metric_id, (mc.mapped.key,)))
    return out


def _describe(check: SumCheck, values: dict[str, Decimal], names: dict[str, str]) -> str:
    parts = " + ".join(f"{names.get(m, m)} {values[m]:,.0f}" for m in check.parts)
    return f"{names.get(check.total, check.total)} {values[check.total]:,.0f} = {parts} (Birr)"


def _distinct(figures: list[MetricCandidate]) -> list[MetricCandidate]:
    kept: list[MetricCandidate] = []
    for mc in figures:
        if not any(_same(abs(mc.normalized.value), abs(k.normalized.value), max(mc.tolerance, k.tolerance)) for k in kept):
            kept.append(mc)
    return kept


def _same(a: Decimal, b: Decimal, tolerance: Decimal) -> bool:
    return abs(a - b) <= tolerance
