"""The redesign's look (D-090, D-092) beyond what .streamlit/config.toml can set, and the small
pieces of markup the mockup uses (chips, thin progress bars, the summary strip).

The CSS only styles Streamlit's stable hooks: containers given a `key` (class `st-key-<key>`)
and documented test ids. Everything else keeps Streamlit's own look from the theme.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape

import streamlit as st

CSS = """
<style>
/* the frame: no Streamlit header, a full-width top bar, then menu | page */
header[data-testid="stHeader"] { display: none; }
.stMainBlockContainer { padding: 0 !important; max-width: none !important; }
.stApp h2 { letter-spacing: -0.01em; }
.st-key-topbar { background: #FFFFFF; border-bottom: 1px solid #E3E6EB; padding: 8px 20px; }
.st-key-topbar [data-testid="stTextInput"] input { font-size: 14px; }
.st-key-topbar [data-baseweb="input"], .st-key-topbar [data-baseweb="base-input"] { background: #FFFFFF; }
.st-key-topbar [data-baseweb="input"] { border: 1px solid #D5D9E0; border-radius: 8px; }
.bpe-brand { display: flex; align-items: center; gap: 12px; font-weight: 600; font-size: 17px; color: #15181E; }
.bpe-brand img { width: 32px; height: 32px; display: block; }
.bpe-icon { font-family: "Material Symbols Rounded"; font-weight: normal; font-style: normal; font-size: 18px;
  line-height: 1; display: inline-block; vertical-align: middle; -webkit-font-smoothing: antialiased;
  font-feature-settings: "liga"; }
.bpe-icon.fill { font-variation-settings: "FILL" 1; }
.bpe-me .bpe-icon { font-size: 18px; color: #4B5363; }
.bpe-me { display: flex; align-items: center; justify-content: flex-end; gap: 8px; font-size: 13px; color: #4B5363; }
.bpe-avatar-lg { width: 30px; height: 30px; border-radius: 50%; background: #ECF0FD; color: #1B3399;
  display: grid; place-items: center; font-weight: 600; font-size: 12px; }
[data-testid="stVerticalBlock"]:has(> div > .st-key-topbar), [data-testid="stVerticalBlock"]:has(.st-key-topbar) { gap: 0; }
[data-testid="stHorizontalBlock"]:has(.st-key-page) { gap: 0; }
@media (min-width: 761px) { [data-testid="stHorizontalBlock"]:has(.st-key-page) { flex-wrap: nowrap; } }
[data-testid="stColumn"]:has(.st-key-nav) { flex: 0 0 232px !important; width: 232px !important; max-width: 232px;
  background: #FFFFFF; border-right: 1px solid #E3E6EB; min-height: calc(100vh - 58px); padding: 12px; }
[data-testid="stColumn"]:has(.st-key-nav_rail) { flex: 0 0 60px !important; width: 60px !important; max-width: 60px;
  background: #FFFFFF; border-right: 1px solid #E3E6EB; min-height: calc(100vh - 58px); padding: 12px 8px; }
[data-testid="stColumn"]:has(.st-key-page) { flex: 1 1 0 !important; width: auto !important; min-width: 0; }
.st-key-page { padding: 24px 28px 40px; gap: 20px; }
.st-key-page.bpe-tight, .st-key-page:has(.st-key-review_work), .st-key-page:has(.st-key-extract_work) { padding: 0; gap: 0; }
.bpe-group { margin: 14px 10px 6px; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase;
  color: #5F6775; font-weight: 600; }
.bpe-navsep { height: 1px; background: #E3E6EB; margin: 10px 10px; }
.st-key-nav { gap: 2px; padding-top: 6px; }
.st-key-nav [data-testid="stPageLink-NavLink"], .st-key-nav_rail [data-testid="stPageLink-NavLink"] {
  border-radius: 6px; min-height: 42px; }
.st-key-nav [data-testid="stPageLink-NavLink"] p { font-size: 15px; }
.st-key-nav [data-testid="stPageLink-NavLink"] [data-testid="stIconMaterial"] { font-size: 20px; }
[class*="st-key-navitem_"][class*="_on"] { background: #EEF2FD; border-radius: 6px; position: relative; }
[class*="st-key-navitem_"][class*="_on"]::before { content: ""; position: absolute; left: -12px; top: 0; bottom: 0;
  width: 3px; background: #2747C9; border-radius: 0 2px 2px 0; }
[class*="st-key-navitem_"][class*="_on"] [data-testid="stPageLink-NavLink"] { background: transparent; }
[class*="st-key-navitem_"][class*="_on"] [data-testid="stPageLink-NavLink"] p,
[class*="st-key-navitem_"][class*="_on"] [data-testid="stPageLink-NavLink"] span { color: #2747C9 !important;
  font-weight: 600; }
.st-key-nav_rail [data-testid="stPageLink-NavLink"] { justify-content: center; min-height: 44px; }
.st-key-nav_rail [data-testid="stPageLink-NavLink"] p { display: none; }
.bpe-count { font: 12px "IBM Plex Mono", monospace; color: #4B5363; display: inline-block; float: right;
  background: #EEF0F3; border-radius: 999px; padding: 1px 8px; margin-right: 6px; }

/* page header: title, one line under it, actions on the right */
.bpe-sub { color: #4B5363; font-size: 14px; margin: 0; }
.bpe-crumb { font-size: 13px; color: #4B5363; }

/* summary strip: one row of figures, not big tiles */
.bpe-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); background: #FFFFFF;
  border: 1px solid #E3E6EB; border-radius: 8px; }
.bpe-stat { padding: 16px 20px; border-right: 1px solid #E3E6EB; display: flex; flex-direction: column; gap: 4px;
  min-width: 0; }
.bpe-stat:last-child { border-right: 0; }
.bpe-stat span { font-size: 12px; color: #4B5363; }
.bpe-stat b { font: 500 22px "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; color: #15181E; }
.bpe-stat b small { font-size: 14px; color: #5F6775; font-weight: 400; }
.bpe-stat b.txt { font: 500 15px "IBM Plex Sans", sans-serif; overflow-wrap: anywhere; }
.bpe-stat em { font-style: normal; font-size: 12px; color: #5F6775; overflow-wrap: anywhere; }
@media (max-width: 760px) { .bpe-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }

/* white panels: any container whose key starts with "panel" */
[class*="st-key-panel"] { background: #FFFFFF; border: 1px solid #E3E6EB; border-radius: 8px; padding: 16px 20px; }

/* notice: one line in amber with its action on the right */
[class*="st-key-notice_"] { background: #FFF9EC; border: 1px solid #F1D9A6; border-radius: 8px; padding: 6px 16px; }

/* chips and thin progress bars */
.bpe-chip { display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 8px; border-radius: 999px;
  font-size: 12px; font-weight: 500; white-space: nowrap; }
.bpe-chip i { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.bpe-chip .bpe-icon { font-size: 15px; }
.bpe-tick { width: 14px; height: 14px; border-radius: 50%; background: currentColor; display: inline-grid; place-items: center;
  flex: none; }
.bpe-tick::after { content: ""; width: 6px; height: 3px; border: 2px solid #FFFFFF; border-top: 0; border-right: 0;
  transform: translateY(-1px) rotate(-45deg); }
.bpe-ok { background: #E7F4EC; color: #17663F; } .bpe-ok i { background: #17663F; }
.bpe-warn { background: #FFF3DC; color: #7A4900; } .bpe-warn i { background: #B26B00; }
.bpe-bad { background: #FDECEA; color: #A1221B; } .bpe-bad i { background: #B3261E; }
.bpe-calc { background: #E3F3F5; color: #0B5E6B; } .bpe-calc i { background: #0B6B7A; }
.bpe-muted { background: #EEF0F3; color: #4B5363; } .bpe-muted i { background: #8A93A3; }
.bpe-bar { height: 6px; border-radius: 3px; background: #EEF0F3; overflow: hidden; width: 140px; max-width: 100%; }
.bpe-bar b { display: block; height: 100%; background: #17663F; }
.bpe-minor { color: #5F6775; font-size: 12px; }
.bpe-num { font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; font-size: 13px; }
.bpe-file { font-size: 12px; color: #5F6775; overflow-wrap: anywhere; }

/* tables built from rows of columns: a grey header row, thin lines between rows */
[class*="st-key-panel_table"] { padding: 0; gap: 0; }
[class*="st-key-thead"] { background: #F9FAFB; border-bottom: 1px solid #E3E6EB; padding: 6px 16px;
  border-radius: 8px 8px 0 0; }
[class*="st-key-thead"] p { font-size: 12px; color: #4B5363; font-weight: 500; }
[class*="st-key-trow_"] { border-bottom: 1px solid #EEF0F3; padding: 8px 16px; }
[class*="st-key-trow_"]:last-child { border-bottom: 0; }
[class*="st-key-trow_"] button, .st-key-sm button { min-height: 30px; padding: 0 10px; font-size: 13px; }

/* read-only tables drawn as HTML (Templates) */
.bpe-tablebox { overflow-x: auto; }
.bpe-table { border-collapse: collapse; width: 100%; font-size: 13px; min-width: 760px; background: #FFFFFF; }
.bpe-table th { text-align: left; font-weight: 500; font-size: 12px; color: #4B5363; padding: 9px 14px;
  border-bottom: 1px solid #E3E6EB; background: #F9FAFB; white-space: nowrap; }
.bpe-table td { padding: 10px 14px; border-bottom: 1px solid #EEF0F3; vertical-align: middle; }
.bpe-table tr:last-child td { border-bottom: 0; }
.bpe-panel-table { border: 1px solid #E3E6EB; border-radius: 8px; }
.bpe-code { font: 12px "IBM Plex Mono", monospace; color: #4B5363; background: #F4F5F7; padding: 2px 6px; border-radius: 4px; }
.st-key-thead_metrics_tools { padding: 12px 16px; border-bottom: 1px solid #E3E6EB; }
.bpe-kv { display: flex; justify-content: space-between; gap: 12px; font-size: 13px; padding: 8px 0;
  border-bottom: 1px solid #EEF0F3; }
.bpe-kv span:first-child { color: #4B5363; } .bpe-kv span:last-child { text-align: right; overflow-wrap: anywhere; }

/* Evidence: the arithmetic, the note */
.bpe-arithbox { font: 14px "IBM Plex Mono", monospace; background: #F9FAFB; border: 1px solid #EEF0F3; border-radius: 6px;
  padding: 12px 14px; overflow-x: auto; white-space: nowrap; }
.bpe-quote { margin: 0; padding: 10px 14px; border-left: 3px solid #CDD2DA; color: #4B5363; background: #FBFBFC; font-size: 13px; }

.bpe-timeline { list-style: none; margin: 0; padding: 0; }
.bpe-timeline li { display: grid; grid-template-columns: 150px 16px minmax(0, 1fr); gap: 12px; padding: 10px 0; font-size: 13px; }
.bpe-timeline time { color: #4B5363; font: 12px "IBM Plex Mono", monospace; }
.bpe-timeline .dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 5px; background: #17663F; }
.bpe-timeline .dot.g { background: #8A93A3; }
.bpe-timeline b { font-weight: 500; }

/* Calculations: the dashed draft note, group rows */
.st-key-draftnote { border: 1px dashed #CDD2DA; border-radius: 8px; background: #FFFFFF; padding: 4px 16px; }
.st-key-draftnote p { font-size: 13px; color: #4B5363; }
[class*="st-key-tgroup_"] { background: #FBFBFC; border-bottom: 1px solid #EEF0F3; }
.st-key-calc_tools { background: #FFFFFF; border-bottom: 1px solid #E3E6EB; padding: 10px 16px; border-radius: 8px 8px 0 0; }
.st-key-panel_facts { padding: 10px 20px; }

/* New report: steps, the file, what happens next */
.bpe-steps { display: flex; flex-wrap: wrap; list-style: none; margin: 0 -20px 8px; padding: 0; border-bottom: 1px solid #E3E6EB; }
.bpe-steps li { flex: 1 1 160px; display: flex; align-items: center; gap: 10px; padding: 14px 20px; font-size: 13px;
  color: #5F6775; border-bottom: 2px solid transparent; margin-bottom: -1px; }
.bpe-steps li b { width: 24px; height: 24px; border-radius: 50%; display: grid; place-items: center;
  font: 500 12px "IBM Plex Mono", monospace; background: #EEF0F3; color: #4B5363; flex: none; }
.bpe-steps li.done b { background: #E7F4EC; color: #17663F; }
.bpe-steps li.on { color: #15181E; font-weight: 500; border-bottom-color: #2747C9; }
.bpe-steps li.on b { background: #2747C9; color: #FFFFFF; }
.bpe-filerow { display: flex; align-items: center; gap: 14px; padding: 14px 16px; border: 1px solid #E3E6EB;
  border-radius: 8px; background: #F9FAFB; }
.bpe-pdf { width: 36px; height: 44px; border-radius: 4px; background: #FDECEA; color: #A1221B; display: grid;
  place-items: center; font: 600 10px "IBM Plex Mono", monospace; flex: none; }
.bpe-next { display: flex; gap: 12px; font-size: 13px; color: #4B5363; padding: 6px 0; }
.bpe-next b { color: #15181E; font-weight: 500; display: block; }
.bpe-stepnum { width: 22px; height: 22px; border-radius: 50%; border: 1px solid #CDD2DA; display: grid; place-items: center;
  font: 500 11px "IBM Plex Mono", monospace; flex: none; color: #4B5363; }

/* pager under a long list */
[class*="st-key-pager_"] { padding: 6px 12px; border-top: 1px solid #EEF0F3; }
[class*="st-key-pager_"] button { min-height: 30px; }

/* review screen (D-091, D-092) */
[class*="st-key-approve_"] button, .st-key-approve_main button { min-height: 44px; font-size: 15px; }
.st-key-metric_steps { background: #FFFFFF; border: 1px solid #E3E6EB; border-radius: 8px; padding: 8px 12px; gap: 4px; }
.st-key-review_work { gap: 0; }
.st-key-review_bar { background: #FFFFFF; border-bottom: 1px solid #E3E6EB; padding: 12px 20px; }
.st-key-review_bar h1 { font-size: 18px; margin: 0; padding: 0; }
.bpe-track { width: 160px; height: 6px; border-radius: 3px; background: #EEF0F3; overflow: hidden; display: inline-block;
  vertical-align: middle; margin-right: 10px; }
.bpe-track b { display: block; height: 100%; background: #17663F; }
.bpe-progress { font-size: 13px; color: #4B5363; white-space: nowrap; }
[data-testid="stHorizontalBlock"]:has(.st-key-queue) { gap: 0; align-items: stretch; }
.st-key-queue { background: #FFFFFF; border-right: 1px solid #E3E6EB; min-height: 100%; gap: 0; padding: 0 0 12px; }
.st-key-queue_head { padding: 12px; border-bottom: 1px solid #EEF0F3; gap: 10px; }
.bpe-qgroup { padding: 10px 12px 4px; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase;
  color: #5F6775; font-weight: 600; }
[class*="st-key-qrow_"] { padding: 0 12px 0 9px; border-left: 3px solid transparent; }
[class*="st-key-q_"] button, [class*="st-key-q_"] button > div, .st-key-other_ways [class*="st-key-rv_mark_"] button,
.st-key-other_ways [class*="st-key-rv_mark_"] button > div { justify-content: flex-start !important; text-align: left; }
[class*="st-key-q_"] button { min-height: 34px; padding: 0; font-size: 13px; width: 100%; }
[class*="st-key-q_"] button p, .st-key-other_ways [class*="st-key-rv_mark_"] button p { text-align: left; }
.st-key-other_ways [class*="st-key-rv_mark_"] button { width: 100%; padding: 8px 0; border-bottom: 1px solid #F1F2F5;
  border-radius: 0; color: #15181E; }
.st-key-queue, .st-key-viewer, .st-key-insp { min-height: calc(100vh - 130px); }
.bpe-qval { font: 12px "IBM Plex Mono", monospace; color: #4B5363; text-align: right; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.bpe-qmore { padding: 12px; font-size: 12px; color: #5F6775; }
.st-key-viewer { background: #E9EBEF; min-height: 100%; gap: 0; }
.st-key-vbar { background: #FFFFFF; border-bottom: 1px solid #E3E6EB; padding: 6px 12px; }
.st-key-vbar button { min-height: 32px; padding: 0 8px; }
.st-key-vbar .bpe-num { white-space: nowrap; }
.st-key-vbar [data-testid="stCheckbox"] p { white-space: nowrap; }
.st-key-vpage { padding: 16px; }
.st-key-strip { background: #FFFFFF; border: 1px solid #E3E6EB; border-radius: 8px; padding: 14px 16px; margin: 16px 16px 0; gap: 12px; }
.st-key-insp { background: #FFFFFF; border-left: 1px solid #E3E6EB; min-height: 100%; gap: 0; }
[class*="st-key-isec"] { padding: 16px 20px; border-bottom: 1px solid #EEF0F3; gap: 10px; }
.bpe-big { font: 500 26px "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.bpe-unit { font-size: 13px; color: #4B5363; }
.bpe-dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 14px; font-size: 13px; margin: 0; }
.bpe-dl dt { color: #4B5363; } .bpe-dl dd { margin: 0; overflow-wrap: anywhere; }
.bpe-check { display: flex; gap: 8px; font-size: 13px; color: #17663F; }
.bpe-checkwarn { font-size: 13px; color: #7A4900; }
.bpe-otherways { font-size: 12px; color: #5F6775; padding-top: 6px; }
.st-key-other_ways [data-testid="stExpander"] details, .st-key-other_ways [data-testid="stPopover"] button {
  border: 0; border-bottom: 1px solid #F1F2F5; border-radius: 0; background: transparent; }
.st-key-other_ways [data-testid="stPopover"] button { justify-content: space-between; width: 100%; padding: 8px 0; }
.bpe-calcbox { background: #F2F9F5; border: 1px solid #CFE7D8; border-radius: 8px; padding: 14px;
  display: flex; flex-direction: column; gap: 8px; }
.bpe-calcbox .lbl { font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: #17663F; font-weight: 600; }
.bpe-arith { font: 13px "IBM Plex Mono", monospace; overflow-x: auto; white-space: nowrap; }
.bpe-pass { color: #17663F; font-weight: 600; font-size: 13px; }
.bpe-fail { color: #A1221B; font-weight: 600; font-size: 13px; }
.bpe-itable { border-collapse: collapse; width: 100%; font-size: 13px; }
.bpe-itable th { text-align: left; font-weight: 500; font-size: 12px; color: #4B5363; padding: 8px 12px 8px 0; border-bottom: 1px solid #E3E6EB; }
.bpe-itable td { padding: 9px 12px 9px 0; border-bottom: 1px solid #EEF0F3; }
.bpe-itable .r { text-align: right; }

/* Extract, as in the approved mockup (2026-10-28): the bar, then white cards on the light page; the page
   never scrolls. Heights: the bar ~78px, the two cards on top share what is left above a fixed drawer. */
.st-key-extract_work { gap: 14px; padding: 0 16px 16px; }
.st-key-extract_bar { padding: 12px 4px 0; }
.st-key-extract_bar h1 { font-size: 21px; font-weight: 600; margin: 0; padding: 0; }
.bpe-note { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: #2E3443; background: #EEF2FD;
  border-radius: 8px; padding: 6px 12px; white-space: nowrap; }
.bpe-note .bpe-icon { color: #2747C9; font-size: 18px; }
.st-key-extract_bar button { min-height: 40px; font-weight: 600; }
.st-key-ex_upper { height: calc(100vh - 517px); min-height: 320px; overflow: hidden; }
.st-key-ex_upper > div, [data-testid="stHorizontalBlock"]:has(> div .st-key-ex_whole) { height: 100%;
  align-items: stretch; }
[data-testid="stColumn"]:has(> div > .st-key-ex_whole) { flex: 0 0 278px !important; width: 278px !important; }
.st-key-ex_whole { background: #FFFFFF; border: 1px solid #E3E6EB; border-radius: 8px; padding: 12px 14px; gap: 10px;
  height: 100%; overflow: hidden; }
.st-key-ex_thumb { border: 1px solid #E3E6EB; border-radius: 6px; padding: 4px; flex: 1 1 auto; min-height: 0;
  overflow: hidden; display: flex !important; align-items: center; justify-content: center; }
.st-key-ex_thumb img { max-height: calc(100vh - 640px); width: auto !important; max-width: 100%; margin: 0 auto;
  display: block; }
.st-key-ex_whole button { min-height: 36px; font-size: 13px; font-weight: 600; }
.bpe-cap { font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: #4B5363; font-weight: 600; }
.st-key-ex_page { gap: 0; height: 100%; display: flex; flex-direction: column; background: #FFFFFF;
  border: 1px solid #E3E6EB; border-radius: 8px; overflow: hidden; }
.st-key-ex_opened { padding: 10px 12px 6px; flex: none; }
.st-key-ex_search { position: relative; }
.st-key-ex_search [data-testid="stTextInput"] input { min-height: 36px; font-size: 14px; }
.st-key-ex_search [data-baseweb="input"], .st-key-ex_search [data-baseweb="base-input"] { background: #FFFFFF; }
.st-key-ex_search [data-baseweb="input"] { border: 1px solid #D5D9E0; border-radius: 8px; }
.st-key-ex_find_clear { position: absolute !important; right: 6px; top: 50%; transform: translateY(-50%); width: auto !important;
  z-index: 2; }
.st-key-ex_find_clear button { min-height: 26px; padding: 0 4px; color: #4B5363; }
.bpe-synced { display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 12px; border-radius: 8px;
  background: #E7F4EC; color: #17663F; font-size: 13px; white-space: nowrap; }
.bpe-synced .bpe-icon { font-size: 17px; }
.st-key-ex_pagegroup, .st-key-ex_zoomgroup { border: 1px solid #D5D9E0; border-radius: 8px; padding: 0 2px; height: 38px;
  flex-wrap: nowrap !important; }
.st-key-ex_pagegroup button, .st-key-ex_zoomgroup button { min-height: 34px; padding: 0 8px; border: 0;
  background: transparent; }
.st-key-ex_pagegroup [data-testid="stNumberInput"] input { min-height: 32px; padding: 0 2px; text-align: right;
  font: 13px "IBM Plex Mono", monospace; background: transparent; }
.st-key-ex_pagegroup [data-baseweb="input"], .st-key-ex_pagegroup [data-baseweb="base-input"],
.st-key-ex_pagegroup [data-testid="stNumberInputContainer"], .st-key-ex_pagegroup [data-testid="stNumberInputContainer"] > div {
  border: 0 !important; background: transparent !important; box-shadow: none !important; }
.st-key-ex_pagegroup [data-testid="stNumberInputStepDown"], .st-key-ex_pagegroup [data-testid="stNumberInputStepUp"] {
  display: none; }
.bpe-of { color: #4B5363; white-space: nowrap; padding: 0 10px 0 4px; }
.bpe-zoom { display: block; min-width: 52px; text-align: center; border-left: 1px solid #E3E6EB;
  border-right: 1px solid #E3E6EB; line-height: 36px; }
.st-key-ex_hits { min-height: 40px; padding: 0 12px 6px; flex: none; flex-wrap: nowrap !important; }
.st-key-ex_hits button { min-height: 30px; padding: 0 4px; font: 13px "IBM Plex Mono", monospace; }
.bpe-hitlabel { font-size: 13px; color: #4B5363; white-space: nowrap; padding-right: 6px; }
.st-key-ex_sheet { height: calc(100vh - 607px) !important; flex: none !important; overflow: hidden !important;
  padding: 0 20px; display: block !important; background: #EEF0F3; }
/* the close-up strip fits the space, so the boxed figure is always in view, in the middle */
.st-key-ex_sheet [data-testid="stImage"], .st-key-ex_sheet [data-testid="stImageContainer"],
.st-key-ex_sheet [data-testid="stFullScreenFrame"] { display: flex; justify-content: center; width: 100%; }
.st-key-ex_sheet img { max-height: calc(100vh - 607px); width: auto !important; max-width: 100%; margin: 0 auto;
  display: block; object-fit: contain; }
.st-key-ex_drawer { gap: 0; }
[data-testid="stHorizontalBlock"]:has(> div .st-key-ex_list) { align-items: stretch; }
[data-testid="stColumn"]:has(> div > .st-key-ex_side) { flex: 0 0 330px !important; width: 330px !important; }
.st-key-ex_list { gap: 0; background: #FFFFFF; border: 1px solid #E3E6EB; border-radius: 8px; overflow: hidden; }
.st-key-ex_tabs { border-bottom: 1px solid #E3E6EB; padding: 0 16px; }
.st-key-ex_tabs [data-testid="stButtonGroup"] button, .st-key-ex_tabs [data-testid="stButtonGroup"] button:hover {
  border: 0; border-radius: 0; background: transparent !important;
  border-bottom: 2px solid transparent; color: #4B5363; min-height: 44px; font-size: 14px; }
.st-key-ex_tabs [data-testid="stButtonGroup"] button[data-testid="stBaseButton-pillsActive"] { border-bottom-color: #2747C9;
  color: #2747C9; background: transparent; font-weight: 600; }
.st-key-ex_filters [data-testid="stButtonGroup"] { justify-content: flex-start; }
.st-key-ex_filters [data-testid="stButtonGroup"] button { border: 1px solid #D5D9E0; border-radius: 999px; min-height: 30px;
  border-bottom-width: 1px; font-size: 13px; color: #2E3443; font-weight: 400; }
.st-key-ex_tabs .st-key-ex_filters [data-testid="stButtonGroup"] button[data-testid="stBaseButton-pillsActive"] {
  background: #EEF2FD; border: 1px solid #2747C9; color: #2747C9; font-weight: 500; }
.bpe-exhead { display: grid; grid-template-columns: 2fr 1fr 0.37fr 1.15fr; gap: 12px; padding: 9px 18px;
  border-bottom: 1px solid #E3E6EB; font-size: 13px; font-weight: 500; color: #4B5363; }
.st-key-ex_rows { gap: 0; min-height: 210px; }
[class*="st-key-exrow_"] { padding: 0 18px; border-bottom: 1px solid #EEF0F3; }
[class*="st-key-exrow_"] button, [class*="st-key-exrow_"] button > div { justify-content: flex-start !important; }
[class*="st-key-exrow_"] button { min-height: 34px; padding: 0; font-size: 14px; width: 100%; }
[class*="st-key-exrow_"] button p { text-align: left; }
[class*="st-key-exrow_"] .bpe-qval { text-align: left; font-size: 13px; color: #2E3443; }
.st-key-ex_pager { padding: 8px 18px; border-top: 1px solid #EEF0F3; }
.st-key-ex_pager button { min-height: 30px; padding: 0 6px; border: 1px solid #D5D9E0; }
.st-key-ex_side { background: #FFFFFF; border: 1px solid #E3E6EB; border-radius: 8px; padding: 16px 16px; gap: 12px;
  height: 100%; }
.bpe-abouttitle { font-size: 18px; font-weight: 600; margin-top: 10px; color: #15181E; }
.bpe-boxed { border: 1px solid #E3E6EB; border-radius: 8px; }
.bpe-boxedpage { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid #EEF0F3;
  font-size: 13px; color: #2E3443; }
.bpe-boxedpage .bpe-icon { color: #5F6775; }
.bpe-boxedval { padding: 14px 12px; text-align: center; font-size: 21px; font-weight: 500; color: #15181E;
  font-variant-numeric: tabular-nums; }
.st-key-ex_side button { min-height: 40px; font-weight: 600; }
.bpe-vs { display: grid; grid-template-columns: 1fr auto; gap: 4px 10px; font-size: 13px; border: 1px solid #E3E6EB;
  border-radius: 8px; padding: 10px 12px; }
.bpe-exchecks td, .bpe-exchecks th { padding-left: 18px; }
/* Review, as in the approved mockup (2026-10-28): the bar, then three white cards on the light page */
.st-key-review_work { gap: 10px; padding: 0 10px 10px; }
.st-key-review_bar { background: transparent; border-bottom: 0; padding: 10px 8px 0; }
.st-key-review_bar h1 { font-size: 20px; font-weight: 700; margin: 0; padding: 0; }
.st-key-review_bar button { min-height: 38px; font-weight: 600; }
.st-key-rv_finish button { background: #2747C9 !important; border-color: #2747C9 !important; color: #FFFFFF !important; }
.st-key-rv_finish button * { color: #FFFFFF !important; }
.bpe-progress { display: flex; align-items: center; gap: 10px; }
.bpe-track { width: 110px; height: 5px; margin-right: 0; }
.bpe-progress .bpe-num { font-family: "IBM Plex Sans", sans-serif; font-size: 12px; }
[data-testid="stHorizontalBlock"]:has(> div .st-key-queue) { gap: 8px; align-items: stretch; flex-wrap: nowrap !important; }
[data-testid="stHorizontalBlock"]:has(> div .st-key-queue) > [data-testid="stColumn"] { min-width: 0; overflow: hidden; }
.st-key-queue, [class*="st-key-viewer_fit_"], .st-key-insp { background: #FFFFFF; border: 1px solid #E3E6EB;
  border-radius: 8px; height: calc(100vh - 140px) !important; min-height: 0 !important; overflow: hidden;
  flex: none !important; }
.st-key-queue { padding: 0; gap: 0; display: flex; flex-direction: column; }
.st-key-queue_head { padding: 10px 8px; border-bottom: 1px solid #EEF0F3; gap: 8px; flex: none; }
.st-key-queue_head [data-baseweb="input"] { border: 1px solid #D5D9E0; border-radius: 6px; background: #FFFFFF; }
.st-key-queue_head [data-baseweb="base-input"] { background: #FFFFFF; }
.st-key-queue_head [data-testid="stButtonGroup"] button { min-height: 24px; padding: 0 8px;
  border: 1px solid #D5D9E0; border-radius: 999px; }
.st-key-queue_head [data-testid="stButtonGroup"] button p { font-size: 11px; }
.st-key-queue_head [data-testid="stButtonGroup"] button[data-testid="stBaseButton-pillsActive"] { background: #EEF2FD;
  border-color: #2747C9; color: #2747C9; }
.st-key-queue_list { overflow-y: auto; flex: 1 1 auto; gap: 0; padding-bottom: 8px; }
[class*="st-key-rv_group_"] button { min-height: 38px; padding: 0 10px; border-top: 1px solid #EEF0F3; border-radius: 0;
  justify-content: space-between !important; }
[class*="st-key-rv_group_"] button p { font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase; color: #4B5363;
  font-weight: 600; }
[class*="st-key-rv_group_"] button > div { justify-content: space-between !important; width: 100%; }
[class*="st-key-qrow_"] { padding: 0 10px 0 7px; border-left: 3px solid transparent; border-bottom: 1px solid #F1F2F5; }
[class*="st-key-q_"] button { min-height: 33px; font-size: 11px; }
.bpe-qstat { display: flex; align-items: center; justify-content: flex-end; gap: 6px; font-size: 10px; white-space: nowrap; }
.bpe-qstat span { display: inline-flex; align-items: center; gap: 4px; }
.bpe-qstat i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; display: inline-block; }
.bpe-qstat .green { color: #17663F; } .bpe-qstat .blue { color: #2747C9; } .bpe-qstat .grey { color: #8A93A3; }
.bpe-qcount { min-width: 16px; height: 16px; border-radius: 50%; border: 1px solid #D5D9E0; display: inline-grid;
  place-items: center; font-size: 9px; color: #4B5363; }
[class*="st-key-viewer_fit_"] { gap: 0; display: flex; flex-direction: column; }
.st-key-vbar { background: #FFFFFF; border-bottom: 1px solid #E3E6EB; padding: 6px 8px; flex: none; flex-wrap: nowrap !important;
  overflow: hidden; }
.st-key-review_bar button p { white-space: nowrap; overflow: visible; }
.st-key-vbar { gap: 6px !important; }
.st-key-vbar button { min-height: 28px; padding: 0 7px; }
.st-key-vbar button p { font-size: 11px; }
.st-key-vbar [data-testid="stIconMaterial"] { font-size: 16px; }
.st-key-rv_pagegroup, .st-key-rv_zoomgroup { border: 1px solid #D5D9E0; border-radius: 6px; height: 30px; flex-wrap: nowrap !important; }
.st-key-rv_pagegroup button, .st-key-rv_zoomgroup button { border: 0; background: transparent; min-height: 26px; }
.st-key-rv_pagegroup [data-testid="stNumberInput"] input { min-height: 24px; padding: 0 2px; text-align: center;
  font: 11px "IBM Plex Mono", monospace; }
.st-key-rv_pagegroup [data-testid="stNumberInputContainer"] { border: 1px solid #D5D9E0 !important; border-radius: 4px; }
.st-key-rv_pagegroup [data-testid="stNumberInputStepDown"], .st-key-rv_pagegroup [data-testid="stNumberInputStepUp"] {
  display: none; }
.st-key-rv_zoomgroup .bpe-zoom { min-width: 44px; line-height: 28px; font-size: 11px; }
.st-key-vbar .bpe-of { font-size: 11px; padding: 0 6px; }
.st-key-rv_fitpage_on button, .st-key-rv_fitwidth_on button { border: 1px solid #2747C9; color: #2747C9;
  background: #EEF2FD; }
.st-key-rv_mark_toggle button { color: #8A93A3; }
.st-key-vpage { background: #EEF0F3; flex: 1 1 auto; overflow-y: auto; padding: 14px 32px; position: relative; }
.st-key-rv_fit_float { position: absolute !important; top: 18px; right: 36px; width: auto !important; z-index: 3; }
.st-key-rv_fit_float button { min-height: 28px; font-size: 11px; padding: 0 10px; }
.st-key-vpage [data-testid="stImage"] img { box-shadow: 0 1px 4px rgba(20, 24, 30, 0.14); }
.st-key-viewer_fit_page .st-key-vpage { overflow: hidden; }
.st-key-viewer_fit_page .st-key-vpage [data-testid="stImage"], .st-key-viewer_fit_page .st-key-vpage
  [data-testid="stFullScreenFrame"] { display: flex; justify-content: center; }
.st-key-viewer_fit_page .st-key-vpage img { max-height: calc(100vh - 220px); width: auto !important; max-width: 100%; }
.st-key-insp { overflow-y: auto; gap: 0; }
[class*="st-key-isec"] { padding: 12px 14px; border-bottom: 1px solid #EEF0F3; gap: 8px; }
.st-key-isec_head .bpe-big { font: 600 20px "IBM Plex Sans", sans-serif; }
.bpe-bigunit { font-size: 17px; font-weight: 500; margin-left: 4px; }
.bpe-rvtitle { font-size: 17px; font-weight: 700; margin: 8px 0 4px; color: #15181E; }
.st-key-insp .bpe-dl { font-size: 11px; grid-template-columns: 88px minmax(0, 1fr); gap: 10px 10px; padding: 0;
  margin: 0; }
.st-key-isec_head .bpe-minor { font-size: 11px; }
.st-key-insp .bpe-dl .bpe-icon { font-size: 14px; color: #4B5363; }
.bpe-dt { font-size: 11px; color: #4B5363; }
.st-key-rv_srcpage { border: 1px solid #D5D9E0; border-radius: 6px; padding: 0 4px 0 10px; height: 28px; }
.st-key-rv_srcpage .bpe-num { font-size: 11px; }
.st-key-rv_srcpage button { min-height: 24px; padding: 0 4px; }
.st-key-insp button { font-size: 11px; min-height: 30px; }
.bpe-h { font-size: 11px; font-weight: 600; color: #15181E; margin-bottom: 8px; }
.bpe-conf { display: inline-flex; align-items: center; gap: 5px; }
.bpe-conf i { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.bpe-conf.hi i { background: #17663F; } .bpe-conf.lo i { background: #B26B00; }
.st-key-rv_snippet { background: #F2F5FD; border: 1px solid #E3E8F7; border-radius: 6px; padding: 8px 10px; gap: 6px; }
.bpe-h2 { font-size: 11px; font-weight: 600; color: #15181E; }
</style>
"""

MARK = '<img src="app/static/logo-icon.svg" alt="" width="28" height="28">'
CHIP_KINDS = {"ok", "warn", "bad", "calc", "muted"}


def apply() -> None:
    """Add the redesign's CSS to the page; call once per run, before anything is drawn."""
    st.html(CSS)


def wide_page() -> None:
    """Kept for the Extract page: every page now uses the whole width beside the menu."""


def mark_svg() -> str:
    """The product mark: an image file, since Streamlit strips SVG from st.html."""
    return MARK


def chip(text: str, kind: str, symbol: str | None = None) -> str:
    """A status pill: kind ok (green) | warn (amber) | bad (red) | calc (teal) | muted (grey); a dot in front,
    or the named icon (symbol)."""
    assert kind in CHIP_KINDS, kind
    mark = icon(symbol, filled=True) if symbol else "<i></i>"
    return f'<span class="bpe-chip bpe-{kind}">{mark}{escape(text)}</span>'


def icon(name: str, filled: bool = False) -> str:
    """A Material Symbols icon inside st.html, from the icon font Streamlit already loads (it strips SVG).
    A filled check_circle is drawn in CSS: the font has no filled style."""
    if name == "check_circle" and filled:
        return '<span class="bpe-tick" aria-hidden="true"></span>'
    return f'<span class="bpe-icon{" fill" if filled else ""}" aria-hidden="true">{escape(name)}</span>'


def bar(fraction: float) -> str:
    """A thin green progress bar."""
    return f'<div class="bpe-bar"><b style="width:{max(0.0, min(1.0, fraction)) * 100:.0f}%"></b></div>'


def stat_strip(stats: Sequence[tuple[str, str, str]]) -> None:
    """One row of summary figures: (label, value, note); a value may end in ' / total'."""
    cells = []
    for label, value, note in stats:
        number, _, total = value.partition(" / ")
        shown = escape(number) + (f"<small> / {escape(total)}</small>" if total else "")
        kind = "" if re.fullmatch(r"[\d.,%\s/()+-]+", value) else ' class="txt"'
        cells.append(f'<div class="bpe-stat"><span>{escape(label)}</span><b{kind}>{shown}</b><em>{escape(note)}</em></div>')
    st.html(f'<div class="bpe-stats">{"".join(cells)}</div>')


def initials(name: str) -> str:
    parts = [p for p in name.replace(".", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() if len(parts) > 1 else name[:2].upper()
