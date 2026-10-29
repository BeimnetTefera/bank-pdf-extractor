"""Change the meaning (definition) of one metric in config/metrics.yaml without losing its comments.

The Templates page uses this so a reviewer can keep the meanings up to date without editing
YAML (D-093). Only the metric's `definition:` line changes; it ends with a comment naming who
changed it and when. Before anything is written the new file is read back with the same checks
as when the app starts (config_loader.load_metrics); if they fail, nothing is written.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.config_loader import ConfigError, load_metrics

_ID = re.compile(r"^\s*- id:\s*['\"]?([\w.-]+)['\"]?\s*(#.*)?$")
_DEFINITION = re.compile(r"^(\s+)definition:\s")
MIN_LENGTH = 10  # a meaning shorter than this is refused


class MetricEditError(ValueError):
    """Raised when the meaning cannot be changed; the message says why."""


def set_definition(path: Path, metric_id: str, text: str, reviewer: str) -> None:
    """Replace the metric's definition with text, signed with the reviewer's name and the date."""
    text = " ".join(text.split())
    if not reviewer.strip():
        raise MetricEditError("Enter your name on Settings first: every change is signed.")
    if len(text) < MIN_LENGTH:
        raise MetricEditError("Write the meaning in at least a short sentence.")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    current, target = None, None
    for i, line in enumerate(lines):
        found = _ID.match(line)
        if found:
            current = found.group(1)
        elif current == metric_id and _DEFINITION.match(line):
            target = i
            break
    if target is None:
        raise MetricEditError(f"No definition line found for {metric_id} in {path.name}.")
    indent = _DEFINITION.match(lines[target]).group(1)
    day = datetime.now(timezone.utc).date().isoformat()
    lines[target] = f"{indent}definition: {json.dumps(text, ensure_ascii=False)}  # edited by {reviewer.strip()}, {day}\n"
    new_text = "".join(lines)
    with tempfile.TemporaryDirectory() as folder:
        check = Path(folder) / path.name
        check.write_text(new_text, encoding="utf-8")
        try:
            metrics = load_metrics(check)
        except ConfigError as exc:
            raise MetricEditError(f"Not saved: the file would not load ({exc}).") from None
    if next(m for m in metrics if m.id == metric_id).definition != text:
        raise MetricEditError("Not saved: the meaning did not read back as written.")
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(new_text, encoding="utf-8")
    os.replace(temporary, path)
