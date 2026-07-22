"""Runner: executes workflows against both versions of the app in lockstep.

For each step, variables captured from earlier steps are substituted into
the path and body, then the identical request is sent to both the base and
target versions. Captured values always come from the base response — this
keeps the substituted inputs to later steps identical across both versions,
so the comparison stays apples-to-apples even if base and target diverge.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

from engine.manifest import Workflow, WorkflowStep
from engine.orchestrator import RunHandles

log = structlog.get_logger(__name__)

_VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_FULL_VAR_PATTERN = re.compile(r"^\{([a-zA-Z_][a-zA-Z0-9_]*)\}$")


class RunnerError(Exception):
    """Raised when a workflow step can't be substituted, sent, or captured."""


class ResponseCapture(BaseModel):
    """A single version's response to a step, with timing."""

    status_code: int
    headers: dict[str, str]
    body: Any = None
    latency_ms: float


class StepResult(BaseModel):
    """One step executed against both versions, with both responses."""

    method: str
    path: str
    body: Any = None
    captured: dict[str, Any] = Field(default_factory=dict)
    base_response: ResponseCapture
    target_response: ResponseCapture


class WorkflowResult(BaseModel):
    """All step results for one workflow run."""

    name: str
    steps: list[StepResult]


def run_workflows(
    workflows: list[Workflow],
    handles: RunHandles,
    *,
    client: httpx.Client | None = None,
) -> list[WorkflowResult]:
    """Execute every workflow's steps against the base and target versions.

    Each workflow gets its own fresh variable scope. Steps within a
    workflow run in order, since later steps may depend on values captured
    from earlier ones.
    """
    owns_client = client is None
    http_client = client if client is not None else httpx.Client()
    try:
        return [_run_workflow(workflow, handles, http_client) for workflow in workflows]
    finally:
        if owns_client:
            http_client.close()


def _run_workflow(workflow: Workflow, handles: RunHandles, client: httpx.Client) -> WorkflowResult:
    log.info("running workflow", name=workflow.name)
    variables: dict[str, Any] = {}
    steps: list[StepResult] = []
    for step in workflow.steps:
        steps.append(_run_step(workflow.name, step, handles, client, variables))
    return WorkflowResult(name=workflow.name, steps=steps)


def _run_step(
    workflow_name: str,
    step: WorkflowStep,
    handles: RunHandles,
    client: httpx.Client,
    variables: dict[str, Any],
) -> StepResult:
    path = _substitute(step.path, workflow_name, variables)
    body = _substitute(step.body, workflow_name, variables)

    base_response = _send(client, handles.base_url, step.method, path, body)
    target_response = _send(client, handles.target_url, step.method, path, body)

    captured: dict[str, Any] = {}
    if step.capture:
        captured = {
            var_name: _extract_jsonpath(expr, base_response.body, workflow_name, step.path)
            for var_name, expr in step.capture.items()
        }
        variables.update(captured)

    return StepResult(
        method=step.method,
        path=path,
        body=body,
        captured=captured,
        base_response=base_response,
        target_response=target_response,
    )


def _send(client: httpx.Client, base_url: str, method: str, path: str, body: Any) -> ResponseCapture:
    url = base_url.rstrip("/") + path
    start = time.monotonic()
    try:
        response = client.request(method, url, json=body)
    except httpx.HTTPError as exc:
        raise RunnerError(f"request failed: {method} {url}: {exc}") from exc
    latency_ms = (time.monotonic() - start) * 1000

    try:
        parsed_body = response.json()
    except ValueError:
        parsed_body = response.text

    return ResponseCapture(
        status_code=response.status_code,
        headers=dict(response.headers),
        body=parsed_body,
        latency_ms=latency_ms,
    )


def _substitute(value: Any, workflow_name: str, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _substitute_str(value, workflow_name, variables)
    if isinstance(value, dict):
        return {key: _substitute(item, workflow_name, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, workflow_name, variables) for item in value]
    return value


def _substitute_str(value: str, workflow_name: str, variables: dict[str, Any]) -> Any:
    full_match = _FULL_VAR_PATTERN.match(value)
    if full_match:
        return _lookup(full_match.group(1), workflow_name, variables)

    def replace(match: re.Match[str]) -> str:
        return str(_lookup(match.group(1), workflow_name, variables))

    return _VAR_PATTERN.sub(replace, value)


def _lookup(name: str, workflow_name: str, variables: dict[str, Any]) -> Any:
    if name not in variables:
        raise RunnerError(
            f"workflow {workflow_name!r}: variable {{{name}}} is not defined "
            "(no prior step captured it)"
        )
    return variables[name]


def _extract_jsonpath(expr: str, body: Any, workflow_name: str, step_path: str) -> Any:
    if not expr.startswith("$."):
        raise RunnerError(
            f"workflow {workflow_name!r} step {step_path!r}: capture expression {expr!r} "
            "must start with '$.'"
        )
    current = body
    for part in expr[2:].split("."):
        if not isinstance(current, dict) or part not in current:
            raise RunnerError(
                f"workflow {workflow_name!r} step {step_path!r}: capture expression {expr!r} "
                f"could not find field {part!r} in response body: {body!r}"
            )
        current = current[part]
    return current
