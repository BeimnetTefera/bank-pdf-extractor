"""Smaller screens: Home, Bank Reports, Templates (metric configuration) and Settings."""

from __future__ import annotations

import streamlit as st

from src.config_loader import AppConfig

WORKFLOW = [
    ("Load a report", "Enter the bank and report year, then upload the annual report PDF.", "Available"),
    ("Check figures against the PDF", "Click a figure to see it boxed on its source page.", "Available"),
    ("Match figures to metrics", "Link figures to the Shega metrics, read units, flag conflicts.", "Phase 3"),
    ("Approve, reject or correct", "Every decision is saved with your name and time.", "Phase 4–5"),
    ("Export to Excel", "Approved figures with their source page and evidence.", "Phase 6"),
]


def home() -> None:
    st.markdown("## Bank PDF Extractor")
    st.caption("Extract figures from bank annual reports, check them against the source page, and export approved values.")
    for number, (title, text, status) in enumerate(WORKFLOW, start=1):
        with st.container(border=True):
            title_col, status_col = st.columns([5, 1], vertical_alignment="center")
            title_col.markdown(f"**{number}. {title}**  \n{text}")
            with status_col:
                if status == "Available":
                    st.badge(status, color="green", icon=":material/check:")
                else:
                    st.badge(status, color="gray")
    st.caption("Start from **Extract & Review** in the menu.")


def bank_reports() -> None:
    st.markdown("## Bank Reports")
    st.info(
        "Saved reports and their review progress will be listed here once saving is built (Phase 4).",
        icon=":material/schedule:",
    )


def templates(config: AppConfig) -> None:
    st.markdown("## Templates")
    st.caption(
        f"Metrics from config/metrics.yaml: {len(config.active_metrics())} active of {len(config.metrics)}. "
        "Edit the YAML file to change them."
    )
    st.dataframe(
        [
            {
                "Metric": m.display_name,
                "Shega column": m.shega_column,
                "Unit": m.expected_unit,
                "Active": m.active,
                "Definition": m.definition,
                "Notes": m.notes or "",
            }
            for m in config.metrics
        ],
        hide_index=True,
        width="stretch",
        height=700,
    )


def settings(config: AppConfig) -> None:
    s = config.settings
    st.markdown("## Settings")
    st.caption("From config/settings.yaml. Edit the file and reload to change them.")
    with st.container(border=True):
        st.markdown(f"**Data folder**  \n`{s.data_dir}`")
        st.markdown(f"**Upload limit**  \n{s.max_upload_mb} MB, {s.max_pages} pages")
        st.markdown(f"**Reviewer name**  \n{s.default_reviewer}")
        st.markdown(f"**Label aliases configured**  \n{len(config.mappings)}")
