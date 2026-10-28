import pymupdf
import pytest

from src.pdf_render import focus_of, render_band_png, render_page_png, screen_box_to_page
from src.pdf_render import visible_area as _visible_area

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_renders_page_as_png(simple_pdf):
    assert render_page_png(simple_pdf, 0).startswith(PNG_SIGNATURE)


def test_highlight_changes_the_image_but_not_the_pdf(simple_pdf):
    original = bytes(simple_pdf)
    plain = render_page_png(simple_pdf, 0)
    boxed = render_page_png(simple_pdf, 0, highlight=(300, 110, 360, 122))
    assert boxed != plain
    assert simple_pdf == original


@pytest.mark.parametrize("page_index", [-1, 1])
def test_page_outside_pdf_is_rejected(simple_pdf, page_index):
    with pytest.raises(ValueError, match="outside this PDF"):
        render_page_png(simple_pdf, page_index)


def png_size(png: bytes) -> tuple[int, int]:
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


@pytest.mark.parametrize("view_zoom", [1.5, 2.0, 4.0])
def test_zoomed_view_keeps_page_proportions_and_sharpness(simple_pdf, view_zoom):
    full_w, full_h = png_size(render_page_png(simple_pdf, 0, zoom=1.0))
    zoom_w, zoom_h = png_size(render_page_png(simple_pdf, 0, zoom=1.0, view_zoom=view_zoom))
    # A smaller area drawn at higher resolution: same pixel size as the whole page.
    assert abs(zoom_w - full_w) <= 1 and abs(zoom_h - full_h) <= 1


def test_zoom_focus_selects_different_areas(simple_pdf):
    top_left = render_page_png(simple_pdf, 0, view_zoom=3.0, focus=(0.0, 0.0))
    around_value = render_page_png(simple_pdf, 0, view_zoom=3.0, focus=(0.55, 0.14))
    assert top_left != around_value


def test_visible_area_stays_inside_the_page():
    page = pymupdf.Rect(0, 0, 600, 800)
    corner = _visible_area(page, 2.0, (0.0, 0.0))
    assert (corner.x0, corner.y0, corner.width, corner.height) == (0, 0, 300, 400)
    far_corner = _visible_area(page, 2.0, (1.0, 1.0))
    assert (far_corner.x1, far_corner.y1) == (600, 800)


def test_focus_of_returns_page_fractions():
    assert focus_of((290, 390, 310, 410), 600, 800) == (0.5, 0.5)


def test_dragged_box_converts_to_page_points():
    # Whole 600x800 page shown 300x400 pixels wide: every pixel is 2 points.
    drag = {"x1": 150, "y1": 50, "x2": 200, "y2": 60, "width": 300, "height": 400}
    assert screen_box_to_page(drag, (600, 800), 1.0, (0.5, 0.5)) == (300, 100, 400, 120)
    # Zoomed 2x on the top-left quarter: the same drag covers half the distance.
    assert screen_box_to_page(drag, (600, 800), 2.0, (0.0, 0.0)) == (150, 50, 200, 60)
    # Dragged from bottom-right to top-left, and past the image edge: still a proper box.
    reverse = {"x1": 200, "y1": 60, "x2": 350, "y2": 50, "width": 300, "height": 400}
    assert screen_box_to_page(reverse, (600, 800), 1.0, (0.5, 0.5)) == (400, 100, 600, 120)


def test_a_click_is_not_a_box():
    assert screen_box_to_page({"x1": 10, "y1": 10, "x2": 11, "y2": 10, "width": 300, "height": 400},
                              (600, 800), 1.0, (0.5, 0.5)) is None


def test_several_coloured_boxes_are_drawn(simple_pdf):
    plain = render_page_png(simple_pdf, 0)
    boxed = render_page_png(simple_pdf, 0, boxes=(((300, 110, 360, 122), "focus", "Total assets"),
                                                  ((400, 110, 450, 122), "recorded", "Not written"),
                                                  ((50, 110, 120, 122), "marked")))
    assert boxed != plain


def test_zoom_below_whole_page_is_rejected(simple_pdf):
    with pytest.raises(ValueError, match="at least 1"):
        render_page_png(simple_pdf, 0, view_zoom=0.5)


def test_band_shows_the_rows_of_all_its_boxes_and_leaves_the_pdf_alone(simple_pdf):
    original = bytes(simple_pdf)
    one_row = pymupdf.Pixmap(render_band_png(simple_pdf, 0, (((300, 110, 360, 122), "input of ROE"),)))
    two_rows = pymupdf.Pixmap(render_band_png(simple_pdf, 0, (((300, 110, 360, 122), "input of ROE"),
                                                               ((300, 200, 360, 212), "input of ROE"))))
    assert two_rows.height > one_row.height and two_rows.width == one_row.width  # full page width, taller band
    assert simple_pdf == original
    with pytest.raises(ValueError, match="outside this PDF"):
        render_band_png(simple_pdf, 5, (((300, 110, 360, 122), ""),))
