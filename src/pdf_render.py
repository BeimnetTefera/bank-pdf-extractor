"""Render a PDF page as an image for the evidence viewer, optionally boxing one value."""

from __future__ import annotations

import pymupdf

HIGHLIGHT_COLOR = (0.86, 0.15, 0.15)  # red, RGB 0-1
HIGHLIGHT_PADDING = 2.0  # points around the value's bounding box
MAX_RENDER_SCALE = 6.0  # cap on resolution for deep zoom, to keep images a reasonable size


def render_page_png(
    pdf_bytes: bytes,
    page_index: int,
    highlight: tuple[float, float, float, float] | None = None,
    zoom: float = 1.5,
    view_zoom: float = 1.0,
    focus: tuple[float, float] = (0.5, 0.5),
) -> bytes:
    """Return page_index (zero-based) as PNG bytes. The source PDF bytes are not modified.

    zoom is the base resolution. view_zoom > 1 shows only part of the page, enlarged:
    at 2.0 the visible area is half the page width and height, centred on focus
    (fractions of page width and height, 0-1) and kept inside the page edges. The
    image keeps the page's proportions at every zoom level.
    """
    if view_zoom < 1:
        raise ValueError(f"view_zoom must be at least 1 (whole page), got {view_zoom}.")
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not 0 <= page_index < doc.page_count:
            raise ValueError(f"Page index {page_index} is outside this PDF (0 to {doc.page_count - 1}).")
        page = doc[page_index]
        if highlight is not None:
            x0, y0, x1, y1 = highlight
            p = HIGHLIGHT_PADDING
            # Drawing changes only this in-memory copy, never the stored file.
            page.draw_rect(pymupdf.Rect(x0 - p, y0 - p, x1 + p, y1 + p), color=HIGHLIGHT_COLOR, width=1.5)

        clip = None if view_zoom == 1 else _visible_area(page.rect, view_zoom, focus)
        scale = min(zoom * view_zoom, MAX_RENDER_SCALE)
        return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip).tobytes("png")


def focus_of(bbox: tuple[float, float, float, float], page_width: float, page_height: float) -> tuple[float, float]:
    """Centre of a box as fractions of the page size, for use as render focus."""
    x0, y0, x1, y1 = bbox
    return ((x0 + x1) / 2 / page_width, (y0 + y1) / 2 / page_height)


def _visible_area(page_rect: pymupdf.Rect, view_zoom: float, focus: tuple[float, float]) -> pymupdf.Rect:
    width, height = page_rect.width / view_zoom, page_rect.height / view_zoom
    left = page_rect.x0 + focus[0] * page_rect.width - width / 2
    top = page_rect.y0 + focus[1] * page_rect.height - height / 2
    left = min(max(left, page_rect.x0), page_rect.x1 - width)
    top = min(max(top, page_rect.y0), page_rect.y1 - height)
    return pymupdf.Rect(left, top, left + width, top + height)
