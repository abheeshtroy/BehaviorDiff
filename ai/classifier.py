"""Classify comparator findings against the intent of the change.

The comparator produces findings; it cannot say whether a finding is the
point of the change or a regression it caused. This module hands the findings
and the extracted ChangeIntent to Claude and asks for a label per finding.

Classification is advisory. The findings themselves — and the evidence
attached to them — are produced deterministically and stand on their own; a
label is a reading of them, not a replacement for them. Nothing here is
allowed to drop a finding, and every classification is checked back against
the findings list before it is returned.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import anthropic
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai.intent import ChangeIntent
from engine.comparator import Finding

log = structlog.get_logger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048

Classification = Literal["intended", "suspicious", "noise"]


class ClassificationError(RuntimeError):
    """Raised when the API call fails or the reply isn't a usable ClassificationResult."""


class ClassifiedFinding(BaseModel):
    """One finding, labelled against the intent of the change."""

    model_config = ConfigDict(extra="forbid")

    finding_index: int
    classification: Classification
    reasoning: str
    confidence: float = Field(ge=0.0, le=1.0)


class ClassificationResult(BaseModel):
    """Labels for every finding, plus an overall read on the change."""

    model_config = ConfigDict(extra="forbid")

    classifications: list[ClassifiedFinding] = Field(default_factory=list)
    summary: str


def _system_prompt() -> str:
    schema = json.dumps(ClassificationResult.model_json_schema(), indent=2)
    return (
        "You are the classification step of BehaviorDiff, a differential testing "
        "engine. BehaviorDiff ran two versions of an application under identical "
        "conditions, executed the same workflows against both, and recorded every "
        "difference it observed in HTTP responses, PostgreSQL state, and outbound "
        "HTTP calls. Obvious nondeterminism has already been suppressed by "
        "measurement, so anything that reached you survived that filter.\n\n"
        "You are given the intent extracted from the code change and the list of "
        "findings. Label every finding with exactly one of:\n\n"
        '- "intended": the difference is what the change set out to do. It follows '
        "from the intent — a stated expected behavior change, or a direct "
        "consequence of one.\n"
        '- "suspicious": a real behavioral difference the intent does not account '
        "for. Anything touching a route or table the change did not claim to "
        "affect belongs here, as does a difference that goes further than the "
        "intent describes.\n"
        '- "noise": an artifact rather than a behavioral difference — residual '
        "timestamp or identifier jitter, header ordering, latency wobble.\n\n"
        "Judge each finding on the evidence attached to it. When the intent does "
        'not explain a difference, say "suspicious" — do not stretch the intent to '
        "cover it, and do not label something noise merely because it looks minor. "
        "Reflect your certainty in `confidence` rather than in the label.\n\n"
        "Classify every finding exactly once, using the `finding_index` given with "
        "each finding. Respond with a single JSON object and nothing else — no "
        "prose, no markdown fences. It must match this schema:\n\n"
        f"{schema}\n\n"
        "`reasoning` is one sentence per finding. `summary` is your overall "
        "assessment of the change across all findings."
    )


def _user_message(findings: list[Finding], intent: ChangeIntent) -> str:
    payload = {
        "intent": intent.model_dump(mode="json"),
        "findings": [
            {"finding_index": index, **finding.model_dump(mode="json")}
            for index, finding in enumerate(findings)
        ],
    }
    return json.dumps(payload, indent=2, default=str)


def _response_text(response: Any) -> str:
    """Concatenate the text blocks of a Messages API response."""
    blocks = getattr(response, "content", None) or []
    text = "".join(
        block.text for block in blocks if getattr(block, "type", None) == "text"
    )
    if not text.strip():
        raise ClassificationError(
            "Claude returned no text content; nothing to parse as a ClassificationResult"
        )
    return text


def _strip_code_fence(text: str) -> str:
    """Tolerate a ```json fence around the object even though we asked for none."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines[1:]).strip()


def _parse(text: str, finding_count: int) -> ClassificationResult:
    payload = _strip_code_fence(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ClassificationError(
            f"Claude's reply was not valid JSON: {exc}; reply was: {payload[:500]}"
        ) from exc

    try:
        result = ClassificationResult.model_validate(data)
    except ValidationError as exc:
        raise ClassificationError(
            f"Claude's reply did not match the ClassificationResult schema: {exc}"
        ) from exc

    # A label that points at no finding is unusable downstream, so reject it here
    # rather than let the CLI render a classification against the wrong evidence.
    for classified in result.classifications:
        if not 0 <= classified.finding_index < finding_count:
            raise ClassificationError(
                f"classification references finding_index {classified.finding_index}, "
                f"but only {finding_count} finding(s) were sent"
            )
    return result


def classify_findings(
    findings: list[Finding], intent: ChangeIntent
) -> ClassificationResult:
    """Label each finding intended / suspicious / noise against the change's intent."""
    if not findings:
        log.info("classification_skipped", reason="no findings to classify")
        return ClassificationResult(
            classifications=[],
            summary="No behavioral differences were found, so there is nothing to classify.",
        )

    log.info("classification_start", findings=len(findings))

    try:
        response = anthropic.Anthropic().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_system_prompt(),
            messages=[{"role": "user", "content": _user_message(findings, intent)}],
        )
    except anthropic.AnthropicError as exc:
        raise ClassificationError(f"Anthropic API call failed: {exc}") from exc

    result = _parse(_response_text(response), len(findings))

    labels = [classified.classification for classified in result.classifications]
    log.info(
        "findings_classified",
        classified=len(labels),
        intended=labels.count("intended"),
        suspicious=labels.count("suspicious"),
        noise=labels.count("noise"),
    )
    return result
