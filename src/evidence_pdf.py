"""The evidence summary as a PDF to hand to someone else (spec section 1), built with PyMuPDF.

One table per report (metric, status, value, source, page, link to its Evidence page), then a
section per recorded value with the close-up of its source line and the reviewer's note in full.
"""

from __future__ import annotations

import html
from collections.abc import Callable

import pymupdf

from src.calculated_entries import number_text
from src.calculation_service import CalculatedValue
from src.evidence import MetricEvidence, evidence_url

PAGE = pymupdf.paper_rect("a4-l")
MARGIN = 36
CSS = """
* { font-family: sans-serif; }
body { font-size: 9pt; color: #0f172a; }
h1 { font-size: 16pt; margin: 0 0 4pt 0; }
h2 { font-size: 12pt; margin: 14pt 0 4pt 0; }
h3 { font-size: 10pt; margin: 10pt 0 2pt 0; }
p { margin: 2pt 0; }
.muted { color: #475569; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 0.5pt solid #cbd5e1; padding: 2pt 4pt; text-align: left; vertical-align: top; }
th { background-color: #e2e8f0; }
blockquote { margin: 2pt 0 2pt 12pt; color: #334155; font-style: italic; }
"""


def summary_pdf(reports: list[tuple[list[MetricEvidence], dict[str, CalculatedValue]]], base_url: str, title: str,
                generated: str, crop: Callable[[MetricEvidence], bytes | None] | None = None) -> bytes:
    """reports: (evidence of each metric, analyst-calculated columns) per report.
    crop(evidence) returns the PNG close-up of the value's source line (for an external source, of
    its uploaded page), or None."""
    archive = pymupdf.Archive()
    parts = [f"<h1>{_e(title)}</h1><p class='muted'>Generated {_e(generated)} UTC. Values in full Birr; fractions "
             "as decimals (0.29 = 29%). Each Evidence link opens where the app runs at "
             f"{_e(base_url)}.</p>"]
    for index, (evidences, calculated) in enumerate(reports):
        if not evidences:
            continue
        doc = evidences[0].document
        parts.append(f"<h2>{_e(evidences[0].report_label)}</h2><p class='muted'>{_e(doc['original_filename'])} · "
                     f"saved {_e(doc['uploaded_at'][:16].replace('T', ' '))} UTC</p>")
        parts.append(_summary_table(evidences, base_url))
        if calculated:
            parts.append(_calculated_table(calculated))
        for e in evidences:
            if e.status in ("approved", "pending", "not_reported") or e.cross_checks:
                parts.append(_section(e, base_url, archive, crop, f"r{index}_{e.metric_id}"))
    return _render("".join(parts), archive)


def _summary_table(evidences: list[MetricEvidence], base_url: str) -> str:
    rows = "".join(
        f"<tr><td>{_e(e.display_name)}</td><td>{_e(e.status_label)}</td><td>{_e(e.value_text)}</td>"
        f"<td>{_e(e.source_label)}</td><td>{e.page or ''}</td>"
        f"<td><a href='{_e(evidence_url(base_url, e.document['id'], e.metric_id))}'>Evidence</a></td></tr>"
        for e in evidences)
    return ("<table><tr><th>Metric</th><th>Status</th><th>Value</th><th>Source</th><th>Page</th><th>Link</th></tr>"
            f"{rows}</table>")


def _calculated_table(calculated: dict[str, CalculatedValue]) -> str:
    rows = "".join(f"<tr><td>{_e(c)}</td><td>{v.value:f}</td><td>{_e(v.formula_note)}</td><td>{_e(v.reviewer)}</td>"
                   f"<td>{_e(v.created_at[:16].replace('T', ' '))}</td></tr>" for c, v in calculated.items())
    return ("<h3>Calculated by an analyst (Calculations page)</h3><table><tr><th>Column</th><th>Value</th>"
            f"<th>Formula note</th><th>By</th><th>When (UTC)</th></tr>{rows}</table>")


