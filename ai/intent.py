"""Extract structured intent from a code change.

The engine finds behavioral differences without knowing why the change was
made. This module reads the change itself — the git diff, optionally a PR
description — and asks Claude what the author was trying to do. The result
feeds the classifier, which uses it to separate differences the change
intended from differences it didn't.

AI proposes here; nothing in this module decides anything. A ChangeIntent is
a hypothesis about the change, and the comparison path never consults it.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = structlog.get_logger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024


class IntentExtractionError(RuntimeError):
    """Raised when the API call fails or the reply isn't a usable ChangeIntent."""


class ChangeIntent(BaseModel):
    """What a change claims to do, as read off the diff."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    changed_routes: list[str] = Field(default_factory=list)
    changed_tables: list[str] = Field(default_factory=list)
    expected_behavior_changes: list[str] = Field(default_factory=list)
    risk_areas: list[str] = Field(default_factory=list)


def _system_prompt() -> str:
    schema = json.dumps(ChangeIntent.model_json_schema(), indent=2)
    return (
        "You are the intent-extraction step of BehaviorDiff, a differential testing "
        "engine. BehaviorDiff runs two versions of an application under identical "
        "conditions, executes the same workflows against both, and compares what it "
        "observes: HTTP responses, PostgreSQL table state, outbound HTTP calls, logs, "
        "and latency. It reports every difference it finds, but it has no idea which "
        "differences the author was going for.\n\n"
        "Your job is to read a code change and state what it is trying to do, so a "
        "later step can tell intended differences apart from unintended ones. Report "
        "what the diff shows, not what would be good practice. If the diff doesn't "
        "touch any HTTP routes or database tables, return empty lists — do not "
        "speculate about routes or tables you cannot see in the change.\n\n"
        "Respond with a single JSON object and nothing else — no prose, no markdown "
        "fences. It must match this schema:\n\n"
        f"{schema}\n\n"
        "Field guidance:\n"
        "- summary: one sentence describing what the change does.\n"
        "- changed_routes: HTTP routes whose behavior changes, formatted as "
        '"METHOD /path" (e.g. "POST /api/checkout").\n'
        "- changed_tables: database tables whose write patterns change.\n"
        "- expected_behavior_changes: observable differences the author is going for "
        '(e.g. "400 instead of 500 on an invalid address").\n'
        "- risk_areas: behavior that might change unintentionally as a side effect."
    )


def _user_message(diff: str, pr_description: str | None) -> str:
    parts: list[str] = []
    if pr_description:
        parts.append(f"PR description:\n{pr_description}")
    parts.append(f"Git diff:\n{diff}")
    return "\n\n".join(parts)


def _response_text(response: Any) -> str:
    """Concatenate the text blocks of a Messages API response."""
    blocks = getattr(response, "content", None) or []
    text = "".join(
        block.text for block in blocks if getattr(block, "type", None) == "text"
    )
    if not text.strip():
        raise IntentExtractionError(
            "Claude returned no text content; nothing to parse as a ChangeIntent"
        )
    return text


def _strip_code_fence(text: str) -> str:
    """Tolerate a ```json fence around the object even though we asked for none."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines[1:]).strip()


def _parse(text: str) -> ChangeIntent:
    payload = _strip_code_fence(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise IntentExtractionError(
            f"Claude's reply was not valid JSON: {exc}; reply was: {payload[:500]}"
        ) from exc

    try:
        return ChangeIntent.model_validate(data)
    except ValidationError as exc:
        raise IntentExtractionError(
            f"Claude's reply did not match the ChangeIntent schema: {exc}"
        ) from exc


def extract_intent(diff: str, pr_description: str | None = None) -> ChangeIntent:
    """Read a git diff (and optional PR description) into a structured ChangeIntent."""
    if not diff.strip():
        raise IntentExtractionError("cannot extract intent from an empty diff")

    log.info(
        "intent_extraction_start",
        diff_bytes=len(diff),
        has_pr_description=pr_description is not None,
    )

    try:
        response = anthropic.Anthropic().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_system_prompt(),
            messages=[{"role": "user", "content": _user_message(diff, pr_description)}],
        )
    except anthropic.AnthropicError as exc:
        raise IntentExtractionError(f"Anthropic API call failed: {exc}") from exc

    intent = _parse(_response_text(response))
    log.info(
        "intent_extracted",
        routes=len(intent.changed_routes),
        tables=len(intent.changed_tables),
        risk_areas=len(intent.risk_areas),
    )
    return intent
