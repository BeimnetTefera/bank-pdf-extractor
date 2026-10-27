"""Why a value is what it is: the evidence for one metric of one saved report (spec section 1).

Built only from the database and the stored PDF, never from screen state or a fresh analysis,
so the Evidence page, the evidence summary, its PDF and the Excel export all tell the same
story, and a link opened from Excel (a new session) shows exactly what was recorded.

Status: approved | pending (a correction or manual entry awaiting approval) | needs_review
(a figure was found but nobody has reviewed it) | not_found.
Source type: pdf_figure (an extracted figure) | pdf_marked_box (a box the reviewer drew or a
figure picked from All Figures) | typed (a value the reviewer typed, with its page but no box)
| calculated | external (from a document other than the report, D-055) | not_reported | none.
Every metric also lists its external cross-checks, each compared with the approved value (D-057).
Sector values (one per fiscal year for all banks, D-056) have their own evidence: sector_evidence.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pymupdf

from src.calculated_entries import CalcInput, SavedCalculation, saved_calculation, stale_reasons
from src.calculation_service import CalculatedValue, current_values
from src.candidate_queries import METHOD_LABELS
from src.config_loader import AppConfig, AppSettings
from src.document_manager import compute_sha256
from src.external_sources import CheckedCrossCheck, SavedSource, checked, source_for_decision
from src.number_parser import parse_number
from src.pdf_extractor import NARRATIVE_METHOD, strip_sentence_punctuation
from src.project_service import SavedCandidate, get_document, load_candidates, load_findings
from src.review_service import Decision, ReviewState, review_state
from src.sector_data import (
    SectorDecision,
    banks_for_year,
    fiscal_year_key,
    is_sector_metric,
    sector_state,
    state_source,
)

Box = tuple[float, float, float, float]

STATUS_LABELS = {"approved": "Approved", "pending": "Correction pending", "needs_review": "Not reviewed yet",
                 "not_found": "Not found", "not_reported": "Not reported"}
SOURCE_LABELS = {"pdf_figure": "PDF figure", "pdf_marked_box": "PDF marked box", "typed": "Typed by reviewer",
                 "calculated": "Calculated (formula)", "external": "External source", "not_reported": "Not reported",
                 "none": "No source"}
SECTOR_STATUS_LABELS = {"open": "No value yet", "pending": "Awaiting approval", "approved": "Approved",
                        "not_reported": "Not reported"}
SECTOR_ACTIONS = {"proposed": "Value from an external source", "approved": "Approved", "reopened": "Reopened"}
ACTION_LABELS = {"approved": "Approved", "rejected": "Rejected a figure", "corrected": "Corrected",
                 "manual_entry": "Entered manually", "reopened": "Reopened"}


class EvidenceError(ValueError):
    """The evidence cannot be shown; the message says why."""


@dataclass(frozen=True)
class OtherCandidate:
    candidate_id: int
    page: int  # 1-based
    printed: str
    label: str
    year_column: int | None
    method: str
    why: str  # why it is not the value used


@dataclass(frozen=True)
class HistoryEntry:
    at: str
    who: str
    action: str
    old: str  # the metric's value before this step ("" when none)
    new: str  # and after it
    reason: str


@dataclass(frozen=True)
class CheckNote:
    rule_id: str
    severity: str
    message: str


@dataclass(frozen=True)
class MetricEvidence:
    document: sqlite3.Row
    metric_id: str
    display_name: str
    definition: str
    expected_unit: str
    export_column: str | None
    status: str
    value: Decimal | None
    unit: str | None
    source_type: str
    page: int | None  # 1-based PDF page of the evidence
    box: Box | None  # the evidence box on that page, in PDF points
    printed: str  # the figure as printed, when known
    label: str  # the printed row label or the sentence around the figure
    conversion: str  # e.g. "189,512,266 (Birr'000 heading: × 1,000) = 189,512,266,000 ETB"
    sign_text: str
    approved_by: str = ""
    approved_at: str = ""
    entered_by: str = ""
    entered_at: str = ""
    approval_note: str = ""
    entry_reason: str = ""
    read_by_ocr: bool = False
    candidate_key: tuple | None = None  # the extracted figure's key, when the value is that figure
    box_found: bool = False  # the reviewer typed the value; the app found the same number on `page`
    typed_page: int | None = None  # the page saved with a typed value, when the number was found elsewhere
    calculation: SavedCalculation | None = None  # for a calculated value: formula, arithmetic, inputs
    calc_inputs: tuple[CalcInput, ...] = ()  # its inputs, with boxes found for typed ones
    stale: tuple[str, ...] = ()  # inputs that changed since the calculation
    calc_input_sources: tuple[tuple[CalcInput, SavedSource], ...] = ()  # inputs whose value is from an external source
    note_calc: NoteCalculation | None = None  # arithmetic written in the note of a typed value
    box_is_input: bool = False  # the marked box holds a figure the value was worked out from, not the value
    external: SavedSource | None = None  # for a value from an external source: title, quote, file, box
    cross_checks: tuple[CheckedCrossCheck, ...] = ()  # external figures to check the value against
    checks: list[CheckNote] = field(default_factory=list)
    other_candidates: list[OtherCandidate] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS[self.source_type]

    @property
    def value_text(self) -> str:
        return format_value(self.value, self.unit) if self.value is not None else ""

    @property
    def report_label(self) -> str:
        return f"{self.document['bank_name']} · {self.document['reporting_period']}"

    @property
    def mismatches(self) -> list[CheckedCrossCheck]:
        """Active cross-checks that disagree with the approved value."""
        return [c for c in self.cross_checks if c.comparison.status == "mismatch"]


@dataclass(frozen=True)
class SectorEvidence:
    """A sector value (one per fiscal year for every bank, D-056): its source and decisions."""

    fiscal_year: str
    metric_id: str
    display_name: str
    definition: str
    export_column: str | None
    status: str  # open | pending | approved | not_reported
    value: Decimal | None
    unit: str | None
    source: SavedSource | None
    proposal: SectorDecision | None  # the pending proposal, or the one the approval accepted
    approval: SectorDecision | None
    history: tuple[SectorDecision, ...]
    banks: tuple[str, ...]  # banks with a saved report for the year: their export rows get the value

    @property
    def status_label(self) -> str:
        return SECTOR_STATUS_LABELS[self.status]

    @property
    def value_text(self) -> str:
        return format_value(self.value, self.unit) if self.value is not None else ""

    @property
    def shown_value(self) -> Decimal | None:
        """The approved value, or the proposal's while it awaits approval."""
        if self.value is not None:
            return self.value
        return self.proposal.value if self.proposal is not None else None


