"""Propose a starter manifest by reading a repository.

Writing the first manifest for an app is the slowest part of adopting
BehaviorDiff: you have to know the start command, the port, the health check,
which tables matter, and what workflows are worth running. All of that is
already in the repo. This module scans for it, hands the result to Claude, and
gets back a manifest a human can edit rather than write.

Two layers, deliberately split:

- `scan_repo` is pure text heuristics. No API, no network, no AI. It reads the
  Dockerfile, the routes, and the table references, and reports what it saw.
- `generate_manifest` sends that context to Claude and returns YAML.

The generated manifest is a draft, not a verdict. It is validated on the way
out, but a manifest that fails validation is still returned — the user is going
to open the file either way, and a near-miss they can fix beats an error that
leaves them with nothing. Nothing in the comparison path calls into this module.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import anthropic
import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

from ai.scaffold import extract_routes_from_diff, extract_tables_from_diff
from engine.manifest import Manifest, ManifestError, parse_manifest

log = structlog.get_logger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096

# Directories that never hold app source but do hold enough files to dominate a
# scan. Matched on the directory name at any depth.
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        "target",
    }
)

# Route declarations are recognized per-language by ai.scaffold; these are the
# languages it knows how to read.
ROUTE_EXTENSIONS = frozenset({".py", ".js", ".ts", ".rb", ".go"})

# Table references show up in the same source files plus raw SQL.
TABLE_EXTENSIONS = ROUTE_EXTENSIONS | {".sql"}

# A file this large is generated, vendored, or a data dump — not something whose
# route declarations we need.
MAX_FILE_BYTES = 512_000

# Paths an app is likely to expose purely to say "I'm up". Matched as suffixes so
# a prefixed route like /api/health is recognized too.
HEALTH_SUFFIXES = (
    "/health",
    "/healthz",
    "/healthcheck",
    "/ready",
    "/readyz",
    "/live",
    "/livez",
    "/ping",
    "/status",
)

_EXPOSE = re.compile(r"^\s*EXPOSE\s+(?P<port>\d{1,5})", re.IGNORECASE | re.MULTILINE)
_DOCKER_ENTRY = re.compile(
    r"^\s*(?P<directive>CMD|ENTRYPOINT)\s+(?P<rest>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# --port 8000 / --port=8000 / -p 8000, as passed to uvicorn, gunicorn, rails, etc.
_PORT_FLAG = re.compile(r"(?:--port|-p)[=\s]+(?P<port>\d{1,5})\b")
# PORT=8000, port: 8000, "port": 8000 — env files, compose, and config literals.
_PORT_ASSIGN = re.compile(r"""["']?\bport\b["']?\s*[=:]\s*["']?(?P<port>\d{1,5})\b""", re.IGNORECASE)
# app.listen(3000) — Express and friends.
_PORT_LISTEN = re.compile(r"\.listen\s*\(\s*(?P<port>\d{1,5})\b")

# ORM and migration declarations, which ai.scaffold's SQL patterns can't see.
_ORM_TABLE_PATTERNS = (
    # SQLAlchemy declarative: __tablename__ = "orders"
    re.compile(r"""__tablename__\s*=\s*['"](?P<table>[A-Za-z_]\w*)['"]"""),
    # Django: class Meta: db_table = "orders"
    re.compile(r"""\bdb_table\s*=\s*['"](?P<table>[A-Za-z_]\w*)['"]"""),
    # SQLAlchemy Core: Table("orders", metadata, ...)
    re.compile(r"""\bTable\s*\(\s*['"](?P<table>[A-Za-z_]\w*)['"]"""),
    # Alembic / Rails / Knex migrations: create_table("orders"), create_table :orders
    re.compile(r"""\bcreate_table\s*[(\s]\s*[:'"](?P<table>[A-Za-z_]\w*)['"]?"""),
    # Query builders: knex.table("orders"), db.table("orders")
    re.compile(r"""\.\s*table\s*\(\s*['"](?P<table>[A-Za-z_]\w*)['"]"""),
)


class ManifestGenerationError(RuntimeError):
    """Raised when the API call fails or returns nothing usable."""


class RepoContext(BaseModel):
    """What a scan of the repository could determine without asking anyone.

    Every field is best-effort. An empty list or a None means "not found", never
    "not there" — a framework the heuristics don't recognize looks exactly like
    an app with no routes.
    """

    model_config = ConfigDict(extra="forbid")

    dockerfile: str | None = None
    routes: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    seed_files: list[str] = Field(default_factory=list)
    app_port: int | None = None
    start_command: str | None = None
    healthcheck_candidates: list[str] = Field(default_factory=list)


