"""Review controls for one metric: Approve, Reject, Correct, Enter manually, Not reported,
Value from an external source, Cross-check, Reopen.

All rules live in src/review_service.py; this module only collects input, calls the
service, and shows its result or error.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import streamlit as st

from src import calculated_entries, external_sources, page_marks, par_value, value_input
from src import review_service as review
from src.config_loader import AppConfig
from src.unit_normalizer import unit_label
from src.ui import routes, source_form
from src.candidate_queries import CandidateKey
from src.database import connect
from src.models import MetricCandidate, MetricResult
from src.project_service import get_candidate
from src.review_service import ReviewError, ReviewState

UNIT_LABELS = {"ETB": "ETB (full Birr, not thousands)", "count": "count", "fraction": "fraction (0.29 = 29%)"}
DECISION_LABELS = {
    "approved": "Approved", "rejected": "Rejected", "corrected": "Corrected",
    "manual_entry": "Entered manually", "reopened": "Reopened",
}


@dataclass(frozen=True)
class ReviewContext:
    db_path: Path
    document_id: str
    candidate_ids: dict[tuple[str, CandidateKey], int]  # (metric id, candidate key) -> candidates.id
    reviewer: str
    page_count: int
    current_page: int
    extraction_finished: bool = False  # figures locked until the extraction review is reopened
    show_box: Callable[[int, tuple], None] | None = None  # move the page viewer to (1-based page, box)
    config: AppConfig | None = None  # metrics, formulas and labels, for Calculate


def value_and_unit(value: Decimal, unit: str | None) -> tuple[str, str]:
    """The number and its unit as separate table cells, e.g. ("1,234,568,000", "ETB")."""
    if unit == "fraction":
        return f"{(value * 100).normalize():f}%", "%"
    return f"{value:,}", unit or ""


def format_value(value: Decimal, unit: str | None) -> str:
    number, _ = value_and_unit(value, unit)
    return f"{number} ETB" if unit == "ETB" else number


def review_section(ctx: ReviewContext | None, result: MetricResult, shown: MetricCandidate | None,
                   state: ReviewState | None) -> None:
    if ctx is None or state is None:
        st.caption("Save the report (button in the top bar) to approve, reject or correct figures.")
        a, r, k = st.columns(3)
        for col, label, icon in ((a, "Approve", "check"), (r, "Reject", "close"), (k, "Correct", "edit")):
            col.button(label, icon=f":material/{icon}:", disabled=True, width="stretch", key=f"disabled_{label}")
        return

    metric_id = result.metric.id
    routes.link("evidence", "View evidence page", ":material/verified:", report=ctx.document_id, metric=metric_id)
    if ctx.extraction_finished:
        if state.status == "approved":
            _approved_box(ctx, state)
        elif state.status == "not_reported":
            _not_reported_box(state)
        st.caption(":material/lock: The extraction review is finished, so figures are locked. "
                   "Reopen it to change them.")
        reopen_extraction(ctx, key="metric")
    elif state.status == "approved":
        _approved_box(ctx, state)
        _reopen(ctx, metric_id, "Reopen")
    elif state.status == "not_reported":
        _not_reported_box(state)
        _reopen(ctx, metric_id, "Reopen")
    elif state.status == "corrected":
        _pending_box(ctx, result, state)
    elif shown is None:
        st.info("No figure was found. If the report shows it, enter it manually with the page you read it from.")
        _manual_entry(ctx, result)
    else:
        candidate_id = ctx.candidate_ids.get((metric_id, shown.mapped.key))
        if candidate_id is None:
            st.warning("This figure is not part of the saved report (the wording or the extractor changed after "
                       "saving). Press **Add them to the saved report** above the page if it is offered, or record "
                       "it with Enter manually.", icon=":material/warning:")
            _manual_entry(ctx, result)
        elif candidate_id in state.rejected_candidate_ids:
            st.info("You rejected this figure. Pick another candidate, correct this one, or reopen the metric.",
                    icon=":material/block:")
            _correct(ctx, result, shown, candidate_id)
        else:
            _approve(ctx, result, shown, candidate_id)
            reject_col, correct_col = st.columns(2)
            with reject_col:
                _reject(ctx, metric_id, candidate_id)
            with correct_col:
                _correct(ctx, result, shown, candidate_id)
    if metric_id == calculated_entries.EPS_METRIC:
        if not ctx.extraction_finished and state.status == "open" and shown is not None:
            _eps_from_par(ctx, shown)
        _eps_cross_check(ctx)
    if not ctx.extraction_finished and state.status == "open":
        _not_reported(ctx, result)
        _external_value(ctx, result)
    if not ctx.extraction_finished:
        from src.ui import calc_panel  # calc_panel uses run_action from this module

        calc_panel.calculate_section(ctx, result, state)
    _cross_check(ctx, result)
    _history(state)


# --- actions -------------------------------------------------------------------------

def run_action(ctx: ReviewContext, action: Callable[[object], object], success: str,
               on_success: Callable[[], None] | None = None) -> None:
    """Run a review action against a fresh connection; show the rule that blocked it, if any.

    on_success runs only when the action was saved, so a refused action changes nothing on screen."""
    try:
        with closing(connect(ctx.db_path)) as conn:
            action(conn)
    except ReviewError as exc:
        st.error(str(exc), icon=":material/error:")
        return
    if on_success is not None:
        on_success()
    st.session_state.pop("marked", None)  # a mark is used once
    st.toast(success, icon=":material/check_circle:")
    st.rerun()


def value_box(label: str, unit: str, key: str, *, default: str = "", sign: str = "as_printed",
              help: str | None = None, where=None) -> value_input.TypedValue:
    """The one value box (D-067): reads what is typed in the unit it will be stored in and always
    shows what will be stored. A bare ratio of 1 or less asks whether it is a fraction or a percentage;
    a negative value (brackets or minus) asks whether to record it as positive or keep it negative,
    starting from sign, the metric's setting (D-068)."""
    where = where or st
    text = where.text_input(label, value=default, key=key, placeholder=value_input.EXAMPLES.get(unit, ""),
                            help=help or value_input.HELP.get(unit))
    small_ratio = "fraction"
    typed = value_input.read_value(text, unit)
    if typed.ambiguous:
        small_ratio = st.radio("This number is", value_input.SMALL_RATIO_CHOICES, horizontal=True,
                               key=f"{key}_ratio", format_func=lambda c: value_input.small_ratio_label(c, text))
        typed = value_input.read_value(text, unit, small_ratio)
    if typed.value is not None and typed.value < 0:
        first = value_input.default_sign(sign)
        choice = st.radio("The value is negative", value_input.SIGN_CHOICES, horizontal=True, key=f"{key}_sign",
                          index=value_input.SIGN_CHOICES.index(first),
                          format_func=lambda c: value_input.sign_label(c, first),
                          help="Banks print amounts such as tax and dividends paid in brackets. The default comes "
                               "from the metric's setting (metrics.yaml, sign); you decide how it is recorded.")
        typed = value_input.read_value(text, unit, small_ratio, sign=choice)
    if typed.value is not None:
        reading = f" · {typed.reading}" if typed.reading else ""
        st.caption(f":material/save: **{typed.preview}**{reading}")
    elif (text or "").strip():
        st.caption(f":material/error: {typed.problem}")
    return typed


