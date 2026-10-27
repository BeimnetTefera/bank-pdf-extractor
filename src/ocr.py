"""OCR for pages that are pictures (scanned or photographed), using Tesseract through PyMuPDF.

Runs only on pages without a text layer, on this computer (no external service). Words come
back with their position on the page, so the normal extractor reads them like any other
page and the reviewer still sees each figure boxed on the page image.

OCR is slow (seconds per page), so the words are saved per PDF in the data folder
(ocr/<sha256>.json) after every page: a PDF is read once, and an interrupted run resumes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from src.config_loader import AppSettings
from src.pdf_extractor import MIN_TEXT_CHARS

OCR_DPI = 300
OCR_LANGUAGE = "eng"
_COMMON_INSTALLS = (
    Path(r"C:\Program Files\Tesseract-OCR"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR",
)

OcrWords = dict[int, list[list]]  # zero-based page -> [x0, y0, x1, y1, text, block, line, word]


@dataclass(frozen=True)
class OcrEngine:
    executable: Path
    tessdata: Path
    version: str  # e.g. "tesseract 5.5.3"


def find_engine(configured_dir: Path | None = None) -> OcrEngine | None:
    """Locate Tesseract: the configured folder, then PATH, then the usual install folders."""
    folders = [configured_dir] if configured_dir else []
    on_path = shutil.which("tesseract")
    if on_path:
        folders.append(Path(on_path).parent)
    folders += list(_COMMON_INSTALLS)
    for folder in folders:
        exe = folder / ("tesseract.exe" if os.name == "nt" else "tesseract")
        tessdata = folder / "tessdata"
        if exe.is_file() and (tessdata / f"{OCR_LANGUAGE}.traineddata").is_file():
            return OcrEngine(exe, tessdata, _version(exe))
    return None


def _version(exe: Path) -> str:
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=20)
        first = (out.stdout or out.stderr).splitlines()[0].strip()
        return first.split(".20")[0] if first else "tesseract"  # drop the build date suffix
    except (OSError, subprocess.SubprocessError, IndexError):
        return "tesseract"


def pages_needing_ocr(pdf_bytes: bytes) -> list[int]:
    """Zero-based pages that have (almost) no text but do have an image."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        return [
            page.number for page in doc
            if len(page.get_text().strip()) < MIN_TEXT_CHARS and page.get_images(full=False)
        ]


def cache_path(settings: AppSettings, sha256: str) -> Path:
    return settings.data_dir / "ocr" / f"{sha256}.json"


def load_cache(path: Path) -> OcrWords:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(page): words for page, words in data.get("pages", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}  # unreadable cache: OCR again rather than trust it


def run_ocr(pdf_bytes: bytes, pages: list[int], engine: OcrEngine, path: Path,
            on_page: Callable[[int, int], None] | None = None) -> OcrWords:
    """OCR the pages not yet in the cache, saving after each page. Returns all cached words."""
    words = load_cache(path)
    todo = [p for p in pages if p not in words]
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for done, page_index in enumerate(todo, start=1):
            page = doc[page_index]  # keep the page alive while its OCR text page is used
            textpage = page.get_textpage_ocr(dpi=OCR_DPI, full=True, language=OCR_LANGUAGE,
                                             tessdata=str(engine.tessdata))
            words[page_index] = [list(w[:8]) for w in page.get_text("words", textpage=textpage)]
            _save(path, words, engine)
            if on_page is not None:
                on_page(done, len(todo))
    return words


def _save(path: Path, words: OcrWords, engine: OcrEngine) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    payload = {"engine": engine.version, "dpi": OCR_DPI, "language": OCR_LANGUAGE,
               "pages": {str(p): w for p, w in sorted(words.items())}}
    temp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temp, path)