# --- links ------------------------------------------------------------------------------

def evidence_url(base_url: str, report: str, metric: str | None = None) -> str:
    """Address of a metric's Evidence page, or of the report's evidence summary when metric is None."""
    if metric is None:
        return f"{base_url.rstrip('/')}/evidence_summary?{urlencode({'report': report})}"
    return f"{base_url.rstrip('/')}/evidence?{urlencode({'report': report, 'metric': metric})}"


def sector_evidence_url(base_url: str, fiscal_year: str, metric: str) -> str:
    """Address of a sector value's Evidence page, e.g. /evidence?sector=2025%2F26&metric=..."""
    return f"{base_url.rstrip('/')}/evidence?{urlencode({'sector': fiscal_year, 'metric': metric})}"


def sector_evidence(conn: sqlite3.Connection, config: AppConfig, fiscal_year: str, metric_id: str) -> SectorEvidence:
    """Evidence for a sector value of a fiscal year, built from sector_decisions and its source."""
    try:
        year = fiscal_year_key(fiscal_year)
    except ValueError as exc:
        raise EvidenceError(str(exc)) from None
    try:
        metric = config.metric(metric_id)
        name, definition, column = metric.display_name, metric.definition, metric.export_column
    except KeyError:
        name, definition, column = metric_id, "", None
    state = sector_state(conn, year, metric_id)
    if not state.history and not is_sector_metric(config, metric_id):
        raise EvidenceError(f"'{metric_id}' is not a sector metric (level: sector in config/metrics.yaml), so it has "
                            "no sector value. Open it from a report's Evidence summary instead.")
    return SectorEvidence(year, metric_id, name, definition, column, state.status, state.value, state.unit,
                          state_source(conn, state), state.proposal, state.accepted, tuple(state.history),
                          tuple(banks_for_year(conn, year)))


# --- building the evidence ----------------------------------------------------------------

