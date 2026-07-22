"""Unit tests for the HTTP observer.

observe() and diff() are pure over hand-built StepResult/WorkflowResult
objects — no real runner or HTTP client is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.observers.http import HttpObserver, HttpObserverError
from engine.runner import ResponseCapture, StepResult, WorkflowResult


def _capture(status_code: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> ResponseCapture:
    return ResponseCapture(status_code=status_code, headers=headers or {}, body=body, latency_ms=1.0)


def _step(
    method: str = "GET",
    path: str = "/x",
    base: ResponseCapture | None = None,
    target: ResponseCapture | None = None,
) -> StepResult:
    return StepResult(
        method=method,
        path=path,
        body=None,
        captured={},
        base_response=base or _capture(),
        target_response=target or _capture(),
    )


def _workflow_result(name: str, steps: list[StepResult]) -> WorkflowResult:
    return WorkflowResult(name=name, steps=steps)


# -- observe(): structuring -------------------------------------------------


def test_observe_splits_steps_into_base_and_target_observations() -> None:
    steps = [
        _step(method="GET", path="/health", base=_capture(status_code=200), target=_capture(status_code=200)),
        _step(
            method="POST",
            path="/api/carts",
            base=_capture(status_code=201, body={"cart_id": 1}),
            target=_capture(status_code=201, body={"cart_id": 1}),
        ),
    ]
    workflow_result = _workflow_result("checkout", steps)

    base_obs, target_obs = HttpObserver().observe([workflow_result])

    assert [(o.workflow, o.step_index, o.method, o.path) for o in base_obs] == [
        ("checkout", 0, "GET", "/health"),
        ("checkout", 1, "POST", "/api/carts"),
    ]
    assert base_obs[1].status_code == 201
    assert base_obs[1].body == {"cart_id": 1}
    assert target_obs[1].status_code == 201


def test_observe_with_no_allowlist_keeps_all_headers() -> None:
    step = _step(base=_capture(headers={"content-type": "application/json", "x-request-id": "abc"}))
    workflow_result = _workflow_result("wf", [step])

    base_obs, _ = HttpObserver().observe([workflow_result])

    assert base_obs[0].headers == {"content-type": "application/json", "x-request-id": "abc"}


def test_observe_with_allowlist_filters_headers_case_insensitively() -> None:
    step = _step(base=_capture(headers={"Content-Type": "application/json", "X-Request-Id": "abc"}))
    workflow_result = _workflow_result("wf", [step])

    base_obs, _ = HttpObserver(header_allowlist=["content-type"]).observe([workflow_result])

    assert base_obs[0].headers == {"Content-Type": "application/json"}


def test_observe_handles_multiple_workflows_independently() -> None:
    wf1 = _workflow_result("wf1", [_step(path="/a")])
    wf2 = _workflow_result("wf2", [_step(path="/b")])

    base_obs, target_obs = HttpObserver().observe([wf1, wf2])

    assert [(o.workflow, o.path) for o in base_obs] == [("wf1", "/a"), ("wf2", "/b")]


# -- diff(): detecting changes ------------------------------------------------


def test_diff_reports_no_findings_when_identical() -> None:
    steps = [_step(base=_capture(status_code=200, body={"ok": True}), target=_capture(status_code=200, body={"ok": True}))]
    workflow_result = _workflow_result("wf", steps)
    base_obs, target_obs = HttpObserver().observe([workflow_result])

    diffs = HttpObserver.diff(base_obs, target_obs)

    assert diffs == []


def test_diff_detects_status_code_change() -> None:
    steps = [_step(base=_capture(status_code=200), target=_capture(status_code=500))]
    workflow_result = _workflow_result("wf", steps)
    base_obs, target_obs = HttpObserver().observe([workflow_result])

    [diff] = HttpObserver.diff(base_obs, target_obs)

    assert diff.status_changed is True
    assert diff.body_changed is False
    assert diff.base.status_code == 200
    assert diff.target.status_code == 500


def test_diff_detects_body_change() -> None:
    steps = [
        _step(
            base=_capture(status_code=200, body={"total": 10}),
            target=_capture(status_code=200, body={"total": 12}),
        )
    ]
    workflow_result = _workflow_result("wf", steps)
    base_obs, target_obs = HttpObserver().observe([workflow_result])

    [diff] = HttpObserver.diff(base_obs, target_obs)

    assert diff.status_changed is False
    assert diff.body_changed is True
    assert diff.base.body == {"total": 10}
    assert diff.target.body == {"total": 12}


def test_diff_detects_header_change_and_reports_which_header() -> None:
    steps = [
        _step(
            base=_capture(headers={"x-cache": "HIT"}),
            target=_capture(headers={"x-cache": "MISS"}),
        )
    ]
    workflow_result = _workflow_result("wf", steps)
    base_obs, target_obs = HttpObserver().observe([workflow_result])

    [diff] = HttpObserver.diff(base_obs, target_obs)

    assert diff.headers_changed is True
    assert diff.changed_headers == ["x-cache"]


def test_diff_ignores_unchanged_steps_alongside_changed_ones() -> None:
    steps = [
        _step(path="/unchanged", base=_capture(status_code=200), target=_capture(status_code=200)),
        _step(path="/changed", base=_capture(status_code=200), target=_capture(status_code=404)),
    ]
    workflow_result = _workflow_result("wf", steps)
    base_obs, target_obs = HttpObserver().observe([workflow_result])

    diffs = HttpObserver.diff(base_obs, target_obs)

    assert len(diffs) == 1
    assert diffs[0].path == "/changed"


def test_diff_raises_when_base_and_target_observe_different_steps() -> None:
    base_obs, _ = HttpObserver().observe([_workflow_result("wf", [_step(path="/a")])])
    _, target_obs = HttpObserver().observe([_workflow_result("wf", [_step(path="/b")])])
    # Force a mismatched step key between base and target.
    target_obs = [o.model_copy(update={"step_index": 1}) for o in target_obs]

    with pytest.raises(HttpObserverError, match="different steps"):
        HttpObserver.diff(base_obs, target_obs)
