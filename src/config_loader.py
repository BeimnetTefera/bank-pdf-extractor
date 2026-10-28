"""Load and validate the YAML configuration in config/.

All problems found in a file are collected and raised together as one ConfigError,
so a user editing YAML can fix everything in one pass.
"""

from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

ALLOWED_UNITS = frozenset({"ETB", "count", "fraction", "ratio"})
LEARNED_ALIASES_FILE = "learned_aliases.yaml"
DEFAULT_APP_URL = "http://localhost:8501"
_METRIC_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ConfigError(ValueError):
    """Raised when a configuration file is missing or invalid."""


@dataclass(frozen=True)
class MetricDefinition:
    id: str
    shega_column: str | None  # column in the Shega pipeline's BANK table, if it has one
    display_name: str
    definition: str
    expected_unit: str
    active: bool
    notes: str | None = None
    export_column: str | None = None  # column in the export's Approved Data sheet
    formula: str | None = None  # standard calculation, e.g. "profit_after_tax_etb / avg(...)" (D-052)
    basis: str | None = None  # machine-readable basis of that calculation, e.g. "average_equity"
    level: str = "bank"  # 'bank': from each bank's report; 'sector': one value per fiscal year for all banks (D-056)
    # How a figure printed in brackets is recorded by default (D-068): 'positive' (an amount such as
    # tax or dividends paid) or 'as_printed' (a metric that can really be negative, e.g. profit)
    sign: str = "positive"
    # A count under a "'000" heading that is below this number is read in thousands (D-069)
    count_in_thousands_below: Decimal | None = None

    @property
    def is_sector(self) -> bool:
        return self.level == "sector"


METRIC_LEVELS = ("bank", "sector")
METRIC_SIGNS = ("positive", "as_printed")


@dataclass(frozen=True)
class InputFigure:
    """A figure marked on the report that is not a metric but feeds a formula (e.g. par value)."""

    id: str
    display_name: str
    expected_unit: str
    definition: str = ""


@dataclass(frozen=True)
class ColumnFormula:
    """The draft formula of a calculated column (export_template.yaml `formulas`, D-088)."""

    column: str
    formula: str
    unit: str  # ETB | fraction | ratio | count: how the result is rounded and shown
    compare_with: str | None = None  # metric id of the printed (Reported) figure to compare with


@dataclass(frozen=True)
class ExportTemplate:
    """Column layout of the export's Approved Data sheet (export_template.yaml)."""

    bank_column: str
    year_column: str
    columns: tuple[str, ...]
    calculated: tuple[str, ...] = ()  # entered by an analyst after extraction (the app proposes a value, D-088)
    formulas: tuple[ColumnFormula, ...] = ()

    def extracted(self) -> tuple[str, ...]:
        """Data columns filled from the report (everything except bank, year and calculated)."""
        return tuple(c for c in self.columns if c not in (self.bank_column, self.year_column, *self.calculated))

    def formula(self, column: str) -> ColumnFormula | None:
        return next((f for f in self.formulas if f.column == column), None)


@dataclass(frozen=True)
class LabelMapping:
    metric_id: str
    aliases: tuple[str, ...] = ()  # exact table-row labels
    text_after: tuple[str, ...] = ()  # sentence words right after the number ("981 branches")
    text_before: tuple[str, ...] = ()  # sentence words just before the number ("ROE reached 29%")


@dataclass(frozen=True)
class LearnedAlias:
    """A table-row label a reviewer taught the app from a report (learned_aliases.yaml, D-043)."""

    metric_id: str
    alias: str
    seen_in: str  # bank, year and PDF page where the label was seen
    added_by: str
    added_at: str
    bank: str | None = None  # used only for this bank's reports; None: every bank


def bank_key(bank: str | None) -> str:
    """Comparable bank name: 'Bank of abyssinia' and 'Bank of Abyssinia' are the same bank."""
    return " ".join((bank or "").split()).casefold()