def metric_evidence(conn: sqlite3.Connection, config: AppConfig, document_id: str, metric_id: str,
                    pdf: PdfWords | None = None) -> MetricEvidence:
    """pdf: the report's words; with it, a typed value gets the box of the same number on its page."""
    document = get_document(conn, document_id)
    if document is None:
        raise EvidenceError("That report is not in the database (it may have been deleted).")
    candidates = load_candidates(conn, document_id, metric_id)
    flags = {r["id"]: r for r in conn.execute(
        "SELECT id, in_chart_row, is_duplicate FROM candidates WHERE document_id = ? AND metric_id = ?",
        (document_id, metric_id))}
    state = review_state(conn, document_id, metric_id)
    by_id = {c.id: c for c in candidates}
    try:
        metric = config.metric(metric_id)
        name, definition, unit, column = metric.display_name, metric.definition, metric.expected_unit, metric.export_column
    except KeyError:  # removed from metrics.yaml after the report was saved
        name, definition, unit, column = metric_id, "", "", None

    base = dict(document=document, metric_id=metric_id, display_name=name, definition=definition,
                expected_unit=unit, export_column=column)
    used_id: int | None = None
    if state.status in ("approved", "not_reported"):
        fields = _decision_fields(state.accepted_decision, _proposal(state), by_id)
        used_id = fields.pop("_candidate_id")
        evidence_fields = dict(status=state.status, **fields)
    elif state.status == "corrected":
        fields = _decision_fields(None, state.pending, by_id)
        fields.pop("_candidate_id")
        evidence_fields = dict(status="pending", **fields)
    else:
        suggested = _suggested(candidates, state)
        if suggested is not None:
            used_id = suggested.id
            evidence_fields = dict(status="needs_review", value=suggested.normalized_value,
                                   unit=suggested.normalized_unit, **_candidate_fields(suggested))
        else:
            evidence_fields = dict(status="not_found", value=None, unit=None, source_type="none", page=None, box=None,
                                   printed="", label="", conversion="", sign_text="")
    proposal = state.pending if state.status == "corrected" else (
        _proposal(state) if state.status in ("approved", "not_reported") else None)
    if proposal is not None and proposal.entry_kind == "not_reported":
        evidence_fields |= dict(source_type="not_reported", page=None, box=None, label="", conversion="", value=None)
    if proposal is not None and proposal.entry_kind == "calculated":
        calc = saved_calculation(conn, proposal.id)
        if calc is not None:
            evidence_fields |= dict(source_type="calculated", page=None, box=None, label=calc.words,
                                    conversion=calc.arithmetic, calculation=calc,
                                    calc_inputs=tuple(_with_found_boxes(calc.inputs, pdf, document_id)),
                                    stale=tuple(stale_reasons(conn, calc)),
                                    calc_input_sources=tuple(_input_sources(conn, calc.inputs)))
    if proposal is not None and proposal.entry_kind == "external":
        source = source_for_decision(conn, proposal.id)
        if source is not None:  # the box is on the source's own page, not on the report
            evidence_fields |= dict(source_type="external", page=None, box=None, printed=source.printed_value,
                                    label=source.quote, conversion=source.conversion, external=source)
    if pdf is not None and evidence_fields["source_type"] == "typed" and evidence_fields["value"] is not None:
        notes = " ".join(evidence_fields.get(k, "") for k in ("entry_reason", "approval_note"))
        found = pdf.locate(evidence_fields["value"], evidence_fields["unit"],
                           [evidence_fields["page"], *pages_named_in(notes)])
        if found is not None:
            page, box, printed = found
            evidence_fields |= dict(box=box, box_found=True, printed=printed,
                                    typed_page=evidence_fields["page"] if page != evidence_fields["page"] else None,
                                    page=page)
        else:  # not printed: perhaps worked out, with the arithmetic written in the note
            evidence_fields["note_calc"] = note_calculation(
                notes, pdf, [*pages_named_in(notes), evidence_fields["page"]])
    if (pdf is not None and evidence_fields["source_type"] == "pdf_marked_box"
            and evidence_fields["value"] not in (None, 0)):  # a nil ("-") box cannot be looked up
        # A derived value recorded by marking one of its inputs: the box does not hold the value itself.
        if pdf.locate(evidence_fields["value"], evidence_fields["unit"], [evidence_fields["page"]]) is None:
            notes = " ".join(evidence_fields.get(k, "") for k in ("entry_reason", "approval_note"))
            evidence_fields |= dict(box_is_input=True, note_calc=note_calculation(
                notes, pdf, [*pages_named_in(notes), evidence_fields["page"]]))
    approved = evidence_fields["status"] == "approved"
    return MetricEvidence(
        **base, **evidence_fields,
        cross_checks=tuple(checked(conn, document_id, metric_id, evidence_fields["value"] if approved else None,
                                   evidence_fields["unit"] if approved else None)),
        checks=[CheckNote(f["rule_id"], f["severity"], f["message"]) for f in load_findings(conn, document_id, metric_id)
                if f["rule_id"] != "NOT-FOUND" or evidence_fields["status"] == "not_found"],
        other_candidates=_other_candidates(candidates, flags, state, used_id),
        history=_history(document, state, by_id, unit),
    )


