"""Extract's metric table shows the approved figure once there is one, not the app's first suggestion
(found in the review of 2026-10-29: a corrected total deposits showed the sentence it corrected)."""

from decimal import Decimal
from types import SimpleNamespace as NS

from src.ui.extract_page import _shown

METRIC = "total_deposits_etb"


def candidate(key: str, text: str, page_index: int) -> NS:
    return NS(mapped=NS(key=key, candidate=NS(raw_value_text=text, page_index=page_index)))


def prepared(state: NS | None) -> NS:
    table, sentence = candidate("table", "158,536,872", 45), candidate("sentence", "18.07", 13)
    result = NS(candidates=[sentence, table], primary=sentence)
    return NS(analysis=NS(metric=lambda metric_id: result), states={METRIC: state} if state else {},
              ctx=NS(candidate_ids={(METRIC, "sentence"): 197, (METRIC, "table"): 198}))


def approved(candidate_id: int, human: bool, history: list | None = None, page: int | None = None) -> NS:
    decision = NS(id=110, candidate_id=candidate_id, source_page=page, approves_decision_id=109 if human else None)
    return NS(status="approved", accepted_value=Decimal("158536872000"), accepted_unit="ETB",
              accepted_decision=decision, is_human_entered=human, history=history or [])


def test_not_approved_shows_the_suggestion():
    assert _shown(prepared(None), METRIC) == ("18.07", "14")


def test_approved_as_printed_shows_that_figure_and_page():
    assert _shown(prepared(approved(198, human=False)), METRIC) == ("158,536,872", "46")


def test_a_correction_shows_the_corrected_value_and_its_page():
    proposal = NS(id=109, source_page=46)
    assert _shown(prepared(approved(197, human=True, history=[proposal])), METRIC) == ("158,536,872,000", "46")


def test_a_unit_fix_with_no_page_of_its_own_takes_the_figures_page():
    """A unit fix on the app's figure (thousands to full Birr) saves no page: the figure's page is shown."""
    proposal = NS(id=109, source_page=None)
    assert _shown(prepared(approved(198, human=True, history=[proposal])), METRIC) == ("158,536,872,000", "46")


def test_a_correction_never_shows_the_page_of_the_figure_it_replaced():
    """BoA 2022/23: the '18.07' IFB sentence on page 14 was corrected to 158,536,872,000."""
    proposal = NS(id=109, source_page=None)
    assert _shown(prepared(approved(197, human=True, history=[proposal])), METRIC) == ("158,536,872,000", "–")
