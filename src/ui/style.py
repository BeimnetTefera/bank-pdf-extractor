"""The redesign's look (D-090) beyond what .streamlit/config.toml can set, and the few small
pieces of page markup it uses (page header, summary strip).

The CSS only styles Streamlit's stable hooks: containers given a `key` (class `st-key-<key>`)
and documented test ids. Everything else keeps Streamlit's own look from the theme.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from pathlib import Path

import streamlit as st

CSS = """
<style>
/* page: grey ground, comfortable width */
.stMainBlockContainer { padding-top: 4rem; max-width: 1360px; }
.stApp h2 { letter-spacing: -0.01em; }

/* sidebar: group labels and the menu */
[data-testid="stSidebar"] { border-right: 1px solid #E3E6EB; }
.bpe-group { margin: 14px 4px 2px; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase;
  color: #5F6775; font-weight: 600; }
[data-testid="stPageLink-NavLink"] { border-radius: 6px; }

/* summary strip: one row of figures, not big tiles */
.bpe-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); background: #FFFFFF;
  border: 1px solid #E3E6EB; border-radius: 8px; }
.bpe-stat { padding: 14px 18px; border-right: 1px solid #E3E6EB; display: flex; flex-direction: column; gap: 2px;
  min-width: 0; }
.bpe-stat:last-child { border-right: 0; }
.bpe-stat span { font-size: 12px; color: #4B5363; }
.bpe-stat b { font: 500 22px "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; color: #15181E; }
.bpe-stat b small { font-size: 14px; color: #5F6775; font-weight: 400; }
.bpe-stat em { font-style: normal; font-size: 12px; color: #5F6775; overflow-wrap: anywhere; }
@media (max-width: 760px) { .bpe-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }

/* white panels: any container whose key starts with "panel" */
[class*="st-key-panel"] { background: #FFFFFF; border: 1px solid #E3E6EB; border-radius: 8px; padding: 4px 16px 12px; }

/* the reports table: a header row and one row per report */
.st-key-panel_reports { padding: 0; gap: 0; }
.st-key-reports_head { background: #F9FAFB; border-bottom: 1px solid #E3E6EB; padding: 8px 16px;
  border-radius: 8px 8px 0 0; }
.st-key-reports_head p { font-size: 12px; color: #4B5363; font-weight: 500; }
[class*="st-key-report_row_"] { border-bottom: 1px solid #EEF0F3; padding: 10px 16px; }
[class*="st-key-report_row_"]:last-child { border-bottom: 0; }
.bpe-file { font-size: 12px; color: #5F6775; overflow-wrap: anywhere; }
.bpe-num { font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; }

/* activity feed */
.bpe-event { display: flex; gap: 10px; font-size: 13px; color: #4B5363; padding: 6px 0; }
.bpe-event b { color: #15181E; font-weight: 500; }
.bpe-avatar { width: 24px; height: 24px; flex: none; border-radius: 50%; background: #ECF0FD; color: #1B3399;
  display: grid; place-items: center; font-weight: 600; font-size: 10px; }
.bpe-when { font-size: 12px; color: #5F6775; }
</style>
"""

STATIC = Path(__file__).resolve().parents[2] / "static"
LOGO, LOGO_ICON = STATIC / "logo.svg", STATIC / "logo-icon.svg"


def apply() -> None:
    """Add the redesign's CSS to the page; call once per run, before anything is drawn."""
    st.html(CSS)


def brand() -> None:
    """The product mark at the top of the sidebar (an image: Streamlit strips SVG from st.html)."""
    st.logo(str(LOGO), size="large", icon_image=str(LOGO_ICON))


def group_label(text: str) -> None:
    """A small uppercase label over a group of menu links."""
    st.html(f'<div class="bpe-group">{escape(text)}</div>')


def stat_strip(stats: Sequence[tuple[str, str, str]]) -> None:
    """One row of summary figures: (label, value, note); a value may end in ' / total'."""
    cells = []
    for label, value, note in stats:
        number, _, total = value.partition(" / ")
        shown = escape(number) + (f"<small> / {escape(total)}</small>" if total else "")
        cells.append(f'<div class="bpe-stat"><span>{escape(label)}</span><b>{shown}</b><em>{escape(note)}</em></div>')
    st.html(f'<div class="bpe-stats">{"".join(cells)}</div>')


def initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() if len(parts) > 1 else name[:2].upper()