def _as_context_diff(text: str) -> str:
    """Reshape file contents into something the diff scanners will read whole.

    `ai.scaffold` parses diffs: it drops lines starting with `-` and strips the
    marker off lines starting with `+`. Prefixing every line with a space makes
    each one a context line, so the scanners see the file exactly as written
    instead of silently dropping any line that happens to start with a hyphen.
    """
    return "".join(f" {line}\n" for line in text.splitlines())


def _dedupe(values: list[str]) -> list[str]:
    """Drop duplicates, keeping first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _valid_port(raw: str) -> int | None:
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 0 < port <= 65535 else None


def _read_text(path: Path) -> str | None:
    """Read a source file, or return None if it isn't one we can scan.

    Oversized, binary, and unreadable files are skipped rather than raised on:
    this is a survey of a repo we know nothing about, and one unreadable file
    should not cost the user the other thousand.
    """
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            log.debug("scan_file_skipped", path=str(path), reason="too large")
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.debug("scan_file_skipped", path=str(path), reason=str(exc))
        return None


def _walk_source_files(repo: Path) -> list[Path]:
    """Collect scannable files under `repo`, skipping vendored directories."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        # Pruning dirnames in place is what stops os.walk from descending.
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            found.append(Path(dirpath) / filename)
    return found


def _extract_orm_tables(text: str) -> list[str]:
    tables: list[str] = []
    for pattern in _ORM_TABLE_PATTERNS:
        for match in pattern.finditer(text):
            tables.append(match.group("table").lower())
    return tables


def _extract_start_command(dockerfile: str) -> str | None:
    """Pull a runnable command out of a Dockerfile's CMD/ENTRYPOINT.

    Handles both exec form (`CMD ["uvicorn", "app:app"]`) and shell form
    (`CMD uvicorn app:app`). When a Dockerfile has both directives they are
    joined, which is what Docker does for an exec-form ENTRYPOINT. Line
    continuations are not followed — the result feeds a prompt, and a truncated
    command Claude can correct beats a parser per Dockerfile dialect.
    """
    entrypoint: str | None = None
    cmd: str | None = None

    for match in _DOCKER_ENTRY.finditer(dockerfile):
        rest = match.group("rest").strip()
        if rest.startswith("["):
            try:
                parts = json.loads(rest)
            except json.JSONDecodeError:
                continue
            if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
                continue
            value = " ".join(parts).strip()
        else:
            value = rest

        if not value:
            continue
        # Later directives win, matching Docker's own last-one-wins behavior.
        if match.group("directive").upper() == "ENTRYPOINT":
            entrypoint = value
        else:
            cmd = value

    if entrypoint and cmd:
        return f"{entrypoint} {cmd}"
    return entrypoint or cmd


def _extract_port(dockerfile: str | None, start_command: str | None, sources: list[str]) -> int | None:
    """Find the port the app listens on, most trustworthy source first.

    EXPOSE is a declaration and beats everything. The start command is next: a
    `--port` flag is what actually gets passed. Config literals come last, since
    a `port:` key in some unrelated YAML is easy to mistake for the app's own.
    """
    if dockerfile:
        match = _EXPOSE.search(dockerfile)
        if match and (port := _valid_port(match.group("port"))):
            return port

    for text in (start_command, dockerfile):
        if text and (match := _PORT_FLAG.search(text)) and (port := _valid_port(match.group("port"))):
            return port

    for text in sources:
        for pattern in (_PORT_LISTEN, _PORT_ASSIGN):
            if (match := pattern.search(text)) and (port := _valid_port(match.group("port"))):
                return port

    return None


def _healthcheck_candidates(routes: list[str]) -> list[str]:
    """Pick out the routes that look like liveness endpoints.

    Routes arrive as "METHOD /path"; candidates come back as bare paths, which is
    what the manifest's `healthcheck` field takes.
    """
    candidates: list[str] = []
    for route in routes:
        _, _, path = route.partition(" ")
        path = path.strip()
        if not path:
            continue
        normalized = path.rstrip("/").lower() or "/"
        if any(normalized.endswith(suffix) for suffix in HEALTH_SUFFIXES):
            candidates.append(path)
    return _dedupe(candidates)


