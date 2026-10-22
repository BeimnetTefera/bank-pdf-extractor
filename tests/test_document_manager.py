from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

from src.document_manager import DocumentError, compute_sha256, store_document, validate_pdf
from tests.conftest import make_pdf


def test_valid_pdf_returns_page_count(settings, simple_pdf):
    assert validate_pdf(simple_pdf, "report.pdf", settings) == 1


def test_uppercase_extension_is_accepted(settings, simple_pdf):
    assert validate_pdf(simple_pdf, "REPORT.PDF", settings) == 1


@pytest.mark.parametrize("filename", ["report.docx", "report.pdf.exe", "report"])
def test_non_pdf_extension_is_rejected(settings, simple_pdf, filename):
    with pytest.raises(DocumentError, match="Only .pdf files are accepted"):
        validate_pdf(simple_pdf, filename, settings)


def test_empty_file_is_rejected(settings):
    with pytest.raises(DocumentError, match="is empty"):
        validate_pdf(b"", "report.pdf", settings)


def test_non_pdf_content_is_rejected(settings):
    with pytest.raises(DocumentError, match="content is not a PDF"):
        validate_pdf(b"PK\x03\x04 this is a zip file", "report.pdf", settings)


def test_damaged_pdf_is_rejected(settings):
    with pytest.raises(DocumentError):
        validate_pdf(b"%PDF-1.7\n garbage without any objects", "report.pdf", settings)


def test_password_protected_pdf_is_rejected(settings):
    doc = pymupdf.open(stream=make_pdf([[("Total assets", ["1"])]]), filetype="pdf")
    encrypted = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    with pytest.raises(DocumentError, match="password-protected"):
        validate_pdf(encrypted, "report.pdf", settings)


def test_file_over_size_limit_is_rejected(settings):
    small_limit = replace(settings, max_upload_mb=1)
    with pytest.raises(DocumentError, match="above the 1 MB limit"):
        validate_pdf(b"%PDF-" + b"0" * (1024 * 1024 + 1), "big.pdf", small_limit)


def test_file_over_page_limit_is_rejected(settings):
    two_pages = make_pdf([[("A", ["1"])], [("B", ["2"])]])
    with pytest.raises(DocumentError, match="above the 1-page limit"):
        validate_pdf(two_pages, "report.pdf", replace(settings, max_pages=1))


def test_sha256_is_stable_and_content_sensitive(simple_pdf):
    assert compute_sha256(simple_pdf) == compute_sha256(bytes(simple_pdf))
    assert compute_sha256(simple_pdf) != compute_sha256(simple_pdf + b"\n")


def test_store_document_keeps_original_bytes(settings, simple_pdf):
    doc = store_document(simple_pdf, "report.pdf", "Synthetic Bank", "2025/26", settings)
    stored = settings.data_dir / doc.stored_path
    assert stored.read_bytes() == simple_pdf
    assert doc.sha256 == compute_sha256(simple_pdf)
    assert doc.page_count == 1
    assert doc.processing_status == "uploaded"
    assert doc.uploaded_at.endswith("+00:00")


def test_same_filename_different_content_does_not_overwrite(settings, simple_pdf):
    other_pdf = make_pdf([[("Total liabilities", ["5"])]])
    first = store_document(simple_pdf, "report.pdf", "Bank A", "2025/26", settings)
    second = store_document(other_pdf, "report.pdf", "Bank A", "2025/26", settings)
    assert first.stored_path != second.stored_path
    assert (settings.data_dir / first.stored_path).read_bytes() == simple_pdf
    assert (settings.data_dir / second.stored_path).read_bytes() == other_pdf


def test_same_content_twice_reuses_stored_file(settings, simple_pdf):
    first = store_document(simple_pdf, "a.pdf", "Bank A", "2025/26", settings)
    second = store_document(simple_pdf, "b.pdf", "Bank A", "2025/26", settings)
    assert first.stored_path == second.stored_path
    assert first.id != second.id  # duplicate warnings need persistence (Phase 4)
    assert len(list((settings.data_dir / "uploads").iterdir())) == 1


def test_user_filename_is_never_used_as_a_path(settings, simple_pdf):
    doc = store_document(simple_pdf, "../../outside/evil.pdf", "Bank A", "2025/26", settings)
    assert doc.original_filename == "evil.pdf"
    assert doc.stored_path == f"uploads/{doc.sha256}.pdf"
    assert not (settings.data_dir.parent / "outside").exists()


@pytest.mark.parametrize("bank,period,message", [(" ", "2025/26", "Bank name"), ("Bank A", "", "Reporting period")])
def test_bank_and_period_are_required(settings, simple_pdf, bank, period, message):
    with pytest.raises(DocumentError, match=message):
        store_document(simple_pdf, "report.pdf", bank, period, settings)


def test_rejected_pdf_is_not_stored(settings):
    with pytest.raises(DocumentError):
        store_document(b"not a pdf", "report.pdf", "Bank A", "2025/26", settings)
    assert not Path(settings.data_dir / "uploads").exists()
