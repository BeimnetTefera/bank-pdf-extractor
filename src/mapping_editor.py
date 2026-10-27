"""Add or remove one piece of wording in config/mappings.yaml without losing its comments.

The Templates page uses this so a reviewer can maintain the wording without editing YAML.
Only the line concerned is changed; every comment and the layout of the rest of the file
stay as they are. Before anything is written, the new text is read back: it must mean
exactly the old wording plus (or minus) the one change, and pass the same checks as when
the app starts (config_loader.load_mappings). Otherwise nothing is written.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.config_loader import ConfigError, load_mappings
from src.metric_mapper import normalize_text

KINDS = {
    "aliases": "Table label",
    "text_after": "Words after a number",
    "text_before": "Words before a number",
}
_ENTRY = re.compile(r"^(\s*)- metric_id:\s*['\"]?([\w.-]+)['\"]?\s*(#.*)?$")
_FIELD = re.compile(r"^(\s+)(aliases|text_after|text_before):(.*)$")


class MappingEditError(ValueError):
    """Raised when the wording cannot be changed; the message says why."""


@dataclass(frozen=True)
class Wording:
    metric_id: str
    kind: str  # aliases | text_after | text_before
    text: str


def list_wording(path: Path) -> list[Wording]:
    """Every alias and phrase in the file, in file order."""
    data = _load(path.read_text(encoding="utf-8"))
    return [Wording(e["metric_id"], kind, text) for e in data for kind in KINDS for text in e.get(kind) or []]


def add_wording(path: Path, metric_id: str, kind: str, text: str, note: str, known_metric_ids: set[str]) -> None:
    """Add one alias or phrase for a metric. note (e.g. "Zemen 2025/26 p22") is written as a comment."""
    _check_kind(kind)
    text = " ".join(text.split())
    if not text:
        raise MappingEditError("Type the wording to add.")
    old = path.read_text(encoding="utf-8")
    data = _load(old)
    _refuse_duplicate(data, metric_id, kind, text)
    comment = _comment(note)
    lines = old.splitlines(keepends=True)
    start, end = _entry_span(lines, metric_id)
    if start is None:
        new_lines = _append_entry(lines, metric_id, kind, text, comment)
    else:
        new_lines = _add_to_entry(lines, start, end, kind, text, comment)
    expected = copy.deepcopy(data)
    entry = next((e for e in expected if e["metric_id"] == metric_id), None)
    if entry is None:
        expected.append({"metric_id": metric_id, kind: [text]})
    else:
        entry.setdefault(kind, []).append(text)
    _write_checked(path, "".join(new_lines), expected, known_metric_ids)


def remove_wording(path: Path, metric_id: str, kind: str, text: str, known_metric_ids: set[str]) -> None:
    """Remove one alias or phrase. An entry left with no wording is removed with its lines."""
    _check_kind(kind)
    old = path.read_text(encoding="utf-8")
    data = _load(old)
    lines = old.splitlines(keepends=True)
    start, end = _entry_span(lines, metric_id)
    entry = next((e for e in data if e["metric_id"] == metric_id), None)
    if start is None or entry is None or text not in (entry.get(kind) or []):
        raise MappingEditError(f"'{text}' is not in the {KINDS[kind].lower()} wording of this metric.")
    new_lines = _remove_from_entry(lines, start, end, kind, text)
    expected = copy.deepcopy(data)
    target = next(e for e in expected if e["metric_id"] == metric_id)
    target[kind].remove(text)
    if not target[kind]:
        del target[kind]
    if not any(k in target for k in KINDS):  # mappings.yaml needs at least one list per entry
        expected.remove(target)
        new_lines = _without_entry(lines, start, end)
    _write_checked(path, "".join(new_lines), expected, known_metric_ids)


# --- finding the lines -------------------------------------------------------------------

def _entry_span(lines: list[str], metric_id: str) -> tuple[int | None, int | None]:
    """First and one-past-last line of the metric's entry (its own lines, without the blank
    line or comments that follow it)."""
    for i, line in enumerate(lines):
        match = _ENTRY.match(line.rstrip("\r\n"))
        if match and match.group(2) == metric_id:
            indent = len(match.group(1))
            end = i + 1
            while end < len(lines):
                stripped = lines[end].strip()
                current_indent = len(lines[end]) - len(lines[end].lstrip())
                if not stripped or current_indent <= indent:
                    break
                end += 1
            return i, end
    return None, None


def _field_line(lines: list[str], start: int, end: int, kind: str) -> int | None:
    for i in range(start, end):
        match = _FIELD.match(lines[i].rstrip("\r\n"))
        if match and match.group(2) == kind:
            return i
    return None


def _block_items(lines: list[str], field: int, end: int) -> list[int]:
    """Line numbers of the '- item' lines under a block-style field."""
    indent = len(lines[field]) - len(lines[field].lstrip())
    items = []
    for i in range(field + 1, end):
        current_indent = len(lines[i]) - len(lines[i].lstrip())
        if current_indent <= indent:
            break
        if lines[i].lstrip().startswith("- "):
            items.append(i)
    return items


# --- editing -----------------------------------------------------------------------------

def _add_to_entry(lines: list[str], start: int, end: int, kind: str, text: str, comment: str) -> list[str]:
    newline = _newline(lines)
    field = _field_line(lines, start, end, kind)
    if field is None:  # a new field at the end of the entry
        indent = _field_indent(lines, start, end)
        return lines[:end] + [f"{indent}{kind}: [{_quote(text)}]{comment}{newline}"] + lines[end:]
    value, trailing = _split_comment(_FIELD.match(lines[field].rstrip("\r\n")).group(3))
    if value.strip().startswith("["):  # flow style: ["a", "b"]  # comment
        inner = value.strip()[1:-1].strip()
        items = f"{inner}, {_quote(text)}" if inner else _quote(text)
        head = lines[field][: lines[field].index(":") + 1]
        note = f"{trailing}; {comment.strip()[2:]}" if trailing and comment else (trailing or comment.strip())
        return lines[:field] + [f"{head} [{items}]{'  ' + note if note else ''}{newline}"] + lines[field + 1:]
    items = _block_items(lines, field, end)
    if not items:
        raise MappingEditError(f"The '{kind}' list of this metric has an unusual layout; edit mappings.yaml by hand.")
    last = items[-1]
    indent = lines[last][: len(lines[last]) - len(lines[last].lstrip())]
    return lines[: last + 1] + [f"{indent}- {_quote(text)}{comment}{newline}"] + lines[last + 1:]


def _remove_from_entry(lines: list[str], start: int, end: int, kind: str, text: str) -> list[str]:
    newline = _newline(lines)
    field = _field_line(lines, start, end, kind)
    if field is None:
        raise MappingEditError(f"Could not find the '{kind}' list in mappings.yaml; edit it by hand.")
    value, trailing = _split_comment(_FIELD.match(lines[field].rstrip("\r\n")).group(3))
    if value.strip().startswith("["):
        remaining = [v for v in yaml.safe_load(value.strip()) if v != text]
        if not remaining:
            return lines[:field] + lines[field + 1:]
        head = lines[field][: lines[field].index(":") + 1]
        items = ", ".join(_quote(v) for v in remaining)
        return lines[:field] + [f"{head} [{items}]{'  ' + trailing if trailing else ''}{newline}"] + lines[field + 1:]
    items = _block_items(lines, field, end)
    keep = [i for i in items if yaml.safe_load(lines[i].strip())[0] != text]
    if not keep:
        last = items[-1] if items else field
        return lines[:field] + lines[last + 1:]
    drop = next(i for i in items if i not in keep)
    return lines[:drop] + lines[drop + 1:]


def _without_entry(lines: list[str], start: int, end: int) -> list[str]:
    """Remove an entry's lines and the blank line after it."""
    after = end + 1 if end < len(lines) and not lines[end].strip() else end
    return lines[:start] + lines[after:]


