"""Link raw candidates to configured metrics using the wording in config/mappings.yaml.

Matching is explicit and conservative:
- table rows: the whole printed label must equal an alias (after normalising case,
  spacing and special characters);
- sentences: a configured phrase must sit right after the number (one extra word
  allowed, not counting a scale word: "2.76 million mobile banking users") or shortly before it (up to MAX_GAP_BEFORE words, with no other number
  in between).
A candidate that matches several metrics keeps every match and is flagged later; one
that matches none stays unmapped and remains visible in the raw figures.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.config_loader import LabelMapping
from src.pdf_extractor import NARRATIVE_METHOD, RawCandidate, is_value_token, strip_sentence_punctuation

MAX_GAP_AFTER = 1  # "10,627 [permanent] employees"
MAX_GAP_BEFORE = 4  # "Return on equity [has jumped to] 29%"
SCALE_WORDS = {"thousand", "million", "billion"}


@dataclass(frozen=True)
class MetricMatch:
    metric_id: str
    basis: str  # "table label", "words after", "words before"
    matched_text: str  # the configured alias or phrase that matched


def normalize_text(text: str) -> str:
    """Comparable form of printed text: 'Proﬁt  Before Tax' -> 'profit before tax'."""
    text = unicodedata.normalize("NFKC", text).replace("�", "'").casefold()
    text = re.sub(r"\s+([,.;:)])", r"\1", text)  # "Loans , advances" -> "loans, advances"
    return " ".join(text.split()).strip(" :")


# A trailing token that is a note reference ("16", "31(c)", "(b)", "13a") or only symbols ("»", "*%").
_NOTE_TAIL = re.compile(r"^(?:\(?\d{1,3}\s?[a-z]?\)?(?:\([a-z]\))?|\([a-z]\)|[^\w]+)$")


def strip_note_reference(label: str) -> str:
    """'income tax expense 16 (c)' -> 'income tax expense'; 'share capital 32 »' -> 'share capital'.

    Only the end of a normalized label is touched, so words inside it are never removed.
    """
    tokens = label.split()
    while len(tokens) > 1 and _NOTE_TAIL.match(tokens[-1]):
        tokens.pop()
    return " ".join(tokens).strip(" :")


def strip_printed_note_reference(label: str) -> str:
    """'Total Assets 16' -> 'Total Assets': like strip_note_reference, but keeps the printed spelling."""
    tokens = label.split()
    while len(tokens) > 1 and _NOTE_TAIL.match(normalize_text(tokens[-1])):
        tokens.pop()
    return " ".join(tokens).strip(" :")


def _tokens(text: str) -> list[str]:
    return [token for token, _ in _tokens_with_breaks(text)]


def _tokens_with_breaks(text: str) -> list[tuple[str, bool]]:
    """Tokens, each with True when the printed word ends a clause (',', ';' or '.')."""
    tokens = []
    for raw in normalize_text(text).split():
        token = strip_sentence_punctuation(raw).strip("()")
        if token:
            tokens.append((token, raw.rstrip(")\"'”’").endswith((",", ";", "."))))
    return tokens


# Words that start another item or clause: a phrase before them does not describe the number.
_CLAUSE_WORDS = {"and", "or", "while", "but", "whereas"}


class MetricMapper:
    def __init__(self, mappings: tuple[LabelMapping, ...]):
        self._aliases: dict[str, list[tuple[str, str]]] = {}
        self._after: list[tuple[list[str], str, str]] = []
        self._before: list[tuple[list[str], str, str]] = []
        for m in mappings:
            for alias in m.aliases:
                self._aliases.setdefault(normalize_text(alias), []).append((m.metric_id, alias))
            self._after += [(_tokens(p), m.metric_id, p) for p in m.text_after]
            self._before += [(_tokens(p), m.metric_id, p) for p in m.text_before]

    def match(self, candidate: RawCandidate) -> tuple[MetricMatch, ...]:
        if candidate.extraction_method == NARRATIVE_METHOD:
            found = self._match_after(candidate) + self._match_before(candidate)
        else:
            label = normalize_text(candidate.raw_label)
            # Exact label first; only if that fails, the label without a trailing note reference.
            hits = self._aliases.get(label) or self._aliases.get(strip_note_reference(label), [])
            found = [MetricMatch(metric_id, "table label", alias) for metric_id, alias in hits]
        unique: dict[str, MetricMatch] = {}
        for m in found:
            unique.setdefault(m.metric_id, m)  # one match per metric is enough evidence
        return tuple(unique.values())

    def _match_after(self, candidate: RawCandidate) -> list[MetricMatch]:
        after = _tokens(candidate.text_after)
        if after and after[0] in SCALE_WORDS:  # "2.76 million mobile banking users": the scale is part of the number
            after = after[1:]
        matches = []
        for phrase, metric_id, text in self._after:
            for gap in range(MAX_GAP_AFTER + 1):
                if any(is_value_token(t) for t in after[:gap]):
                    break
                if after[gap : gap + len(phrase)] == phrase:
                    matches.append(MetricMatch(metric_id, "words after", text))
                    break
        return matches

    def _match_before(self, candidate: RawCandidate) -> list[MetricMatch]:
        """The words between phrase and number may not hold another number, a clause break
        (',' ';' '.') or a word such as "and": in "6,045 PoS terminals, and 80 virtual
        centres" the 80 is not a number of PoS terminals."""
        pairs = _tokens_with_breaks(candidate.text_before)
        before = [token for token, _ in pairs]
        matches = []
        for phrase, metric_id, text in self._before:
            for gap in range(MAX_GAP_BEFORE + 1):
                end = len(before) - gap
                if end < len(phrase) or any(is_value_token(t) or t in _CLAUSE_WORDS for t in before[end:]):
                    break
                if any(ends_clause for _, ends_clause in pairs[end - 1 : len(pairs) - 1]):
                    break  # a ',' after the phrase's last word or inside the gap
                if before[end - len(phrase) : end] == phrase:
                    matches.append(MetricMatch(metric_id, "words before", text))
                    break
        return matches
