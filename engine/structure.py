"""Structural diff: where inside two captured values the difference actually is.

A finding says a step's body changed, but the reader still has to line the two
evidence blobs up by eye to see that it was ``total`` and not ``status``. This
walks two already-captured JSON-like values the same way the normalizer walks
one — dict keys joined with ``.``, list indices appended as ``[N]`` — and
returns the paths of the leaves that differ.

Like the observers and the normalizer, this is a pure function over captured
data: no live app, database, or HTTP client is involved, and callers decide what
to compare (the comparator compares normalized values, so UUID placeholders and
ignored fields never show up as paths).

A difference at the root itself (two scalars, or a dict that became a list) is
reported as the empty path ``""``, since there is no field to name.
"""

from __future__ import annotations

from typing import Any, Iterator

DEFAULT_PATH_LIMIT = 50


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _index(path: str, index: int) -> str:
    return f"{path}[{index}]"


def _iter_changed_paths(base: Any, target: Any, path: str) -> Iterator[str]:
    if isinstance(base, dict) and isinstance(target, dict):
        for key in sorted(set(base) | set(target), key=str):
            child_path = _join(path, key)
            if key not in base or key not in target:
                yield child_path
            else:
                yield from _iter_changed_paths(base[key], target[key], child_path)
        return

    if isinstance(base, list) and isinstance(target, list):
        for index in range(max(len(base), len(target))):
            child_path = _index(path, index)
            if index >= len(base) or index >= len(target):
                yield child_path
            else:
                yield from _iter_changed_paths(base[index], target[index], child_path)
        return

    if base != target:
        yield path


def changed_paths(base: Any, target: Any, *, limit: int | None = DEFAULT_PATH_LIMIT) -> list[str]:
    """Paths whose values differ between two captured values.

    ``limit=None`` returns every path; the default caps the walk so a body that
    changed wholesale cannot produce a finding with thousands of paths in it.
    Ordered as the values are walked: dict keys sorted, list indices in order.
    """
    if limit is not None and limit <= 0:
        return []
    paths: list[str] = []
    for path in _iter_changed_paths(base, target, ""):
        paths.append(path)
        if limit is not None and len(paths) >= limit:
            break
    return paths


def describe_paths(paths: list[str], *, limit: int = 3) -> str:
    """A short human-readable list of paths: "total, items[0].price (+2 more)".

    Returns "" for an empty list and "the whole value" when the only path is the
    root, so a caller can build a sentence without special-casing either.
    """
    if not paths:
        return ""
    if paths == [""]:
        return "the whole value"
    shown = paths[:limit]
    rendered = ", ".join(path or "the whole value" for path in shown)
    if len(paths) > len(shown):
        rendered += f" (+{len(paths) - len(shown)} more)"
    return rendered
