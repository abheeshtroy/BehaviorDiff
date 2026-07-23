"""Best-effort extraction of app context from a git diff.

Workflow generation needs to know what routes exist and what tables the app
touches. Parsing that properly would mean a parser per framework, so these
helpers pattern-match instead — they are feeding context to an LLM, not making
decisions, and a missed route costs a slightly worse proposal rather than a
wrong finding. Nothing in the comparison path calls into this module.

Both helpers ignore removed (`-`) lines. Workflows generated from this context
run against both versions, and a route the change deleted would only produce
404s on the target.
"""

from __future__ import annotations

import re

import structlog

log = structlog.get_logger(__name__)

# An optionally-quoted, optionally-schema-qualified SQL identifier.
_IDENT = r"[`\"']?(?P<table>[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)[`\"']?"

_HTTP_VERBS = "get|post|put|patch|delete|head|options"

# @app.get("/x") / @router.post("/x") / app.get("/x", handler) — FastAPI and Express
# both read this way. Requiring the path to start with "/" keeps outbound HTTP
# calls like requests.get("https://...") out of the results.
_METHOD_CALL = re.compile(
    rf"""(?:@\s*)?[A-Za-z_]\w*\s*\.\s*(?P<method>{_HTTP_VERBS})\s*\(\s*['"](?P<path>/[^'"]*)['"]""",
    re.IGNORECASE,
)

# @app.route("/x", methods=["GET", "POST"]) — Flask and Blueprint.
_FLASK_ROUTE = re.compile(
    r"""@\s*[A-Za-z_]\w*\s*\.\s*route\s*\(\s*['"](?P<path>/[^'"]*)['"](?P<rest>[^)]*)""",
    re.IGNORECASE,
)
_FLASK_METHODS = re.compile(r"""methods\s*=\s*\[(?P<methods>[^\]]*)\]""", re.IGNORECASE)

# path("api/orders/", views.orders) / re_path(r"^api/orders/$", ...) — Django.
# Django routes carry no HTTP method, so they are reported as ANY.
_DJANGO_PATH = re.compile(
    r"""\b(?:path|re_path)\s*\(\s*r?['"](?P<path>[^'"]*)['"]""",
)

_TABLE_PATTERNS = (
    re.compile(rf"\bINSERT\s+INTO\s+{_IDENT}", re.IGNORECASE),
    re.compile(rf"\bUPDATE\s+{_IDENT}", re.IGNORECASE),
    # Anchored on SELECT so a Python `from x import y` isn't read as a table.
    re.compile(rf"\bSELECT\b.*?\bFROM\s+{_IDENT}", re.IGNORECASE),
    re.compile(
        rf"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}",
        re.IGNORECASE,
    ),
    re.compile(rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}", re.IGNORECASE),
)


def _content_lines(diff: str) -> list[str]:
    """Yield added and context lines, stripped of their diff marker.

    Drops file headers (`---`/`+++`), hunk headers (`@@`), and removed lines.
    """
    lines: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith(("---", "+++", "@@", "diff --git", "index ")):
            continue
        if raw.startswith("-"):
            continue
        lines.append(raw[1:] if raw.startswith("+") else raw)
    return lines


def _dedupe(values: list[str]) -> list[str]:
    """Drop duplicates, keeping first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def extract_routes_from_diff(diff: str) -> list[str]:
    """Pull route-like declarations out of a diff as "METHOD /path" strings.

    Recognizes FastAPI/Express method calls, Flask `@route` decorators (including
    their `methods=` list), and Django `path()` entries. Best-effort by design —
    an unrecognized framework yields an empty list, not an error.
    """
    routes: list[str] = []

    for line in _content_lines(diff):
        for match in _METHOD_CALL.finditer(line):
            routes.append(f"{match.group('method').upper()} {match.group('path')}")

        for match in _FLASK_ROUTE.finditer(line):
            path = match.group("path")
            methods_match = _FLASK_METHODS.search(match.group("rest") or "")
            if methods_match:
                verbs = re.findall(r"""['"]([A-Za-z]+)['"]""", methods_match.group("methods"))
            else:
                verbs = ["GET"]  # Flask's own default when methods= is omitted
            routes.extend(f"{verb.upper()} {path}" for verb in verbs)

        for match in _DJANGO_PATH.finditer(line):
            path = match.group("path").strip()
            if not path:
                continue
            # Django patterns are relative and may carry regex anchors.
            path = path.lstrip("^").rstrip("$")
            routes.append(f"ANY /{path.lstrip('/')}")

    result = _dedupe(routes)
    log.debug("routes_extracted", count=len(result))
    return result


def extract_tables_from_diff(diff: str) -> list[str]:
    """Pull SQL table names out of a diff, lowercased and deduplicated.

    Recognizes INSERT INTO, UPDATE, SELECT ... FROM, CREATE TABLE, and
    ALTER TABLE. Best-effort by design — a table referenced only through an ORM
    will not appear.
    """
    tables: list[str] = []

    for line in _content_lines(diff):
        for pattern in _TABLE_PATTERNS:
            for match in pattern.finditer(line):
                tables.append(match.group("table").lower())

    result = _dedupe(tables)
    log.debug("tables_extracted", count=len(result))
    return result
