"""Pipeline: one comparison run, expressed as a stream of events.

This holds the logic that used to live inline in cli.py's main(): stand up
both versions, run the workflows, observe HTTP and Postgres, compare, run the
advisory AI layer, persist. It is a generator so a caller can react to
progress as it happens — the CLI turns each event back into the log line it
always printed, and the web layer can stream the same events to a browser.

Stages, in the order a successful run yields them:

    environments_starting        about to start (or attach to) both versions
    environments_ready           both versions are reachable
    postgres_observation_skipped a dsn is missing, so no DB observation
    observing_postgres           snapshotting tables (data.phase before/after)
    workflow_started             one workflow is about to run
    workflow_completed           that workflow finished
    workflows_complete           every workflow has run
    observing_http               structuring and diffing HTTP observations
    comparing                    turning observer diffs into findings
    classifying                  advisory AI layer (only when a diff is given)
    persisting                   writing the run to the store
    done                         terminal, carries the full result
    error                        terminal, carries the failure

The generator always ends with exactly one terminal event: a caller can rely
on seeing "done" or "error" and never has to handle an exception escaping
mid-iteration. Teardown happens in a finally block regardless.

Every event a successful run yields is also stored with the run, so a caller
reading it back from the store later has the same progress stream a live
watcher saw. The stored stream ends at "persisting" — the store call is what
mints the run id, so nothing after it can be inside the row it wrote.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from ai.classifier import ClassificationError, ClassificationResult, classify_findings
from ai.intent import ChangeIntent, IntentExtractionError, extract_intent
from ai.scaffold import extract_routes_from_diff, extract_tables_from_diff
from ai.workflow_gen import (
    WorkflowGenerationError,
    WorkflowProposal,
    generate_workflows,
)
from engine.comparator import compare
from engine.manifest import Manifest
from engine.observers.http import HttpObserver
from engine.observers.postgres import PostgresObserver
from engine.orchestrator import Orchestrator, OrchestratorError, RunHandles
from engine.runner import RunnerError, run_workflows
from web.store import save_run

log = structlog.get_logger(__name__)

# Keys in RunEvent.data that carry live Python objects for an in-process
# caller (the CLI prints from the pydantic models, and re-raises the original
# exception). Anything serializing an event must drop them — json_data() does.
_LOCAL_ONLY_KEYS = frozenset({"models", "exception"})


class RunEvent(BaseModel):
    """One step of a run, as it happens."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    message: str
    timestamp: float
    data: dict | None = None

    def json_data(self) -> dict[str, Any] | None:
        """The event's data without the in-process-only keys.

        Use this instead of ``data`` when sending an event over the wire.
        """
        if self.data is None:
            return None
        return {key: value for key, value in self.data.items() if key not in _LOCAL_ONLY_KEYS}


def _event(stage: str, message: str, data: dict[str, Any] | None = None) -> RunEvent:
    return RunEvent(stage=stage, message=message, timestamp=time.time(), data=data)