@dataclass(frozen=True)
class SumCheck:
    """An arithmetic check between figures of one report: total = sum of parts (checks.yaml, D-045)."""

    id: str
    name: str
    total: str  # metric id
    parts: tuple[str, ...]  # metric ids


@dataclass(frozen=True)
class AppSettings:
    data_dir: Path
    max_upload_mb: int
    max_pages: int
    default_reviewer: str
    tesseract_dir: Path | None = None  # Tesseract OCR install folder; None: find it automatically
    app_url: str = DEFAULT_APP_URL  # where the app runs; Evidence links in Excel and PDFs point here

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"


@dataclass(frozen=True)
class AppConfig:
    metrics: tuple[MetricDefinition, ...]
    mappings: tuple[LabelMapping, ...]
    settings: AppSettings
    banks: tuple[str, ...] = ()  # names offered in the Bank dropdown
    export_template: ExportTemplate = ExportTemplate("bank", "fiscal_year", ("bank", "fiscal_year"))
    checks: tuple[SumCheck, ...] = ()
    learned_aliases: tuple[LearnedAlias, ...] = ()  # per bank; see mappings_for
    config_dir: Path | None = None  # where the files were read from; learned wording is written there
    input_figures: tuple[InputFigure, ...] = ()

    def input_figure(self, figure_id: str) -> InputFigure | None:
        return next((f for f in self.input_figures if f.id == figure_id), None)

    @property
    def learned_aliases_path(self) -> Path:
        return (self.config_dir or CONFIG_DIR) / LEARNED_ALIASES_FILE

    def mappings_for(self, bank: str | None) -> tuple[LabelMapping, ...]:
        """The wording used for one bank's report: mappings.yaml (every bank) plus the labels
        learned from that bank's reports. With no bank, only mappings.yaml and labels for every bank."""
        key = bank_key(bank)
        learned = tuple(LabelMapping(a.metric_id, aliases=(a.alias,)) for a in self.learned_aliases
                        if a.bank is None or (key and bank_key(a.bank) == key))
        return self.mappings + learned

    def active_metrics(self) -> tuple[MetricDefinition, ...]:
        return tuple(m for m in self.metrics if m.active)

    def metric(self, metric_id: str) -> MetricDefinition:
        for m in self.metrics:
            if m.id == metric_id:
                return m
        raise KeyError(f"Unknown metric id '{metric_id}'")


def load_config(config_dir: Path = CONFIG_DIR, project_root: Path = PROJECT_ROOT) -> AppConfig:
    """Load metrics, mappings, settings, banks and export_template YAML files from config_dir,
    plus the optional checks.yaml and learned_aliases.yaml."""
    metrics = load_metrics(config_dir / "metrics.yaml")
    metric_ids = {m.id for m in metrics}
    mappings = load_mappings(config_dir / "mappings.yaml", metric_ids)
    learned = load_learned_aliases(config_dir / LEARNED_ALIASES_FILE, metric_ids)
    settings = load_settings(config_dir / "settings.yaml", project_root)
    banks = load_banks(config_dir / "banks.yaml")
    template = load_export_template(config_dir / "export_template.yaml")
    _check_export_columns(config_dir / "metrics.yaml", metrics, template)
    checks = load_checks(config_dir / "checks.yaml", metrics)
    figures = load_input_figures(config_dir / "metrics.yaml", metric_ids)
    _check_formulas(config_dir / "metrics.yaml", metrics, figures)
    _check_column_formulas(config_dir / "export_template.yaml", template, metrics, figures)
    return AppConfig(metrics=metrics, mappings=mappings, settings=settings, banks=banks, export_template=template,
                     checks=checks, learned_aliases=learned, config_dir=config_dir, input_figures=figures)


# --- metrics.yaml ------------------------------------------------------------

_METRIC_REQUIRED = ("id", "display_name", "definition", "expected_unit", "active")
_METRIC_OPTIONAL = ("shega_column", "notes", "export_column", "formula", "basis", "level", "sign")
_METRIC_NUMBERS = ("count_in_thousands_below",)  # optional whole numbers


