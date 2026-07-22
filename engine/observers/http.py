"""HTTP observer: structures runner StepResults into comparable observations.

Each StepResult already carries a base and a target ResponseCapture. This
module splits them into two flat lists of HttpObservation keyed by
(workflow, step_index), then diffs those lists position-by-position. Diffing
is a pure function over already-captured observations, so it can be tested
without a live app — same pattern as PostgresObserver.diff.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, Field

from engine.runner import ResponseCapture, StepResult, WorkflowResult

log = structlog.get_logger(__name__)


class HttpObserverError(Exception):
    """Raised when workflow results can't be structured or diffed."""


class HttpObservation(BaseModel):
    """One version's observed response to a single workflow step."""

    workflow: str
    step_index: int
    method: str
    path: str
    status_code: int
    body: Any = None
    headers: dict[str, str] = Field(default_factory=dict)


class HttpDiff(BaseModel):
    """What differed between base and target for one workflow step.

    Carries the full base and target observations as evidence, not just the
    fact that something changed.
    """

    workflow: str
    step_index: int
    method: str
    path: str
    status_changed: bool
    body_changed: bool
    changed_headers: list[str] = Field(default_factory=list)
    base: HttpObservation
    target: HttpObservation

    @property
    def headers_changed(self) -> bool:
        return bool(self.changed_headers)


class HttpObserver:
    """Structures runner output into per-version HTTP observations and diffs them.

    header_allowlist restricts which response headers are carried into the
    observation (case-insensitive). Pass None to keep every header.
    """

    def __init__(self, header_allowlist: list[str] | None = None) -> None:
        self.header_allowlist = {h.lower() for h in header_allowlist} if header_allowlist is not None else None

    def observe(self, workflow_results: list[WorkflowResult]) -> tuple[list[HttpObservation], list[HttpObservation]]:
        """Split each workflow's steps into base and target HttpObservation lists.

        Returns (base_observations, target_observations) in workflow/step
        order, so diff() can match them by (workflow, step_index).
        """
        base_observations: list[HttpObservation] = []
        target_observations: list[HttpObservation] = []
        for workflow_result in workflow_results:
            for step_index, step in enumerate(workflow_result.steps):
                base_observations.append(
                    self._to_observation(workflow_result.name, step_index, step, step.base_response)
                )
                target_observations.append(
                    self._to_observation(workflow_result.name, step_index, step, step.target_response)
                )
        return base_observations, target_observations

    def _to_observation(
        self, workflow: str, step_index: int, step: StepResult, response: ResponseCapture
    ) -> HttpObservation:
        return HttpObservation(
            workflow=workflow,
            step_index=step_index,
            method=step.method,
            path=step.path,
            status_code=response.status_code,
            body=response.body,
            headers=self._select_headers(response.headers),
        )

    def _select_headers(self, headers: dict[str, str]) -> dict[str, str]:
        if self.header_allowlist is None:
            return dict(headers)
        return {key: value for key, value in headers.items() if key.lower() in self.header_allowlist}

    @staticmethod
    def diff(base: list[HttpObservation], target: list[HttpObservation]) -> list[HttpDiff]:
        """Compare base vs target observations and return the steps that differ.

        Pure function over already-captured observations; matches
        observations by (workflow, step_index), the same way
        PostgresObserver.diff matches rows by primary key.
        """
        base_by_key = {(o.workflow, o.step_index): o for o in base}
        target_by_key = {(o.workflow, o.step_index): o for o in target}

        if base_by_key.keys() != target_by_key.keys():
            raise HttpObserverError(
                f"base and target observe different steps: base={sorted(base_by_key)} "
                f"target={sorted(target_by_key)}"
            )

        diffs: list[HttpDiff] = []
        for key in sorted(base_by_key):
            base_obs = base_by_key[key]
            target_obs = target_by_key[key]

            status_changed = base_obs.status_code != target_obs.status_code
            body_changed = base_obs.body != target_obs.body
            all_header_names = set(base_obs.headers) | set(target_obs.headers)
            changed_headers = sorted(
                name for name in all_header_names if base_obs.headers.get(name) != target_obs.headers.get(name)
            )

            if status_changed or body_changed or changed_headers:
                diffs.append(
                    HttpDiff(
                        workflow=base_obs.workflow,
                        step_index=base_obs.step_index,
                        method=base_obs.method,
                        path=base_obs.path,
                        status_changed=status_changed,
                        body_changed=body_changed,
                        changed_headers=changed_headers,
                        base=base_obs,
                        target=target_obs,
                    )
                )
        return diffs
