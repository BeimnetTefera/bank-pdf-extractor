"""Export button: writes one fiscal year's approved-only workbook (all banks) and offers it for download."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import streamlit as st

from src.config_loader import AppConfig
from src.database import connect
from src.excel_exporter import XLSX_MIME, ExportError, export_year, fiscal_year_label


def export_button(config: AppConfig, end_year: int, key: str) -> None:
    label = fiscal_year_label(end_year)
    if st.button(f"Export {label} to Excel (all banks)", icon=":material/table_view:", key=key, width="stretch",
                 help=f"One workbook for {label}: every saved bank's approved values, with source page and reviewer. "
                      "Exporting again replaces it with an up-to-date copy."):
        try:
            with closing(connect(config.settings.database_path)) as conn:
                result = export_year(conn, config, end_year,
                                     exported_by=st.session_state.get("reviewer", "").strip() or "not given")
        except ExportError as exc:
            st.session_state.pop(f"{key}_result", None)
            st.error(str(exc), icon=":material/error:")
        else:
            st.session_state[f"{key}_result"] = result

    result = st.session_state.get(f"{key}_result")
    if result is not None and Path(result.path).is_file():
        st.success(f"Saved **{result.path.name}**: {result.reports} bank report(s), {result.values} approved "
                   f"value(s), in `{result.path.parent}`", icon=":material/check_circle:")
        for warning in result.warnings:
            st.warning(warning, icon=":material/content_copy:")
        st.download_button("Download", data=result.path.read_bytes(), file_name=result.path.name, mime=XLSX_MIME,
                           icon=":material/download:", key=f"{key}_download", width="stretch")
