"""Comparator: ties observer diffs together into a unified list of findings.

Each observer (http, postgres, proxy) already knows how to diff its own kind
of observation; this module doesn't re-derive those diffs. Its job is to
turn each observer's diff output into Finding objects with consistent
shape, applying the normalizer to HTTP bodies and Postgres row values along
the way so that noise (ignored fields, UUIDs, floating-point jitter) doesn't
surface as a finding.

Base and target are normalized independently (a fresh Normalizer per side),
the same pattern normalizer.detect_instability uses for repeated runs of one
version: it makes UUID placeholders positional ("first UUID seen on this
side") rather than tied to the literal value, so two different random UUIDs
in the same structural position still compare equal.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from engine.manifest import NormalizeConfig
from engine.normalizer import Normalizer
from engine.observers.http import HttpDiff
from engine.observers.postgres import PostgresDiff, RowChange
from engine.observers.proxy import OutboundCall, OutboundCallDiff

log = structlog.get_logger(__name__)

FindingCategory = Literal["http", "postgres", "outbound", "latency"]
FindingSeverity = Literal["changed", "added", "removed"]


class Finding(BaseModel):
    """One observed behavioral difference between base and target, with evidence."""

    category: FindingCategory
    workflow_name: str | None = None
    step_index: int | None = None
    summary: str
    evidence_base: Any = None
    evidence_target: Any = None
    severity: FindingSeverity


class NoiseSummary(BaseModel):
    """How many would-be differences were suppressed entirely by normalization."""

    http_suppressed: int = 0
    postgres_suppressed: int = 0

    @property
    def total_suppressed(self) -> int:
        return self.http_suppressed + self.postgres_suppressed


class ComparisonMetadata(BaseModel):
    total_workflows: int
    total_steps: int
    duration_seconds: float


class ComparisonResult(BaseModel):
    """Full output of comparing base and target: findings, suppressed noise, run stats."""

    findings: list[Finding] = Field(default_factory=list)
    noise_summary: NoiseSummary = Field(default_factory=NoiseSummary)
    metadata: ComparisonMetadata


def _normalize_independently(value: Any, config: NormalizeConfig) -> Any:
    """Normalize one value with a fresh Normalizer.

    Using a fresh instance (rather than sharing one across base/target)
    keeps UUID placeholders positional per side instead of tracking literal
    values across sides — see module docstring.
    """
    normalized, _ = Normalizer(config).normalize(value)
    return normalized


def _compare_http(diff: HttpDiff, config: NormalizeConfig) -> Finding | None:
    normalized_base_body = _normalize_independently(diff.base.body, config)
    normalized_target_body = _normalize_independently(diff.target.body, config)
    body_changed = normalized_base_body != normalized_target_body

    if not (diff.status_changed or body_changed or diff.headers_changed):
        return None

    parts: list[str] = []
    if diff.status_changed:
        parts.append(f"status {diff.base.status_code} -> {diff.target.status_code}")
    if body_changed:
        parts.append("body changed")
    if diff.headers_changed:
        parts.append(f"headers changed: {', '.join(diff.changed_headers)}")

    return Finding(
        category="http",
        workflow_name=diff.workflow,
        step_index=diff.step_index,
        summary=f"{diff.method} {diff.path}: {'; '.join(parts)}",
        evidence_base=diff.base.model_dump(),
        evidence_target=diff.target.model_dump(),
        severity="changed",
    )


def _compare_postgres_modified(table: str, row_change: RowChange, config: NormalizeConfig) -> Finding | None:
    normalized_before = _normalize_independently(row_change.before, config)
    normalized_after = _normalize_independently(row_change.after, config)
    if normalized_before == normalized_after:
        return None

    return Finding(
        category="postgres",
        summary=f"row modified in {table} (pk={row_change.primary_key})",
        evidence_base=row_change.before,
        evidence_target=row_change.after,
        severity="changed",
    )


def _compare_postgres(diff: PostgresDiff, config: NormalizeConfig) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    suppressed = 0

    for table, table_diff in diff.tables.items():
        for row in table_diff.inserted:
            findings.append(
                Finding(
                    category="postgres",
                    summary=f"row inserted into {table}",
                    evidence_base=None,
                    evidence_target=row,
                    severity="added",
                )
            )
        for row in table_diff.deleted:
            findings.append(
                Finding(
                    category="postgres",
                    summary=f"row deleted from {table}",
                    evidence_base=row,
                    evidence_target=None,
                    severity="removed",
                )
            )
        for row_change in table_diff.modified:
            finding = _compare_postgres_modified(table, row_change, config)
            if finding is None:
                suppressed += 1
            else:
                findings.append(finding)

    return findings, suppressed


def _compare_outbound(diff: OutboundCallDiff) -> list[Finding]:
    findings: list[Finding] = []
    for call in diff.only_in_base:
        findings.append(_outbound_finding(call, only_in="base"))
    for call in diff.only_in_target:
        findings.append(_outbound_finding(call, only_in="target"))
    return findings


def _outbound_finding(call: OutboundCall, only_in: Literal["base", "target"]) -> Finding:
    if only_in == "base":
        return Finding(
            category="outbound",
            summary=f"outbound call {call.method} {call.path} made only by base",
            evidence_base=call.model_dump(),
            evidence_target=None,
            severity="removed",
        )
    return Finding(
        category="outbound",
        summary=f"outbound call {call.method} {call.path} made only by target",
        evidence_base=None,
        evidence_target=call.model_dump(),
        severity="added",
    )


def compare(
    *,
    http_diffs: list[HttpDiff],
    postgres_diff: PostgresDiff | None,
    outbound_diff: OutboundCallDiff | None,
    normalize_config: NormalizeConfig,
    total_workflows: int,
    total_steps: int,
    duration_seconds: float,
) -> ComparisonResult:
    """Combine every observer's diff output into one ComparisonResult.

    postgres_diff and outbound_diff are optional since a manifest may not
    configure a database or outbound services at all.
    """
    findings: list[Finding] = []
    noise = NoiseSummary()

    for http_diff in http_diffs:
        finding = _compare_http(http_diff, normalize_config)
        if finding is None:
            noise.http_suppressed += 1
        else:
            findings.append(finding)

    if postgres_diff is not None:
        postgres_findings, suppressed = _compare_postgres(postgres_diff, normalize_config)
        findings.extend(postgres_findings)
        noise.postgres_suppressed += suppressed

    if outbound_diff is not None:
        findings.extend(_compare_outbound(outbound_diff))

    metadata = ComparisonMetadata(
        total_workflows=total_workflows,
        total_steps=total_steps,
        duration_seconds=duration_seconds,
    )

    log.info(
        "comparison complete",
        findings=len(findings),
        noise_suppressed=noise.total_suppressed,
    )
    return ComparisonResult(findings=findings, noise_summary=noise, metadata=metadata)