def typed_or_error(typed: value_input.TypedValue) -> Decimal:
    """The value to save, or ReviewError with the reader's explanation (nothing is saved)."""
    if typed.value is None:
        raise ReviewError(typed.problem or "Type the value first: the Value box is empty.")
    return typed.value


def typed_sign(typed: value_input.TypedValue) -> str | None:
    """The direction kept with a typed value (D-068): 'positive' when a value typed in brackets is
    recorded as positive, 'as_printed' when it is kept negative, None when it is not negative."""
    return value_input.sign_of(typed)


def _approve(ctx: ReviewContext, result: MetricResult, shown: MetricCandidate, candidate_id: int) -> None:
    metric_id = result.metric.id
    if shown.normalized.value is None:
        st.warning(f"Cannot approve as printed: {shown.normalized.issue} Use **Correct** to enter the right value.",
                   icon=":material/warning:")
        return
    with closing(connect(ctx.db_path)) as conn:
        warnings = review.relevant_warnings(conn, ctx.document_id, metric_id, candidate_id)
        saved = get_candidate(conn, candidate_id)
    # approve() stores the figure as converted when the report was saved; say so if today's reading differs
    # (e.g. a share count saved before count_in_thousands_below, D-069), so the button never promises another value.
    if saved is not None and (saved.normalized_value, saved.normalized_unit) != (shown.normalized.value,
                                                                                 shown.normalized.unit):
        if saved.normalized_value is None:
            st.warning("When the report was saved this figure could not be converted, so it cannot be approved as "
                       "printed. Use **Correct** to enter the right value.", icon=":material/warning:")
            return
        st.warning(f"When the report was saved this figure was converted to "
                   f"{value_input.stored_text(saved.normalized_value, saved.normalized_unit)}; today it reads as "
                   f"{value_input.stored_text(shown.normalized.value, shown.normalized.unit)}. Approve stores the "
                   "saved value; use **Correct** to record the other one.", icon=":material/warning:")
        shown = replace(shown, normalized=replace(shown.normalized, value=saved.normalized_value,
                                                   unit=saved.normalized_unit))
    acknowledged = True
    if warnings:
        acknowledged = st.checkbox(f"I have checked the {len(warnings)} warning(s) above",
                                   key=f"ack_{metric_id}_{candidate_id}")
    sign = "as_printed"
    printed = shown.mapped.candidate.raw_value_text
    if shown.normalized.value < 0:
        options = ["As printed (negative)", "As a positive amount"]
        choice = st.radio("Record the sign", options, horizontal=True, key=f"sign_{metric_id}_{candidate_id}",
                          index=1 if result.metric.sign == "positive" else 0,
                          help="Banks print amounts such as tax and dividends paid in brackets. The default comes "
                               "from the metric's setting (metrics.yaml, sign); you decide how it is recorded.")
        sign = "positive" if choice.startswith("As a positive") else "as_printed"
        st.caption(value_input.sign_note(printed, shown.normalized.value, shown.normalized.unit, sign))
    stored = value_input.stored_on_approval(shown.normalized.value, sign)
    statement = value_input.approval_label(printed, unit_label(shown.mapped.unit), stored, shown.normalized.unit)
    st.markdown(f":material/save: **{statement}**")
    note = st.text_input("Note (optional)", key=f"note_{metric_id}_{candidate_id}", placeholder="e.g. statement figure")
    if st.button("Approve", icon=":material/check:", type="primary", width="stretch", help=statement,
                 key=f"approve_{metric_id}_{candidate_id}"):
        run_action(ctx, lambda conn: review.approve(conn, candidate_id, ctx.reviewer, sign=sign,
                                         acknowledge_warnings=acknowledged, note=note),
             f"{result.metric.display_name} approved")


