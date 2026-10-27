"""External sources (spec section 2c, D-055) and external cross-checks (spec section 6, D-057).

An external source is a document other than the bank's report that gives a figure: a National
Bank of Ethiopia publication, a press release, a regulator's table. It is recorded with its
title, publisher, address (URL, stored as text: the app never opens it, D-006) and/or an
uploaded copy (PDF or image, validated, stored by SHA-256 under data_dir/sources/), the page of
that copy (required with a file) and an optional box on it, the exact quote, and the value as
printed with its scale. The value is converted to the metric's unit and the conversion is shown
("1,234.5 billion Birr = 1,234,500,000,000 ETB").

Uses:
  record_external_value  a report metric's value from an external source: a proposal
                         (manual_entry, entry_kind 'external') with its source row, saved in one
                         transaction and approved like a correction (D-030)
  add_cross_check        an external figure the approved value is compared with (advisory only);
                         withdrawn with a reason, never deleted
Sector values (src/sector_data.py) use the same sources with no report.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePath

import pymupdf

from src.config_loader import AppSettings
from src.database import utc_now
from src.document_manager import DocumentError, compute_sha256, validate_pdf
from src.value_input import SCALE_WORDS, split_typed
from src.pdf_render import render_crop_png, render_page_png
from src.project_service import get_document
from src.review_service import ReviewError, extraction_status, insert_decision

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class Scale:
    label: str  # as offered to the reviewer
    factor: Decimal  # printed number x factor = value in the metric's unit
    units: tuple[str, ...]  # metric units it can be used for


SCALES = {
    "birr": Scale("Birr", Decimal(1), ("ETB",)),
    "birr_thousand": Scale("Birr'000 (thousands)", Decimal(1_000), ("ETB",)),
    "million": Scale("million", Decimal(1_000_000), ("ETB", "count")),
    "billion": Scale("billion", Decimal(1_000_000_000), ("ETB", "count")),
    "count": Scale("count", Decimal(1), ("count",)),
    "percent": Scale("% (percent)", Decimal("0.01"), ("fraction",)),
}
MEDIA_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
EXTENSIONS = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}
_IMAGE_MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
MATCH_TOLERANCE = Decimal("0.005")  # 0.5% of the approved value
THOUSANDS_ROUNDING = Decimal(2_000)  # a Birr'000 figure may differ by rounding of up to 2 thousand


# --- scales and conversion -------------------------------------------------------------------

def scales_for(unit: str | None) -> list[str]:
    """The scales a figure in this unit can be printed in (Birr, Birr'000, million, ... for ETB)."""
    return [key for key, scale in SCALES.items() if unit in scale.units]


def convert(printed: str, scale: str, unit: str) -> Decimal:
    """The printed number in the metric's unit: '1,234.5' in billion -> 1234500000000 (ETB)."""
    if scale not in SCALES:
        raise ReviewError(f"Choose the scale the figure is printed in ({', '.join(SCALES)}).")
    if unit not in SCALES[scale].units:
        allowed = ", ".join(SCALES[s].label for s in scales_for(unit)) or "none (this metric cannot be entered)"
        raise ReviewError(f"A figure in {SCALES[scale].label} cannot be stored as {unit}. Choose one of: {allowed}.")
    typed = split_typed(printed)  # the shared reader of typed values (D-067)
    if typed.is_percent and scale != "percent":
        raise ReviewError(f"'{printed}' is a percentage: choose % as the scale (only for a percentage metric).")
    if typed.number is None:
        raise ReviewError(f"'{printed}' is not a number. Type it as printed, e.g. 1,234.5 or (12.3).")
    factor = SCALES[scale].factor
    if typed.scale_word:
        if factor != 1 or unit not in ("ETB", "count"):
            raise ReviewError(f"'{printed}' has a scale word and the scale is {SCALES[scale].label}: give the scale "
                              "once, either in the value or under Printed in.")
        factor = SCALE_WORDS[typed.scale_word]
    value = typed.number * factor
    if unit == "count" and value != value.to_integral_value():
        raise ReviewError(f"{printed} {SCALES[scale].label} is not a whole number, so it cannot be a count.")
    return _exact(value)


def conversion_text(printed: str, scale: str, value: Decimal, unit: str) -> str:
    """'1,234.5 billion Birr = 1,234,500,000,000 ETB', '12.5% = 0.125 (12.5%)', '4.5 million = 4,500,000'."""
    shown = " ".join((printed or "").split())
    if scale == "percent":
        left = shown if shown.endswith("%") else f"{shown}%"
        return (f"{left} = {value.normalize():f} ({(value * 100).normalize():f}%)"
                + _made_positive_note(printed, value))
    words = {"birr": "Birr", "birr_thousand": "Birr'000", "count": ""}.get(scale)
    if words is None:  # million / billion
        words = f"{scale} Birr" if unit == "ETB" else scale
    right = f"{value:,f} ETB" if unit == "ETB" else f"{value:,f}"
    return f"{shown} {words} = {right}".replace("  ", " ") + _made_positive_note(printed, value)


def signed(value: Decimal, sign: str | None) -> Decimal:
    """The converted value with the reviewer's sign choice (D-068): 'positive' records a figure
    printed in brackets as a positive amount; otherwise it stays as printed."""
    return abs(value) if sign == "positive" else value


def sign_choice(printed: str, sign: str | None) -> str | None:
    """The direction kept with the value (review_decisions.sign_choice), as for a typed value:
    'positive' or 'as_printed' when the figure is printed negative, None when it is not."""
    typed = split_typed(printed)
    if typed.number is None or typed.number >= 0:
        return None
    return "positive" if sign == "positive" else "as_printed"


def _made_positive_note(printed: str, value: Decimal) -> str:
    typed = split_typed(printed)
    return " (recorded as a positive amount)" if typed.number is not None and typed.number < 0 <= value else ""


def _exact(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize()


# --- uploaded files ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceFile:
    sha256: str
    name: str  # the uploaded file's name, metadata only (never used as a path)
    media_type: str
    page_count: int


def validate_source_file(data: bytes, filename: str, settings: AppSettings) -> SourceFile:
    """Check an uploaded source copy: a readable PDF, PNG or JPEG within the size limits."""
    name = PurePath(filename or "").name
    media_type = MEDIA_TYPES.get(PurePath(name).suffix.lower())
    if media_type is None:
        raise ReviewError(f"'{name}' is not a PDF or an image. Upload a .pdf, .png or .jpg file.")
    if media_type == "application/pdf":
        try:
            pages = validate_pdf(data, name, settings)
        except DocumentError as exc:
            raise ReviewError(str(exc)) from None
        return SourceFile(compute_sha256(data), name, media_type, pages)
    if not data:
        raise ReviewError(f"'{name}' is empty.")
    if len(data) / (1024 * 1024) > settings.max_upload_mb:
        raise ReviewError(f"'{name}' is above the {settings.max_upload_mb} MB limit (max_upload_mb in settings.yaml).")
    if not data.startswith(_IMAGE_MAGIC[media_type]):
        raise ReviewError(f"'{name}' has an image extension, but its content is not a {EXTENSIONS[media_type].upper()} "
                          "image.")
    try:
        with pymupdf.open(stream=data, filetype=EXTENSIONS[media_type]) as doc:
            pages = doc.page_count
    except Exception as exc:  # PyMuPDF raises several error types for damaged files
        raise ReviewError(f"'{name}' could not be opened; the image may be damaged. ({exc})") from None
    return SourceFile(compute_sha256(data), name, media_type, pages)


def source_path(data_dir: Path, sha256: str, media_type: str) -> Path:
    return Path(data_dir) / "sources" / f"{sha256}.{EXTENSIONS[media_type]}"


def store_source_file(data: bytes, filename: str, settings: AppSettings) -> SourceFile:
    """Validate the file and keep a copy named by its SHA-256 (an identical copy is written once)."""
    file = validate_source_file(data, filename, settings)
    target = source_path(settings.data_dir, file.sha256, file.media_type)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    return file


def load_source_file(data_dir: Path, sha256: str, media_type: str) -> bytes:
    """The stored copy, after checking it is the file that was recorded."""
    path = source_path(data_dir, sha256, media_type)
    if not path.is_file():
        raise ReviewError(f"The uploaded source file is missing: {path}")
    data = path.read_bytes()
    if compute_sha256(data) != sha256:
        raise ReviewError("The uploaded source file does not match its saved fingerprint (it was changed or damaged).")
    return data


def page_pdf(data: bytes, media_type: str) -> bytes:
    """The file as PDF bytes, to render a page and draw a box: an image becomes a one-page PDF."""
    if media_type == "application/pdf":
        return data
    with pymupdf.open(stream=data, filetype=EXTENSIONS[media_type]) as image:
        return image.convert_to_pdf()


def page_size(pdf_bytes: bytes, page: int) -> tuple[float, float]:
    """Width and height in points of a 1-based page."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        rect = doc[page - 1].rect
        return rect.width, rect.height


# --- the source as entered and as saved ---------------------------------------------------------

@dataclass(frozen=True)
class SourceInput:
    """An external source as the reviewer entered it, before it is checked and saved."""

    title: str
    publisher: str
    quote: str
    printed_value: str
    scale: str
    url: str = ""
    file_name: str = ""
    file_data: bytes | None = None  # an uploaded copy (PDF or image)
    page: int | None = None  # 1-based page of that copy (required with a file)
    box: Box | None = None  # the figure on that page, in PDF points (optional)
    sign: str | None = None  # a figure printed negative: 'positive' or 'as_printed' (D-068); None keeps it as printed


@dataclass(frozen=True)
class SavedSource:
    id: int
    document_id: str | None  # None: a sector value
    purpose: str  # value | cross_check | sector
    decision_id: int | None
    title: str
    publisher: str
    url: str
    file_sha256: str | None
    file_name: str | None
    media_type: str | None
    page: int | None
    box: Box | None
    quote: str
    printed_value: str
    scale: str
    value: Decimal
    unit: str
    reviewer: str
    created_at: str

    @property
    def conversion(self) -> str:
        return conversion_text(self.printed_value, self.scale, self.value, self.unit)

    @property
    def has_file(self) -> bool:
        return self.file_sha256 is not None and self.media_type is not None

    @property
    def citation(self) -> str:
        """One line naming the source: 'NBE Annual Report 2024/25 · National Bank of Ethiopia · p. 45 · <url>'."""
        parts = [self.title, self.publisher]
        if self.page:
            parts.append(f"page {self.page}" + (f" of {self.file_name}" if self.file_name else ""))
        if self.url:
            parts.append(self.url)
        return " · ".join(p for p in parts if p)


def check_source(source: SourceInput, unit: str, settings: AppSettings) -> tuple[Decimal, SourceFile | None]:
    """Everything a source needs before it is saved; returns its value in unit and the file checked."""
    for value, what in ((source.title, "the document title"), (source.publisher, "the publisher, e.g. National "
                                                                               "Bank of Ethiopia"),
                        (source.quote, "the exact quote around the figure")):
        if not value or not value.strip():
            raise ReviewError(f"Enter {what}.")
    url = (source.url or "").strip()
    if url and not _URL.match(url):
        raise ReviewError("The address must start with http:// or https:// (it is stored as text, never opened).")
    file = None
    if source.file_data is not None:
        file = validate_source_file(source.file_data, source.file_name, settings)
    if not url and file is None:
        raise ReviewError("Give the address (URL) of the document or upload a copy, so others can find the source.")
    if file is not None and source.page is None:
        raise ReviewError("Enter the page of the uploaded file that shows the figure.")
    if source.page is not None and (not isinstance(source.page, int) or source.page < 1
                                    or (file is not None and source.page > file.page_count)):
        limit = f" (1 to {file.page_count})" if file is not None else ""
        raise ReviewError(f"Enter the page that shows the figure{limit}.")
    if source.box is not None:
        if file is None:
            raise ReviewError("A box can only be marked on an uploaded file.")
        x0, y0, x1, y1 = source.box
        if not (x0 < x1 and y0 < y1):
            raise ReviewError("The marked box is empty; drag a box around the figure again.")
    return signed(convert(source.printed_value, source.scale, unit), source.sign), file


def insert_source(conn: sqlite3.Connection, source: SourceInput, value: Decimal, unit: str, file: SourceFile | None,
                  *, purpose: str, reviewer: str, document_id: str | None = None,
                  decision_id: int | None = None) -> int:
    """Add one source row without committing, so it is saved with related rows in one transaction."""
    return conn.execute(
        """INSERT INTO external_sources (document_id, purpose, decision_id, title, publisher, url, file_sha256,
               file_name, media_type, page, bbox, quote, printed_value, scale, value, unit, reviewer, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (document_id, purpose, decision_id, source.title.strip(), source.publisher.strip(), (source.url or "").strip(),
         file.sha256 if file else None, file.name if file else None, file.media_type if file else None, source.page,
         None if source.box is None else json.dumps([round(v, 2) for v in source.box]), source.quote.strip(),
         " ".join(source.printed_value.split()), source.scale, str(value), unit, reviewer.strip(), utc_now()),
    ).lastrowid


def prepare(source: SourceInput, unit: str, settings: AppSettings) -> tuple[Decimal, SourceFile | None]:
    """Check the source, then keep its uploaded copy (before the database rows are written)."""
    value, file = check_source(source, unit, settings)
    if file is not None:
        store_source_file(source.file_data, source.file_name, settings)
    return value, file


def get_source(conn: sqlite3.Connection, source_id: int) -> SavedSource | None:
    row = conn.execute("SELECT * FROM external_sources WHERE id = ?", (source_id,)).fetchone()
    return None if row is None else _saved(row)


def source_for_decision(conn: sqlite3.Connection, decision_id: int) -> SavedSource | None:
    row = conn.execute("SELECT * FROM external_sources WHERE decision_id = ?", (decision_id,)).fetchone()
    return None if row is None else _saved(row)


def _saved(row: sqlite3.Row) -> SavedSource:
    return SavedSource(
        id=row["id"], document_id=row["document_id"], purpose=row["purpose"], decision_id=row["decision_id"],
        title=row["title"], publisher=row["publisher"], url=row["url"], file_sha256=row["file_sha256"],
        file_name=row["file_name"], media_type=row["media_type"], page=row["page"],
        box=None if row["bbox"] is None else tuple(json.loads(row["bbox"])), quote=row["quote"],
        printed_value=row["printed_value"], scale=row["scale"], value=Decimal(row["value"]), unit=row["unit"],
        reviewer=row["reviewer"], created_at=row["created_at"])


# --- a report metric's value from an external source -------------------------------------------

def record_external_value(conn: sqlite3.Connection, settings: AppSettings, document_id: str, metric_id: str,
                          unit: str, source: SourceInput, note: str, reviewer: str) -> int:
    """Propose the metric's value from an external source; returns the decision id. Needs approval."""
    if get_document(conn, document_id) is None:
        raise ReviewError(f"Unknown document {document_id}.")
    _require(reviewer, "Enter the reviewer name.")
    _require(note, "Write a note: why this value comes from outside the report, e.g. 'not disclosed in the "
                   "report; NBE publishes it'.")
    if extraction_status(conn, document_id).finished:  # checked before the uploaded copy is kept
        raise ReviewError("The extraction review of this report is finished, so its figures are locked. "
                          "Reopen the extraction review (with a reason) to change them.")
    value, file = prepare(source, unit, settings)
    with conn:  # the decision and its source together, or nothing
        decision_id = insert_decision(conn, document_id, metric_id, None, "manual_entry", reviewer=reviewer,
                                      value=value, unit=unit, reason=note, entry_kind="external",
                                      sign=sign_choice(source.printed_value, source.sign))
        insert_source(conn, source, value, unit, file, purpose="value", reviewer=reviewer, document_id=document_id,
                      decision_id=decision_id)
    return decision_id


# --- external cross-checks ----------------------------------------------------------------------

@dataclass(frozen=True)
class Withdrawal:
    reason: str
    reviewer: str
    created_at: str


@dataclass(frozen=True)
class CrossCheck:
    id: int
    document_id: str
    metric_id: str
    source: SavedSource
    note: str
    reviewer: str
    created_at: str
    withdrawal: Withdrawal | None = None

    @property
    def active(self) -> bool:
        return self.withdrawal is None


@dataclass(frozen=True)
class Comparison:
    status: str  # match | mismatch | not_compared
    text: str
    difference: Decimal | None = None

    @property
    def label(self) -> str:
        return {"match": "Match", "mismatch": "Mismatch", "not_compared": "Not compared"}[self.status]


@dataclass(frozen=True)
class CheckedCrossCheck:
    check: CrossCheck
    comparison: Comparison


def add_cross_check(conn: sqlite3.Connection, settings: AppSettings, document_id: str, metric_id: str, unit: str,
                    source: SourceInput, note: str, reviewer: str) -> int:
    """Record an external figure to compare the metric's approved value with. Advisory: it never
    changes, approves or rejects a value, so it may be added after the extraction review is finished."""
    if get_document(conn, document_id) is None:
        raise ReviewError(f"Unknown document {document_id}.")
    _require(reviewer, "Enter the reviewer name.")
    value, file = prepare(source, unit, settings)
    with conn:
        source_id = insert_source(conn, source, value, unit, file, purpose="cross_check", reviewer=reviewer,
                                  document_id=document_id)
        return conn.execute(
            """INSERT INTO external_cross_checks (document_id, metric_id, external_source_id, note, reviewer, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (document_id, metric_id, source_id, (note or "").strip(), reviewer.strip(), utc_now())).lastrowid


def withdraw_cross_check(conn: sqlite3.Connection, cross_check_id: int, reviewer: str, reason: str) -> int:
    """Withdraw a cross-check (it stays in the record, marked withdrawn, with the reason)."""
    _require(reviewer, "Enter the reviewer name.")
    _require(reason, "Say why the cross-check is withdrawn, e.g. 'the source counts a different year'.")
    row = conn.execute("SELECT * FROM external_cross_checks WHERE id = ?", (cross_check_id,)).fetchone()
    if row is None:
        raise ReviewError("That cross-check is not in the database.")
    if conn.execute("SELECT 1 FROM external_cross_check_withdrawals WHERE cross_check_id = ?",
                    (cross_check_id,)).fetchone():
        raise ReviewError("That cross-check is already withdrawn.")
    with conn:
        return conn.execute(
            """INSERT INTO external_cross_check_withdrawals (cross_check_id, document_id, reason, reviewer, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (cross_check_id, row["document_id"], reason.strip(), reviewer.strip(), utc_now())).lastrowid


def cross_checks(conn: sqlite3.Connection, document_id: str, metric_id: str | None = None) -> list[CrossCheck]:
    """Every cross-check of a report (or one metric), oldest first, withdrawn ones included."""
    rows = conn.execute(
        """SELECT c.*, w.reason AS w_reason, w.reviewer AS w_reviewer, w.created_at AS w_created_at
           FROM external_cross_checks c LEFT JOIN external_cross_check_withdrawals w ON w.cross_check_id = c.id
           WHERE c.document_id = ? AND (? IS NULL OR c.metric_id = ?) ORDER BY c.id""",
        (document_id, metric_id, metric_id)).fetchall()
    return [CrossCheck(r["id"], r["document_id"], r["metric_id"], get_source(conn, r["external_source_id"]), r["note"],
                       r["reviewer"], r["created_at"],
                       None if r["w_reason"] is None else Withdrawal(r["w_reason"], r["w_reviewer"], r["w_created_at"]))
            for r in rows]


def compare(value: Decimal | None, unit: str | None, source: SavedSource) -> Comparison:
    """The approved value against an external figure. Sizes are compared (signs ignored, D-028).
    Match: within 0.5% of the approved value, or, for Birr'000 figures, a rounding difference of at
    most 2 thousand Birr (the external figure printed in Birr'000, or the approved value a whole
    number of thousands, as figures from a Birr'000 statement are)."""
    if value is None:
        return Comparison("not_compared", "No approved value to compare with yet.")
    if unit != source.unit:
        return Comparison("not_compared", f"The approved value is in {unit}, the cross-check in {source.unit}.")
    approved, external = abs(value), abs(source.value)
    difference = abs(approved - external)
    thousands = unit == "ETB" and (source.scale == "birr_thousand" or approved % 1000 == 0)
    within = difference <= MATCH_TOLERANCE * approved or (thousands and difference <= THOUSANDS_ROUNDING)
    share = f"{(difference / approved * 100).quantize(Decimal('0.01'))}%" if approved else "n/a"
    numbers = (f"external {_number(source.value, unit)} vs approved {_number(value, unit)} "
               f"(difference {_number(difference, unit)}, {share})")
    if within:
        return Comparison("match", f"Matches: {numbers}.", difference)
    return Comparison("mismatch", f"Does not match: {numbers}. Allowed: 0.5%"
                      + (" or 2 thousand Birr of rounding" if thousands else "") + ".", difference)


def checked(conn: sqlite3.Connection, document_id: str, metric_id: str, value: Decimal | None,
            unit: str | None) -> list[CheckedCrossCheck]:
    """The metric's cross-checks, each compared with value (None: not approved, nothing compared)."""
    return [CheckedCrossCheck(c, compare(value, unit, c.source) if c.active
                              else Comparison("not_compared", "Withdrawn."))
            for c in cross_checks(conn, document_id, metric_id)]


def mismatch_text(check: CheckedCrossCheck) -> str:
    """One line for an export warning or cell: the source and the comparison."""
    return f"{check.check.source.title}: {check.comparison.text}"


# --- the uploaded page as an image -------------------------------------------------------------

def source_page_png(data_dir: Path, source: SavedSource, zoom: float = 1.2, label: str = "") -> bytes | None:
    """The uploaded page of a source with its box in blue (the value's own box); None without a file."""
    if not source.has_file or not source.page:
        return None
    pdf = page_pdf(load_source_file(data_dir, source.file_sha256, source.media_type), source.media_type)
    boxes = ((tuple(source.box), "focus", label),) if source.box is not None else ()
    return render_page_png(pdf, source.page - 1, zoom=zoom, boxes=boxes)


def source_crop_png(data_dir: Path, source: SavedSource, scale: float = 2.0) -> bytes | None:
    """A close-up of the line holding the source's box; None without a box."""
    if not source.has_file or not source.page or source.box is None:
        return None
    pdf = page_pdf(load_source_file(data_dir, source.file_sha256, source.media_type), source.media_type)
    return render_crop_png(pdf, source.page - 1, tuple(source.box), scale=scale, kind="focus")


def _number(value: Decimal, unit: str | None) -> str:
    if unit == "fraction":
        return f"{(value * 100).normalize():f}%"
    return f"{value:,f}" + (" ETB" if unit == "ETB" else "")


def _require(text: str, message: str) -> None:
    if not text or not text.strip():
        raise ReviewError(message)
