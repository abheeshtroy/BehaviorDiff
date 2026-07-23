"""Unit tests for finding classification.

Every test replaces anthropic.Anthropic with a stub, so no test in this file
reaches the network or needs an API key. Findings are built directly rather
than run through the comparator — classify_findings takes them as given.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import pytest

from ai.classifier import (
    ClassificationError,
    ClassificationResult,
    classify_findings,
)
from ai.intent import ChangeIntent
from engine.comparator import Finding

INTENT = ChangeIntent(
    summary="Validate the checkout address instead of letting a KeyError escape.",
    changed_routes=["POST /api/checkout"],
    changed_tables=["orders"],
    expected_behavior_changes=["400 instead of 500 on a missing address city"],
    risk_areas=["discounts cleared on a failed checkout"],
)

FINDINGS = [
    Finding(
        category="http",
        workflow_name="checkout-with-invalid-address",
        step_index=2,
        summary="POST /api/checkout: status 500 -> 400",
        evidence_base={"status_code": 500},
        evidence_target={"status_code": 400},
        severity="changed",
    ),
    Finding(
        category="postgres",
        summary="row modified in carts (pk={'id': 1})",
        evidence_base={"discount_code": "SAVE10"},
        evidence_target={"discount_code": None},
        severity="changed",
    ),
    Finding(
        category="http",
        workflow_name="checkout-with-invalid-address",
        step_index=0,
        summary="POST /api/carts: headers changed: date",
        evidence_base={"headers": {"date": "Mon, 01 Jan 2026 00:00:00 GMT"}},
        evidence_target={"headers": {"date": "Mon, 01 Jan 2026 00:00:01 GMT"}},
        severity="changed",
    ),
]

VALID_RESPONSE = json.dumps(
    {
        "classifications": [
            {
                "finding_index": 0,
                "classification": "intended",
                "reasoning": "The 500 -> 400 change is exactly the stated expected behavior change.",
                "confidence": 0.95,
            },
            {
                "finding_index": 1,
                "classification": "suspicious",
                "reasoning": "The cart's discount is cleared on a failed checkout, which the intent does not describe.",
                "confidence": 0.8,
            },
            {
                "finding_index": 2,
                "classification": "noise",
                "reasoning": "Only the Date header differs, which is clock jitter rather than behavior.",
                "confidence": 0.99,
            },
        ],
        "summary": "The intended validation landed, but checkout now clears cart discounts on failure.",
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


def test_findings_are_classified_with_matching_indices():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        result = classify_findings(FINDINGS, INTENT)

    assert isinstance(result, ClassificationResult)
    assert [c.finding_index for c in result.classifications] == [0, 1, 2]
    assert result.summary.startswith("The intended validation landed")


def test_all_three_classifications_appear():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        result = classify_findings(FINDINGS, INTENT)

    by_index = {c.finding_index: c for c in result.classifications}
    assert by_index[0].classification == "intended"
    assert by_index[1].classification == "suspicious"
    assert by_index[2].classification == "noise"
    assert by_index[0].confidence == pytest.approx(0.95)
    assert by_index[1].reasoning.startswith("The cart's discount is cleared")


def test_findings_and_intent_are_sent_to_the_model():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        classify_findings(FINDINGS, INTENT)

    (call,) = stub.calls
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 2048
    payload = json.loads(call["messages"][0]["content"])
    assert payload["intent"]["changed_routes"] == ["POST /api/checkout"]
    assert [f["finding_index"] for f in payload["findings"]] == [0, 1, 2]
    assert payload["findings"][0]["summary"] == "POST /api/checkout: status 500 -> 400"
    assert payload["findings"][1]["evidence_base"] == {"discount_code": "SAVE10"}


def test_empty_findings_returns_empty_result_without_calling_the_api():
    stub = _StubClient(text=VALID_RESPONSE)
    with _patch_client(stub):
        result = classify_findings([], INTENT)

    assert result.classifications == []
    assert result.summary
    assert stub.calls == []


def test_response_wrapped_in_code_fence_still_parses():
    stub = _StubClient(text=f"```json\n{VALID_RESPONSE}\n```")
    with _patch_client(stub):
        result = classify_findings(FINDINGS, INTENT)

    assert len(result.classifications) == 3


def test_out_of_range_finding_index_raises_classification_error():
    payload = {
        "classifications": [
            {
                "finding_index": 7,
                "classification": "intended",
                "reasoning": "Points at a finding that was never sent.",
                "confidence": 0.5,
            }
        ],
        "summary": "Bogus.",
    }
    stub = _StubClient(text=json.dumps(payload))
    with _patch_client(stub):
        with pytest.raises(ClassificationError, match="only 3 finding\\(s\\) were sent"):
            classify_findings(FINDINGS, INTENT)


def test_unknown_classification_label_raises_classification_error():
    payload = {
        "classifications": [
            {
                "finding_index": 0,
                "classification": "probably-fine",
                "reasoning": "Not one of the three allowed labels.",
                "confidence": 0.5,
            }
        ],
        "summary": "Bogus.",
    }
    stub = _StubClient(text=json.dumps(payload))
    with _patch_client(stub):
        with pytest.raises(ClassificationError, match="did not match the ClassificationResult schema"):
            classify_findings(FINDINGS, INTENT)


def test_malformed_json_raises_classification_error():
    stub = _StubClient(text="Here's my assessment: everything looks intended.")
    with _patch_client(stub):
        with pytest.raises(ClassificationError, match="not valid JSON"):
            classify_findings(FINDINGS, INTENT)


def test_api_error_raises_classification_error():
    error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    stub = _StubClient(error=error)
    with _patch_client(stub):
        with pytest.raises(ClassificationError, match="Anthropic API call failed"):
            classify_findings(FINDINGS, INTENT)


def test_empty_response_content_raises_classification_error():
    stub = _StubClient(text="")
    with _patch_client(stub):
        with pytest.raises(ClassificationError, match="no text content"):
            classify_findings(FINDINGS, INTENT)