def load_metrics(path: Path) -> tuple[MetricDefinition, ...]:
    entries = _read_top_level_list(path, "metrics")
    errors: list[str] = []
    metrics: list[MetricDefinition] = []
    seen_ids: set[str] = set()
    seen_columns: set[str] = set()

    for index, entry in enumerate(entries):
        where = _describe_entry(index, entry)
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping of fields, got {type(entry).__name__}.")
            continue
        entry_errors = _check_fields(entry, _METRIC_REQUIRED, _METRIC_OPTIONAL + _METRIC_NUMBERS, where)
        for field in ("id", "display_name", "definition", "expected_unit", *_METRIC_OPTIONAL):
            if field in entry and entry[field] is not None and not _is_non_blank_str(entry[field]):
                entry_errors.append(f"{where}: '{field}' must be a non-empty text value.")
        for field in _METRIC_OPTIONAL:
            if field in entry and entry[field] is None:
                entry_errors.append(f"{where}: '{field}' is empty; remove it or add text.")
        if entry_errors:
            errors.extend(entry_errors)
            continue

        metric_id = entry["id"]
        if not _METRIC_ID_PATTERN.match(metric_id):
            errors.append(
                f"{where}: id '{metric_id}' must be lowercase snake_case "
                "(letters, digits, underscores; starting with a letter)."
            )
        if metric_id in seen_ids:
            errors.append(f"{where}: duplicate id '{metric_id}'. Each metric id must be unique.")
        if entry.get("shega_column") is not None and entry["shega_column"] in seen_columns:
            errors.append(
                f"{where}: shega_column '{entry['shega_column']}' is already used by another metric."
            )
        if entry["expected_unit"] not in ALLOWED_UNITS:
            errors.append(
                f"{where}: expected_unit '{entry['expected_unit']}' is not supported. "
                f"Use one of: {', '.join(sorted(ALLOWED_UNITS))}."
            )
        if not isinstance(entry["active"], bool):
            errors.append(f"{where}: 'active' must be true or false, got {entry['active']!r}.")
        level = entry.get("level", "bank")
        if level not in METRIC_LEVELS:
            errors.append(f"{where}: level '{level}' is not supported. Use 'bank' (from each bank's report, the "
                          "default) or 'sector' (one value per fiscal year for the whole sector).")
        elif level == "sector" and entry.get("formula") is not None:
            errors.append(f"{where}: a sector metric is recorded from an external source, so it cannot have a formula.")
        if entry.get("sign", "positive") not in METRIC_SIGNS:
            errors.append(f"{where}: sign '{entry['sign']}' is not supported. Use 'positive' (a figure in brackets "
                          "is recorded as a positive amount, the default) or 'as_printed' (it can really be negative).")
        below = entry.get("count_in_thousands_below")
        if below is not None and (isinstance(below, bool) or not isinstance(below, int) or below <= 0
                                  or entry["expected_unit"] != "count"):
            errors.append(f"{where}: count_in_thousands_below must be a positive whole number, for a count metric.")

        seen_ids.add(metric_id)
        if entry.get("shega_column") is not None:
            seen_columns.add(entry["shega_column"])
        metrics.append(
            MetricDefinition(
                id=metric_id,
                shega_column=entry.get("shega_column"),
                display_name=entry["display_name"].strip(),
                definition=entry["definition"].strip(),
                expected_unit=entry["expected_unit"],
                active=entry["active"],
                notes=entry.get("notes"),
                export_column=entry.get("export_column"),
                formula=entry.get("formula"),
                basis=entry.get("basis"),
                level=entry.get("level", "bank"),
                sign=entry.get("sign", "positive"),
                count_in_thousands_below=None if below is None else Decimal(below),
            )
        )

    _raise_if_errors(path, errors)
    return tuple(metrics)


