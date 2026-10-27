"""Warn when a statement prints a second line that may belong in a metric's total.

Some banks split one headline figure over two statement lines. BoA (2024/25 onwards) and
Oromia print "Deposits from customers" and "Interest free customers' deposits" separately,
and the bank's own total deposits figure is the two added together. The app never adds
lines up by itself (D-024); it points the reviewer at the other line instead, so the total
can be calculated (Calculate in Metric Details) or the single line approved knowingly.
"""

from __future__ import annotations

from src.metric_mapper import normalize_text, strip_printed_note_reference
from src.models import Finding, MetricCandidate
from src.pdf_extractor import TABLE_METHOD, ExtractionResult, RawCandidate

RULE_ID = "RELATED-LINE"
COLUMN_ALIGNMENT = 12.0  # points between right edges of two values in the same column
# metric id -> word groups; a statement row whose label contains every word of one group is
# a line that may need adding in. Seen in: BOA 2025/26 p111, BoA 2024/25 p44, Oromia p50.
RELATED_LINES: dict[str, tuple[tuple[str, ...], ...]] = {
    "total_deposits_etb": (("interest free", "deposit"), ("wadiah", "deposit")),
}


def related_line_findings(extraction: ExtractionResult, metric_id: str, metric_name: str,
                          statement_figures: list[MetricCandidate]) -> list[Finding]:
    """One warning per statement figure of the metric when its block prints a related line."""
    groups = RELATED_LINES.get(metric_id)
    if not groups:
        return []
    findings = []
    for mc in statement_figures:
        c = mc.mapped.candidate
        related = _related_rows(extraction, c, groups)
        if not related:
            continue
        shown = "; ".join(f"'{strip_printed_note_reference(r.raw_label)} {r.raw_value_text}'" for r in related)
        findings.append(Finding(
            RULE_ID, "warning",
            f"PDF page {c.page_index + 1}: the statement also prints {shown} next to this line. Check whether "
            f"{metric_name} should include it; if so, add the lines with Calculate.",
            metric_id, (mc.mapped.key,)))
    return findings


def _related_rows(extraction: ExtractionResult, figure: RawCandidate,
                  groups: tuple[tuple[str, ...], ...]) -> list[RawCandidate]:
    page = extraction.pages[figure.page_index]
    block = page.block_of((figure.bbox[0] + figure.bbox[2]) / 2)
    rows = []
    for c in extraction.candidates:
        if (c.extraction_method != TABLE_METHOD or c.page_index != figure.page_index or c.row_index == figure.row_index
                or abs(c.bbox[2] - figure.bbox[2]) > COLUMN_ALIGNMENT
                or page.block_of((c.bbox[0] + c.bbox[2]) / 2) != block):
            continue  # only the value printed in the same column of the same table
        label = normalize_text(c.raw_label)
        if any(all(word in label for word in group) for group in groups):
            rows.append(c)
    return rows
