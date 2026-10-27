"""Read what a sentence says about its number: a year-end total, a change, or an estimate.

Reports often print both in one sentence: "opened 123 additional branches, bringing the
total number of branches to 849". The total is the figure a metric wants; the change is
not. These cues only rank sentence candidates and add findings for the reviewer; they never
discard a figure.
"""

from __future__ import annotations

from src.pdf_extractor import RawCandidate, strip_sentence_punctuation

TOTAL = "total"
CHANGE = "change"
NEUTRAL = ""

# Words just before the number that introduce a year-end total: "bringing the total number of
# branches to 849", "deposits reached 158.54 billion", "total deposits stood at ...".
_TOTAL_WORDS = {"total", "bringing", "raising", "reached", "reaching", "stood", "stands", "standing"}
TOTAL_LOOKBACK = 8  # words before the number searched for a total cue
# A change or a comparison, right next to the number: "opened 123 additional branches",
# "recruited 2,145 employees", "grew by 31.9 percent", "up from 25.3%", "140 employees joined".
_CHANGE_BEFORE = {"opened", "recruited", "hired", "added", "adding", "additional", "new", "from", "another"}
_CHANGE_VERBS = {"increased", "increase", "grew", "grown", "rose", "risen", "decreased", "decrease", "declined",
                 "fell", "dropped", "expanded", "jumped", "up", "down", "growth"}  # "... by N" after one of these
_CHANGE_AFTER = {"additional", "new", "more", "fewer", "less"}
_CHANGE_AFTER_LATER = {"joined", "recruited", "hired", "added", "opened"}  # within a few words after
CHANGE_LOOKAHEAD = 4  # "30 IFB-focused branches were opened"
# An estimate: "nearly 64,000", "more than 2.76 million".
_APPROXIMATE_BEFORE = {"nearly", "about", "approximately", "around", "almost", "over", "some", "roughly"}
_APPROXIMATE_PAIRS = {("more", "than"), ("close", "to"), ("up", "to")}


def _words(text: str) -> list[str]:
    return [strip_sentence_punctuation(t).lower() for t in text.split() if strip_sentence_punctuation(t)]


def _same_sentence(text_after: str) -> list[str]:
    """Words after the number up to the end of its sentence. The number's own full stop is not
    kept with it, so a word like "The" (capital, then small letters) is taken as a new sentence;
    acronyms such as "IFB-focused" or "ATMs" are not."""
    words = []
    for raw in text_after.split():
        if raw[:1].isupper() and raw[1:2].islower():
            break
        word = strip_sentence_punctuation(raw).lower()
        if word:
            words.append(word)
        if raw.endswith((".", ";", "!", "?")):
            break
    return words


def sentence_cue(candidate: RawCandidate) -> str:
    """TOTAL, CHANGE or NEUTRAL for a sentence number (NEUTRAL for a table value)."""
    before = _words(candidate.text_before)
    after = _words(candidate.text_after)
    if before and before[-1] in _APPROXIMATE_BEFORE:  # "opened nearly 50 new": look past the estimate word
        before = before[:-1]
    if before and before[-1] in _CHANGE_BEFORE:
        return CHANGE
    if len(before) >= 2 and before[-1] == "by" and before[-2] in _CHANGE_VERBS:
        return CHANGE
    if after and after[0] in _CHANGE_AFTER:
        return CHANGE
    if any(w in _CHANGE_AFTER_LATER for w in _same_sentence(candidate.text_after)[:CHANGE_LOOKAHEAD]):
        return CHANGE
    if any(w in _TOTAL_WORDS for w in before[-TOTAL_LOOKBACK:]):
        return TOTAL
    return NEUTRAL


def approximate_word(candidate: RawCandidate) -> str | None:
    """The word(s) that mark the number as an estimate ("nearly", "more than"), or None."""
    before = _words(candidate.text_before)
    if before and before[-1] in _APPROXIMATE_BEFORE:
        return before[-1]
    if len(before) >= 2 and (before[-2], before[-1]) in _APPROXIMATE_PAIRS:
        return f"{before[-2]} {before[-1]}"
    return None


def cue_rank(candidate: RawCandidate) -> int:
    """0 for a stated total, 1 for a plain sentence or a table value, 2 for a change."""
    return {TOTAL: 0, NEUTRAL: 1, CHANGE: 2}[sentence_cue(candidate)]


def is_change_or_estimate(candidate: RawCandidate) -> bool:
    """A sentence figure that is a change or an estimate: not compared with exact totals."""
    return sentence_cue(candidate) == CHANGE or approximate_word(candidate) is not None
