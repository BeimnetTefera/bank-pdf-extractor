"""Manual Phase 2 check: validate one PDF, extract raw candidates and summarise them.

Usage (from the project folder, with the virtual environment active):

    python -m scripts.inspect_pdf "path/to/report.pdf"
    python -m scripts.inspect_pdf "path/to/report.pdf" --find "total assets"
    python -m scripts.inspect_pdf "path/to/report.pdf" --page 45
    python -m scripts.inspect_pdf "path/to/report.pdf" --csv

Nothing is sent over the network. --csv writes every candidate to the app's exports
folder (outside the project) so it can be opened in Excel.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from src.config_loader import ConfigError, load_config
from src.document_manager import DocumentError, compute_sha256, validate_pdf
from src.pdf_extractor import ExtractionResult, RawCandidate, extract_candidates

CSV_COLUMNS = [
    "pdf_page",
    "page_index",
    "row_index",
    "column_index",
    "row_value_count",
    "raw_label",
    "raw_value_text",
    "row_text",
    "context_text",
    "extraction_method",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path, help="path to a local PDF")
    parser.add_argument("--find", help="show rows whose label contains this text (case-insensitive)")
    parser.add_argument("--page", type=int, help="show all rows on this PDF page (1-based)")
    parser.add_argument("--csv", action="store_true", help="write all candidates to a CSV file")
    args = parser.parse_args(argv)

    try:
        settings = load_config().settings
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    if not args.pdf.is_file():
        print(f"File not found: {args.pdf}", file=sys.stderr)
        return 2
    data = args.pdf.read_bytes()
    try:
        validate_pdf(data, args.pdf.name, settings)
    except DocumentError as exc:
        print(f"Rejected: {exc}", file=sys.stderr)
        return 1

    sha256 = compute_sha256(data)
    result = extract_candidates(data)
    print_summary(args.pdf.name, len(data), sha256, result)

    if args.page is not None:
        print_rows(f"PDF page {args.page}", [c for c in result.candidates if c.page_index == args.page - 1])
    if args.find:
        needle = args.find.lower()
        print_rows(f"labels containing '{args.find}'", [c for c in result.candidates if needle in c.raw_label.lower()])
    if args.csv:
        out = settings.exports_dir / f"phase2_candidates_{sha256[:12]}.csv"
        write_csv(out, result.candidates)
        print(f"\nCSV written: {out}")
    return 0


def print_summary(filename: str, size: int, sha256: str, result: ExtractionResult) -> None:
    print(f"File:        {filename} ({size / 1024 / 1024:.1f} MB)")
    print(f"SHA-256:     {sha256}")
    print(f"Pages:       {result.page_count}")
    print(f"Text status: {result.text_status}")
    print(f"Extractor:   {result.extractor_version}")
    for message in result.messages:
        print(f"  - {message}")

    per_page: dict[int, int] = {}
    for c in result.candidates:
        per_page[c.page_index] = per_page.get(c.page_index, 0) + 1
    busiest = sorted(per_page.items(), key=lambda kv: kv[1], reverse=True)[:10]
    if busiest:
        print("\nPages with the most candidates (likely financial tables):")
        for page_index, count in busiest:
            print(f"  PDF page {page_index + 1:>4}: {count} values")


def print_rows(title: str, candidates: list[RawCandidate]) -> None:
    print(f"\n--- {title}: {len(candidates)} values (UNVERIFIED) ---")
    seen: set[tuple[int, int]] = set()
    for c in candidates:
        key = (c.page_index, c.row_index)
        if key in seen:
            continue
        seen.add(key)
        values = " | ".join(x.raw_value_text for x in candidates if (x.page_index, x.row_index) == key)
        label = c.raw_label or "(no label)"
        print(f"  p{c.page_index + 1:<4} {label[:70]:<70}  {values}")


def write_csv(path: Path, candidates: list[RawCandidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel shows non-ASCII characters correctly
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for c in candidates:
            writer.writerow(
                {
                    "pdf_page": c.page_index + 1,
                    "page_index": c.page_index,
                    "row_index": c.row_index,
                    "column_index": c.column_index,
                    "row_value_count": c.row_value_count,
                    "raw_label": c.raw_label,
                    "raw_value_text": c.raw_value_text,
                    "row_text": c.row_text,
                    "context_text": c.context_text,
                    "extraction_method": c.extraction_method,
                }
            )


if __name__ == "__main__":
    sys.exit(main())