def report_evidence(conn: sqlite3.Connection, config: AppConfig, document_id: str,
                    pdf: PdfWords | None = None) -> list[MetricEvidence]:
    """Evidence for every metric analysed in the report, in the order they were analysed."""
    document = get_document(conn, document_id)
    if document is None:
        raise EvidenceError("That report is not in the database (it may have been deleted).")
    return [metric_evidence(conn, config, document_id, m, pdf) for m in json.loads(document["metric_ids"])]


class PdfWords:
    """The words of one report's pages, read once each, to find where a typed value is printed."""

    SCALES = (Decimal(1), Decimal(1_000), Decimal(1_000_000), Decimal(1_000_000_000))

    def __init__(self, pdf_bytes: bytes):
        self._doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        self._pages: dict[int, list[tuple[Box, str]]] = {}

    def close(self) -> None:
        self._doc.close()

    def words(self, page: int) -> list[tuple[Box, str]]:
        """(box, text) of the words on a 1-based page; empty for a page outside the PDF."""
        if page not in self._pages:
            if not 1 <= page <= self._doc.page_count:
                return []
            self._pages[page] = [((w[0], w[1], w[2], w[3]), w[4]) for w in self._doc[page - 1].get_text("words")]
        return self._pages[page]

    def locate(self, value: Decimal, unit: str | None, pages: list[int | None]) -> tuple[int, Box, str] | None:
        """The first of `pages` that prints the value, as printed or in thousands, millions or
        billions (a page heading such as Birr'000), and the number's box: (page, box, printed)."""
        target = abs(value)
        for page in dict.fromkeys(p for p in pages if p):
            for box, text in self.words(page):
                parsed = parse_number(strip_sentence_punctuation(text))
                if parsed.value is None or parsed.value == 0:
                    continue
                number = abs(parsed.value)
                if unit == "fraction":
                    if parsed.is_percent and number / 100 == target:
                        return page, box, text
                elif not parsed.is_percent and any(number * s == target for s in self.SCALES):
                    return page, box, text
        return None


def _input_sources(conn: sqlite3.Connection, inputs: list[CalcInput]) -> list[tuple[CalcInput, SavedSource]]:
    """Inputs taken from an approved metric whose value came from an external source (D-055),
    each with that source: they have no page in the report, so the source is their evidence."""
    found = []
    for i in inputs:
        if i.kind not in ("metric", "previous_report") or i.source_decision_id is None:
            continue
        row = conn.execute(
            """SELECT p.id FROM review_decisions a JOIN review_decisions p ON p.id = a.approves_decision_id
               WHERE a.id = ? AND p.entry_kind = 'external'""", (i.source_decision_id,)).fetchone()
        source = source_for_decision(conn, row["id"]) if row is not None else None
        if source is not None:
            found.append((i, source))
    return found


def _with_found_boxes(inputs: list[CalcInput], pdf: PdfWords | None, document_id: str) -> list[CalcInput]:
    """A typed input (page but no box) of this report gets the box of its printed number, when found."""
    found = []
    for i in inputs:
        if pdf is not None and i.box is None and i.page and i.value is not None and i.source_document_id == document_id:
            hit = pdf.locate(i.value, i.unit, [i.page])
            if hit is not None:
                i = replace(i, box=hit[1], printed=i.printed or hit[2])
        found.append(i)
    return found


@dataclass(frozen=True)
class NoteFigure:
    label: str  # the words next to the number in the note, e.g. "weighted shares"
    printed: str  # as written in the note, e.g. "3,869,598 Birr'000"
    value: Decimal  # in full (thousands multiplied out, percentages as fractions)
    unit: str | None
    page: int | None = None  # where the number is printed, when found
    box: Box | None = None