def _section(e: MetricEvidence, base_url: str, archive: pymupdf.Archive,
             crop: Callable[[MetricEvidence], bytes | None] | None, name: str) -> str:
    lines = [f"<h3>{_e(e.display_name)}: {_e(e.value_text)} ({_e(e.status_label)})</h3>",
             f"<p>Source: {_e(e.source_label)}" + (f", PDF page {e.page}" if e.page else "") + "</p>"]
    if e.calculation is not None:
        lines.append(f"<p>Formula: {_e(e.display_name)} = {_e(e.calculation.words)}</p>"
                     f"<p>Arithmetic: {_e(e.calculation.arithmetic)}</p>")
        lines.append("<table><tr><th>Input</th><th>Value</th><th>Where it is</th></tr>" + "".join(
            f"<tr><td>{_e(i.label)}</td><td>{_e(number_text(i.value, i.unit))}</td><td>{_e(i.where)}</td></tr>"
            for i in e.calc_inputs) + "</table>")
        if e.stale:
            lines.append(f"<p><b>Stale:</b> {_e(' '.join(e.stale))}</p>")
    elif e.external is not None:
        s = e.external
        lines.append(f"<p>Document: <b>{_e(s.title)}</b>" + (f" · {_e(s.publisher)}" if s.publisher else "")
                     + (f" · page {s.page}" + (f" of {_e(s.file_name)}" if s.file_name else "") if s.page else "")
                     + "</p>")
        if s.url:
            lines.append(f"<p>Address (as recorded, not opened by the app): {_e(s.url)}</p>")
        lines.append(f"<blockquote>&ldquo;{_e(s.quote)}&rdquo;</blockquote>")
        lines.append(f"<p>As printed: {_e(s.conversion)}</p>")
    else:
        if e.conversion:
            lines.append(f"<p>As printed: {_e(e.conversion)}</p>")
        if e.label:
            lines.append(f"<p>Label: {_e(e.label)}</p>")
        if e.note_calc is not None:
            check = "agrees with" if e.note_calc.matches else "does NOT agree with"
            lines.append(f"<p>Calculation written in the note: {_e(e.note_calc.arithmetic)} ({check} the stated "
                         f"{e.note_calc.stated:f})</p>")
            lines.append("<table><tr><th>Figure</th><th>Value</th><th>Printed on</th></tr>" + "".join(
                f"<tr><td>{_e(f.label or f.printed)}</td><td>{_e(number_text(f.value, f.unit))}</td>"
                f"<td>{'PDF page ' + str(f.page) if f.page else 'not found'}</td></tr>" for f in e.note_calc.figures)
                + "</table>")
    if e.sign_text:
        lines.append(f"<p>{_e(e.sign_text)}</p>")
    if e.entry_reason:
        lines.append(f"<p>Reason ({_e(e.entered_by)}, {_e(e.entered_at[:16].replace('T', ' '))}): {_e(e.entry_reason)}</p>")
    if e.approved_by:
        note = f": {_e(e.approval_note)}" if e.approval_note else ""
        lines.append(f"<p>Approved by {_e(e.approved_by)}, {_e(e.approved_at[:16].replace('T', ' '))} UTC{note}</p>")
    has_box = e.box is not None or (e.external is not None and e.external.box is not None)
    image = crop(e) if crop is not None and has_box else None
    if image:
        archive.add(image, f"{name}.png")
        lines.append(f"<img src='{name}.png' width='640'/>")
    if e.cross_checks:
        lines.append("<p><b>Cross-checks</b></p><table><tr><th>Result</th><th>Source</th><th>Quote</th>"
                     "<th>Comparison</th></tr>" + "".join(
                         f"<tr><td>{'Withdrawn' if not c.check.active else _e(c.comparison.label)}</td>"
                         f"<td>{_e(c.check.source.citation)}</td><td>{_e(c.check.source.quote)}</td>"
                         f"<td>{_e(c.comparison.text if c.check.active else c.check.withdrawal.reason)}</td></tr>"
                         for c in e.cross_checks) + "</table>")
    lines.append(f"<p><a href='{_e(evidence_url(base_url, e.document['id'], e.metric_id))}'>Open the Evidence page</a></p>")
    return "".join(lines)


def _render(body: str, archive: pymupdf.Archive) -> bytes:
    story = pymupdf.Story(html=f"<body>{body}</body>", user_css=CSS, archive=archive)
    where = PAGE + (MARGIN, MARGIN, -MARGIN, -MARGIN)
    # write_with_links keeps the <a href> links clickable (a plain DocumentWriter drops them).
    with story.write_with_links(lambda rect_num, filled: (PAGE, where, None)) as doc:
        return doc.tobytes(garbage=3, deflate=True)


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)