# --- input figures and formulas (metrics.yaml) -------------------------------

PREV_SUFFIX = "_prev"  # total_equity_etb_prev: the same metric for the previous year
_FIGURE_FIELDS = ("id", "display_name", "expected_unit")


def load_input_figures(path: Path, metric_ids: set[str]) -> tuple[InputFigure, ...]:
    """The optional top-level 'input_figures:' list of metrics.yaml."""
    raw = _read_yaml(path)
    entries = raw.get("input_figures") if isinstance(raw, dict) else None
    if not entries:
        return ()
    if not isinstance(entries, list):
        raise ConfigError(f"{path.name}: 'input_figures' must be a list.")
    errors: list[str] = []
    figures: list[InputFigure] = []
    for index, entry in enumerate(entries):
        where = "input figure " + _describe_entry(index, entry)
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping of fields.")
            continue
        entry_errors = _check_fields(entry, _FIGURE_FIELDS, ("definition",), where)
        if not entry_errors:
            if not _METRIC_ID_PATTERN.match(str(entry["id"])):
                entry_errors.append(f"{where}: id must be lowercase snake_case.")
            elif entry["id"] in metric_ids or entry["id"] in {f.id for f in figures}:
                entry_errors.append(f"{where}: id '{entry['id']}' is already used by a metric or another figure.")
            if entry["expected_unit"] not in ALLOWED_UNITS:
                entry_errors.append(f"{where}: expected_unit must be one of {', '.join(sorted(ALLOWED_UNITS))}.")
        if entry_errors:
            errors.extend(entry_errors)
            continue
        figures.append(InputFigure(entry["id"], str(entry["display_name"]).strip(), entry["expected_unit"],
                                   str(entry.get("definition") or "").strip()))
    _raise_if_errors(path, errors)
    return tuple(figures)


def known_formula_names(metrics: tuple[MetricDefinition, ...], figures: tuple[InputFigure, ...]) -> set[str]:
    """Names a configured formula may use: metric ids, metric ids + '_prev', input figure ids."""
    ids = {m.id for m in metrics}
    return ids | {m + PREV_SUFFIX for m in ids} | {f.id for f in figures}


def _check_formulas(path: Path, metrics: tuple[MetricDefinition, ...], figures: tuple[InputFigure, ...]) -> None:
    from src.formula import FormulaError, parse  # formula has no config dependency; imported late to keep it optional

    known = known_formula_names(metrics, figures)
    errors = []
    for m in metrics:
        if m.formula is None:
            continue
        try:
            target, formula = parse(m.formula)
        except FormulaError as exc:
            errors.append(f"{m.id}: formula cannot be read: {exc}")
            continue
        if target is not None and target != m.id:
            errors.append(f"{m.id}: formula assigns to '{target}'; leave the target out or write {m.id} = ...")
        unknown = [n for n in formula.names if n not in known]
        if unknown:
            errors.append(f"{m.id}: formula uses unknown name(s) {unknown}: use metric ids, <metric>_prev "
                          "or ids from input_figures.")
        if m.id in formula.names:
            errors.append(f"{m.id}: formula uses the metric itself.")
    _raise_if_errors(path, errors)


def _check_column_formulas(path: Path, template: ExportTemplate, metrics: tuple[MetricDefinition, ...],
                           figures: tuple[InputFigure, ...]) -> None:
    from src.formula import FormulaError, parse

    known = known_formula_names(metrics, figures)
    metric_ids = {m.id for m in metrics}
    errors = []
    for f in template.formulas:
        try:
            target, formula = parse(f.formula)
        except FormulaError as exc:
            errors.append(f"formula of '{f.column}' cannot be read: {exc}")
            continue
        if target is not None:
            errors.append(f"formula of '{f.column}': leave out the '{target} =' part.")
        unknown = [n for n in formula.names if n not in known]
        if unknown:
            errors.append(f"formula of '{f.column}' uses unknown name(s) {unknown}: use metric ids, <metric>_prev "
                          "or ids from input_figures.")
        if f.compare_with is not None and f.compare_with not in metric_ids:
            errors.append(f"formula of '{f.column}': compare_with '{f.compare_with}' is not a metric id.")
    _raise_if_errors(path, errors)