def _reject(ctx: ReviewContext, metric_id: str, candidate_id: int) -> None:
    with st.popover("Reject", icon=":material/close:", width="stretch"):
        reason = st.text_area("Why is this figure wrong?", key=f"reject_reason_{metric_id}_{candidate_id}",
                              placeholder="e.g. prior-year column, belongs to a note breakdown")
        if st.button("Confirm reject", type="primary", key=f"reject_{metric_id}_{candidate_id}"):
            run_action(ctx, lambda conn: review.reject(conn, candidate_id, ctx.reviewer, reason), "Figure rejected")


def _correct(ctx: ReviewContext, result: MetricResult, shown: MetricCandidate, candidate_id: int) -> None:
    metric_id = result.metric.id
    mark = _current_mark()
    with st.popover("Correct", icon=":material/edit:", width="stretch"):
        st.caption("Enter the right value. The extracted figure is kept unchanged; your correction is saved "
                   "separately with your name and reason, and needs approval.")
        if mark is not None:
            default = _mark_info(mark, result)
        else:
            st.caption(":material/crop_free: If the suggested figure is the wrong number, switch on **Mark a number** "
                       "under the page and drag a box around the right one: your box is saved as the evidence.")
            default = value_input.input_text(shown.normalized.value, shown.normalized.unit)
        suffix = _mark_suffix(mark)
        unit = unit_select(result, f"correct_unit_{metric_id}_{candidate_id}")
        typed = value_box("Correct value", unit, f"correct_value_{metric_id}_{candidate_id}{suffix}", default=default,
                          sign=result.metric.sign)
        reason = st.text_area("Reason", key=f"correct_reason_{metric_id}_{candidate_id}",
                              placeholder="e.g. wrong row picked; last digit misread")
        if st.button("Save correction", type="primary", key=f"correct_{metric_id}_{candidate_id}"):
            run_action(ctx, lambda conn: review.correct(
                conn, candidate_id, ctx.reviewer, typed_or_error(typed), unit, reason,
                source_page=mark.page_index + 1 if mark else None, source_bbox=mark.bbox if mark else None,
                sign=typed_sign(typed)),
                "Correction saved; approve it to use it")


