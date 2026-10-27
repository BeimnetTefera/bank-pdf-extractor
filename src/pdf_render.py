"""Render a PDF page as an image for the evidence viewer, with coloured boxes around values."""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

HIGHLIGHT_COLOR = (0.86, 0.15, 0.15)  # red: a suggested figure being looked at (not recorded)
MARK_COLOR = (0.15, 0.39, 0.92)  # blue: the marker; every recorded value uses it
FAINT_MARK_COLOR = (0.55, 0.70, 0.97)
INPUT_COLOR = (0.05, 0.55, 0.30)  # green: a figure used as an input of a calculated value (D-052)


@dataclass(frozen=True)
class BoxStyle:
    color: tuple[float, float, float]
    width: float
    padding: float  # points around the value's bounding box
    fill: bool = False  # light shading inside the box
    labelled: bool = False  # write the box's label (metric name) above it


# Only the metric being looked at is drawn strongly and named; other recorded values are thin,
# unlabelled outlines, so names never cover the figures on a dense statement page.
BOX_STYLES = {
    "selected": BoxStyle(HIGHLIGHT_COLOR, 1.5, 1.5),  # a suggested figure being looked at
    "focus": BoxStyle(MARK_COLOR, 2.2, 1.5, fill=True, labelled=True),  # the selected metric's recorded value
    "recorded": BoxStyle(FAINT_MARK_COLOR, 0.6, 0.8),  # other recorded values on the page
    "marked": BoxStyle(MARK_COLOR, 1.5, 1.5),  # the box the reviewer just drew
    "input": BoxStyle(INPUT_COLOR, 2.2, 1.5, fill=True, labelled=True),  # an input of a calculated value
}
BOX_COLORS = {kind: style.color for kind, style in BOX_STYLES.items()}
LABEL_FONT_SIZE = 6.5
MAX_RENDER_SCALE = 6.0  # cap on resolution for deep zoom, to keep images a reasonable size

Box = tuple[float, float, float, float]


def render_page_png(
    pdf_bytes: bytes,
    page_index: int,
    highlight: Box | None = None,
    zoom: float = 1.5,
    view_zoom: float = 1.0,
    focus: tuple[float, float] = (0.5, 0.5),
    boxes: tuple[tuple, ...] = (),
) -> bytes:
    """Return page_index (zero-based) as PNG bytes. The source PDF bytes are not modified.

    highlight is drawn in red; boxes are (box, kind) or (box, kind, label) with kind a key of
    BOX_STYLES; a label (the metric name) is written above the box when its style is labelled.
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
        # Drawing changes only this in-memory copy, never the stored file.
        # Faint boxes first, so the strong ones are drawn on top.
        drawn = (*boxes, *(((highlight, "selected"),) if highlight is not None else ()))
        for box, kind, *label in sorted(drawn, key=lambda b: b[1] != "recorded"):
            style = BOX_STYLES[kind]
            x0, y0, x1, y1 = box
            p = style.padding
            page.draw_rect(pymupdf.Rect(x0 - p, y0 - p, x1 + p, y1 + p), color=style.color, width=style.width,
                           fill=style.color if style.fill else None, fill_opacity=0.15 if style.fill else 1)
            if style.labelled and label and label[0]:
                page.insert_text((x0 - p, y0 - p - 1.5), label[0], fontsize=LABEL_FONT_SIZE, color=style.color)
        clip = visible_area(page.rect, view_zoom, focus)
        scale = min(zoom * view_zoom, MAX_RENDER_SCALE)
        return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip).tobytes("png")


def render_crop_png(pdf_bytes: bytes, page_index: int, box: Box, margin: float = 14.0, scale: float = 2.5,
                    kind: str = "focus") -> bytes:
    """A close-up of the line holding box: the full page width, margin points above and below,
    with the box drawn in (in the colour of kind), so the printed label and the value are read together."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not 0 <= page_index < doc.page_count:
            raise ValueError(f"Page index {page_index} is outside this PDF (0 to {doc.page_count - 1}).")
        page = doc[page_index]
        x0, y0, x1, y1 = box
        style = BOX_STYLES[kind]
        p = style.padding
        page.draw_rect(pymupdf.Rect(x0 - p, y0 - p, x1 + p, y1 + p), color=style.color, width=1.2)
        clip = pymupdf.Rect(page.rect.x0, y0 - margin, page.rect.x1, y1 + margin) & page.rect
        return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip).tobytes("png")


def focus_of(bbox: Box, page_width: float, page_height: float) -> tuple[float, float]:
    """Centre of a box as fractions of the page size, for use as render focus."""
    x0, y0, x1, y1 = bbox
    return ((x0 + x1) / 2 / page_width, (y0 + y1) / 2 / page_height)


def visible_area(page_rect: pymupdf.Rect, view_zoom: float, focus: tuple[float, float]) -> pymupdf.Rect:
    """The part of the page shown at this zoom and focus (the whole page at zoom 1)."""
    width, height = page_rect.width / view_zoom, page_rect.height / view_zoom
    left = page_rect.x0 + focus[0] * page_rect.width - width / 2
    top = page_rect.y0 + focus[1] * page_rect.height - height / 2
    left = min(max(left, page_rect.x0), page_rect.x1 - width)
    top = min(max(top, page_rect.y0), page_rect.y1 - height)
    return pymupdf.Rect(left, top, left + width, top + height)


def screen_box_to_page(drag: dict, page_size: tuple[float, float], view_zoom: float,
                       focus: tuple[float, float]) -> Box | None:
    """A box dragged on the displayed page image ({x1, y1, x2, y2, width, height} in screen
    pixels) in PDF page points, clipped to the visible area. None for a click without a drag."""
    shown = visible_area(pymupdf.Rect(0, 0, *page_size), view_zoom, focus)
    if not drag.get("width") or not drag.get("height"):
        return None
    sx, sy = shown.width / drag["width"], shown.height / drag["height"]
    xs = sorted(min(max(v, 0), drag["width"]) for v in (drag["x1"], drag["x2"]))
    ys = sorted(min(max(v, 0), drag["height"]) for v in (drag["y1"], drag["y2"]))
    if xs[1] - xs[0] < 3 or ys[1] - ys[0] < 3:  # a click, not a box
        return None
    return (shown.x0 + xs[0] * sx, shown.y0 + ys[0] * sy, shown.x0 + xs[1] * sx, shown.y0 + ys[1] * sy)
