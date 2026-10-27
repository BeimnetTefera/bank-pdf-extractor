"""The form for an external source (D-055), shared by Metric Details, the Evidence page and the
Sector data page.

It collects the document title, publisher, address (stored as text, never opened), an optional
uploaded copy with its page and a box dragged around the figure on that page, the exact quote,
and the value as printed with its scale, and shows the conversion. Checking and saving are in
src/external_sources.py; this module only collects input.
"""

from __future__ import annotations

import io

import streamlit as st
from PIL import Image
from streamlit_image_coordinates import streamlit_image_coordinates

from src.config_loader import AppSettings
from src.external_sources import (
    SCALES,
    Box,
    SavedSource,
    SourceFile,
    SourceInput,
    conversion_text,
    convert,
    page_pdf,
    page_size,
    scales_for,
    signed,
    validate_source_file,
)
from src.pdf_render import render_page_png, screen_box_to_page
from src.review_service import ReviewError
from src.ui.metric_view import esc
from src.value_input import SIGN_CHOICES, default_sign, sign_label, stored_text

DEFAULT_SCALE = {"ETB": "billion", "count": "count", "fraction": "percent"}


def source_form(key: str, unit: str, settings: AppSettings, sign: str | None = None) -> SourceInput:
    """Draw the fields; return what was entered (checked only when it is saved).

    sign: the metric's sign setting (D-068). When given and the figure is printed negative, the
    reviewer chooses to record it as positive or keep it negative, starting from that setting;
    without it (a cross-check, compared by size) the figure stays as printed."""
    title = st.text_input("Document title", key=f"{key}_title", placeholder="e.g. NBE Annual Report 2024/25")
    publisher = st.text_input("Publisher", key=f"{key}_publisher", placeholder="e.g. National Bank of Ethiopia")
    url = st.text_input("Address (URL)", key=f"{key}_url", placeholder="https://...",
                        help="Stored as text so others can find the document. The app never opens it.")
    upload = st.file_uploader("Or upload a copy (PDF or image)", type=["pdf", "png", "jpg", "jpeg"], key=f"{key}_file",
                              help="Kept on this computer, named by its fingerprint (SHA-256). Give the address, "
                                   "a copy, or both.")
    data, name, page, box = None, "", None, None
    if upload is not None:
        data, name = upload.getvalue(), upload.name
        try:
            file = validate_source_file(data, name, settings)
        except ReviewError as exc:
            st.error(str(exc), icon=":material/error:")
            file = None
        if file is not None:
            page = int(st.number_input(f"Page of the uploaded file that shows the figure (1 to {file.page_count})",
                                       min_value=1, max_value=file.page_count, value=1, step=1,
                                       key=f"{key}_page_{file.sha256[:12]}"))
            box = _box_picker(key, data, file, page)
    quote = st.text_area("Exact quote", key=f"{key}_quote", height=80,
                         placeholder="Copy the words around the figure exactly, e.g. 'total deposits of the banking "
                                     "system reached Birr 2,345.6 billion'")
    scales = scales_for(unit) or list(SCALES)
    value_col, scale_col = st.columns([3, 2])
    printed = value_col.text_input("Value as printed", key=f"{key}_printed", placeholder="e.g. 1,234.5",
                                   help="As printed: commas, brackets, % or a scale word (1,234.5 million) are read; "
                                        "give the scale once, here or under Printed in.")
    default = scales.index(DEFAULT_SCALE[unit]) if DEFAULT_SCALE.get(unit) in scales else 0
    scale = scale_col.selectbox("Printed in", scales, index=default, format_func=lambda s: SCALES[s].label,
                                key=f"{key}_scale")
    choice = None
    if printed.strip():
        try:
            value = convert(printed, scale, unit)
            if sign is not None and value < 0:
                first = default_sign(sign)
                choice = st.radio("The value is negative", SIGN_CHOICES, horizontal=True, key=f"{key}_sign",
                                  index=SIGN_CHOICES.index(first), format_func=lambda c: sign_label(c, first),
                                  help="The default comes from the metric's setting (metrics.yaml, sign); you "
                                       "decide how it is recorded.")
                value = signed(value, choice)
            st.caption(f":material/save: **Will store {esc(stored_text(value, unit))}** · "
                       f"{esc(conversion_text(printed, scale, value, unit))}")
        except ReviewError as exc:
            st.warning(str(exc), icon=":material/warning:")
    return SourceInput(title=title, publisher=publisher, quote=quote, printed_value=printed, scale=scale, url=url,
                       file_name=name, file_data=data, page=page, box=box, sign=choice)


def _box_picker(key: str, data: bytes, file: SourceFile, page: int) -> Box | None:
    """Optionally drag a box around the figure on the uploaded page (saved in PDF points)."""
    state_key = f"{key}_box"
    saved = st.session_state.get(state_key)
    box = saved[2] if saved is not None and saved[:2] == (file.sha256, page) else None
    if not st.toggle("Mark the figure on the page (optional)", key=f"{key}_marking"):
        if box is not None:
            st.caption(":material/crop_free: A box is marked on this page.")
        return box
    pdf = page_pdf(data, file.media_type)
    boxes = ((box, "focus", "figure"),) if box is not None else ()
    png = render_page_png(pdf, page - 1, zoom=1.2, boxes=boxes)
    st.caption("Drag a box around the figure. Drag again to replace it.")
    drag = streamlit_image_coordinates(Image.open(io.BytesIO(png)), width="stretch", click_and_drag=True,
                                       cursor="crosshair", key=f"{key}_drag_{file.sha256[:12]}_{page}")
    if drag and drag.get("unix_time") != st.session_state.get(f"{state_key}_time"):
        st.session_state[f"{state_key}_time"] = drag.get("unix_time")
        new_box = screen_box_to_page(drag, page_size(pdf, page), 1.0, (0.5, 0.5))
        if new_box is None:
            st.toast("Drag a box around the number (a click alone is not a box).", icon=":material/touch_app:")
        else:
            st.session_state[state_key] = (file.sha256, page, new_box)
            st.rerun()
    if box is not None and st.button("Clear the box", key=f"{key}_clear_box", icon=":material/close:"):
        st.session_state.pop(state_key, None)
        st.rerun()
    return box


def clear_form(key: str) -> None:
    """Forget what was typed, after a successful save."""
    for name in [k for k in st.session_state if isinstance(k, str) and k.startswith(f"{key}_")]:
        del st.session_state[name]


def source_lines(source: SavedSource) -> str:
    """Markdown naming the source: title, publisher, page of the copy, address as text."""
    parts = [f"**{esc(source.title)}**"]
    if source.publisher:
        parts.append(esc(source.publisher))
    if source.page:
        parts.append(f"page {source.page}" + (f" of {esc(source.file_name)}" if source.file_name else ""))
    text = " · ".join(parts)
    if source.url:
        text += f"  \nAddress (as recorded, not opened by the app): `{source.url}`"
    return text
