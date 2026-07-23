"""Unit tests for the runner, mocking httpx so no real servers are needed."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.manifest import Workflow, WorkflowStep
from engine.orchestrator import RunHandles
from engine.runner import RunnerError, run_workflows


def _handles() -> RunHandles:
    return RunHandles(
        base_url="http://base.local",
        target_url="http://target.local",
        base_postgres_dsn=None,
        target_postgres_dsn=None,
    )


def _response(status_code: int = 200, json_body: Any = None, headers: dict[str, str] | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {"content-type": "application/json"}
    if json_body is None:
        response.json.side_effect = ValueError("no json")
        response.text = ""
    else:
        response.json.return_value = json_body
        response.text = str(json_body)
    return response


def _mock_client(responses: list[MagicMock]) -> MagicMock:
    client = MagicMock()
    results = iter(responses)
    client.request.side_effect = lambda *a, **k: next(results)
    return client


# -- variable substitution -------------------------------------------------


def test_full_match_placeholder_preserves_captured_type() -> None:
    workflow = Workflow(
        name="wf",
        steps=[
            WorkflowStep(method="POST", path="/api/carts", body=None, capture={"cart_id": "$.cart_id"}),
            WorkflowStep(method="POST", path="/api/checkout", body={"cart_id": "{cart_id}"}),
        ],
    )
    client = _mock_client(
        [
            _response(json_body={"cart_id": 42}),  # step 1 base
            _response(json_body={"cart_id": 42}),  # step 1 target
            _response(json_body={"ok": True}),  # step 2 base
            _response(json_body={"ok": True}),  # step 2 target
        ]
    )

    [result] = run_workflows([workflow], _handles(), client=client)

    assert result.steps[1].body == {"cart_id": 42}
    assert isinstance(result.steps[1].body["cart_id"], int)


def test_partial_match_placeholder_substitutes_into_string() -> None:
    workflow = Workflow(
        name="wf",
        steps=[
            WorkflowStep(method="POST", path="/api/carts", body=None, capture={"cart_id": "$.cart_id"}),
            WorkflowStep(method="POST", path="/api/carts/{cart_id}/discount", body={"code": "SAVE10"}),
        ],
    )
    client = _mock_client(
        [
            _response(json_body={"cart_id": "abc-123"}),
            _response(json_body={"cart_id": "abc-123"}),
            _response(json_body={}),
            _response(json_body={}),
        ]
    )

    [result] = run_workflows([workflow], _handles(), client=client)

    assert result.steps[1].path == "/api/carts/abc-123/discount"


def test_undefined_variable_raises_runner_error() -> None:
    workflow = Workflow(
        name="wf",
        steps=[WorkflowStep(method="GET", path="/api/carts/{cart_id}", body=None)],
    )

    with pytest.raises(RunnerError, match="cart_id"):
        run_workflows([workflow], _handles(), client=_mock_client([]))


def test_nested_dict_and_list_substitution() -> None:
    workflow = Workflow(
        name="wf",
        steps=[
            WorkflowStep(method="POST", path="/api/carts", body=None, capture={"sku": "$.sku"}),
            WorkflowStep(
                method="POST",
                path="/api/checkout",
                body={"items": [{"sku": "{sku}", "qty": 1}], "note": "sku is {sku}"},
            ),
        ],
    )
    client = _mock_client(
        [
            _response(json_body={"sku": "SHOE-42"}),
            _response(json_body={"sku": "SHOE-42"}),
            _response(json_body={}),
            _response(json_body={}),
        ]
    )

    [result] = run_workflows([workflow], _handles(), client=client)

    assert result.steps[1].body == {
        "items": [{"sku": "SHOE-42", "qty": 1}],
        "note": "sku is SHOE-42",
    }


# -- capture extraction ------------------------------------------------------


def test_capture_extracts_nested_field() -> None:
    workflow = Workflow(
        name="wf",
        steps=[
            WorkflowStep(
                method="GET",
                path="/api/carts/1",
                body=None,
                capture={"city": "$.address.city"},
            )
        ],
    )
    client = _mock_client(
        [
            _response(json_body={"address": {"city": "SF"}}),
            _response(json_body={"address": {"city": "SF"}}),
        ]
    )

    [result] = run_workflows([workflow], _handles(), client=client)

    assert result.steps[0].captured == {"city": "SF"}
    assert result.steps[0].captured_target == {"city": "SF"}


def test_capture_missing_field_raises_runner_error() -> None:
    workflow = Workflow(
        name="wf",
        steps=[WorkflowStep(method="GET", path="/api/carts/1", body=None, capture={"cart_id": "$.cart_id"})],
    )
    client = _mock_client([_response(json_body={"other": 1}), _response(json_body={"other": 1})])

    with pytest.raises(RunnerError, match="cart_id"):
        run_workflows([workflow], _handles(), client=client)


def test_capture_expression_must_start_with_dollar_dot() -> None:
    workflow = Workflow(
        name="wf",
        steps=[WorkflowStep(method="GET", path="/api/carts/1", body=None, capture={"cart_id": "cart_id"})],
    )
    client = _mock_client([_response(json_body={"cart_id": 1}), _response(json_body={"cart_id": 1})])

    with pytest.raises(RunnerError, match=r"\$\."):
        run_workflows([workflow], _handles(), client=client)


# -- multi-step workflow / lockstep execution --------------------------------


def test_multi_step_workflow_step_two_uses_capture_from_step_one() -> None:
    workflow = Workflow(
        name="checkout",
        steps=[
            WorkflowStep(
                method="POST",
                path="/api/carts",
                body={"items": [{"sku": "SHOE-42", "qty": 1}]},
                capture={"cart_id": "$.cart_id"},
            ),
            WorkflowStep(
                method="POST",
                path="/api/carts/{cart_id}/discount",
                body={"code": "SAVE10"},
            ),
            WorkflowStep(
                method="POST",
                path="/api/checkout",
                body={"cart_id": "{cart_id}", "address": {"city": "SF"}},
            ),
        ],
    )
    client = _mock_client(
        [
            _response(json_body={"cart_id": "cart_1"}),  # step1 base
            _response(json_body={"cart_id": "cart_1"}),  # step1 target
            _response(json_body={"discounted": True}),  # step2 base
            _response(json_body={"discounted": True}),  # step2 target
            _response(json_body={"order_id": "order_1"}),  # step3 base
            _response(json_body={"order_id": "order_1"}),  # step3 target
        ]
    )

    [result] = run_workflows([workflow], _handles(), client=client)

    assert result.name == "checkout"
    assert len(result.steps) == 3
    assert result.steps[0].captured == {"cart_id": "cart_1"}
    assert result.steps[0].captured_target == {"cart_id": "cart_1"}
    assert result.steps[1].path == "/api/carts/cart_1/discount"
    assert result.steps[2].body == {"cart_id": "cart_1", "address": {"city": "SF"}}


def test_dual_track_capture_uses_correct_id_per_version() -> None:
    """With separate databases, base and target may generate different ids for
    the same logical resource. Each version must chain off its own captured
    value rather than reusing whatever base captured."""
    workflow = Workflow(
        name="wf",
        steps=[
            WorkflowStep(method="POST", path="/api/carts", body=None, capture={"cart_id": "$.cart_id"}),
            WorkflowStep(
                method="POST",
                path="/api/carts/{cart_id}/discount",
                body={"code": "SAVE10"},
            ),
        ],
    )
    client = _mock_client(
        [
            _response(json_body={"cart_id": "base_1"}),  # step1 base
            _response(json_body={"cart_id": "target_1"}),  # step1 target
            _response(json_body={"discounted": True}),  # step2 base
            _response(json_body={"discounted": True}),  # step2 target
        ]
    )

    [result] = run_workflows([workflow], _handles(), client=client)

    assert result.steps[0].captured == {"cart_id": "base_1"}
    assert result.steps[0].captured_target == {"cart_id": "target_1"}
    assert result.steps[1].path == "/api/carts/base_1/discount"

    calls = client.request.call_args_list
    assert calls[2].args == ("POST", "http://base.local/api/carts/base_1/discount")
    assert calls[3].args == ("POST", "http://target.local/api/carts/target_1/discount")


def test_identical_request_sent_to_base_and_target() -> None:
    workflow = Workflow(
        name="wf",
        steps=[WorkflowStep(method="POST", path="/api/carts", body={"items": []})],
    )
    client = _mock_client([_response(json_body={"ok": True}), _response(json_body={"ok": True})])

    run_workflows([workflow], _handles(), client=client)

    calls = client.request.call_args_list
    assert len(calls) == 2
    base_call, target_call = calls
    assert base_call.args == ("POST", "http://base.local/api/carts")
    assert target_call.args == ("POST", "http://target.local/api/carts")
    assert base_call.kwargs["json"] == target_call.kwargs["json"] == {"items": []}


def test_step_result_captures_status_body_headers_latency() -> None:
    workflow = Workflow(
        name="wf",
        steps=[WorkflowStep(method="GET", path="/health", body=None)],
    )
    client = _mock_client(
        [
            _response(status_code=200, json_body={"status": "ok"}, headers={"x-request-id": "abc"}),
            _response(status_code=503, json_body={"status": "down"}, headers={"x-request-id": "def"}),
        ]
    )

    [result] = run_workflows([workflow], _handles(), client=client)
    step = result.steps[0]

    assert step.base_response.status_code == 200
    assert step.base_response.body == {"status": "ok"}
    assert step.base_response.headers["x-request-id"] == "abc"
    assert step.base_response.latency_ms >= 0

    assert step.target_response.status_code == 503
    assert step.target_response.body == {"status": "down"}


def test_non_json_response_falls_back_to_text_body() -> None:
    workflow = Workflow(
        name="wf",
        steps=[WorkflowStep(method="GET", path="/health", body=None)],
    )
    base_resp = _response(status_code=200, json_body=None)
    base_resp.text = "plain text"
    target_resp = _response(status_code=200, json_body=None)
    target_resp.text = "plain text"
    client = _mock_client([base_resp, target_resp])

    [result] = run_workflows([workflow], _handles(), client=client)

    assert result.steps[0].base_response.body == "plain text"


def test_owned_client_is_closed_after_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import engine.runner as runner_module

    fake_client = _mock_client([_response(json_body={}), _response(json_body={})])
    monkeypatch.setattr(runner_module.httpx, "Client", MagicMock(return_value=fake_client))

    workflow = Workflow(name="wf", steps=[WorkflowStep(method="GET", path="/health", body=None)])
    run_workflows([workflow], _handles())

    fake_client.close.assert_called_once()


def test_externally_provided_client_is_not_closed() -> None:
    client = _mock_client([_response(json_body={}), _response(json_body={})])
    workflow = Workflow(name="wf", steps=[WorkflowStep(method="GET", path="/health", body=None)])

    run_workflows([workflow], _handles(), client=client)

    client.close.assert_not_called()
