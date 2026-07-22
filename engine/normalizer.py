"""Normalizer: suppresses nondeterministic noise in raw observation data.

Applies the manifest's NormalizeConfig to raw observation data (HTTP bodies,
Postgres rows, or any nested JSON-like structure of dicts/lists/scalars):
field ignoring, UUID remapping, and numeric tolerance. Row ordering and
instability detection operate one level up, over whole rows and whole runs
rather than individual values.

Field paths are dot-joined key names (list indices appended as "[N]"), with
no leading separator: a top-level key "id" has path "id"; a nested key has
path "user.id". A pattern like "*.created_at" matches both the exact nested
form (`fnmatch` lets "*" absorb "user.") and the common depth-0 shorthand
(matched against the trailing path segment), so config authors don't need
to know how deep a field sits before deciding to ignore it.

Nothing here should require a live app or database — normalization is a
pure function over already-captured data, same as the observer diff logic.
"""

from __future__ import annotations

import fnmatch
import math
import re
from typing import Any

import structlog
from pydantic import BaseModel, Field

from engine.manifest import NormalizeConfig

log = structlog.get_logger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_MISSING = object()


class NormalizationReport(BaseModel):
    """What a Normalizer changed while processing one piece of observation data."""

    ignored_fields: list[str] = Field(default_factory=list)
    remapped_uuids: dict[str, str] = Field(default_factory=dict)
    tolerance_suppressed: list[str] = Field(default_factory=list)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _index(path: str, idx: int) -> str:
    return f"{path}[{idx}]"


def _matches_any(path: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    last_segment = path.rsplit(".", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("*.") and fnmatch.fnmatch(last_segment, pattern[2:]):
            return True
    return False


def _decimal_places(tolerance: float) -> int:
    """Number of decimal places implied by a numeric_tolerance value, e.g. 0.001 -> 3.

    Returns -1 (meaning "don't round") when tolerance is 0, the manifest default.
    """
    if tolerance <= 0:
        return -1
    return max(0, -math.floor(math.log10(tolerance)))


class Normalizer:
    """Applies field ignoring, UUID remapping, and numeric tolerance to observation data.

    UUID placeholders are assigned in order of first appearance and held for
    the lifetime of this Normalizer instance, so the same UUID maps to the
    same placeholder across multiple normalize() calls within one run (e.g.
    a cart_id threaded through several workflow steps). Call reset(), or
    construct a new Normalizer, to start a fresh run.
    """

    def __init__(self, config: NormalizeConfig) -> None:
        self.config = config
        self._decimal_places = _decimal_places(config.numeric_tolerance)
        self._uuid_map: dict[str, str] = {}

    def reset(self) -> None:
        """Clear UUID placeholder state, starting a new run."""
        self._uuid_map = {}

    def normalize(self, data: Any) -> tuple[Any, NormalizationReport]:
        """Normalize one observation, returning the transformed data and a report of changes."""
        report = NormalizationReport()
        normalized = self._walk(data, "", report)
        log.debug(
            "normalized observation",
            ignored_fields=len(report.ignored_fields),
            remapped_uuids=len(report.remapped_uuids),
            tolerance_suppressed=len(report.tolerance_suppressed),
        )
        return normalized, report

    def _walk(self, value: Any, path: str, report: NormalizationReport) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                child_path = _join(path, key)
                if _matches_any(child_path, self.config.ignore_fields):
                    report.ignored_fields.append(child_path)
                    continue
                result[key] = self._walk(child, child_path, report)
            return result
        if isinstance(value, list):
            return [self._walk(child, _index(path, i), report) for i, child in enumerate(value)]
        if isinstance(value, str):
            return self._normalize_string(value, path, report)
        if isinstance(value, float):
            return self._normalize_float(value, path, report)
        return value

    def _normalize_string(self, value: str, path: str, report: NormalizationReport) -> str:
        is_declared_uuid_field = _matches_any(path, self.config.uuid_fields)
        if not (is_declared_uuid_field or _UUID_RE.match(value)):
            return value

        placeholder = self._uuid_map.get(value)
        if placeholder is None:
            placeholder = f"uuid-{len(self._uuid_map)}"
            self._uuid_map[value] = placeholder
        report.remapped_uuids[value] = placeholder
        return placeholder

    def _normalize_float(self, value: float, path: str, report: NormalizationReport) -> float:
        if self._decimal_places < 0:
            return value
        rounded = round(value, self._decimal_places)
        if rounded != value:
            report.tolerance_suppressed.append(path)
        return rounded


def _row_sort_key(row: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, repr(value)) for key, value in row.items()))


def normalize_rows(table: str, rows: list[dict[str, Any]], config: NormalizeConfig) -> list[dict[str, Any]]:
    """Sort rows by their values for tables where row order is nondeterministic.

    Only applies to tables listed in config.ignore_row_order; other tables
    are returned in their original order, since sorting them would hide a
    real ordering regression instead of suppressing noise.
    """
    if table not in config.ignore_row_order:
        return list(rows)
    return sorted(rows, key=_row_sort_key)


def _flatten(value: Any, path: str, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(child, _join(path, key), out)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _flatten(child, _index(path, i), out)
    else:
        out[path] = value


def detect_instability(runs: list[Any], config: NormalizeConfig) -> set[str]:
    """Run the same workflow against the same version multiple times and find
    which field paths vary between runs.

    Each run is normalized independently (field ignoring, UUID remapping,
    numeric tolerance) with its own fresh Normalizer before comparison, so
    per-run randomness in UUID values or floating-point noise doesn't itself
    register as instability — only fields whose *normalized* value still
    differs across runs do. Fields missing from some runs but present in
    others also count as unstable.
    """
    if len(runs) < 2:
        return set()

    flattened_runs: list[dict[str, Any]] = []
    for run in runs:
        normalized, _ = Normalizer(config).normalize(run)
        flattened: dict[str, Any] = {}
        _flatten(normalized, "", flattened)
        flattened_runs.append(flattened)

    all_paths: set[str] = set()
    for flattened in flattened_runs:
        all_paths.update(flattened)

    unstable = {
        path
        for path in all_paths
        if any(flattened.get(path, _MISSING) != flattened_runs[0].get(path, _MISSING) for flattened in flattened_runs[1:])
    }
    log.info("instability detection complete", runs=len(runs), unstable_fields=len(unstable))
    return unstable