def _append_entry(lines: list[str], metric_id: str, kind: str, text: str, comment: str) -> list[str]:
    newline = _newline(lines)
    indent = next((m.group(1) for m in (_ENTRY.match(line.rstrip("\r\n")) for line in lines) if m), "  ")
    body = list(lines)
    if body and not body[-1].endswith(("\n", "\r")):
        body[-1] += newline
    return body + [newline, f"{indent}- metric_id: {metric_id}{newline}",
                   f"{indent}  {kind}: [{_quote(text)}]{comment}{newline}"]


# --- checks and helpers ------------------------------------------------------------------

def _write_checked(path: Path, text: str, expected: list[dict], known_metric_ids: set[str]) -> None:
    """Write text only if it reads back as expected and passes the start-up checks."""
    if _load(text) != expected:
        raise MappingEditError("The change could not be made safely, so mappings.yaml was not changed. "
                               "Edit the file by hand.")
    folder = path.parent
    handle, temp_name = tempfile.mkstemp(prefix=".mappings-", suffix=".yaml", dir=folder)
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        try:
            load_mappings(temp, known_metric_ids)
        except ConfigError as exc:
            raise MappingEditError(f"mappings.yaml was not changed: {exc}") from exc
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _load(text: str) -> list[dict]:
    raw = yaml.safe_load(text) or {}
    return raw.get("mappings") or []


def _refuse_duplicate(data: list[dict], metric_id: str, kind: str, text: str) -> None:
    wanted = normalize_text(text)
    for entry in data:
        for existing in entry.get(kind) or []:
            if normalize_text(existing) != wanted:
                continue
            if entry["metric_id"] == metric_id:
                raise MappingEditError(f"'{text}' is already listed for this metric.")
            raise MappingEditError(f"'{text}' already means {entry['metric_id']}. The same wording for two "
                                   "metrics would make both ambiguous, so it was not added.")


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise MappingEditError(f"Unknown kind of wording '{kind}'.")


def _quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a valid double-quoted YAML string


def _comment(note: str) -> str:
    note = " ".join(note.split()).replace("#", "")
    return f"  # {note}" if note else ""


def _split_comment(value: str) -> tuple[str, str]:
    """'["a"]   # BOA p111' -> ('["a"]', '# BOA p111'); a '#' inside quotes is kept."""
    quote = None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip(), value[i:].strip()
    return value.rstrip(), ""


def _field_indent(lines: list[str], start: int, end: int) -> str:
    for i in range(start + 1, end):
        if _FIELD.match(lines[i].rstrip("\r\n")):
            return lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    entry_indent = len(lines[start]) - len(lines[start].lstrip())
    return " " * (entry_indent + 2)


def _newline(lines: list[str]) -> str:
    return "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