def _manual_entry(ctx: ReviewContext, result: MetricResult) -> None:
    metric_id = result.metric.id
    mark = _current_mark()
    with st.popover("Enter manually", icon=":material/edit_note:", width="stretch"):
        st.caption("For a figure the extractor did not find. Mark it on the page: your box is saved as the "
                   "evidence, so anyone can check it later. The entry needs approval.")
        if mark is None:
            st.info("Switch on **Mark a number** under the page, go to the page with the figure and drag a box "
                    "around it.", icon=":material/crop_free:")
            st.button("Save entry", type="primary", disabled=True, key=f"manual_{metric_id}")
            return
        default = _mark_info(mark, result)
        unit = unit_select(result, f"manual_unit_{metric_id}")
        typed = value_box("Value", unit, f"manual_value_{metric_id}{_mark_suffix(mark)}", default=default,
                          sign=result.metric.sign)
        reason = st.text_area("Note / reason", key=f"manual_reason_{metric_id}",
                              placeholder="e.g. highlights chart: ETB 55.24 billion")
        if st.button("Save entry", type="primary", key=f"manual_{metric_id}"):
            run_action(ctx, lambda conn: review.manual_entry(
                conn, ctx.document_id, metric_id, ctx.reviewer, typed_or_error(typed), unit, mark.page_index + 1,
                reason, source_bbox=mark.bbox, sign=typed_sign(typed)), "Entry saved; approve it to use it")


@st.cache_data(show_spinner=False, max_entries=8)
def _par_values(sha256: str, _pdf: bytes) -> list[par_value.ParValue]:
    return par_value.find_par_values(_pdf)


def report_par_value(ctx: ReviewContext) -> par_value.ParValue | None:
    """The par value stated in the saved report's PDF (D-070), or None when it is not found or readable."""
    if ctx.config is None:
        return None
    from src.evidence import EvidenceError, load_report_pdf  # evidence imports this package's neighbours

    with closing(connect(ctx.db_path)) as conn:
        document = review.get_document(conn, ctx.document_id)
    if document is None:
        return None
    try:
        pdf = load_report_pdf(ctx.config.settings, document)
    except EvidenceError:
        return None
    return par_value.best_par_value(_par_values(document["sha256"], pdf))


