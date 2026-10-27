"""Calculate a metric from a formula, in Metric Details (spec section 2, D-052).

The reviewer takes the metric's standard formula (metrics.yaml) or types one. Every input is
listed with where it comes from; a missing figure is marked on the page ("Mark it on the
page", then a box or Use this figure). The preview shows the arithmetic; Save records it as a
proposal that is approved like a correction. The rules are in src/calculated_entries.py.
"""

from __future__ import annotations

from contextlib import closing

import streamlit as st

from src.calculated_entries import is_metric, number_text, preview, save_calculation, variable_label, variable_unit
from src.database import connect
from src.models import MetricResult
from src.review_service import ReviewState
from src.ui.metric_view import esc
from src.ui.review_panel import ReviewContext, run_action

CALC_INPUT = "calc_input"  # session key: the input figure being marked on the page, if any


def calculate_section(ctx: ReviewContext, result: MetricResult, state: ReviewState) -> None:
    config = ctx.config
    if config is None:
        return
    metric = result.metric
    opened = st.session_state.get("calc_open") == metric.id
    with st.expander("Calculate from other figures", icon=":material/calculate:", expanded=opened):
        if state.status == "approved":
            st.caption("This metric is approved. A calculation saved here replaces that value once you approve it.")
        choices = (["standard"] if metric.formula else []) + ["own"]
        labels = {"standard": "Standard formula", "own": "Type my own"}
        choice = st.radio("Formula", choices, format_func=labels.get, horizontal=True, key=f"calc_choice_{metric.id}")
        if choice == "standard":
            expression = metric.formula
            st.code(f"{metric.id} = {expression}", language=None, wrap_lines=True)
            if metric.basis:
                st.caption(f"Basis: {metric.basis.replace('_', ' ')} (from config/metrics.yaml, the same for every bank)")
        else:
            expression = st.text_input(
                "Formula", key=f"calc_expr_{metric.id}", placeholder="e.g. mobile_banking_users + internet_banking_users",
                help="Use metric ids, <metric id>_prev for the previous year, or a new name for a figure you will "
                     "mark on the page (e.g. net_operating_income). + - * / ( ) and avg, sum, abs, min, max.")
            if not expression or not expression.strip():
                return
        with closing(connect(ctx.db_path)) as conn:
            p = preview(conn, config, ctx.document_id, metric.id, expression)
        if p.formula is None:
            st.error(p.problems[0], icon=":material/error:")
            return
        st.markdown(f"**{esc(metric.display_name)}** = {esc(p.words)}")
        st.caption("Inputs and where each one is printed")
        for i in p.inputs:
            _input_row(ctx, metric.id, i)
        if not p.ready:
            for problem in p.problems:
                if not any(problem == i.problem for i in p.inputs):
                    st.warning(problem, icon=":material/warning:")
            return
        st.caption("With the numbers")
        st.code(p.arithmetic, language=None, wrap_lines=True)
        st.markdown(f"Will store **{esc(number_text(p.value, metric.expected_unit))}**"
                    + (f" ({(p.value * 100).normalize():f}%)" if metric.expected_unit == "fraction" else "")
                    + f" · {metric.expected_unit}")
        note = st.text_area("Note", key=f"calc_note_{metric.id}", height=80,
                            placeholder="Why this calculation, e.g. ROE is not printed in this report.")
        if st.button("Save calculation", type="primary", icon=":material/save:", width="stretch",
                     key=f"calc_save_{metric.id}"):
            def done() -> None:
                st.session_state.pop("calc_open", None)

            run_action(ctx, lambda conn: save_calculation(conn, config, ctx.document_id, metric.id, expression,
                                                          note, ctx.reviewer),
                       f"{metric.display_name} calculated. Approve it to use it.", on_success=done)


def _input_row(ctx: ReviewContext, metric_id: str, i) -> None:
    text_col, button_col = st.columns([4, 1], vertical_alignment="center")
    if i.kind == "missing":
        text_col.markdown(f":red[:material/cancel:] **{esc(i.label)}** · {esc(i.problem)}")
    else:
        text_col.markdown(f":green[:material/check_circle:] **{esc(i.label)}** = "
                          f"{esc(number_text(i.value, i.unit))}  \n{esc(i.where)}")
    if is_metric(ctx.config, i.variable):
        return  # a metric is approved in its own Metric Details, not marked here
    label = "Mark it" if i.kind == "missing" else "Mark again"
    if button_col.button(label, key=f"calc_mark_{metric_id}_{i.variable}", width="stretch",
                         help="Mark this figure on the page: drag a box with Mark a number, or pick it in All Figures."):
        st.session_state[CALC_INPUT] = {"metric": metric_id, "variable": i.variable,
                                        "label": variable_label(ctx.config, i.variable),
                                        "unit": variable_unit(ctx.config, i.variable)}
        st.session_state.calc_open = metric_id
        st.session_state.turn_on_marking = True  # the viewer switches Mark a number on before drawing it
        st.toast(f"Now mark {variable_label(ctx.config, i.variable)} on the page: drag a box around the number "
                 "(Mark a number is on), or pick it in All Figures.", icon=":material/crop_free:")
        st.rerun()
