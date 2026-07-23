"""Unit tests for intent extraction.

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

from ai.intent import ChangeIntent, IntentExtractionError, extract_intent

DIFF = """\
diff --git a/app/main.py b/app/main.py
@@ -10,7 +10,7 @@ def checkout(cart_id: str, address: dict):
-    city = address["city"]
+    if "city" not in address:
+        raise HTTPException(status_code=400, detail="address.city is required")
"""

VALID_RESPONSE = json.dumps(
    {
        "summary": "Validate the checkout address instead of letting a KeyError escape.",
        "changed_routes": ["POST /api/checkout"],
        "changed_tables": ["orders"],
        "expected_behavior_changes": ["400 instead of 500 on a missing address city"],
        "risk_areas": ["carts left in a pending state when checkout now fails early"],
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


def test_valid_json_parses_into_change_intent():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        intent = extract_intent(DIFF)

    assert isinstance(intent, ChangeIntent)
    assert intent.summary.startswith("Validate the checkout address")
    assert intent.changed_routes == ["POST /api/checkout"]
    assert intent.changed_tables == ["orders"]
    assert intent.expected_behavior_changes == [
        "400 instead of 500 on a missing address city"
    ]
    assert intent.risk_areas == [
        "carts left in a pending state when checkout now fails early"
    ]


def test_diff_is_sent_and_pr_description_is_omitted_when_absent():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        extract_intent(DIFF)

    (call,) = stub.calls
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 1024
    user_message = call["messages"][0]["content"]
    assert DIFF in user_message
    assert "PR description" not in user_message


def test_pr_description_is_included_when_provided():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        intent = extract_intent(DIFF, pr_description="Return 400 on malformed addresses")

    (call,) = stub.calls
    user_message = call["messages"][0]["content"]
    assert "Return 400 on malformed addresses" in user_message
    assert DIFF in user_message
    assert intent.changed_routes == ["POST /api/checkout"]


def test_response_wrapped_in_code_fence_still_parses():
    stub = _StubClient(text=f"```json\n{VALID_RESPONSE}\n```")
    with _patch_client(stub):
        intent = extract_intent(DIFF)

    assert intent.changed_routes == ["POST /api/checkout"]


def test_malformed_json_raises_intent_extraction_error():
    stub = _StubClient(text="Sure! Here's what the change does: it adds validation.")
    with _patch_client(stub):
        with pytest.raises(IntentExtractionError, match="not valid JSON"):
            extract_intent(DIFF)


def test_json_missing_required_field_raises_intent_extraction_error():
    stub = _StubClient(text=json.dumps({"changed_routes": ["POST /api/checkout"]}))
    with _patch_client(stub):
        with pytest.raises(IntentExtractionError, match="did not match the ChangeIntent schema"):
            extract_intent(DIFF)


def test_unknown_field_is_rejected_by_extra_forbid():
    payload = json.loads(VALID_RESPONSE) | {"confidence": 0.9}
    stub = _StubClient(text=json.dumps(payload))
    with _patch_client(stub):
        with pytest.raises(IntentExtractionError, match="did not match the ChangeIntent schema"):
            extract_intent(DIFF)


def test_api_error_raises_intent_extraction_error():
    error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    stub = _StubClient(error=error)
    with _patch_client(stub):
        with pytest.raises(IntentExtractionError, match="Anthropic API call failed"):
            extract_intent(DIFF)


def test_empty_response_content_raises_intent_extraction_error():
    stub = _StubClient(text="")
    with _patch_client(stub):
        with pytest.raises(IntentExtractionError, match="no text content"):
            extract_intent(DIFF)


def test_empty_diff_raises_without_calling_the_api():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        with pytest.raises(IntentExtractionError, match="empty diff"):
            extract_intent("   \n")

    assert stub.calls == []
