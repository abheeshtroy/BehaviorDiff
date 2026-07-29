"""Unit tests for the comparator.

compare() is a pure function over already-computed observer diffs (HttpDiff,
PostgresDiff, OutboundCallDiff) plus a NormalizeConfig, so these tests build
diffs with the same observers used elsewhere in the suite and hand them to
compare() directly — no live app, database, or proxy is needed.
"""

from __future__ import annotations

from engine.comparator import compare
from engine.manifest import NormalizeConfig
from engine.observers.http import HttpObserver
from engine.observers.postgres import PostgresObserver, PostgresSnapshot
from engine.observers.proxy import OutboundCall, ProxyObserver
from engine.runner import ResponseCapture, StepResult, WorkflowResult


def _config(
    ignore_fields: list[str] | None = None,
    uuid_fields: list[str] | None = None,
    numeric_tolerance: float = 0.0,
) -> NormalizeConfig:
    return NormalizeConfig(
        ignore_fields=ignore_fields or [],
        uuid_fields=uuid_fields or [],
        numeric_tolerance=numeric_tolerance,
    )


def _capture(status_code: int = 200, body=None, headers: dict[str, str] | None = None) -> ResponseCapture:
    return ResponseCapture(status_code=status_code, headers=headers or {}, body=body, latency_ms=1.0)


def _workflow_result(
    name: str,
    method: str = "GET",
    path: str = "/x",
    base: ResponseCapture | None = None,
    target: ResponseCapture | None = None,
) -> WorkflowResult:
    step = StepResult(
        method=method,
        path=path,
        body=None,
        captured={},
        base_response=base or _capture(),
        target_response=target or _capture(),
    )
    return WorkflowResult(name=name, steps=[step])


def _http_diffs(workflow_result: WorkflowResult):
    base_obs, target_obs = HttpObserver().observe([workflow_result])
    return HttpObserver.diff(base_obs, target_obs)


def _snapshot(tables: dict[str, list[dict]], primary_keys: dict[str, list[str]]) -> PostgresSnapshot:
    return PostgresSnapshot(tables=tables, primary_keys=primary_keys)


def _metadata_kwargs() -> dict[str, float | int]:
    return {"total_workflows": 1, "total_steps": 1, "duration_seconds": 1.5}


# -- http findings ------------------------------------------------------------


def test_http_status_diff_produces_finding() -> None:
    workflow_result = _workflow_result("wf", base=_capture(status_code=200), target=_capture(status_code=500))

    result = compare(
        http_diffs=_http_diffs(workflow_result),
        postgres_diff=None,
        outbound_diff=None,
        normalize_config=_config(),
        **_metadata_kwargs(),
    )

    [finding] = result.findings
    assert finding.category == "http"
    assert finding.severity == "changed"
    assert finding.workflow_name == "wf"
    assert finding.step_index == 0
    assert "status 200 -> 500" in finding.summary
    assert finding.evidence_base["status_code"] == 200
    assert finding.evidence_target["status_code"] == 500


def test_ignored_header_alone_does_not_produce_a_finding() -> None:
    # Date moves whenever two responses land either side of a second boundary,
    # which says nothing about behaviour.
    workflow_result = _workflow_result(
        "wf",
        base=_capture(headers={"date": "Mon, 01 Jan 2035 00:00:00 GMT", "server": "uvicorn"}),
        target=_capture(headers={"date": "Mon, 01 Jan 2035 00:00:01 GMT", "server": "uvicorn"}),
    )

    result = compare(
        http_diffs=_http_diffs(workflow_result),
        postgres_diff=None,
        outbound_diff=None,
        normalize_config=_config(ignore_fields=["headers.date"]),
        **_metadata_kwargs(),
    )

    assert result.findings == []
    assert result.noise_summary.http_suppressed == 1


def test_header_not_covered_by_ignore_rules_is_still_reported() -> None:
    workflow_result = _workflow_result(
        "wf",
        base=_capture(headers={"date": "Mon, 01 Jan 2035 00:00:00 GMT", "x-cache": "hit"}),
        target=_capture(headers={"date": "Mon, 01 Jan 2035 00:00:01 GMT", "x-cache": "miss"}),
    )

    result = compare(
        http_diffs=_http_diffs(workflow_result),
        postgres_diff=None,
        outbound_diff=None,
        normalize_config=_config(ignore_fields=["headers.date"]),
        **_metadata_kwargs(),
    )

    [finding] = result.findings
    assert "headers changed: x-cache" in finding.summary
    assert "date" not in finding.summary


def test_ignored_header_does_not_hide_a_real_difference() -> None:
    workflow_result = _workflow_result(
        "wf",
        base=_capture(status_code=500, headers={"date": "Mon, 01 Jan 2035 00:00:00 GMT"}),
        target=_capture(status_code=400, headers={"date": "Mon, 01 Jan 2035 00:00:01 GMT"}),
    )

    result = compare(
        http_diffs=_http_diffs(workflow_result),
        postgres_diff=None,
        outbound_diff=None,
        normalize_config=_config(ignore_fields=["headers.date"]),
        **_metadata_kwargs(),
    )

    [finding] = result.findings
    assert "status 500 -> 400" in finding.summary
    assert "headers changed" not in finding.summary


