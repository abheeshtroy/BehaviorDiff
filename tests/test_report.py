"""Tests for the standalone, offline BehaviorDiff review document."""

from __future__ import annotations

import json

from engine.report import render_report, sanitize_evidence, write_report, write_sanitized_json


def _payload(**overrides):
    payload = {
        "findings": [{
            "category": "http", "workflow_name": "currency conversion", "step_index": 1,
            "summary": "GET /quote: body changed", "severity": "changed",
            "evidence_base": {"status_code": 200, "headers": {"Authorization": "Bearer top-secret"}, "body": {"currency": "USD", "note": "<script>alert(1)</script>"}},
            "evidence_target": {"status_code": 200, "body": {"currency": "EUR", "url": "https://example.test/?token=abc"}},
        }],
        "metadata": {"total_workflows": 1, "total_steps": 2, "duration_seconds": 0.45},
        "intent": {"summary": "Convert quotes to EUR"},
        "classification": {"classifications": [{"finding_index": 0, "classification": "suspicious", "reasoning": "Unexpected", "confidence": 0.9}]},
    }
    payload.update(overrides)
    return payload


def test_one_finding_is_a_self_contained_document_not_a_sidebar_inspector():
    report = render_report(_payload())

    assert "1 behavioral change detected" in report
    assert 'class="surface-counts"' in report
    assert 'href="#http">1 HTTP</a>' in report
    assert 'class="finding-group" id="http"' in report
    assert 'class="finding-card"' in report
    assert "GET /quote: body changed" in report
    assert "currency conversion · step 1" in report
    assert 'class="badge classification suspicious"' in report
    assert "Full captured evidence" in report
    assert "navigator" not in report and "inspector" not in report and "data-finding" not in report
    assert "react" not in report.lower() and "ArrowDown" not in report
    assert "@media print" in report and "@media(max-width:640px)" in report


def test_report_styles_are_offline_safe_and_disable_atmosphere_for_print():
    report = render_report(_payload())

    assert "@import" not in report
    assert "url(http" not in report.lower()
    assert 'Georgia,"Times New Roman",serif' in report
    assert 'ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif' in report
    assert "body::before,body::after{display:none}" in report


def test_inline_base_target_diffs_are_redacted_and_structured():
    report = render_report(_payload())

    assert 'class="base-column"' in report and 'class="target-column"' in report
    assert 'class="base-value">USD' in report
    assert 'class="target-value">EUR' in report
    assert "body.currency" in report
    assert "top-secret" not in report and "token=abc" not in report
    assert "[REDACTED]" in report
    assert "<script>alert(1)</script>" not in report
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report


def test_findings_are_grouped_in_fixed_surface_order():
    report = render_report(_payload(findings=[
        {"category": "outbound", "summary": "Target notified billing", "severity": "added", "evidence_target": {"url": "/billing"}},
        {"category": "latency", "summary": "Response took longer", "severity": "changed", "evidence_base": {"ms": 12}, "evidence_target": {"ms": 30}},
        {"category": "postgres", "summary": "row modified in invoices", "severity": "changed", "evidence_base": {"state": "open"}, "evidence_target": {"state": "paid"}},
        {"category": "http", "summary": "GET /health changed", "severity": "changed", "evidence_base": {"status": 200}, "evidence_target": {"status": 500}},
        {"category": "custom", "summary": "custom surface changed", "severity": "changed", "evidence_base": {"x": 1}, "evidence_target": {"x": 2}},
    ]))

    positions = [report.index(f'id="{group}"') for group in ("http", "database", "outbound", "timing", "other")]
    assert positions == sorted(positions)
    assert "1 HTTP" in report and "1 Database" in report and "1 Outbound" in report and "1 Timing" in report


def test_target_only_and_base_only_evidence_keep_base_target_columns():
    report = render_report(_payload(findings=[
        {"category": "outbound", "summary": "Target called payment provider", "severity": "added", "evidence_target": {"url": "https://payments.test/charge?token=secret"}},
        {"category": "postgres", "summary": "Target removed legacy row", "severity": "removed", "evidence_base": {"id": 1}},
    ]))

    assert "only on target" in report and "only on base" in report
    assert report.count("<h4>Base</h4>") == 2 and report.count("<h4>Target</h4>") == 2
    assert "token=secret" not in report and "token=[REDACTED]" in report


def test_clean_result_and_absent_metadata_do_not_invent_context():
    report = render_report({"findings": []})

    assert "No behavioral changes detected" in report
    assert "Both versions matched across the observed surfaces." in report
    for absent in ("Repository", "Compare", "Workflows", "Duration", "Generated", "Change intent", "Confidence"):
        assert absent not in report


def test_explicit_error_with_partial_metadata_cannot_look_clean():
    report = render_report({"error": "failed", "metadata": {}})

    assert "Run could not complete" in report
    assert "failed" in report
    assert "No behavioral changes detected" not in report


def test_missing_or_invalid_comparison_result_is_a_failed_review():
    for payload in ({}, {"metadata": {}}, {"result": {"metadata": {}}}, {"result": []}):
        report = render_report(payload)
        assert "Run could not complete" in report
        assert "No behavioral changes detected" not in report


def test_completed_empty_findings_result_remains_clean():
    report = render_report({"findings": [], "metadata": {"total_workflows": 0, "duration_seconds": 0}})

    assert "No behavioral changes detected" in report
    assert "Run could not complete" not in report


def test_sensitive_values_are_recursively_redacted_and_output_is_written(tmp_path):
    sanitized = sanitize_evidence({"nested": [{"session_id": "abc"}], "url": "/x?api_key=abc"})
    assert sanitized == {"nested": [{"session_id": "[REDACTED]"}], "url": "/x?api_key=[REDACTED]"}
    destination = write_report(tmp_path / "artifact" / "report.html", _payload())
    assert destination.read_text().startswith("<!doctype html>")


def test_sanitized_json_and_document_output_are_deterministic(tmp_path):
    payload = {"headers": {"Authorization": "Bearer top-secret"}, "url": "https://example.test/?api_key=key-secret"}
    destination = write_sanitized_json(tmp_path / "artifact" / "result.json", payload)
    serialized = destination.read_text()
    assert json.loads(serialized)["headers"]["Authorization"] == "[REDACTED]"
    assert "top-secret" not in serialized and "key-secret" not in serialized
    assert render_report(_payload()) == render_report(_payload())


def test_report_renders_an_expected_error():
    report = render_report({"error": "Docker is unavailable"})
    assert "Run could not complete" in report and "Docker is unavailable" in report
