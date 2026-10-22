"""Load and validate the YAML configuration in config/.

All problems found in a file are collected and raised together as one ConfigError,
so a user editing YAML can fix everything in one pass.
"""

from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

ALLOWED_UNITS = frozenset({"ETB", "count", "fraction", "ratio"})
_METRIC_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ConfigError(ValueError):
    """Raised when a configuration file is missing or invalid."""


@dataclass(frozen=True)
class MetricDefinition:
    id: str
    shega_column: str
    display_name: str
    definition: str
    expected_unit: str
    active: bool
    notes: str | None = None


@dataclass(frozen=True)
class LabelMapping:
    metric_id: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class AppSettings:
    data_dir: Path
    max_upload_mb: int
    max_pages: int
    default_reviewer: str

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

    def active_metrics(self) -> tuple[MetricDefinition, ...]:
        return tuple(m for m in self.metrics if m.active)

    def metric(self, metric_id: str) -> MetricDefinition:
        for m in self.metrics:
            if m.id == metric_id:
                return m
        raise KeyError(f"Unknown metric id '{metric_id}'")


def load_config(config_dir: Path = CONFIG_DIR, project_root: Path = PROJECT_ROOT) -> AppConfig:
    """Load metrics.yaml, mappings.yaml and settings.yaml from config_dir."""
    metrics = load_metrics(config_dir / "metrics.yaml")
    mappings = load_mappings(config_dir / "mappings.yaml", {m.id for m in metrics})
    settings = load_settings(config_dir / "settings.yaml", project_root)
    return AppConfig(metrics=metrics, mappings=mappings, settings=settings)


# --- metrics.yaml ------------------------------------------------------------

_METRIC_REQUIRED = ("id", "shega_column", "display_name", "definition", "expected_unit", "active")
_METRIC_OPTIONAL = ("notes",)


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
        entry_errors = _check_fields(entry, _METRIC_REQUIRED, _METRIC_OPTIONAL, where)
        for field in ("id", "shega_column", "display_name", "definition", "expected_unit", "notes"):
            if field in entry and entry[field] is not None and not _is_non_blank_str(entry[field]):
                entry_errors.append(f"{where}: '{field}' must be a non-empty text value.")
        if "notes" in entry and entry["notes"] is None:
            entry_errors.append(f"{where}: 'notes' is empty; remove it or add text.")
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
        if entry["shega_column"] in seen_columns:
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

        seen_ids.add(metric_id)
        seen_columns.add(entry["shega_column"])
        metrics.append(
            MetricDefinition(
                id=metric_id,
                shega_column=entry["shega_column"],
                display_name=entry["display_name"].strip(),
                definition=entry["definition"].strip(),
                expected_unit=entry["expected_unit"],
                active=entry["active"],
                notes=entry.get("notes"),
            )
        )

    _raise_if_errors(path, errors)
    return tuple(metrics)


# --- mappings.yaml -----------------------------------------------------------

_MAPPING_REQUIRED = ("metric_id", "aliases")


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
        entry_errors = _check_fields(entry, _MAPPING_REQUIRED, (), where)
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

        aliases = entry["aliases"]
        if not isinstance(aliases, list) or not aliases:
            errors.append(f"{where}: 'aliases' must be a non-empty list of labels.")
            continue
        bad = [a for a in aliases if not _is_non_blank_str(a)]
        if bad:
            errors.append(f"{where}: every alias must be non-empty text; invalid: {bad!r}.")
            continue
        mappings.append(LabelMapping(metric_id=metric_id, aliases=tuple(aliases)))

    _raise_if_errors(path, errors)
    return tuple(mappings)


# --- settings.yaml -----------------------------------------------------------

_SETTINGS_FIELDS = ("data_dir", "max_upload_mb", "max_pages", "default_reviewer")


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

    _raise_if_errors(path, errors)

    return AppSettings(
        data_dir=_resolve_data_dir(data_dir_raw, project_root),
        max_upload_mb=raw["max_upload_mb"],
        max_pages=raw["max_pages"],
        default_reviewer=(reviewer_raw or _os_user_name()).strip(),
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