def test_wildcard_ignore_pattern_covers_headers() -> None:
    workflow_result = _workflow_result(
        "wf",
        base=_capture(headers={"date": "Mon, 01 Jan 2035 00:00:00 GMT"}),
        target=_capture(headers={"date": "Mon, 01 Jan 2035 00:00:01 GMT"}),
    )

    result = compare(
        http_diffs=_http_diffs(workflow_result),
        postgres_diff=None,
        outbound_diff=None,
        normalize_config=_config(ignore_fields=["*.date"]),
        **_metadata_kwargs(),
    )

    assert result.findings == []


# -- postgres findings ---------------------------------------------------------


def test_postgres_row_change_produces_finding() -> None:
    before = _snapshot({"orders": [{"id": 1, "status": "pending"}]}, {"orders": ["id"]})
    after = _snapshot({"orders": [{"id": 1, "status": "shipped"}]}, {"orders": ["id"]})
    postgres_diff = PostgresObserver.diff(before, after)

    result = compare(
        http_diffs=[],
        postgres_diff=postgres_diff,
        outbound_diff=None,
        normalize_config=_config(),
        **_metadata_kwargs(),
    )

    [finding] = result.findings
    assert finding.category == "postgres"
    assert finding.severity == "changed"
    assert finding.evidence_base == {"id": 1, "status": "pending"}
    assert finding.evidence_target == {"id": 1, "status": "shipped"}


def test_postgres_inserted_and_deleted_rows_produce_findings() -> None:
    before = _snapshot({"orders": [{"id": 1, "status": "pending"}]}, {"orders": ["id"]})
    after = _snapshot({"orders": [{"id": 2, "status": "pending"}]}, {"orders": ["id"]})
    postgres_diff = PostgresObserver.diff(before, after)

    result = compare(
        http_diffs=[],
        postgres_diff=postgres_diff,
        outbound_diff=None,
        normalize_config=_config(),
        **_metadata_kwargs(),
    )

    severities = {f.severity for f in result.findings}
    assert severities == {"added", "removed"}


# -- outbound findings ---------------------------------------------------------


def test_outbound_call_only_in_target_produces_finding() -> None:
    base_calls: list[OutboundCall] = []
    target_calls = [OutboundCall(method="POST", path="/v1/authorize", body={"amount": 10})]
    outbound_diff = ProxyObserver.diff(base_calls, target_calls)

    result = compare(
        http_diffs=[],
        postgres_diff=None,
        outbound_diff=outbound_diff,
        normalize_config=_config(),
        **_metadata_kwargs(),
    )

    [finding] = result.findings
    assert finding.category == "outbound"
    assert finding.severity == "added"
    assert finding.evidence_target["path"] == "/v1/authorize"
    assert finding.evidence_base is None


def test_outbound_calls_in_both_produce_no_finding() -> None:
    call = OutboundCall(method="GET", path="/v1/status")
    outbound_diff = ProxyObserver.diff([call], [call])

    result = compare(
        http_diffs=[],
        postgres_diff=None,
        outbound_diff=outbound_diff,
        normalize_config=_config(),
        **_metadata_kwargs(),
    )

    assert result.findings == []


# -- normalization suppresses noise --------------------------------------------


def test_normalized_fields_do_not_produce_findings() -> None:
    workflow_result = _workflow_result(
        "wf",
        base=_capture(body={"order_id": 1, "created_at": "2026-01-01T00:00:00Z"}),
        target=_capture(body={"order_id": 1, "created_at": "2026-01-02T00:00:00Z"}),
    )

    result = compare(
        http_diffs=_http_diffs(workflow_result),
        postgres_diff=None,
        outbound_diff=None,
        normalize_config=_config(ignore_fields=["*.created_at"]),
        **_metadata_kwargs(),
    )

    assert result.findings == []
    assert result.noise_summary.http_suppressed == 1
    assert result.noise_summary.total_suppressed == 1


def test_normalized_uuid_row_change_does_not_produce_finding() -> None:
    before = _snapshot(
        {"carts": [{"id": 1, "cart_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"}]},
        {"carts": ["id"]},
    )
    after = _snapshot(
        {"carts": [{"id": 1, "cart_id": "11111111-1111-1111-1111-111111111111"}]},
        {"carts": ["id"]},
    )
    postgres_diff = PostgresObserver.diff(before, after)

    result = compare(
        http_diffs=[],
        postgres_diff=postgres_diff,
        outbound_diff=None,
        normalize_config=_config(),
        **_metadata_kwargs(),
    )

    assert result.findings == []
    assert result.noise_summary.postgres_suppressed == 1


# -- clean comparison -----------------------------------------------------------


def test_clean_comparison_returns_empty_findings_list() -> None:
    workflow_result = _workflow_result(
        "wf", base=_capture(status_code=200, body={"ok": True}), target=_capture(status_code=200, body={"ok": True})
    )

    result = compare(
        http_diffs=_http_diffs(workflow_result),
        postgres_diff=None,
        outbound_diff=None,
        normalize_config=_config(),
        **_metadata_kwargs(),
    )

    assert result.findings == []
    assert result.noise_summary.total_suppressed == 0


def test_metadata_is_carried_through_to_result() -> None:
    result = compare(
        http_diffs=[],
        postgres_diff=None,
        outbound_diff=None,
        normalize_config=_config(),
        total_workflows=3,
        total_steps=7,
        duration_seconds=12.5,
    )

    assert result.metadata.total_workflows == 3
    assert result.metadata.total_steps == 7
    assert result.metadata.duration_seconds == 12.5