def scan_repo(repo_path: str) -> RepoContext:
    """Collect app context from a repository without calling any API.

    Walks the tree once, reading each file at most once, and reports the
    Dockerfile, routes, tables, seed files, port, start command, and health
    check candidates it could find. A repo with nothing recognizable yields an
    empty RepoContext rather than an error.
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        raise ManifestGenerationError(f"not a directory: {repo_path}")

    dockerfile: str | None = None
    routes: list[str] = []
    tables: list[str] = []
    seed_files: list[str] = []
    port_sources: list[str] = []

    for path in _walk_source_files(repo):
        relative = path.relative_to(repo).as_posix()
        suffix = path.suffix.lower()
        is_dockerfile = path.name == "Dockerfile" or path.name.startswith("Dockerfile.")

        if suffix == ".sql":
            seed_files.append(relative)

        if not (is_dockerfile or suffix in TABLE_EXTENSIONS):
            continue

        text = _read_text(path)
        if text is None:
            continue

        if is_dockerfile:
            # First one wins, and os.walk reaches the root before any subdirectory,
            # so a root Dockerfile beats a service-level one.
            if dockerfile is None:
                dockerfile = text
            continue

        as_diff = _as_context_diff(text)
        if suffix in ROUTE_EXTENSIONS:
            routes.extend(extract_routes_from_diff(as_diff))
            port_sources.append(text)
        if suffix in TABLE_EXTENSIONS:
            tables.extend(extract_tables_from_diff(as_diff))
            tables.extend(_extract_orm_tables(text))

    routes = _dedupe(routes)
    start_command = _extract_start_command(dockerfile) if dockerfile else None

    context = RepoContext(
        dockerfile=dockerfile,
        routes=routes,
        tables=_dedupe(tables),
        seed_files=_dedupe(seed_files),
        app_port=_extract_port(dockerfile, start_command, port_sources),
        start_command=start_command,
        healthcheck_candidates=_healthcheck_candidates(routes),
    )
    log.info(
        "repo_scanned",
        repo=str(repo),
        has_dockerfile=context.dockerfile is not None,
        routes=len(context.routes),
        tables=len(context.tables),
        seed_files=len(context.seed_files),
        app_port=context.app_port,
    )
    return context


def _system_prompt() -> str:
    schema = json.dumps(Manifest.model_json_schema(), indent=2)
    return (
        "You are the onboarding step of BehaviorDiff, a differential testing "
        "engine. BehaviorDiff runs two versions of an application under identical "
        "conditions, executes the same workflows against both, and compares what "
        "it observes: HTTP responses, PostgreSQL table state, outbound HTTP "
        "calls, logs, and latency.\n\n"
        "Everything the engine knows about an app comes from its manifest. Your "
        "job is to read a scan of a repository and write a complete starter "
        "manifest for it, so the user edits YAML instead of writing it from "
        "scratch.\n\n"
        "The manifest is a YAML document that must validate against this JSON "
        "schema:\n\n"
        f"{schema}\n\n"
        "Section guidance:\n"
        "- app: name, start (the command that runs the server in the foreground, "
        "bound to 0.0.0.0), port, healthcheck (an absolute path that returns 200 "
        "when the app is up), and dockerfile. Use the scanned start command and "
        "port when they are given. If no health endpoint was found, choose the "
        "cheapest existing GET route rather than inventing one.\n"
        "- database: include it only if the app appears to use Postgres. Set seed "
        "to a scanned .sql file when one exists, otherwise 'seed.sql'. List the "
        "tables whose contents a behavioral change would show up in — the ones "
        "the app writes to. Skip migration bookkeeping tables.\n"
        "- outbound: include it only if the scan shows calls to an external HTTP "
        "service. Mock responses are keyed 'METHOD /path'.\n"
        "- compare: use the base_ref and target_ref you are given, and '.' for repo.\n"
        "- workflows: between 3 and 6, covering the app's main routes. Each is a "
        "name and an ordered list of HTTP steps. A step has method (uppercase "
        "verb), path (absolute, may contain {variable} placeholders), body (the "
        "JSON request body, omitted when there is none), and capture (a mapping "
        "of variable name to a JSONPath expression rooted at the response body, "
        'e.g. {"cart_id": "$.cart_id"}, omitted when the step captures nothing). '
        "A value captured by an earlier step is substituted into any later step "
        "by wrapping its name in braces, in the path or anywhere in the body. "
        "Only reference a variable an earlier step in the same workflow captured. "
        "Cover the happy path and at least one error path. Every workflow must "
        "run against a freshly seeded database, so start from whatever creation "
        "step it needs rather than assuming a record exists.\n"
        "- normalize: ignore_fields and uuid_fields take glob patterns like "
        "'*.created_at'. Suppress the fields that will differ on every run — "
        "timestamps, generated ids — so real differences stand out.\n\n"
        "Use only the routes you are given. Do not invent an endpoint that is not "
        "in the route list. Where the scan is silent, choose a conservative "
        "default and mark it with a YAML comment beginning '# TODO:' so the user "
        "knows what to check.\n\n"
        "Respond with the YAML document and nothing else — no prose, no markdown "
        "fences."
    )


def _user_message(
    context: RepoContext,
    base_ref: str,
    target_ref: str,
    pr_description: str | None,
) -> str:
    parts: list[str] = []
    if pr_description:
        parts.append(f"PR description:\n{pr_description}")

    parts.append(f"Git refs to compare:\n- base_ref: {base_ref}\n- target_ref: {target_ref}")

    if context.dockerfile:
        parts.append(f"Dockerfile:\n{context.dockerfile}")
    else:
        parts.append(
            "Dockerfile: none was found. Infer the app section from the routes "
            "below and mark each guess with a '# TODO:' comment."
        )

    if context.start_command:
        parts.append(f"Start command from the Dockerfile:\n{context.start_command}")
    if context.app_port is not None:
        parts.append(f"Detected port: {context.app_port}")

    if context.routes:
        parts.append("Routes found:\n" + "\n".join(f"- {r}" for r in context.routes))
    else:
        parts.append(
            "Routes: none were detected. The app may use a framework the scanner "
            "does not recognize. Propose a minimal manifest with placeholder "
            "workflows and mark them with '# TODO:' comments."
        )

    if context.healthcheck_candidates:
        parts.append(
            "Health endpoint candidates:\n"
            + "\n".join(f"- {p}" for p in context.healthcheck_candidates)
        )

    if context.tables:
        parts.append("Tables referenced:\n" + "\n".join(f"- {t}" for t in context.tables))
    else:
        parts.append("Tables: none were detected. Omit the database section unless the Dockerfile shows Postgres.")

    if context.seed_files:
        parts.append("SQL files found:\n" + "\n".join(f"- {p}" for p in context.seed_files))

    return "\n\n".join(parts)


def _response_text(response: object) -> str:
    """Concatenate the text blocks of a Messages API response."""
    blocks = getattr(response, "content", None) or []
    text = "".join(
        block.text for block in blocks if getattr(block, "type", None) == "text"
    )
    if not text.strip():
        raise ManifestGenerationError(
            "Claude returned no text content; nothing to use as a manifest"
        )
    return text


def _strip_code_fence(text: str) -> str:
    """Tolerate a ```yaml fence around the document even though we asked for none."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines[1:]).strip()


def _validate(manifest_yaml: str) -> None:
    """Parse the generated YAML, logging rather than raising on failure.

    The caller returns this YAML either way. A manifest that nearly validates is
    worth more to someone about to open it in an editor than an exception that
    leaves them with an empty file, so a failure here is reported, not fatal.
    """
    try:
        data = yaml.safe_load(manifest_yaml)
    except yaml.YAMLError as exc:
        log.warning("generated_manifest_invalid", stage="yaml", error=str(exc))
        return

    if data is None:
        log.warning("generated_manifest_invalid", stage="yaml", error="document is empty")
        return

    try:
        manifest = parse_manifest(data, source="<generated manifest>")
    except ManifestError as exc:
        log.warning("generated_manifest_invalid", stage="schema", error=str(exc))
        return

    log.info(
        "generated_manifest_valid",
        app=manifest.app.name,
        workflows=len(manifest.workflows),
    )


def generate_manifest(
    repo_path: str,
    base_ref: str = "main",
    target_ref: str = "HEAD",
    pr_description: str | None = None,
) -> str:
    """Scan a repository and return a starter manifest as YAML.

    The YAML is validated before it is returned, but an invalid document is
    still returned — with the failure logged — so the user gets a file to fix
    rather than nothing at all.

    Raises ManifestGenerationError if the repo can't be scanned or the API call
    fails.
    """
    context = scan_repo(repo_path)

    log.info(
        "manifest_generation_start",
        repo=repo_path,
        base_ref=base_ref,
        target_ref=target_ref,
        routes=len(context.routes),
        tables=len(context.tables),
        has_pr_description=pr_description is not None,
    )

    try:
        response = anthropic.Anthropic().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_system_prompt(),
            messages=[
                {
                    "role": "user",
                    "content": _user_message(context, base_ref, target_ref, pr_description),
                }
            ],
        )
    except anthropic.AnthropicError as exc:
        raise ManifestGenerationError(f"Anthropic API call failed: {exc}") from exc

    manifest_yaml = _strip_code_fence(_response_text(response))
    _validate(manifest_yaml)
    log.info("manifest_generated", bytes=len(manifest_yaml))
    return manifest_yaml
