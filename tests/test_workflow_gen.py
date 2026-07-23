"""Unit tests for workflow generation.

Every test replaces anthropic.Anthropic with a stub, so no test in this file
reaches the network or needs an API key. The stub records the kwargs it was
called with, which is how the tests assert on prompt construction.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import pytest

from ai.workflow_gen import (
    ProposedWorkflow,
    WorkflowGenerationError,
    WorkflowProposal,
    generate_workflows,
)

DIFF = """\
diff --git a/app/main.py b/app/main.py
@@ -10,7 +10,7 @@ def checkout(cart_id: str, address: dict):
-    city = address["city"]
+    if "city" not in address:
+        raise HTTPException(status_code=400, detail="address.city is required")
"""

ROUTES = ["POST /api/carts", "POST /api/checkout"]
TABLES = ["carts", "orders"]

VALID_RESPONSE = json.dumps(
    {
        "workflows": [
            {
                "name": "checkout-with-missing-city",
                "rationale": "Drives the new validation branch directly.",
                "steps": [
                    {
                        "method": "POST",
                        "path": "/api/carts",
                        "body": {"items": [{"sku": "SHOE-42", "qty": 1}]},
                        "capture": {"cart_id": "$.cart_id"},
                    },
                    {
                        "method": "POST",
                        "path": "/api/checkout",
                        "body": {"cart_id": "{cart_id}", "address": {}},
                    },
                ],
            },
            {
                "name": "checkout-happy-path",
                "rationale": "Confirms a well-formed address still succeeds.",
                "steps": [
                    {"method": "POST", "path": "/api/checkout"},
                ],
            },
        ],
        "coverage_notes": "Covers the validation branch and the happy path. "
        "Does not cover payment failures.",
    }
)


class _StubClient:
    """Stands in for anthropic.Anthropic(); replays a canned reply or raises."""

    def __init__(self, *, text: str | None = None, error: Exception | None = None):
        self._text = text
        self._error = error
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)]
        )


def _patch_client(stub: _StubClient):
    return patch("anthropic.Anthropic", return_value=stub)


def test_valid_json_parses_into_workflow_proposal():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        proposal = generate_workflows(DIFF, ROUTES)

    assert isinstance(proposal, WorkflowProposal)
    assert len(proposal.workflows) == 2
    assert all(isinstance(w, ProposedWorkflow) for w in proposal.workflows)
    assert proposal.coverage_notes.startswith("Covers the validation branch")


def test_steps_stay_plain_dicts_with_optional_keys_intact():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        proposal = generate_workflows(DIFF, ROUTES)

    first, second = proposal.workflows[0].steps
    assert first == {
        "method": "POST",
        "path": "/api/carts",
        "body": {"items": [{"sku": "SHOE-42", "qty": 1}]},
        "capture": {"cart_id": "$.cart_id"},
    }
    # A step that captures nothing keeps no capture key at all, so it can be
    # written straight back out as YAML.
    assert second == {
        "method": "POST",
        "path": "/api/checkout",
        "body": {"cart_id": "{cart_id}", "address": {}},
    }
    assert "capture" not in second


def test_model_and_max_tokens_match_the_spec():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        generate_workflows(DIFF, ROUTES)

    (call,) = stub.calls
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 4096


def test_routes_and_tables_are_sent_when_provided():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        generate_workflows(DIFF, ROUTES, TABLES)

    (call,) = stub.calls
    user_message = call["messages"][0]["content"]
    assert "POST /api/carts" in user_message
    assert "POST /api/checkout" in user_message
    assert "carts" in user_message
    assert "orders" in user_message
    assert DIFF in user_message
    assert "PR description" not in user_message


def test_tables_section_is_omitted_when_not_provided():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        generate_workflows(DIFF, ROUTES)

    (call,) = stub.calls
    user_message = call["messages"][0]["content"]
    assert "Observed tables" not in user_message
    assert "Available routes" in user_message


def test_empty_routes_tells_the_model_to_infer_them():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        generate_workflows(DIFF, [])

    (call,) = stub.calls
    user_message = call["messages"][0]["content"]
    assert "none were detected" in user_message


def test_pr_description_is_included_when_provided():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        generate_workflows(
            DIFF, ROUTES, TABLES, pr_description="Return 400 on malformed addresses"
        )

    (call,) = stub.calls
    user_message = call["messages"][0]["content"]
    assert "Return 400 on malformed addresses" in user_message


def test_empty_diff_still_generates_from_routes():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        proposal = generate_workflows("", ROUTES)

    assert len(proposal.workflows) == 2
    (call,) = stub.calls
    user_message = call["messages"][0]["content"]
    assert "none was provided" in user_message
    assert "POST /api/carts" in user_message


def test_response_wrapped_in_code_fence_still_parses():
    stub = _StubClient(text=f"```json\n{VALID_RESPONSE}\n```")
    with _patch_client(stub):
        proposal = generate_workflows(DIFF, ROUTES)

    assert len(proposal.workflows) == 2


def test_malformed_json_raises_workflow_generation_error():
    stub = _StubClient(text="Sure! Here are some workflows you could try.")
    with _patch_client(stub):
        with pytest.raises(WorkflowGenerationError, match="not valid JSON"):
            generate_workflows(DIFF, ROUTES)


def test_json_missing_required_field_raises_workflow_generation_error():
    stub = _StubClient(text=json.dumps({"workflows": []}))
    with _patch_client(stub):
        with pytest.raises(
            WorkflowGenerationError, match="did not match the WorkflowProposal schema"
        ):
            generate_workflows(DIFF, ROUTES)


def test_unknown_field_is_rejected_by_extra_forbid():
    payload = json.loads(VALID_RESPONSE)
    payload["workflows"][0]["confidence"] = 0.9
    stub = _StubClient(text=json.dumps(payload))
    with _patch_client(stub):
        with pytest.raises(
            WorkflowGenerationError, match="did not match the WorkflowProposal schema"
        ):
            generate_workflows(DIFF, ROUTES)


def test_api_error_raises_workflow_generation_error():
    error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    stub = _StubClient(error=error)
    with _patch_client(stub):
        with pytest.raises(WorkflowGenerationError, match="Anthropic API call failed"):
            generate_workflows(DIFF, ROUTES)


def test_empty_response_content_raises_workflow_generation_error():
    stub = _StubClient(text="")
    with _patch_client(stub):
        with pytest.raises(WorkflowGenerationError, match="no text content"):
            generate_workflows(DIFF, ROUTES)