def _eps_from_par(ctx: ReviewContext, shown: MetricCandidate) -> None:
    """EPS printed as a percentage of par value: one click records both figures and the calculation (D-070)."""
    parsed = shown.mapped.parsed
    if not parsed.is_percent or parsed.value is None or ctx.config is None:
        return
    par = report_par_value(ctx)
    if par is None:
        st.caption(":material/calculate: EPS is printed as a percentage of the par value, but no par value "
                   "(e.g. 'ordinary shares of Birr 1,000 each') was found in the report. Use **Calculate** and "
                   "mark the par value on its page.")
        return
    candidate = shown.mapped.candidate
    eps = calculated_entries.FoundFigure(parsed.value / 100, candidate.page_index + 1, candidate.bbox,
                                         candidate.raw_value_text)
    found = calculated_entries.FoundFigure(par.value, par.page, par.bbox, par.printed)
    label = calculated_entries.eps_conversion_text(eps.value, par.value, par.page)
    if st.button(label, icon=":material/calculate:", width="stretch", key=f"eps_par_{ctx.document_id}",
                 help=f"Read from \"{par.text}\" on PDF page {par.page}. Records {candidate.raw_value_text} (page "
                      f"{eps.page}) and the par value as input figures, and proposes EPS = eps_percent x par_value "
                      "for approval."):
        run_action(ctx, lambda conn: calculated_entries.convert_eps_from_par(
            conn, ctx.config, ctx.document_id, eps, found, ctx.reviewer),
            "EPS calculated from the par value; approve it to use it")


def _eps_cross_check(ctx: ReviewContext) -> None:
    with closing(connect(ctx.db_path)) as conn:
        check = calculated_entries.eps_cross_check(conn, ctx.document_id)
    if check is not None:
        profit, shares, eps = check
        st.caption(f":material/fact_check: Cross-check: profit after tax {profit:,f} / weighted average shares "
                   f"{shares:,f} = {eps:,.2f} Birr per share.")


def _current_mark() -> page_marks.Mark | None:
    return st.session_state.get("marked")


def _mark_suffix(mark: page_marks.Mark | None) -> str:
    """Widget key suffix, so a new mark refreshes the pre-filled value."""
    return "" if mark is None else f"_m{abs(hash((mark.page_index, mark.bbox))) % 10**8}"


def _mark_info(mark: page_marks.Mark, result: MetricResult) -> str:
    """Show what the mark contains; return the suggested value (in the metric's unit) to pre-fill."""
    suggestion = page_marks.suggested_value(mark, result.metric.expected_unit, result.metric.count_in_thousands_below)
    read = mark.printed_value or (f"“{mark.text}”" if mark.text else "no readable text")
    st.info(f"Using **your mark on page {mark.page_index + 1}**: {read}. The box is saved as the evidence.",
            icon=":material/crop_free:")
    if suggestion is None or suggestion.value is None:
        st.caption("Type the value; the number in the box could not be read as one figure.")
        return ""
    st.caption(f"Suggested from your mark: {suggestion.value:,} ({suggestion.basis}). Check it.")
    return value_input.input_text(suggestion.value, suggestion.unit)


def _pending_box(ctx: ReviewContext, result: MetricResult, state: ReviewState) -> None:
    p = state.pending
    kind = ("Not reported" if p.entry_kind == "not_reported" else "Calculation" if p.entry_kind == "calculated" else
            "External source" if p.entry_kind == "external" else
            "Manual entry" if p.decision == "manual_entry" else "Correction")
    where = f" · PDF page {p.source_page}" if p.source_page else ""
    boxed = " · box marked on the page" if p.source_bbox else ""
    shown = format_value(p.reviewed_value, p.reviewed_unit) if p.reviewed_value is not None else "no value (not reported)"
    st.info(f"**{kind} awaiting approval:** {shown}{where}{boxed}  \n"
            f"by {p.reviewer} · {p.created_at[:16].replace('T', ' ')} UTC  \nReason: {p.reason}",
            icon=":material/edit:")
    _show_box_button(ctx, p, f"pending_{p.id}")
    metric_id = result.metric.id
    with closing(connect(ctx.db_path)) as conn:
        warnings = review.relevant_warnings(conn, ctx.document_id, metric_id)
    acknowledged = True
    if warnings:
        acknowledged = st.checkbox(f"I have checked the {len(warnings)} warning(s) for this metric",
                                   key=f"ack_pending_{metric_id}_{p.id}")
    if st.button(f"Approve {kind.lower()}", icon=":material/check:", type="primary", width="stretch",
                 key=f"approve_pending_{metric_id}_{p.id}"):
        run_action(ctx, lambda conn: review.approve_correction(conn, ctx.document_id, metric_id, ctx.reviewer,
                                                    acknowledge_warnings=acknowledged),
             f"{result.metric.display_name} approved")
    _reopen(ctx, metric_id, f"Withdraw {kind.lower()}")


