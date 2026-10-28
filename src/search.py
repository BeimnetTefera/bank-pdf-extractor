"""The search box in the top bar (D-092): reports, metrics and printed figures across every saved report.

A query matches a report by bank, year or file name; a metric by its name (one hit per saved
report that has it, with its approved value when there is one); and a printed figure by its
digits, so "286,226" or "286226" both find 286,226,976.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from src import review_service
from src.analysis import report_end_year
from src.config_loader import AppConfig
from src.excel_exporter import fiscal_year_label

MAX_HITS = 8


def _year(period: str) -> str:
    end_year = report_end_year(period)
    return fiscal_year_label(end_year) if end_year else period


@dataclass(frozen=True)
class Hit:
    kind: str  # report | metric | figure
    title: str
    detail: str
    document_id: str
    metric_id: str | None = None
    page: int | None = None  # 1-based, for a printed figure


def search(conn: sqlite3.Connection, config: AppConfig, query: str) -> list[Hit]:
    words = query.strip().casefold()
    if not words:
        return []
    documents = conn.execute("SELECT * FROM documents ORDER BY uploaded_at DESC").fetchall()
    hits = [Hit("report", f"{d['bank_name']} · {_year(d['reporting_period'])}", d["original_filename"], d["id"])
            for d in documents
            if words in f"{d['bank_name']} {d['reporting_period']} {d['original_filename']}".casefold()]
    names = {m.id: m.display_name for m in config.metrics}
    for metric_id, name in names.items():
        if words not in name.casefold():
            continue
        for d in documents:
            if metric_id not in json.loads(d["metric_ids"]):
                continue
            state = review_service.review_state(conn, d["id"], metric_id)
            shown = (f"approved {state.accepted_value:,f}" if state.status == "approved" and state.accepted_value is not None
                     else state.status.replace("_", " "))
            hits.append(Hit("metric", name, f"{d['bank_name']} · {_year(d['reporting_period'])} · {shown}", d["id"], metric_id))
    digits = re.sub(r"[^\d]", "", query)
    if len(digits) >= 3:
        rows = conn.execute(
            """SELECT c.document_id, c.metric_id, c.raw_label, c.raw_value_text, c.page_index, d.bank_name,
                      d.reporting_period
               FROM candidates c JOIN documents d ON d.id = c.document_id
               WHERE REPLACE(REPLACE(REPLACE(c.raw_value_text, ',', ''), ' ', ''), '.', '') LIKE ?
               ORDER BY c.document_id, c.page_index LIMIT ?""", (f"%{digits}%", MAX_HITS)).fetchall()
        hits += [Hit("figure", f"{r['raw_value_text']} · {r['raw_label'] or 'figure'}",
                     f"{r['bank_name']} · {_year(r['reporting_period'])} · PDF page {r['page_index'] + 1}",
                     r["document_id"], r["metric_id"], r["page_index"] + 1) for r in rows]
    return hits[:MAX_HITS]