# --- mappings.yaml -----------------------------------------------------------

_MAPPING_REQUIRED = ("metric_id",)
_MAPPING_PHRASE_LISTS = ("aliases", "text_after", "text_before")


def load_mappings(path: Path, known_metric_ids: set[str]) -> tuple[LabelMapping, ...]:
    entries = _read_top_level_list(path, "mappings")
    errors: list[str] = []
    mappings: list[LabelMapping] = []
    seen_metric_ids: set[str] = set()

    for index, entry in enumerate(entries):
        where = _describe_entry(index, entry, id_field="metric_id")
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping of fields, got {type(entry).__name__}.")
            continue
        entry_errors = _check_fields(entry, _MAPPING_REQUIRED, _MAPPING_PHRASE_LISTS, where)
        if entry_errors:
            errors.extend(entry_errors)
            continue

        metric_id = entry["metric_id"]
        if metric_id not in known_metric_ids:
            errors.append(
                f"{where}: metric_id '{metric_id}' is not defined in metrics.yaml. "
                "Check the spelling or add the metric there first."
            )
        if metric_id in seen_metric_ids:
            errors.append(
                f"{where}: metric_id '{metric_id}' appears in more than one entry; "
                "merge the aliases into a single entry."
            )
        seen_metric_ids.add(metric_id)

        lists: dict[str, tuple[str, ...]] = {}
        for field in _MAPPING_PHRASE_LISTS:
            if field not in entry:
                continue
            values = entry[field]
            if not isinstance(values, list) or not values:
                errors.append(f"{where}: '{field}' must be a non-empty list of text (or be left out).")
                continue
            bad = [v for v in values if not _is_non_blank_str(v)]
            if bad:
                errors.append(f"{where}: every entry in '{field}' must be non-empty text; invalid: {bad!r}.")
                continue
            lists[field] = tuple(values)
        if not any(field in entry for field in _MAPPING_PHRASE_LISTS):
            errors.append(f"{where}: add at least one of: {', '.join(_MAPPING_PHRASE_LISTS)}.")
            continue
        mappings.append(LabelMapping(metric_id=metric_id, **lists))

    _raise_if_errors(path, errors)
    return tuple(mappings)


# --- learned_aliases.yaml ----------------------------------------------------

_LEARNED_FIELDS = ("metric_id", "alias", "seen_in", "added_by", "added_at")


def load_learned_aliases(path: Path, known_metric_ids: set[str]) -> tuple[LearnedAlias, ...]:
    """Labels added from the review screen. The file is optional: missing means none learned yet."""
    if not path.is_file():
        return ()
    entries = _read_top_level_list(path, "learned")
    errors: list[str] = []
    learned: list[LearnedAlias] = []
    for index, entry in enumerate(entries):
        where = _describe_entry(index, entry, id_field="metric_id")
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping of fields, got {type(entry).__name__}.")
            continue
        entry_errors = _check_fields(entry, _LEARNED_FIELDS, ("bank",), where)
        # YAML may read an unquoted time as a datetime; it is kept as text.
        entry_errors += [f"{where}: '{f}' must be non-empty text." for f in (*_LEARNED_FIELDS[:4], "bank")
                         if f in entry and entry[f] is not None and not _is_non_blank_str(entry[f])]
        if not entry_errors and entry["metric_id"] not in known_metric_ids:
            entry_errors.append(f"{where}: metric_id '{entry['metric_id']}' is not defined in metrics.yaml.")
        if entry_errors:
            errors.extend(entry_errors)
            continue
        bank = entry.get("bank")
        learned.append(LearnedAlias(entry["metric_id"], entry["alias"].strip(), entry["seen_in"].strip(),
                                    entry["added_by"].strip(), str(entry["added_at"]), bank.strip() if bank else None))
    _raise_if_errors(path, errors)
    return tuple(learned)