def _not_reported(ctx: ReviewContext, result: MetricResult) -> None:
    """Record that the report does not disclose this metric (D-053): not Not found, not zero."""
    metric_id = result.metric.id
    with st.popover("Not reported", icon=":material/do_not_disturb_on:", width="stretch",
                    help="The report does not disclose this figure. Different from Not found and from zero."):
        st.caption("Use this when you have looked and the report does not give the figure. It is exported as a "
                   "blank cell with your reason, never as zero.")
        reason = st.text_area("Why is it not reported?", key=f"not_reported_reason_{metric_id}", height=80,
                              placeholder="e.g. The report does not disclose the number of shareholders; searched "
                                          "the directors' report and notes 27-28.")
        if st.button("Record as not reported", type="primary", key=f"not_reported_save_{metric_id}", width="stretch"):
            run_action(ctx, lambda conn: review.not_reported(conn, ctx.document_id, metric_id, ctx.reviewer, reason),
                       f"{result.metric.display_name}: recorded as not reported. Approve it to confirm.")


def _external_value(ctx: ReviewContext, result: MetricResult) -> None:
    """Record the metric's value from a document other than the report (D-055); needs approval."""
    if ctx.config is None:
        return
    metric = result.metric
    key = f"ext_value_{metric.id}"
    with st.expander("Value from an external source", icon=":material/travel_explore:"):
        if metric.is_sector:
            st.info("This is a sector figure (the same for every bank). Record it once per fiscal year on the "
                    "**Sector data** page; a value recorded here is used only when that page has none.",
                    icon=":material/public:")
            routes.link("sector_data", "Open Sector data", ":material/public:")
        st.caption("For a figure the report does not print, taken from another document (e.g. a National Bank of "
                   "Ethiopia publication). It needs approval, like a correction.")
        source = source_form.source_form(key, metric.expected_unit, ctx.config.settings, sign=metric.sign)
        note = st.text_area("Why from an external source?", key=f"{key}_note", height=70,
                            placeholder="e.g. Not disclosed in the report; NBE publishes it for every bank.")
        if st.button("Save value from external source", type="primary", key=f"{key}_save", width="stretch"):
            run_action(ctx, lambda conn: external_sources.record_external_value(
                conn, ctx.config.settings, ctx.document_id, metric.id, metric.expected_unit, source, note,
                ctx.reviewer), f"{metric.display_name}: value from an external source saved. Approve it to use it.",
                on_success=lambda: source_form.clear_form(key))


def _cross_check(ctx: ReviewContext, result: MetricResult) -> None:
    """An external figure to check the approved value against (advisory, D-057)."""
    if ctx.config is None:
        return
    metric = result.metric
    key = f"xcheck_{metric.id}"
    with closing(connect(ctx.db_path)) as conn:
        state = review.review_state(conn, ctx.document_id, metric.id)
        approved = state.status == "approved"
        checks = external_sources.checked(conn, ctx.document_id, metric.id, state.accepted_value if approved else None,
                                          state.accepted_unit if approved else None)
    active = [c for c in checks if c.check.active]
    flag = " · mismatch" if any(c.comparison.status == "mismatch" for c in active) else ""
    with st.expander(f"Cross-check with an external source ({len(active)}{flag})", icon=":material/rule:"):
        for c in active:
            message = f"**{c.comparison.label}** · {c.check.source.title}: {c.comparison.text}"
            {"match": st.success, "mismatch": st.error}.get(c.comparison.status, st.info)(message)
        st.caption("Advisory only: a cross-check never changes the value. It may be added at any time; withdraw one "
                   "on the Evidence page.")
        source = source_form.source_form(key, metric.expected_unit, ctx.config.settings)
        note = st.text_input("Note (optional)", key=f"{key}_note")
        if st.button("Add cross-check", key=f"{key}_save", width="stretch"):
            run_action(ctx, lambda conn: external_sources.add_cross_check(
                conn, ctx.config.settings, ctx.document_id, metric.id, metric.expected_unit, source, note,
                ctx.reviewer), "Cross-check added", on_success=lambda: source_form.clear_form(key))