@dataclass(frozen=True)
class NoteCalculation:
    """Arithmetic a reviewer wrote in a note, e.g. 'profit 3,869,598 Birr'000 / 9,907,298 weighted
    shares = Birr 390.58', worked out again, with each number found on the pages the note names."""

    written: str  # the arithmetic as written in the note
    arithmetic: str  # with full numbers: 3,869,598,000 / 9,907,298 = 390.5764...
    result: Decimal
    stated: Decimal
    matches: bool  # the stated result agrees within its rounding
    figures: tuple[NoteFigure, ...]


_NUM = r"\d[\d,]*(?:\.\d+)?%?"
_NOTE_TOKEN = re.compile(
    rf"\(\s*p(?:age)?\.?\s*(?P<page>\d{{1,4}})\s*\)"           # a page reference: (p45), (page 46)
    rf"|(?P<num>{_NUM})(?:\s*(?P<scale>million\b|billion\b|thousand\b|bn\b|mn\b|birr\s*['’]000|['’]000))?"
    r"|(?P<op>divided\s+by|minus|plus|times|[/x×*+−-])(?![A-Za-z])"
    r"|(?P<paren>[()])"
    r"|(?P<word>[A-Za-z][A-Za-z'’]*(?:['’]000)?)", re.IGNORECASE)
_OPS = {"x": "*", "×": "*", "*": "*", "times": "*", "/": "/", "+": "+", "plus": "+", "-": "-", "−": "-", "minus": "-"}
_SCALE_WORDS = {"thousand": Decimal(10**3), "million": Decimal(10**6), "mn": Decimal(10**6), "billion": Decimal(10**9),
                "bn": Decimal(10**9)}
_FILLER = re.compile(r"^(?:birr(?:'000)?|etb|nearly|about|approximately|approx|around|some|of|the|a|an|and|is|"
                     r"total|so|which|gives|minus|plus)$", re.IGNORECASE)


def note_calculation(note: str, pdf: PdfWords | None = None, pages: list[int | None] = ()) -> NoteCalculation | None:
    """Arithmetic written in a note ('PAT 3,872,832 (p45) / average equity (19,475,296 + 14,216,185)/2 (p46)
    = 0.2299', 'a minus b = c', 'label = a + b + c = d'), worked out again, with each figure it used found
    on its page (its own (pNN) first, then the pages the note names). None when the note has none."""
    for clause in re.split(r";|\.\s+(?=[A-Z])", note or ""):
        parts = clause.split("=")
        for i in range(len(parts) - 1, 0, -1):  # the last '=' with arithmetic before it gives the result
            stated_match = re.match(rf"\s*(?:Birr|ETB)?\s*({_NUM})", parts[i])
            if stated_match is None:
                continue
            expression = parts[i - 1].rsplit(":", 1)[-1]
            calc = _note_arithmetic(expression, stated_match.group(1), pdf, pages)
            if calc is not None:
                return calc
    return None


