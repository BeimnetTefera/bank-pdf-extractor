"""Validate, fingerprint and store one uploaded PDF.

The original bytes are stored unchanged under a name derived from their SHA-256 hash,
so a repeated filename can never overwrite a different report, and the user-supplied
filename is never used as a filesystem path.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath

import pymupdf

from src.config_loader import AppSettings

PDF_MAGIC = b"%PDF-"


class DocumentError(ValueError):
    """Raised when an uploaded file cannot be accepted as a readable PDF."""


@dataclass(frozen=True)
class Document:
    id: str
    bank_name: str
    reporting_period: str
    original_filename: str
    stored_path: str  # relative to settings.data_dir
    sha256: str
    page_count: int
    uploaded_at: str  # ISO 8601, UTC
    processing_status: str


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_pdf(data: bytes, original_filename: str, settings: AppSettings) -> int:
    """Check that data is a readable, unencrypted PDF within limits. Returns the page count."""
    name = PurePath(original_filename).name
    if PurePath(name).suffix.lower() != ".pdf":
        raise DocumentError(f"'{name}' is not a PDF file. Only .pdf files are accepted.")
    if not data:
        raise DocumentError(f"'{name}' is empty.")

    size_mb = len(data) / (1024 * 1024)
    if size_mb > settings.max_upload_mb:
        raise DocumentError(
            f"'{name}' is {size_mb:.1f} MB, above the {settings.max_upload_mb} MB limit "
            "(max_upload_mb in config/settings.yaml)."
        )
    if not data.lstrip()[:5] == PDF_MAGIC:
        raise DocumentError(f"'{name}' has a .pdf extension but its content is not a PDF.")

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several error types for damaged files
        raise DocumentError(f"'{name}' could not be opened; the file may be damaged. ({exc})") from exc

    with doc:
        if doc.needs_pass:
            raise DocumentError(
                f"'{name}' is password-protected. Remove the password and upload it again."
            )
        page_count = doc.page_count

    if page_count == 0:
        raise DocumentError(f"'{name}' contains no pages.")
    if page_count > settings.max_pages:
        raise DocumentError(
            f"'{name}' has {page_count} pages, above the {settings.max_pages}-page limit "
            "(max_pages in config/settings.yaml)."
        )
    return page_count


def store_document(
    data: bytes,
    original_filename: str,
    bank_name: str,
    reporting_period: str,
    settings: AppSettings,
) -> Document:
    """Validate the PDF, copy it into the uploads folder and return its document record."""
    if not bank_name.strip():
        raise DocumentError("Bank name is required.")
    if not reporting_period.strip():
        raise DocumentError("Reporting period is required.")

    page_count = validate_pdf(data, original_filename, settings)
    sha256 = compute_sha256(data)
    relative_path = Path("uploads") / f"{sha256}.pdf"
    _write_once(settings.data_dir / relative_path, data)

    return Document(
        id=uuid.uuid4().hex,
        bank_name=bank_name.strip(),
        reporting_period=reporting_period.strip(),
        original_filename=PurePath(original_filename).name,
        stored_path=relative_path.as_posix(),
        sha256=sha256,
        page_count=page_count,
        uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        processing_status="uploaded",
    )


def _write_once(target: Path, data: bytes) -> None:
    """Write data to target unless an identical file (same hash name) is already there."""
    if target.exists():
        return  # the name is the content hash, so the existing file holds the same bytes
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
