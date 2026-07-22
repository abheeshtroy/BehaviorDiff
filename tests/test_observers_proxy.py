"""Unit tests for the proxy observer.

Recording and diff logic are tested directly against OutboundCall/
ProxyObserver methods and the module's body-decoding helper — no real socket
server is started, per the observer's own separation of concerns (record()
and diff() are plain data operations; start()/stop() own the socket).
"""

from __future__ import annotations

import pytest

from engine.manifest import MockResponse, OutboundService
from engine.observers.proxy import (
    OutboundCall,
    ProxyObserver,
    ProxyObserverError,
    _decode_body,
)


def _service(mock_responses: dict[str, dict] | None = None) -> OutboundService:
    return OutboundService(
        name="payment-provider",
        base_url="https://api.payments.example.com",
        mock_responses={
            key: MockResponse(**value)
            for key, value in (mock_responses or {"POST /v1/authorize": {"status": 200, "body": {"ok": True}}}).items()
        },
    )


# -- record() -----------------------------------------------------------------


def test_record_appends_outbound_call_with_uppercased_method() -> None:
    observer = ProxyObserver(_service())

    call = observer.record("post", "/v1/authorize", {"content-type": "application/json"}, {"amount": 10})

    assert observer.calls == [call]
    assert call.method == "POST"
    assert call.path == "/v1/authorize"
    assert call.headers == {"content-type": "application/json"}
    assert call.body == {"amount": 10}


def test_record_accumulates_multiple_calls_in_order() -> None:
    observer = ProxyObserver(_service())

    observer.record("POST", "/v1/authorize", {}, {"amount": 1})
    observer.record("POST", "/v1/authorize", {}, {"amount": 2})

    assert [call.body for call in observer.calls] == [{"amount": 1}, {"amount": 2}]


# -- mock_response_for() -------------------------------------------------------


def test_mock_response_for_returns_configured_mock() -> None:
    observer = ProxyObserver(_service({"POST /v1/authorize": {"status": 200, "body": {"auth_id": "mock_001"}}}))

    mock = observer.mock_response_for("POST", "/v1/authorize")

    assert mock.status == 200
    assert mock.body == {"auth_id": "mock_001"}


def test_mock_response_for_strips_query_string_before_matching() -> None:
    observer = ProxyObserver(_service({"GET /v1/status": {"status": 200, "body": {}}}))

    mock = observer.mock_response_for("GET", "/v1/status?id=42")

    assert mock.status == 200


def test_mock_response_for_raises_when_not_configured() -> None:
    observer = ProxyObserver(_service())

    with pytest.raises(ProxyObserverError, match="no mock_response configured"):
        observer.mock_response_for("DELETE", "/v1/unknown")


# -- diff() ---------------------------------------------------------------------


def test_diff_call_made_by_both_versions() -> None:
    call = OutboundCall(method="POST", path="/v1/authorize", headers={}, body={"amount": 10})

    diff = ProxyObserver.diff([call], [call.model_copy()])

    assert diff.in_both == [call]
    assert diff.only_in_base == []
    assert diff.only_in_target == []


def test_diff_call_only_in_base() -> None:
    base_call = OutboundCall(method="POST", path="/v1/authorize", headers={}, body={"amount": 10})

    diff = ProxyObserver.diff([base_call], [])

    assert diff.only_in_base == [base_call]
    assert diff.in_both == []
    assert diff.only_in_target == []


def test_diff_call_only_in_target() -> None:
    target_call = OutboundCall(method="POST", path="/v1/refund", headers={}, body={"amount": 5})

    diff = ProxyObserver.diff([], [target_call])

    assert diff.only_in_target == [target_call]
    assert diff.in_both == []
    assert diff.only_in_base == []


def test_diff_matches_ignore_headers_but_require_method_path_body_equal() -> None:
    base_call = OutboundCall(method="POST", path="/v1/authorize", headers={"x-request-id": "a"}, body={"amount": 10})
    target_call = OutboundCall(
        method="POST", path="/v1/authorize", headers={"x-request-id": "b"}, body={"amount": 10}
    )

    diff = ProxyObserver.diff([base_call], [target_call])

    assert diff.in_both == [base_call]
    assert diff.only_in_base == []
    assert diff.only_in_target == []


def test_diff_treats_different_bodies_as_distinct_calls() -> None:
    base_call = OutboundCall(method="POST", path="/v1/authorize", headers={}, body={"amount": 10})
    target_call = OutboundCall(method="POST", path="/v1/authorize", headers={}, body={"amount": 20})

    diff = ProxyObserver.diff([base_call], [target_call])

    assert diff.only_in_base == [base_call]
    assert diff.only_in_target == [target_call]
    assert diff.in_both == []


def test_diff_matches_duplicate_calls_one_to_one() -> None:
    call = OutboundCall(method="POST", path="/v1/authorize", headers={}, body={"amount": 10})
    base_calls = [call.model_copy(), call.model_copy()]
    target_calls = [call.model_copy()]

    diff = ProxyObserver.diff(base_calls, target_calls)

    assert len(diff.in_both) == 1
    assert len(diff.only_in_base) == 1
    assert diff.only_in_target == []


def test_diff_empty_lists_produce_empty_diff() -> None:
    diff = ProxyObserver.diff([], [])

    assert diff.in_both == []
    assert diff.only_in_base == []
    assert diff.only_in_target == []


# -- _decode_body() -------------------------------------------------------------


def test_decode_body_empty_bytes_returns_none() -> None:
    assert _decode_body(b"") is None


def test_decode_body_parses_json() -> None:
    assert _decode_body(b'{"amount": 10}') == {"amount": 10}


def test_decode_body_falls_back_to_text_for_non_json() -> None:
    assert _decode_body(b"not json") == "not json"
