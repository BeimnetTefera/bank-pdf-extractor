"""Streamlit entry point. Run with: streamlit run app.py

Phase 1 placeholder: shows that configuration loads. The review workflow is built in Phase 5.
"""

import streamlit as st

from src.config_loader import ConfigError, load_config

st.set_page_config(page_title="Bank PDF Extractor", layout="wide")
st.title("Bank PDF Extractor")
st.caption("Local prototype. Extracted figures are unverified candidates until reviewed.")

try:
    config = load_config()
except ConfigError as exc:
    st.error("The configuration could not be loaded. Fix the file below and reload the page.")
    st.code(str(exc), language="text")
    st.stop()

settings = config.settings
st.info("Extraction and review are not built yet (Phase 1 skeleton).")

col_metrics, col_settings = st.columns([2, 1])
with col_metrics:
    st.subheader(f"Configured metrics ({len(config.active_metrics())} active of {len(config.metrics)})")
    st.dataframe(
        [
            {
                "Metric": m.display_name,
                "ID": m.id,
                "Unit": m.expected_unit,
                "Active": m.active,
                "Notes": m.notes or "",
            }
            for m in config.metrics
        ],
        hide_index=True,
        width="stretch",
    )
with col_settings:
    st.subheader("Settings")
    st.write(f"**Data folder:** `{settings.data_dir}`")
    st.write(f"**Upload limit:** {settings.max_upload_mb} MB, {settings.max_pages} pages")
    st.write(f"**Reviewer:** {settings.default_reviewer}")
    st.write(f"**Label aliases configured:** {len(config.mappings)}")
