"""Propose behavioral workflows that exercise a code change.

The engine can only find a difference on a code path something actually walked.
A manifest written before the change was made won't cover the change; this
module reads the diff, the routes the app exposes, and the tables it writes,
and asks Claude for workflows aimed at the changed behavior.

Proposals are suggestions, not workflows. Nothing here runs anything or edits a
manifest — the output is meant to be read, edited, and pasted in by a human, so
a bad proposal costs a review rather than a wrong comparison.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = structlog.get_logger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096


class WorkflowGenerationError(RuntimeError):
    """Raised when the API call fails or the reply isn't a usable WorkflowProposal."""


class ProposedWorkflow(BaseModel):
    """One suggested workflow, in the shape the manifest expects."""

    model_config = ConfigDict(extra="forbid")

    name: str
    rationale: str
    # Steps stay plain dicts rather than WorkflowStep models: these are written
    # straight into a manifest as YAML, and a model would drop the optional keys
    # a hand-edited manifest is allowed to omit.
    steps: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowProposal(BaseModel):
    """A set of proposed workflows plus an honest read on what they miss."""

    model_config = ConfigDict(extra="forbid")

    workflows: list[ProposedWorkflow] = Field(default_factory=list)
    coverage_notes: str


def _system_prompt() -> str:
    schema = json.dumps(WorkflowProposal.model_json_schema(), indent=2)
    return (
        "You are the workflow-generation step of BehaviorDiff, a differential "
        "testing engine. BehaviorDiff runs two versions of an application under "
        "identical conditions, executes the same workflows against both, and "
        "compares what it observes: HTTP responses, PostgreSQL table state, "
        "outbound HTTP calls, logs, and latency. It can only surface a difference "
        "on a code path a workflow actually walks.\n\n"
        "Your job is to read a code change and propose workflows that walk the "
        "paths the change touched. Propose between 2 and 5 workflows. Cover the "
        "happy path, at least one error path, and any edge case the change makes "
        "reachable — a boundary value, a missing field, a repeated call, an "
        "out-of-order sequence.\n\n"
        "A workflow is a name and an ordered list of HTTP steps. Each step has:\n"
        '- method: an HTTP verb, uppercase (e.g. "POST").\n'
        '- path: an absolute path starting with "/". It may contain '
        '{variable} placeholders.\n'
        "- body: the JSON request body. Omit it for requests that have none.\n"
        "- capture: a mapping of variable name to a JSONPath expression rooted at "
        'the response body, e.g. {"cart_id": "$.cart_id"}. Omit it when the step '
        "captures nothing.\n\n"
        "A value captured by an earlier step is substituted into any later step "
        "by wrapping its name in braces — it works in the path and anywhere "
        'inside the body. So a step that captures {"cart_id": "$.cart_id"} lets a '
        'later step use "/api/carts/{cart_id}/discount" as its path, or '
        '{"cart_id": "{cart_id}"} in its body. Only reference a variable some '
        "earlier step in the same workflow captured.\n\n"
        "Use only the routes you are given. Do not invent an endpoint that is not "
        "in the route list or visible in the diff. Every workflow must be able to "
        "run against a freshly seeded database — start from whatever creation "
        "step it needs rather than assuming a record already exists.\n\n"
        "Respond with a single JSON object and nothing else — no prose, no "
        "markdown fences. It must match this schema:\n\n"
        f"{schema}\n\n"
        "Field guidance:\n"
        "- name: a short kebab-case identifier for the workflow "
        '(e.g. "checkout-with-missing-city").\n'
        "- rationale: one or two sentences on what this workflow exercises about "
        "this specific change, and what a difference here would mean.\n"
        "- steps: the ordered HTTP steps described above.\n"
        "- coverage_notes: what these workflows collectively cover, and — just as "
        "importantly — what they do not. Name the paths you could not reach with "
        "the routes available."
    )


def _user_message(
    diff: str,
    routes: list[str],
    tables: list[str] | None,
    pr_description: str | None,
) -> str:
    parts: list[str] = []
    if pr_description:
        parts.append(f"PR description:\n{pr_description}")

    if routes:
        parts.append("Available routes:\n" + "\n".join(f"- {r}" for r in routes))
    else:
        parts.append(
            "Available routes: none were detected. Infer the routes from the diff, "
            "and say so in coverage_notes."
        )

    if tables:
        parts.append("Observed tables:\n" + "\n".join(f"- {t}" for t in tables))

    if diff.strip():
        parts.append(f"Git diff:\n{diff}")
    else:
        parts.append(
            "Git diff: none was provided. Propose workflows that exercise the "
            "routes above, and note the missing diff in coverage_notes."
        )

    return "\n\n".join(parts)


def _response_text(response: Any) -> str:
    """Concatenate the text blocks of a Messages API response."""
    blocks = getattr(response, "content", None) or []
    text = "".join(
        block.text for block in blocks if getattr(block, "type", None) == "text"
    )
    if not text.strip():
        raise WorkflowGenerationError(
            "Claude returned no text content; nothing to parse as a WorkflowProposal"
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


def _parse(text: str) -> WorkflowProposal:
    payload = _strip_code_fence(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WorkflowGenerationError(
            f"Claude's reply was not valid JSON: {exc}; reply was: {payload[:500]}"
        ) from exc

    try:
        return WorkflowProposal.model_validate(data)
    except ValidationError as exc:
        raise WorkflowGenerationError(
            f"Claude's reply did not match the WorkflowProposal schema: {exc}"
        ) from exc


def generate_workflows(
    diff: str,
    routes: list[str],
    tables: list[str] | None = None,
    pr_description: str | None = None,
) -> WorkflowProposal:
    """Propose workflows that exercise the code paths a change touched."""
    log.info(
        "workflow_generation_start",
        diff_bytes=len(diff),
        routes=len(routes),
        tables=len(tables or []),
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
                    "content": _user_message(diff, routes, tables, pr_description),
                }
            ],
        )
    except anthropic.AnthropicError as exc:
        raise WorkflowGenerationError(f"Anthropic API call failed: {exc}") from exc

    proposal = _parse(_response_text(response))
    log.info(
        "workflows_generated",
        workflows=len(proposal.workflows),
        steps=sum(len(w.steps) for w in proposal.workflows),
    )
    return proposal