# --- checks.yaml -------------------------------------------------------------

_CHECK_FIELDS = ("id", "name", "total", "parts")


def load_checks(path: Path, metrics: tuple[MetricDefinition, ...]) -> tuple[SumCheck, ...]:
    """Arithmetic checks between metrics. The file is optional: missing means no checks."""
    if not path.is_file():
        return ()
    entries = _read_top_level_list(path, "checks")
    units = {m.id: m.expected_unit for m in metrics}
    errors: list[str] = []
    checks: list[SumCheck] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = _describe_entry(index, entry)
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping of fields, got {type(entry).__name__}.")
            continue
        entry_errors = _check_fields(entry, _CHECK_FIELDS, (), where)
        if entry_errors:
            errors.extend(entry_errors)
            continue
        parts = entry["parts"]
        if not all(_is_non_blank_str(entry[f]) for f in ("id", "name", "total")):
            errors.append(f"{where}: 'id', 'name' and 'total' must be non-empty text.")
            continue
        if not isinstance(parts, list) or not parts or not all(_is_non_blank_str(p) for p in parts):
            errors.append(f"{where}: 'parts' must be a non-empty list of metric ids.")
            continue
        if entry["id"] in seen:
            errors.append(f"{where}: duplicate id '{entry['id']}'.")
        seen.add(entry["id"])
        metric_ids = [entry["total"], *parts]
        unknown = [m for m in metric_ids if m not in units]
        if unknown:
            errors.append(f"{where}: unknown metric id(s) {unknown}; check metrics.yaml.")
        elif len({units[m] for m in metric_ids}) > 1:
            errors.append(f"{where}: the metrics have different units; a sum needs the same unit.")
        if entry["total"] in parts:
            errors.append(f"{where}: the total '{entry['total']}' is also listed as a part.")
        checks.append(SumCheck(entry["id"], entry["name"].strip(), entry["total"], tuple(parts)))
    _raise_if_errors(path, errors)
    return tuple(checks)


# --- banks.yaml --------------------------------------------------------------

def load_banks(path: Path) -> tuple[str, ...]:
    entries = _read_top_level_list(path, "banks")
    errors: list[str] = []
    seen: set[str] = set()
    for index, name in enumerate(entries):
        if not _is_non_blank_str(name):
            errors.append(f"entry #{index + 1}: a bank must be a non-empty name, got {name!r}.")
            continue
        if name.strip().casefold() in seen:
            errors.append(f"entry #{index + 1}: '{name}' is listed more than once.")
        seen.add(name.strip().casefold())
    if not entries:
        errors.append("the list is empty; add at least one bank.")
    _raise_if_errors(path, errors)
    return tuple(name.strip() for name in entries)


# --- export_template.yaml ----------------------------------------------------

def load_export_template(path: Path) -> ExportTemplate:
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: expected 'bank_column', 'year_column' and a 'columns' list.")
    errors: list[str] = []
    unknown = sorted(set(raw) - {"bank_column", "year_column", "columns", "calculated", "formulas"})
    if unknown:
        errors.append(f"unknown key(s) {unknown}. Allowed: bank_column, year_column, columns, calculated, formulas.")
    columns = raw.get("columns")
    if not isinstance(columns, list) or not columns or not all(_is_non_blank_str(c) for c in columns):
        errors.append("'columns' must be a non-empty list of column names.")
        columns = []
    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if duplicates:
        errors.append(f"column(s) listed more than once: {duplicates}.")
    for key in ("bank_column", "year_column"):
        if not _is_non_blank_str(raw.get(key)):
            errors.append(f"'{key}' must name a column.")
        elif raw[key] not in columns:
            errors.append(f"{key} '{raw[key]}' is not in 'columns'.")
    calculated = raw.get("calculated") or []
    if not isinstance(calculated, list) or not all(_is_non_blank_str(c) for c in calculated):
        errors.append("'calculated' must be a list of column names (or be left out).")
        calculated = []
    for column in calculated:
        if column not in columns:
            errors.append(f"calculated column '{column}' is not in 'columns'.")
        elif column in (raw.get("bank_column"), raw.get("year_column")):
            errors.append(f"calculated column '{column}' is the bank or year column.")
    formulas = _column_formulas(raw.get("formulas"), calculated, errors)
    _raise_if_errors(path, errors)
    return ExportTemplate(raw["bank_column"], raw["year_column"], tuple(columns), tuple(dict.fromkeys(calculated)),
                          formulas)


