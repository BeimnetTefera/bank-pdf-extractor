"""Shared fixtures. All PDFs are synthetic and generated in memory; no real reports."""

from pathlib import Path

import pymupdf
import pytest

from src.config_loader import AppSettings

# A row is (label, [values]) drawn as label at the left margin and values in columns.
Row = tuple[str, list[str]]
LABEL_X = 50
VALUE_XS = (300, 400, 500)


def make_pdf(pages: list[list[Row] | None]) -> bytes:
    """Build a PDF. Each page is a list of table rows, or None for a page with no text."""
    doc = pymupdf.open()
    for rows in pages:
        page = doc.new_page()
        for i, (label, values) in enumerate(rows or []):
            y = 100 + i * 20
            if label:
                page.insert_text((LABEL_X, y), label, fontsize=10)
            for x, value in zip(VALUE_XS, values):
                page.insert_text((x, y), value, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(data_dir=tmp_path / "appdata", max_upload_mb=100, max_pages=1000, default_reviewer="tester")


@pytest.fixture
def simple_pdf() -> bytes:
    return make_pdf([[("Synthetic Bank S.C.", []), ("Total assets", ["1,234,567", "987,654"])]])