def _note_arithmetic(expression: str, stated_text: str, pdf: PdfWords | None,
                     pages: list[int | None]) -> NoteCalculation | None:
    from src.formula import FormulaError, parse  # local: formula is only needed for notes with arithmetic

    parts: list[str] = []  # the formula with full numbers, for the parser
    shown: list[str] = []  # the same, written for a reader
    figures: list[dict] = []
    words: list[str] = []  # words since the last number or operator: the next figure's label
    pending_pages: list[dict] = []  # figures since the last page reference
    for m in _NOTE_TOKEN.finditer(expression):
        if m.group("page"):
            for f in pending_pages:
                f["page_hint"] = int(m.group("page"))
            pending_pages = []
        elif m.group("num"):
            parsed = parse_number(m.group("num"))
            if parsed.value is None:
                return None
            value = parsed.value / 100 if parsed.is_percent else parsed.value
            if m.group("scale"):  # "2.76 million", "3,869,598 Birr'000": this figure in those units
                scale = m.group("scale").lower()
                value *= Decimal(1_000) if "000" in scale else _SCALE_WORDS[scale]
            parts.append(str(value))
            shown.append(f"{value.normalize():,f}" if not parsed.is_percent else f"{value.normalize():f}")
            constant = not m.group("scale") and "," not in m.group("num") and parsed.value < 100 and not parsed.is_percent
            if not constant:  # 2 in '/2' is a constant; figures are looked up on the page
                figure = {"label": " ".join(w for w in words if not _FILLER.match(w)), "printed": m.group(0).strip(),
                          "value": value, "unit": "fraction" if parsed.is_percent else None, "page_hint": None}
                figures.append(figure)
                pending_pages.append(figure)
            words = []
        elif m.group("op"):
            if m.group("op") in "-−" and expression[max(m.start() - 1, 0):m.start()].isalpha():
                continue  # a hyphen inside a word ("year-end"), not a minus
            op = _OPS[re.sub(r"\s+", " ", m.group("op").lower())] if not m.group("op").lower().startswith("divided") else "/"
            parts.append(op)
            shown.append({"*": "×", "/": "÷", "-": "−", "+": "+"}[op])
            if figures and not figures[-1]["label"]:  # 'N million mobile banking users +': label after the number
                figures[-1]["label"] = " ".join(w for w in words if not _FILLER.match(w))
            words = []
        elif m.group("paren"):
            parts.append(m.group("paren"))
            shown.append(m.group("paren"))
        else:
            words.append(m.group("word"))
    if figures and not figures[-1]["label"]:
        figures[-1]["label"] = " ".join(w for w in words if not _FILLER.match(w))
    _group_averages(parts, shown)
    if len(figures) < 2 or not any(p in ("+", "-", "*", "/") for p in parts):
        return None  # not a calculation: a single figure, or words only
    try:
        result = parse(" ".join(parts))[1].evaluate({})
    except FormulaError:
        return None
    stated = parse_number(stated_text).value
    if stated is None:
        return None
    decimals = len(stated_text.split(".")[1]) if "." in stated_text else 0
    matches = abs(result - stated) <= Decimal(5).scaleb(-decimals - 1)
    found = []
    for f in figures:
        hit = pdf.locate(f["value"], f["unit"], [f["page_hint"], *pages]) if pdf is not None else None
        found.append(NoteFigure(f["label"].strip(" ;,:()"), f["printed"], f["value"], f["unit"],
                                hit[0] if hit else None, hit[1] if hit else None))
    written = f"{expression.strip()} = {stated_text}"
    arithmetic = " ".join(shown).replace("( ", "(").replace(" )", ")") + \
        f" = {result.quantize(Decimal('0.000001')).normalize():,f}"
    return NoteCalculation(written, arithmetic, result, stated, matches, tuple(found))


def _group_averages(parts: list[str], shown: list[str]) -> None:
    """'a / (b + c) / 2' as reviewers write an average means a / ((b + c) / 2): when a bracketed group
    after * or / is divided by a constant, the group and the constant are one quantity. Edits in place."""
    i = 0
    while i < len(parts) - 2:
        if parts[i] == ")" and parts[i + 1] == "/" and re.fullmatch(r"\d+", parts[i + 2]):
            depth, start = 0, i
            while start >= 0:
                depth += parts[start] == ")"
                depth -= parts[start] == "("
                if depth == 0:
                    break
                start -= 1
            if start > 0 and parts[start - 1] in ("/", "*"):
                for seq in (parts, shown):
                    seq.insert(i + 3, ")")
                    seq.insert(start, "(")
                i += 2
        i += 1


def pages_named_in(text: str) -> list[int]:
    """Pages a note mentions: 'p46', 'p. 46', 'page 46', 'pages 45 and 115' -> [46, 45, 115]."""
    pages = []
    for match in re.finditer(r"\b(?:p\.?|pages?)\s*(\d{1,4})((?:\s*(?:,|and|&)\s*\d{1,4})*)", text, re.IGNORECASE):
        pages.append(int(match.group(1)))
        pages += [int(n) for n in re.findall(r"\d{1,4}", match.group(2))]
    return pages


def calculated_columns(conn: sqlite3.Connection, document_id: str) -> dict[str, CalculatedValue]:
    """Values an analyst calculated and typed on the Calculations page (decision D-037)."""
    return current_values(conn, document_id)