_FORMULA_UNITS = ("ETB", "fraction", "ratio", "count")


def _column_formulas(raw: object, calculated: list, errors: list[str]) -> tuple[ColumnFormula, ...]:
    """The `formulas` mapping: calculated column -> {formula, unit, compare_with?}."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        errors.append("'formulas' must map calculated column names to {formula, unit, compare_with}.")
        return ()
    found = []
    for column, entry in raw.items():
        where = f"formula of '{column}'"
        if column not in calculated:
            errors.append(f"{where}: '{column}' is not a calculated column.")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected 'formula' and 'unit'.")
            continue
        unknown = sorted(set(entry) - {"formula", "unit", "compare_with"})
        if unknown:
            errors.append(f"{where}: unknown key(s) {unknown}. Allowed: formula, unit, compare_with.")
        if not _is_non_blank_str(entry.get("formula")):
            errors.append(f"{where}: 'formula' must be a non-empty text value.")
            continue
        if entry.get("unit") not in _FORMULA_UNITS:
            errors.append(f"{where}: 'unit' must be one of {list(_FORMULA_UNITS)}.")
            continue
        compare = entry.get("compare_with")
        if compare is not None and not _is_non_blank_str(compare):
            errors.append(f"{where}: 'compare_with' must be a metric id (or be left out).")
            continue
        found.append(ColumnFormula(column, entry["formula"].strip(), entry["unit"], compare))
    return tuple(found)


def _check_export_columns(path: Path, metrics: tuple[MetricDefinition, ...], template: ExportTemplate) -> None:
    errors: list[str] = []
    seen: dict[str, str] = {}
    for m in metrics:
        if m.export_column is None:
            continue
        if m.export_column not in template.columns:
            errors.append(f"{m.id}: export_column '{m.export_column}' is not a column in export_template.yaml.")
        elif m.export_column in (template.bank_column, template.year_column):
            errors.append(f"{m.id}: export_column '{m.export_column}' is reserved for the bank or year.")
        elif m.export_column in template.calculated:
            errors.append(f"{m.id}: export_column '{m.export_column}' is a calculated column (entered by an "
                          "analyst), not an extracted one.")
        elif m.export_column in seen:
            errors.append(f"{m.id}: export_column '{m.export_column}' is already used by {seen[m.export_column]}.")
        seen.setdefault(m.export_column, m.id)
    _raise_if_errors(path, errors)


# --- settings.yaml -----------------------------------------------------------

_SETTINGS_FIELDS = ("data_dir", "max_upload_mb", "max_pages", "default_reviewer", "tesseract_dir", "app_url")


def load_settings(path: Path, project_root: Path = PROJECT_ROOT) -> AppSettings:
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: expected key/value settings at the top level.")
    errors: list[str] = []

    unknown = sorted(set(raw) - set(_SETTINGS_FIELDS))
    if unknown:
        errors.append(
            f"unknown setting(s) {unknown}. Allowed: {', '.join(_SETTINGS_FIELDS)}."
        )

    for field in ("max_upload_mb", "max_pages"):
        value = raw.get(field)
        if not _is_positive_int(value):
            errors.append(f"'{field}' must be a whole number greater than 0, got {value!r}.")

    data_dir_raw = raw.get("data_dir")
    if data_dir_raw is not None and not _is_non_blank_str(data_dir_raw):
        errors.append(f"'data_dir' must be a folder path or null, got {data_dir_raw!r}.")

    reviewer_raw = raw.get("default_reviewer")
    if reviewer_raw is not None and not _is_non_blank_str(reviewer_raw):
        errors.append(f"'default_reviewer' must be text or null, got {reviewer_raw!r}.")

    tesseract_raw = raw.get("tesseract_dir")
    if tesseract_raw is not None and not _is_non_blank_str(tesseract_raw):
        errors.append(f"'tesseract_dir' must be a folder path or null, got {tesseract_raw!r}.")

    # On a hosted copy the address is set in the host's settings, not in the file.
    app_url_raw = os.environ.get("BANK_EXTRACTOR_APP_URL") or raw.get("app_url")
    if app_url_raw is not None and not (_is_non_blank_str(app_url_raw)
                                        and re.match(r"^https?://[^\s/]+", app_url_raw.strip())):
        errors.append(f"'app_url' must be the app's address, e.g. http://localhost:8501, got {app_url_raw!r}.")

    _raise_if_errors(path, errors)

    return AppSettings(
        data_dir=_resolve_data_dir(data_dir_raw, project_root),
        max_upload_mb=raw["max_upload_mb"],
        max_pages=raw["max_pages"],
        default_reviewer=(reviewer_raw or _os_user_name()).strip(),
        tesseract_dir=Path(os.path.expandvars(tesseract_raw.strip())) if tesseract_raw else None,
        app_url=(app_url_raw or DEFAULT_APP_URL).strip().rstrip("/"),
    )


def default_data_dir() -> Path:
    """Per-user app data folder outside OneDrive / the project folder."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "BankPdfExtractor"
    return Path.home() / ".local" / "share" / "BankPdfExtractor"


