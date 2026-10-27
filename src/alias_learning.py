"""Learn table-row wording from review decisions (decision D-043).

When a reviewer approves a value taken from a table row the app did not recognise
(a marked box or "Use this figure for a metric"), that row's printed label is offered as
new wording for the metric. Nothing is learned automatically: the reviewer presses Add,
and the label is written to config/learned_aliases.yaml with the bank, who added it and
where it was seen. From then on the label is matched like the aliases in mappings.yaml,
in that bank's reports only (decision D-046): each bank keeps its own wording.

Callers pass the wording that applies to the report's bank (AppConfig.mappings_for).

Only table-row labels are learned. Sentence wording ("981 branches") still needs a
person to choose the right words, in mappings.yaml.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.config_loader import LabelMapping
from src.database import utc_now
from src.metric_mapper import normalize_text, strip_note_reference, strip_printed_note_reference
from src.pdf_extractor import TABLE_METHOD, RawCandidate
from src.review_service import ApprovedResult

Box = tuple[float, float, float, float]

_FILE_HEADER = """\
# Table-row labels learned from reviewed reports (decision D-043).
#
# Written by the app: in Extract & Review, "Learn wording" offers the label of each value
# a reviewer approved from a row the app did not recognise. Each entry is used like an
# alias in mappings.yaml, but only for reports of its `bank` (decision D-046). To remove
# one, delete its entry here and reload the app.

"""


class LearnError(ValueError):
    """Raised when a label cannot be learned; the message says why."""


@dataclass(frozen=True)
class AliasSuggestion:
    metric_id: str
    label: str  # as printed, without a trailing note reference
    page: int  # 1-based PDF page where it was seen
    already_used_by: tuple[str, ...] = ()  # other metrics this label already means

    @property
    def can_add(self) -> bool:
        """A label that already means another metric would make both ambiguous, so it is not offered."""
        return not self.already_used_by


def suggest_aliases(results: Iterable[ApprovedResult], candidates: list[RawCandidate],
                    mappings: tuple[LabelMapping, ...]) -> list[AliasSuggestion]:
    """Labels of approved, reviewer-entered values whose table row is not linked to the metric yet."""
    known = known_aliases(mappings)
    suggestions: dict[tuple[str, str], AliasSuggestion] = {}
    for r in results:
        box = r.evidence_box
        if not r.human_entered or box is None or not r.source_page:
            continue  # an extracted figure was matched by wording already
        label = label_in_box(candidates, r.source_page - 1, box)
        if label is None:
            continue
        key = alias_key(label)
        users = known.get(key, set())
        if r.metric_id in users:
            continue
        suggestions.setdefault((r.metric_id, key), AliasSuggestion(
            r.metric_id, label, r.source_page, tuple(sorted(users))))
    return list(suggestions.values())


def label_in_box(candidates: list[RawCandidate], page_index: int, box: Box) -> str | None:
    """The label of the table row whose value sits in the box; None if there is no single one."""
    labels = {
        strip_printed_note_reference(c.raw_label)
        for c in candidates
        if c.extraction_method == TABLE_METHOD and c.page_index == page_index and c.raw_label
        and _centre_in(c.bbox, box)
    }
    labels.discard("")
    return labels.pop() if len(labels) == 1 else None


def alias_key(label: str) -> str:
    """The form used to compare labels: case, spacing and note references ignored."""
    return strip_note_reference(normalize_text(label))


def known_aliases(mappings: tuple[LabelMapping, ...]) -> dict[str, set[str]]:
    """Comparable alias -> metric ids it is linked to."""
    known: dict[str, set[str]] = {}
    for m in mappings:
        for alias in m.aliases:
            known.setdefault(alias_key(alias), set()).add(m.metric_id)
    return known


def add_learned_alias(path: Path, mappings: tuple[LabelMapping, ...], metric_id: str, label: str,
                      seen_in: str, added_by: str, bank: str) -> None:
    """Append a label for one bank to learned_aliases.yaml (created if missing).

    mappings: the wording that already applies to that bank (AppConfig.mappings_for(bank)).
    """
    if not added_by.strip():
        raise LearnError("Enter the reviewer name.")
    if not bank or not bank.strip():
        raise LearnError("Choose the bank first: learned wording is kept per bank.")
    users = known_aliases(mappings).get(alias_key(label), set())
    if metric_id in users:
        raise LearnError(f"'{label}' is already linked to this metric.")
    if users:
        raise LearnError(f"'{label}' already means {', '.join(sorted(users))}; adding it would make both ambiguous.")
    entries = _read_entries(path)
    entries.append({"bank": bank.strip(), "metric_id": metric_id, "alias": label, "seen_in": seen_in,
                    "added_by": added_by.strip(), "added_at": utc_now()})
    body = yaml.safe_dump({"learned": entries}, sort_keys=False, allow_unicode=True, width=1000)
    path.write_text(_FILE_HEADER + body, encoding="utf-8")


def _read_entries(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("learned") if isinstance(raw, dict) else None
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise LearnError(f"{path.name}: 'learned' must be a list; fix the file before adding wording.")
    return entries


def _centre_in(bbox: Box, box: Box) -> bool:
    x, y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]