def load_report_pdf(settings: AppSettings, document: sqlite3.Row) -> bytes:
    """The stored PDF of a report, after checking it is the file that was saved."""
    path = Path(settings.data_dir) / document["stored_path"]
    if not path.is_file():
        raise EvidenceError(f"The stored PDF is missing: {path}")
    data = path.read_bytes()
    if compute_sha256(data) != document["sha256"]:
        raise EvidenceError("The stored PDF does not match its saved fingerprint (it was changed or damaged).")
    return data


# --- formatting -----------------------------------------------------------------------------

def format_value(value: Decimal, unit: str | None) -> str:
    """The value as stored, in words a reader expects: full Birr, a count, or a fraction with its percentage."""
    if unit == "fraction":
        return f"{value.normalize():f} ({(value * 100).normalize():f}%)"
    if unit == "ETB":
        return f"{value:,f} ETB"
    return f"{value:,f}"


# --- helpers ----------------------------------------------------------------------------------

def _proposal(state: ReviewState) -> Decision | None:
    approval = state.accepted_decision
    if approval is None or approval.approves_decision_id is None:
        return None
    return next((d for d in state.history if d.id == approval.approves_decision_id), None)


def _decision_fields(approval: Decision | None, proposal: Decision | None,
                     by_id: dict[int, SavedCandidate]) -> dict:
    """Value and source of an approval (approval set) or of a correction awaiting approval (proposal only)."""
    decision = approval or proposal
    candidate = by_id.get(decision.candidate_id) if decision.candidate_id is not None else None
    human = proposal is not None
    fields = dict(value=decision.reviewed_value, unit=decision.reviewed_unit, sign_text="", read_by_ocr=False,
                  _candidate_id=None)
    if decision.source_bbox is not None:  # a box the reviewer drew, or a figure picked from All Figures
        fields |= dict(source_type="pdf_marked_box", page=decision.source_page, box=decision.source_bbox,
                       printed="", label="", conversion="")
    elif not human and candidate is not None:  # the extracted figure itself
        fields |= _candidate_fields(candidate)
        fields["_candidate_id"] = candidate.id
        fields["sign_text"] = _sign_text(approval, candidate)
    else:  # typed by the reviewer
        page = decision.source_page or (candidate.page_index + 1 if candidate is not None else None)
        replaced = (f"Replaces the extracted figure {candidate.raw_value_text} on PDF page {candidate.page_index + 1}."
                    if candidate is not None and proposal is not None and proposal.decision == "corrected" else "")
        fields |= dict(source_type="typed", page=page, box=None, printed="", label=replaced, conversion="")
    if human:  # a value typed (or taken from an external source) in brackets: the reviewer's choice (D-068)
        fields["sign_text"] = _typed_sign_text(proposal)
    if approval is not None:
        fields |= dict(approved_by=approval.reviewer, approved_at=approval.created_at, approval_note=approval.reason)
    if proposal is not None:
        fields |= dict(entered_by=proposal.reviewer, entered_at=proposal.created_at, entry_reason=proposal.reason)
    return fields


def _candidate_fields(c: SavedCandidate) -> dict:
    label = (f"…{c.text_before} [{c.raw_value_text}] {c.text_after}…" if c.extraction_method == NARRATIVE_METHOD
             else c.raw_label)
    if c.normalized_value is None:
        conversion = f"{c.raw_value_text}: not converted ({c.normalization_issue or 'no value'})"
    else:
        conversion = f"{c.raw_value_text} ({c.conversion_basis}) = {format_value(c.normalized_value, c.normalized_unit)}"
    return dict(source_type="pdf_figure", page=c.page_index + 1, box=c.bbox, printed=c.raw_value_text, label=label,
                conversion=conversion, sign_text="", read_by_ocr=c.read_by_ocr, candidate_key=c.key)


def _sign_text(approval: Decision, c: SavedCandidate) -> str:
    if c.normalized_value is None or c.normalized_value >= 0:
        return ""
    if approval.sign_choice == "positive":
        return "Printed in brackets (negative); recorded as a positive amount, as the reviewer chose."
    return "Printed in brackets; recorded as printed (negative), as the reviewer chose."


def _typed_sign_text(proposal: Decision) -> str:
    """How a value entered as negative was recorded, as the reviewer chose; "" when it was not negative."""
    where = "Printed in brackets in the external source" if proposal.entry_kind == "external" else "Typed in brackets"
    if proposal.sign_choice == "positive":
        return f"{where} (negative); recorded as a positive amount, as the reviewer chose."
    if proposal.sign_choice == "as_printed":
        return f"{where} (negative); kept negative, as the reviewer chose."
    return ""