def _not_reported_box(state: ReviewState) -> None:
    a = state.accepted_decision
    proposal = next((d for d in state.history if d.id == a.approves_decision_id), None)
    reason = proposal.reason if proposal is not None else ""
    st.info(f"**Not reported** in this report (not the same as zero)  \nReason: {reason}  \n"
            f"Approved by {a.reviewer} · {a.created_at[:16].replace('T', ' ')} UTC", icon=":material/do_not_disturb_on:")


def _approved_box(ctx: ReviewContext, state: ReviewState) -> None:
    a = state.accepted_decision
    proposal = next((d for d in state.history if d.id == a.approves_decision_id), None)
    origin = ("value from an external source" if proposal is not None and proposal.entry_kind == "external" else
              "human-entered value" if state.is_human_entered else "extracted figure")
    sign = " · recorded as a positive amount" if a.sign_choice == "positive" else ""
    boxed = f" · box marked on PDF page {a.source_page}" if a.source_bbox else ""
    st.success(f"**Approved:** {format_value(state.accepted_value, state.accepted_unit)}  \n"
               f"{origin}{sign}{boxed} · by {a.reviewer} · {a.created_at[:16].replace('T', ' ')} UTC",
               icon=":material/check_circle:")
    _show_box_button(ctx, a, f"approved_{a.id}")


def _show_box_button(ctx: ReviewContext, decision: review.Decision, key: str) -> None:
    if decision.source_bbox is not None and decision.source_page and ctx.show_box is not None:
        st.button("Show the marked box", icon=":material/crop_free:", width="stretch", key=f"show_box_{key}",
                  on_click=ctx.show_box, args=(decision.source_page, decision.source_bbox))


def reopen_extraction(ctx: ReviewContext, key: str) -> None:
    """Unlock a finished extraction review so figures can be approved, corrected or reopened again."""
    with st.popover("Reopen extraction", icon=":material/lock_open:", width="stretch"):
        st.caption("Unlocks all figures of this report. Calculated values already entered are kept; "
                   "check them again after changing figures, then finish the extraction again.")
        reason = st.text_area("Reason", key=f"reopen_extraction_reason_{key}",
                              placeholder="e.g. wrong figure approved for total liabilities")
        if st.button("Confirm reopen", type="primary", key=f"reopen_extraction_{key}"):
            run_action(ctx, lambda conn: review.reopen_extraction(conn, ctx.document_id, ctx.reviewer, reason),
                       "Extraction review reopened: figures can be changed again")


def _reopen(ctx: ReviewContext, metric_id: str, label: str) -> None:
    with st.popover(label, icon=":material/undo:", width="stretch"):
        reason = st.text_area("Reason", key=f"reopen_reason_{metric_id}_{label}")
        if st.button("Confirm", type="primary", key=f"reopen_{metric_id}_{label}"):
            run_action(ctx, lambda conn: review.reopen(conn, ctx.document_id, metric_id, ctx.reviewer, reason),
                 "Metric reopened")


def unit_select(result: MetricResult, key: str) -> str:
    units = list(UNIT_LABELS)
    default = units.index(result.metric.expected_unit) if result.metric.expected_unit in units else 0
    return st.selectbox("Unit", units, index=default, format_func=UNIT_LABELS.get, key=key)


def _history(state: ReviewState) -> None:
    if not state.history:
        return
    with st.expander(f"Review history ({len(state.history)})"):
        for d in reversed(state.history):
            value = f" · {format_value(d.reviewed_value, d.reviewed_unit)}" if d.reviewed_value is not None else ""
            reason = f" · “{d.reason}”" if d.reason else ""
            st.caption(f"{d.created_at[:16].replace('T', ' ')} · {d.reviewer} · "
                       f"{DECISION_LABELS[d.decision]}{value}{reason}")