def run_pipeline(
    manifest: Manifest,
    *,
    manifest_path: str | Path | None = None,
    base_url: str | None = None,
    target_url: str | None = None,
    base_pg_dsn: str | None = None,
    target_pg_dsn: str | None = None,
    diff_text: str | None = None,
    pr_description: str | None = None,
    generate_workflows: bool = False,
) -> Iterator[RunEvent]:
    """Run one comparison of the manifest's base and target, yielding progress.

    Passing both base_url and target_url attaches to already-running versions
    and skips orchestration entirely; the Postgres dsns are then the caller's
    to supply, and DB observation is skipped without them.

    diff_text enables the advisory AI layer (intent extraction and finding
    classification), and generate_workflows additionally asks for suggested
    workflows. Neither can fail the run — the findings are already final by
    the time either runs.

    manifest_path is recorded with the persisted run so the dashboard can show
    where the run came from.
    """
    orchestrator: Orchestrator | None = None

    # Every event this run has yielded, in wire-safe form, so the stored run
    # carries its own progress stream and the detail view can reconstruct the
    # run's shape in time once the live socket is gone. Persisting happens
    # before the terminal event by necessity, so "persisting" is the last stage
    # in here — "done" carries what the store already has.
    emitted_events: list[dict[str, Any]] = []

    def emit(stage: str, message: str, data: dict[str, Any] | None = None) -> RunEvent:
        event = _event(stage, message, data)
        emitted_events.append(
            {
                "stage": event.stage,
                "message": event.message,
                "timestamp": event.timestamp,
                # json_data(), not data: the live models and the raw exception
                # ride along for an in-process caller and cannot be stored.
                "data": event.json_data(),
            }
        )
        return event

    try:
        start = time.monotonic()
        use_direct = base_url and target_url
        if use_direct:
            yield emit(
                "environments_starting",
                "Attaching to the running base and target versions",
                {"direct": True},
            )
            handles = RunHandles(
                base_url=base_url.rstrip("/"),
                target_url=target_url.rstrip("/"),
                base_postgres_dsn=base_pg_dsn or None,
                target_postgres_dsn=target_pg_dsn or None,
            )
        else:
            yield emit(
                "environments_starting",
                "Building and starting both versions",
                {"direct": False},
            )
            orchestrator = Orchestrator(manifest)
            handles = orchestrator.start()

        yield emit(
            "environments_ready",
            f"Environments ready: base {handles.base_url}, target {handles.target_url}",
            {"base_url": handles.base_url, "target_url": handles.target_url},
        )

        # Each version has its own database, so there's no single "base-state vs
        # target-state" snapshot to diff directly — the rows live in separate
        # databases entirely. Instead we snapshot each database before and after
        # the workflows run, diff each version against its own seed to get that
        # version's delta, then compare the two deltas. Since both databases were
        # seeded identically, a change both versions made in the same way cancels
        # out and only real behavioral differences remain.
        observe_db = bool(manifest.database and handles.base_postgres_dsn and handles.target_postgres_dsn)
        if manifest.database and not observe_db:
            yield emit(
                "postgres_observation_skipped",
                "Skipping Postgres observation: a dsn is missing for one or both versions",
                {"reason": "a dsn is missing for one or both versions"},
            )

        base_pg_before = target_pg_before = None
        if observe_db:
            assert manifest.database is not None
            tables = manifest.database.observe_tables
            yield emit(
                "observing_postgres",
                f"Snapshotting {len(tables)} table(s) before the workflows run",
                {"phase": "before", "tables": list(tables)},
            )
            base_pg_before = PostgresObserver(handles.base_postgres_dsn).snapshot(tables)
            target_pg_before = PostgresObserver(handles.target_postgres_dsn).snapshot(tables)

        # Run workflows. One shared client across all of them, the same way
        # run_workflows would if it were handed the whole list at once; the
        # loop exists only so progress can be reported per workflow.
        workflow_results = []
        total = len(manifest.workflows)
        with httpx.Client() as client:
            for index, workflow in enumerate(manifest.workflows):
                yield emit(
                    "workflow_started",
                    f"Running workflow: {workflow.name}",
                    {"name": workflow.name, "index": index, "total": total},
                )
                result = run_workflows([workflow], handles, client=client)[0]
                workflow_results.append(result)
                yield emit(
                    "workflow_completed",
                    f"Finished workflow: {workflow.name} ({len(result.steps)} step(s))",
                    {
                        "name": workflow.name,
                        "index": index,
                        "total": total,
                        "steps": len(result.steps),
                    },
                )

        yield emit(
            "workflows_complete",
            f"Ran {len(workflow_results)} workflow(s)",
            {"count": len(workflow_results)},
        )

        # Count total steps
        total_steps = sum(len(wr.steps) for wr in workflow_results)

        # Observe HTTP
        yield emit("observing_http", "Comparing HTTP responses")
        http_observer = HttpObserver()
        base_obs, target_obs = http_observer.observe(workflow_results)
        http_diffs = HttpObserver.diff(base_obs, target_obs)

        # Observe Postgres
        pg_diff = None
        if observe_db:
            assert manifest.database is not None
            assert base_pg_before is not None and target_pg_before is not None
            tables = manifest.database.observe_tables
            yield emit(
                "observing_postgres",
                f"Snapshotting {len(tables)} table(s) after the workflows ran",
                {"phase": "after", "tables": list(tables)},
            )
            base_pg_after = PostgresObserver(handles.base_postgres_dsn).snapshot(tables)
            target_pg_after = PostgresObserver(handles.target_postgres_dsn).snapshot(tables)
            base_delta = PostgresObserver.diff(base_pg_before, base_pg_after)
            target_delta = PostgresObserver.diff(target_pg_before, target_pg_after)
            pg_diff = PostgresObserver.compare_deltas(base_delta, target_delta)

        # Observe outbound calls
        outbound_diff = None
        # TODO: wire proxy observer once proxy is started by orchestrator

        duration = time.monotonic() - start

        # Compare
        yield emit("comparing", "Comparing observations")
        result = compare(
            http_diffs=http_diffs,
            postgres_diff=pg_diff,
            outbound_diff=outbound_diff,
            normalize_config=manifest.normalize,
            total_workflows=len(workflow_results),
            total_steps=total_steps,
            duration_seconds=round(duration, 2),
        )

        # AI layer: intent extraction + finding classification. Advisory only —
        # the findings above are already final, so any failure here degrades to
        # plain output rather than failing the run.
        intent = None
        classification = None
        proposal = None
        if diff_text is not None:
            yield emit(
                "classifying",
                f"Classifying {len(result.findings)} finding(s) against the change's intent",
                {"findings": len(result.findings)},
            )
            intent, classification = _classify(diff_text, pr_description, result.findings)
            if generate_workflows:
                proposal = _propose_workflows(diff_text, pr_description)

        # Persist to SQLite for web dashboard
        result_payload = result.model_dump(mode="json")
        intent_payload = intent.model_dump(mode="json") if intent else None
        classification_payload = classification.model_dump(mode="json") if classification else None
        proposal_payload = proposal.model_dump(mode="json") if proposal else None

        yield emit("persisting", "Saving the run")
        run_id = save_run(
            manifest_path=str(manifest_path) if manifest_path is not None else "",
            app_name=manifest.app.name,
            result=result_payload,
            intent=intent_payload,
            classification=classification_payload,
            # A copy: the generator keeps emitting after this call, and what was
            # persisted must not change under the store afterwards.
            events=list(emitted_events),
        )

        yield emit(
            "done",
            f"Run complete: {len(result.findings)} finding(s)",
            {
                "result": result_payload,
                "intent": intent_payload,
                "classification": classification_payload,
                "proposed_workflows": proposal_payload,
                "run_id": run_id,
                # Live models, for an in-process caller that wants to render
                # them rather than re-parse the payloads above.
                "models": {
                    "result": result,
                    "intent": intent,
                    "classification": classification,
                    "proposal": proposal,
                },
            },
        )

    except (OrchestratorError, RunnerError) as exc:
        yield _error_event(exc)
    except Exception as exc:  # noqa: BLE001 - re-raised by the caller via data["exception"]
        yield _error_event(exc)
    finally:
        if orchestrator:
            orchestrator.cleanup()