def _resolve_data_dir(value: str | None, project_root: Path) -> Path:
    if value is None:
        return default_data_dir()
    path = Path(os.path.expandvars(os.path.expanduser(value.strip())))
    return path if path.is_absolute() else (project_root / path).resolve()


def _os_user_name() -> str:
    try:
        return getpass.getuser()
    except Exception:  # getuser raises different errors per platform when no name is set
        return "local-user"


# --- shared helpers ----------------------------------------------------------

def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        location = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            location = f" (line {mark.line + 1}, column {mark.column + 1})"
        raise ConfigError(
            f"{path.name}: invalid YAML{location}. Check indentation and quoting. Details: {exc}"
        ) from exc


def _read_top_level_list(path: Path, key: str) -> list[Any]:
    raw = _read_yaml(path)
    if not isinstance(raw, dict) or key not in raw:
        raise ConfigError(f"{path.name}: expected a top-level '{key}:' list.")
    value = raw[key]
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{path.name}: '{key}' must be a list (use '{key}: []' for none).")
    return value


def _check_fields(
    entry: dict[str, Any], required: tuple[str, ...], optional: tuple[str, ...], where: str
) -> list[str]:
    errors = []
    missing = [f for f in required if f not in entry or entry[f] is None]
    if missing:
        errors.append(f"{where}: missing required field(s): {', '.join(missing)}.")
    unknown = sorted(set(entry) - set(required) - set(optional))
    if unknown:
        errors.append(
            f"{where}: unknown field(s) {unknown}. "
            f"Allowed: {', '.join(required + optional)}."
        )
    return errors


def _describe_entry(index: int, entry: Any, id_field: str = "id") -> str:
    label = f"entry #{index + 1}"
    if isinstance(entry, dict) and isinstance(entry.get(id_field), str):
        label += f" ({entry[id_field]})"
    return label


def _raise_if_errors(path: Path, errors: list[str]) -> None:
    if errors:
        bullet_list = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(f"{path.name} has {len(errors)} problem(s):\n{bullet_list}")


def _is_non_blank_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