def _suggested(candidates: list[SavedCandidate], state: ReviewState) -> SavedCandidate | None:
    """The figure the app suggests: the first saved this-year (or year-unknown) figure not rejected."""
    return next((c for c in candidates if c.period in ("current", "unknown") and c.id not in state.rejected_candidate_ids),
                None)


def _other_candidates(candidates: list[SavedCandidate], flags: dict, state: ReviewState,
                      used_id: int | None) -> list[OtherCandidate]:
    reasons = {d.candidate_id: d.reason for d in state.history if d.decision == "rejected"}
    others = []
    for c in candidates:
        if c.id == used_id:
            continue
        if c.id in state.rejected_candidate_ids:
            why = f"Rejected: {reasons.get(c.id) or 'no reason given'}"
        elif c.period == "prior":
            why = f"Prior-year column ({c.period_year})"
        elif c.period == "other":
            why = f"Another year's column ({c.period_year})"
        elif flags.get(c.id) is not None and flags[c.id]["in_chart_row"]:
            why = "Chart row: label and value may not belong together"
        elif flags.get(c.id) is not None and flags[c.id]["is_duplicate"]:
            why = "The same printed number as a table row"
        elif c.normalized_value is None:
            why = f"Not convertible: {c.normalization_issue or 'no value'}"
        else:
            why = "Alternative, not chosen"
        label = (f"…{c.text_before[-60:]} [{c.raw_value_text}] {c.text_after[:60]}…"
                 if c.extraction_method == NARRATIVE_METHOD else c.raw_label)
        others.append(OtherCandidate(c.id, c.page_index + 1, c.raw_value_text, label, c.period_year,
                                     METHOD_LABELS.get(c.extraction_method, c.extraction_method)
                                     + (" · OCR" if c.read_by_ocr else ""), why))
    return others


def _history(document: sqlite3.Row, state: ReviewState, by_id: dict[int, SavedCandidate], unit: str) -> list[HistoryEntry]:
    """Every step, oldest first, with the metric's value before and after it."""
    def text(value: Decimal | None, value_unit: str | None) -> str:
        return format_value(value, value_unit or unit) if value is not None else ""

    entries = [HistoryEntry(document["uploaded_at"], "extractor", "Figures extracted and saved with the report", "",
                            "", f"{len(by_id)} candidate figure(s) for this metric")]
    # The same transitions as review_service.review_state, keeping the value as text.
    accepted = pending = ""
    accepted_from = pending_from = None  # candidate the accepted / pending value belongs to
    for d in state.history:
        before = accepted or pending
        action = ACTION_LABELS[d.decision]
        if d.decision == "approved":
            action = "Approved the correction" if d.approves_decision_id is not None else "Approved the extracted figure"
            if d.approves_decision_id is not None and d.reviewed_value is None:
                action, accepted = "Approved: not reported", "Not reported"
            else:
                accepted = text(d.reviewed_value, d.reviewed_unit)
            accepted_from, pending, pending_from = d.candidate_id, "", None
        elif d.decision in ("corrected", "manual_entry"):
            if d.entry_kind == "calculated":
                action = "Calculated with a formula"
            elif d.entry_kind == "not_reported":
                action = "Marked Not reported"
            elif d.entry_kind == "external":
                action = "Value from an external source"
            shown = text(d.reviewed_value, d.reviewed_unit) if d.reviewed_value is not None else "Not reported"
            pending, pending_from = shown + " (awaiting approval)", d.candidate_id
            accepted, accepted_from = "", None
        elif d.decision == "rejected":
            c = by_id.get(d.candidate_id)
            if c is not None:
                action = f"Rejected the figure {c.raw_value_text} on PDF page {c.page_index + 1}"
            if accepted and accepted_from == d.candidate_id:
                accepted, accepted_from = "", None
            if pending and pending_from == d.candidate_id:
                pending, pending_from = "", None
        elif d.decision == "reopened":
            accepted = pending = ""
            accepted_from = pending_from = None
        where = f" (PDF page {d.source_page}, box marked)" if d.source_bbox is not None else (
            f" (PDF page {d.source_page})" if d.source_page else "")
        entries.append(HistoryEntry(d.created_at, d.reviewer, action + where, before, accepted or pending, d.reason))
    return entries