def _error_event(exc: Exception) -> RunEvent:
    """Terminal event for a failed run, carrying the exception for the caller.

    The exception object rides along under a local-only key so an in-process
    caller can decide what to do with it — the CLI re-raises anything that
    isn't an OrchestratorError or RunnerError rather than swallowing a bug.
    """
    return _event(
        "error",
        str(exc),
        {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "expected": isinstance(exc, (OrchestratorError, RunnerError)),
            "exception": exc,
        },
    )


def _classify(
    diff: str,
    pr_description: str | None,
    findings: list,
) -> tuple[ChangeIntent | None, ClassificationResult | None]:
    """Extract intent from the diff and classify findings against it.

    Returns (None, None) when the AI layer can't run or fails — the engine's
    findings are complete without it, so a missing API key or a bad reply
    should not cost the user their comparison.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning(
            "ai_classification_skipped",
            reason="ANTHROPIC_API_KEY is not set",
        )
        return None, None

    try:
        intent = extract_intent(diff, pr_description)
    except IntentExtractionError as exc:
        log.error("intent_extraction_failed", error=str(exc))
        return None, None

    try:
        return intent, classify_findings(findings, intent)
    except ClassificationError as exc:
        log.error("classification_failed", error=str(exc))
        return intent, None


def _propose_workflows(
    diff: str,
    pr_description: str | None,
) -> WorkflowProposal | None:
    """Suggest workflows that exercise the change described by the diff.

    Advisory like the rest of the AI layer: returns None on any failure so a bad
    reply costs the suggestion, not the run.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning(
            "workflow_generation_skipped",
            reason="ANTHROPIC_API_KEY is not set",
        )
        return None

    routes = extract_routes_from_diff(diff)
    tables = extract_tables_from_diff(diff)
    log.info("scaffold_extracted", routes=len(routes), tables=len(tables))

    try:
        return generate_workflows(diff, routes, tables, pr_description)
    except WorkflowGenerationError as exc:
        log.error("workflow_generation_failed", error=str(exc))
        return None
